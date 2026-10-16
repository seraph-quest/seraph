"""Selected local desktop draft actions (C5.3, #1014).

One action only: replace the currently selected text in a supported editable
text control of the operator-selected app/window, after an exact preview, with
a literal readback. Scope is the active window's focused control, never a
global desktop search; there is no coordinate fallback, hotkey or clipboard.

The local operator flow picks the target explicitly: a countdown lets the
operator focus the target window and select the text, then the surface is
captured. The exact preview (control characters, bidi and zero-width marks
shown visibly) is confirmed on the controlling terminal, and the write happens
only once focus has returned to the identical app, window and control with
the identical text and selection; any change aborts with no write. The write
site re-checks the same accessibility node immediately before deleting. After
the write the control is read back by identity (never by window title).

Terminals, password fields, browsers (form submission), password managers,
authentication prompts, payment-like fields and system settings are refused by
role and by exact application name; the name list is a deny list, not an
exhaustive guarantee, and only plain editable text roles are allowed. A failed
or mismatching write is reported as an unknown local mutation with the current
text for inspection, never reapplied.

Adapters advertise their actual readiness: Linux AT-SPI needs the
``gir1.2-atspi-2.0`` typelib, a session bus, an enabled accessibility bus and
a reachable desktop; macOS Accessibility is not shipped and always reports
unavailable. An unavailable adapter leaves the browser and the rest of the
daemon usable.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
import sys
import time
from typing import Literal, Protocol
import unicodedata

CAPABILITY = "desktop.draft.replace_selected_text.v1"
MAX_TEXT_BYTES = 64 * 1024
MAX_REPLACEMENT_BYTES = 8 * 1024
DEFAULT_COUNTDOWN_SECONDS = 5
ALLOWED_ROLES = frozenset({"text", "entry", "paragraph", "document_text"})
DENIED_ROLES = frozenset({"password_text", "terminal"})
# Exact (case-insensitive) application names; never a substring match.
DENIED_APP_NAMES = frozenset({
    # terminals
    "gnome-terminal", "gnome-terminal-server", "terminal", "console", "kgx", "ptyxis", "konsole", "xterm",
    "uxterm", "alacritty", "kitty", "wezterm", "wezterm-gui", "tilix", "terminator", "xfce4-terminal",
    "mate-terminal", "lxterminal", "qterminal", "foot", "footclient", "guake", "tilda", "terminology", "st",
    "urxvt", "rxvt", "sakura", "blackbox", "cool-retro-term", "ghostty", "contour", "warp", "iterm2",
    # browsers (form submission)
    "firefox", "firefox-esr", "librewolf", "waterfox", "floorp", "zen", "chromium", "chromium-browser",
    "google-chrome", "google chrome", "chrome", "brave", "brave-browser", "microsoft-edge", "msedge", "opera",
    "vivaldi", "epiphany", "web", "safari", "falkon", "midori", "qutebrowser", "konqueror", "tor browser",
    # password managers and secret stores
    "keepassxc", "keepass", "keepass2", "1password", "bitwarden", "enpass", "seahorse", "gnome-keyring",
    "kwalletmanager", "kwalletmanager5", "kwalletd5", "kwalletd6", "secrets", "proton pass",
    # authentication prompts, shells and system settings
    "polkit-gnome-authentication-agent-1", "polkit-kde-authentication-agent-1", "lxpolkit", "mate-polkit",
    "pkexec", "gcr-prompter", "pinentry", "pinentry-gtk-2", "pinentry-gnome3", "pinentry-qt",
    "gnome-shell", "plasmashell", "kwin_wayland", "kwin_x11", "gnome-control-center", "settings",
    "systemsettings", "system settings", "kcmshell5", "kcmshell6", "xfce4-settings-manager",
    "gnome-initial-setup", "software-properties-gtk",
})
PAYMENT_LIKE = re.compile(r"(card|cvv|cvc|iban|account number|routing|expir|security code|passw|pin\b)",
                          re.IGNORECASE)


def _digest(value) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":"),
                                                            ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Characters that render as nothing (or as blank) although they are not
# format controls: variation selectors, the combining grapheme joiner and the
# Hangul fillers.
_INVISIBLE = frozenset({0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x180B, 0x180C, 0x180D, 0x180F})


def _invisible(code: int) -> bool:
    return code in _INVISIBLE or 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF


def visible_text(text: str) -> str:
    """Show text so a preview cannot be spoofed by terminal or Unicode controls.

    Backslashes, controls (ANSI escapes, carriage return, backspace), format
    characters (bidi overrides, zero-width marks), separators other than the
    plain space, unassigned/private code points, variation selectors, the
    combining grapheme joiner and Hangul fillers are all escaped visibly.
    """
    out = []
    for char in text:
        category = unicodedata.category(char)
        if char == "\\":
            out.append("\\\\")
        elif char == "\n":
            out.append("\\n\n")
        elif char == "\t":
            out.append("\\t")
        elif (category in {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"} or (category == "Zs" and char != " ")
              or _invisible(ord(char))):
            out.append(f"\\u{{{ord(char):04X}}}")
        else:
            out.append(char)
    return "".join(out)


def text_summary(text: str) -> str:
    escaped = sum(1 for char in text if visible_text(char) != char)
    non_ascii = sum(1 for char in text if ord(char) > 0x7E)
    return (f"{len(text.encode('utf-8'))} bytes, {len(text)} code points, {non_ascii} non-ASCII, "
            f"{escaped} shown escaped")


class DesktopDraftError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class DesktopWriteUncertain(DesktopDraftError):
    """The write started; the local text state is unknown until inspected."""


@dataclass(frozen=True)
class DesktopSurfaceRef:
    paired_edge_id: str
    app_id: str
    window_id: str
    snapshot_revision: str


@dataclass(frozen=True)
class DesktopAction:
    kind: Literal["inspect", "select_text", "replace_selected_text"]
    expected_selection_digest: str
    replacement_artifact_ref: str  # "sha256:<digest of the exact replacement UTF-8 bytes>"


@dataclass(frozen=True)
class FocusedControl:
    """What an adapter observed for the focused control of the active window.

    ``app_id``, ``window_id`` and ``control_id`` are structural identities;
    titles and names are display-only and never part of identity.
    """
    app_id: str
    app_name: str
    window_id: str
    window_title: str
    control_id: str
    role: str
    name: str
    editable: bool
    text: str
    selection_start: int
    selection_end: int

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.app_id, self.window_id, self.control_id


class TextAdapter(Protocol):
    def probe(self) -> dict: ...
    def focused(self, *, require_selection: bool = True) -> FocusedControl: ...
    def replace(self, control: FocusedControl, start: int, end: int, replacement: str) -> None: ...


# ---------------------------------------------------------------- platform adapters

def _load_atspi():
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    return Atspi


def _a11y_bus_enabled() -> bool:
    """org.a11y.Status.IsEnabled on the session bus (GNOME/KDE accessibility switch)."""
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    reply = bus.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Get",
                          GLib.Variant("(ss)", ("org.a11y.Status", "IsEnabled")), GLib.VariantType("(v)"),
                          Gio.DBusCallFlags.NONE, 2000, None)
    return bool(reply.unpack()[0])


class AtspiTextAdapter:
    """Linux AT-SPI2 adapter for the focused control of the active window."""

    def __init__(self, *, atspi=None, a11y_enabled=None, environ=None):
        self._atspi = atspi
        self._reason = None
        self._a11y_enabled = a11y_enabled or _a11y_bus_enabled
        environ = os.environ if environ is None else environ
        if self._atspi is None:
            try:
                self._atspi = _load_atspi()
            except (ImportError, ValueError):
                self._reason = "atspi_typelib_missing"
        if self._reason is None and not environ.get("DBUS_SESSION_BUS_ADDRESS"):
            self._reason = "atspi_session_bus_missing"
        self._initialized = False

    def probe(self) -> dict:
        reason = self._reason
        if reason is None:
            try:
                self._init()
            except DesktopDraftError as exc:
                reason = exc.code
        return {"capability": CAPABILITY, "platform": "linux", "adapter": "atspi",
                "state": "ready" if reason is None else "unavailable", "reason": reason}

    def _init(self):
        """Really initialise AT-SPI and reach the desktop; readiness is never assumed."""
        if self._reason is not None:
            raise DesktopDraftError(self._reason)
        Atspi = self._atspi
        if not self._initialized:
            try:
                if Atspi.init() not in (0, 1):  # 1: already initialised
                    raise DesktopDraftError("atspi_init_failed")
            except DesktopDraftError:
                raise
            except Exception:
                raise DesktopDraftError("atspi_init_failed") from None
            self._initialized = True
        try:
            enabled = self._a11y_enabled()
        except Exception:
            raise DesktopDraftError("atspi_accessibility_bus_unavailable") from None
        if not enabled:
            raise DesktopDraftError("atspi_accessibility_disabled")
        try:
            desktop = Atspi.get_desktop(0)
            if desktop is None or desktop.get_child_count() < 0:
                raise DesktopDraftError("atspi_desktop_unavailable")
        except DesktopDraftError:
            raise
        except Exception:
            raise DesktopDraftError("atspi_desktop_unavailable") from None
        return Atspi, desktop

    def _resolve(self):
        """The active window's focused node, resolved once, with its structural path."""
        Atspi, desktop = self._init()
        for app_index in range(min(desktop.get_child_count(), 256)):
            app = desktop.get_child_at_index(app_index)
            for window_index in range(min(app.get_child_count(), 64) if app else 0):
                window = app.get_child_at_index(window_index)
                if window is not None and window.get_state_set().contains(Atspi.StateType.ACTIVE):
                    path = self._focused_path(Atspi, window, ())
                    if path is None:
                        raise DesktopDraftError("desktop_no_focused_control")
                    node = window
                    for index in path:
                        node = node.get_child_at_index(index)
                    return app, app_index, window, window_index, node, path
        raise DesktopDraftError("desktop_no_active_window")

    def _focused_path(self, Atspi, node, path):
        if node is None or len(path) > 64:
            return None
        if path and node.get_state_set().contains(Atspi.StateType.FOCUSED):
            return path
        for index in range(min(node.get_child_count(), 512)):
            found = self._focused_path(Atspi, node.get_child_at_index(index), (*path, index))
            if found is not None:
                return found
        return None

    @staticmethod
    def _stable_ref(node) -> str:
        """The D-Bus object reference (bus name + object path) and any app-set
        accessible id, when the toolkit exposes them; empty otherwise, in which
        case identity falls back to the structural position only."""
        parts = []
        try:
            path = getattr(node, "path", None)
            bus = getattr(getattr(node, "app", None), "bus_name", None)
            if isinstance(path, str) and path:
                parts.append(f"{bus or ''}{path}")
        except Exception:
            pass
        try:
            accessible_id = node.get_accessible_id() if hasattr(node, "get_accessible_id") else None
            if isinstance(accessible_id, str) and accessible_id:
                parts.append(f"id={accessible_id}")
        except Exception:
            pass
        return ("@" + "|".join(parts)) if parts else ""

    def _describe(self, resolved, *, require_selection: bool) -> FocusedControl:
        Atspi = self._atspi
        app, app_index, window, window_index, node, path = resolved
        text_iface = node.get_text_iface()
        if text_iface is None:
            raise DesktopDraftError("desktop_control_not_text")
        text = text_iface.get_text(0, text_iface.get_character_count())
        selections = text_iface.get_n_selections()
        if require_selection and selections != 1:
            raise DesktopDraftError("desktop_selection_required")
        selection = text_iface.get_selection(0) if selections == 1 else None
        start, end = (selection.start_offset, selection.end_offset) if selection is not None else (0, 0)
        role = (node.get_role_name() or "").replace(" ", "_")
        app_name = app.get_name() or ""
        return FocusedControl(
            app_id=f"{app.get_process_id()}:{app_name}", app_name=app_name,
            window_id=f"{app_index}/{window_index}:{window.get_role_name()}{self._stable_ref(window)}",
            window_title=window.get_name() or "",
            control_id="/".join(str(index) for index in path) + f":{role}{self._stable_ref(node)}", role=role,
            name=node.get_name() or "",
            editable=node.get_state_set().contains(Atspi.StateType.EDITABLE), text=text,
            selection_start=start, selection_end=end)

    def focused(self, *, require_selection: bool = True) -> FocusedControl:
        return self._describe(self._resolve(), require_selection=require_selection)

    def replace(self, control: FocusedControl, start: int, end: int, replacement: str) -> None:
        Atspi = self._atspi
        resolved = self._resolve()
        node = resolved[4]
        # Verify and write on this one resolved node.
        if self._describe(resolved, require_selection=True) != control:
            raise DesktopDraftError("desktop_focus_changed")
        editable, text_iface = node.get_editable_text_iface(), node.get_text_iface()
        if editable is None or text_iface is None:
            raise DesktopDraftError("desktop_control_not_editable")
        # Final re-check immediately before the first mutating call. AT-SPI has
        # no compare-and-swap: a change between this check and delete_text, or
        # between delete_text and insert_text, is caught only by the readback.
        selection = text_iface.get_selection(0) if text_iface.get_n_selections() == 1 else None
        if (not node.get_state_set().contains(Atspi.StateType.FOCUSED) or selection is None
                or (selection.start_offset, selection.end_offset) != (start, end)
                or text_iface.get_character_count() != len(control.text)):
            raise DesktopDraftError("desktop_focus_changed")
        try:
            written = editable.delete_text(start, end) and editable.insert_text(
                start, replacement, len(replacement.encode("utf-8")))
        except Exception:  # GLib/D-Bus errors after the write began
            written = False
        if not written:
            raise DesktopWriteUncertain("desktop_write_failed")


class MacAccessibilityAdapter:
    """macOS Accessibility is not shipped in this milestone; it says so."""

    def probe(self) -> dict:
        return {"capability": CAPABILITY, "platform": "macos", "adapter": "accessibility", "state": "unavailable",
                "reason": "macos_accessibility_adapter_not_shipped"}

    def focused(self, *, require_selection: bool = True) -> FocusedControl:
        raise DesktopDraftError("macos_accessibility_adapter_not_shipped")

    def replace(self, control, start, end, replacement) -> None:
        raise DesktopDraftError("macos_accessibility_adapter_not_shipped")


class UnsupportedPlatformAdapter:
    def probe(self) -> dict:
        return {"capability": CAPABILITY, "platform": sys.platform, "adapter": "none", "state": "unavailable",
                "reason": "desktop_platform_unsupported"}

    def focused(self, *, require_selection: bool = True) -> FocusedControl:
        raise DesktopDraftError("desktop_platform_unsupported")

    def replace(self, control, start, end, replacement) -> None:
        raise DesktopDraftError("desktop_platform_unsupported")


def platform_adapter() -> TextAdapter:
    if sys.platform.startswith("linux"):
        return AtspiTextAdapter()
    if sys.platform == "darwin":
        return MacAccessibilityAdapter()
    return UnsupportedPlatformAdapter()


# ---------------------------------------------------------------- executor

@dataclass(frozen=True)
class DesktopPreview:
    surface: DesktopSurfaceRef
    control_id: str
    control_role: str
    selected_text: str
    replacement: str
    expected_selection_digest: str
    replacement_artifact_ref: str
    preview_digest: str

    def action(self) -> DesktopAction:
        return DesktopAction("replace_selected_text", self.expected_selection_digest, self.replacement_artifact_ref)


def _snapshot_revision(control: FocusedControl) -> str:
    return _digest({"app": control.app_id, "window": control.window_id, "control": control.control_id,
                    "text": text_digest(control.text), "start": control.selection_start, "end": control.selection_end})


def _denied(control: FocusedControl) -> str | None:
    if control.role in DENIED_ROLES:
        return "desktop_control_denied"
    app_name = control.app_name.strip().lower()
    if app_name in DENIED_APP_NAMES or control.app_id.split(":", 1)[-1].strip().lower() in DENIED_APP_NAMES:
        return "desktop_app_denied"
    if PAYMENT_LIKE.search(control.name):
        return "desktop_control_denied"
    if control.role not in ALLOWED_ROLES or not control.editable:
        return "desktop_control_unsupported"
    return None


class DesktopDraftExecutor:
    """Inspect / exact preview / replace-with-readback on the selected surface."""

    def __init__(self, adapter: TextAdapter, *, paired_edge_id: str):
        self.adapter = adapter
        self.paired_edge_id = paired_edge_id

    def _current(self) -> FocusedControl:
        control = self.adapter.focused()
        if len(control.text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise DesktopDraftError("desktop_text_bounds")
        if not 0 <= control.selection_start < control.selection_end <= len(control.text):
            raise DesktopDraftError("desktop_selection_required")
        return control

    def inspect(self) -> dict:
        control = self._current()
        reason = _denied(control)
        surface = DesktopSurfaceRef(self.paired_edge_id, control.app_id, control.window_id, _snapshot_revision(control))
        selected = control.text[control.selection_start:control.selection_end]
        # The selected text comes from another application: display it, never act on it.
        return {"surface": surface, "control_role": control.role, "supported": reason is None, "reason": reason,
                "selection_digest": text_digest(selected), "untrusted_selected_text": selected}

    def preview(self, surface: DesktopSurfaceRef, replacement: str) -> DesktopPreview:
        if type(replacement) is not str or len(replacement.encode("utf-8")) > MAX_REPLACEMENT_BYTES:
            raise DesktopDraftError("desktop_replacement_bounds")
        control = self._current()
        self._same_surface(surface, control)
        reason = _denied(control)
        if reason:
            raise DesktopDraftError(reason)
        selected = control.text[control.selection_start:control.selection_end]
        selection_digest = text_digest(selected)
        replacement_ref = "sha256:" + text_digest(replacement)
        preview_digest = _digest({"surface": surface.__dict__, "control": control.control_id,
                                  "selection": selection_digest, "replacement": replacement_ref})
        return DesktopPreview(surface, control.control_id, control.role, selected, replacement, selection_digest,
                              replacement_ref, preview_digest)

    def _same_surface(self, surface: DesktopSurfaceRef, control: FocusedControl) -> None:
        if surface.paired_edge_id != self.paired_edge_id:
            raise DesktopDraftError("desktop_paired_edge_changed")
        if (control.app_id, control.window_id) != (surface.app_id, surface.window_id):
            raise DesktopDraftError("desktop_focus_changed")
        if _snapshot_revision(control) != surface.snapshot_revision:
            raise DesktopDraftError("desktop_selection_changed")

    def matches(self, surface: DesktopSurfaceRef) -> bool:
        """Is the identical surface (focus, control, text, selection) current again?"""
        try:
            self._same_surface(surface, self._current())
        except DesktopDraftError:
            return False
        return True

    def apply(self, preview: DesktopPreview, *, confirmed_preview_digest: str, replacement: str) -> dict:
        """Write only if focus, control, text and selection are exactly as previewed."""
        action = preview.action()
        if confirmed_preview_digest != preview.preview_digest:
            return {"status": "blocked", "reason": "desktop_preview_not_confirmed", "written": False}
        if action.replacement_artifact_ref != "sha256:" + text_digest(replacement) or replacement != preview.replacement:
            return {"status": "blocked", "reason": "desktop_replacement_changed", "written": False}
        try:
            control = self._current()
            self._same_surface(preview.surface, control)
            if control.control_id != preview.control_id:
                raise DesktopDraftError("desktop_focus_changed")
            selected = control.text[control.selection_start:control.selection_end]
            if text_digest(selected) != action.expected_selection_digest:
                raise DesktopDraftError("desktop_selection_changed")
            reason = _denied(control)
            if reason:
                raise DesktopDraftError(reason)
        except DesktopDraftError as exc:
            return {"status": "blocked", "reason": exc.code, "written": False}
        expected = control.text[:control.selection_start] + replacement + control.text[control.selection_end:]
        try:
            self.adapter.replace(control, control.selection_start, control.selection_end, replacement)
        except DesktopWriteUncertain as exc:
            return self._unknown(exc.code)
        except DesktopDraftError as exc:
            # Refused at the write site before any mutation.
            return {"status": "blocked", "reason": exc.code, "written": False}
        try:
            # Readback needs the same control identity, not a selection (writes collapse it).
            after = self.adapter.focused(require_selection=False)
        except DesktopDraftError as exc:
            return self._unknown(exc.code)
        if after.identity != control.identity or after.text != expected:
            return self._unknown("desktop_readback_mismatch")
        return {"status": "succeeded", "written": True, "readback_digest": text_digest(after.text),
                "replacement_digest": text_digest(replacement), "preview_digest": preview.preview_digest}

    def _unknown(self, reason: str) -> dict:
        """Never reapply: offer the current text for inspection instead."""
        try:
            current = self.adapter.focused(require_selection=False)
            inspect = {"control_id": current.control_id, "untrusted_text": current.text[:MAX_TEXT_BYTES]}
        except DesktopDraftError as exc:
            inspect = {"error": exc.code}
        return {"status": "unknown_local_mutation", "reason": reason, "written": None,
                "reapply_offered": False, "inspect_current_text": inspect}


# ---------------------------------------------------------------- local operator CLI

class _Terminal:
    """The controlling terminal; confirmation never comes from piped stdin."""

    def __init__(self):
        self._handle = open("/dev/tty", "r+", encoding="utf-8", errors="replace")

    def ask(self, prompt: str) -> str:
        self._handle.write(prompt)
        self._handle.flush()
        return self._handle.readline()

    def close(self):
        self._handle.close()


def _countdown(seconds: int, message: str, *, out, sleep) -> None:
    out(message)
    for remaining in range(seconds, 0, -1):
        out(f"  {remaining}...")
        sleep(1)


def main(argv=None, *, adapter: TextAdapter | None = None, terminal=None, out=print, sleep=time.sleep,
         clock=time.monotonic) -> int:
    """Local-only flow: pick the target, preview exactly, confirm, refocus, write, read back."""
    import argparse
    parser = argparse.ArgumentParser(description="Seraph selected desktop draft actions")
    parser.add_argument("command", choices=["probe", "inspect", "replace"])
    parser.add_argument("--replacement-file", help="UTF-8 file with the exact replacement text")
    parser.add_argument("--paired-edge-id", default="local")
    parser.add_argument("--countdown", type=int, default=DEFAULT_COUNTDOWN_SECONDS, choices=range(2, 31),
                        metavar="2-30")
    args = parser.parse_args(argv)
    adapter = adapter or platform_adapter()
    probe = adapter.probe()
    if args.command == "probe":
        out(json.dumps(probe, sort_keys=True))
        return 0
    if probe["state"] != "ready":
        out(json.dumps({"status": "blocked", "reason": probe["reason"]}))
        return 2
    replacement = None
    if args.command == "replace":
        if not args.replacement_file:
            parser.error("replace requires --replacement-file")
        try:
            with open(args.replacement_file, encoding="utf-8", errors="strict") as handle:
                replacement = handle.read(MAX_REPLACEMENT_BYTES + 1)
        except UnicodeDecodeError:
            out(json.dumps({"status": "blocked", "reason": "desktop_replacement_not_utf8", "written": False}))
            return 2
        except OSError:
            out(json.dumps({"status": "blocked", "reason": "desktop_replacement_unreadable", "written": False}))
            return 2
        if terminal is None:
            if not sys.stdin.isatty():
                out(json.dumps({"status": "blocked", "reason": "desktop_confirmation_requires_tty"}))
                return 2
            terminal = _Terminal()
    executor = DesktopDraftExecutor(adapter, paired_edge_id=args.paired_edge_id)
    try:
        _countdown(args.countdown, "Focus the target window and select the text to replace. Capturing in:",
                   out=out, sleep=sleep)
        current = executor.inspect()
        if args.command == "inspect":
            out(json.dumps({**current, "surface": current["surface"].__dict__}, sort_keys=True))
            return 0
        preview = executor.preview(current["surface"], replacement)
    except DesktopDraftError as exc:
        out(json.dumps({"status": "blocked", "reason": exc.code}))
        return 2
    out(f"Target: {visible_text(current['surface'].app_id)} / {visible_text(preview.surface.window_id)}"
        f" ({preview.control_role}). Text below is untrusted and shown with controls escaped.")
    out(f"--- selected ({text_summary(preview.selected_text)}) ---\n{visible_text(preview.selected_text)}")
    out(f"--- replacement ({text_summary(preview.replacement)}) ---\n{visible_text(preview.replacement)}")
    out(f"--- end ---\nType {preview.preview_digest[:12]} to confirm, then return focus to the target window "
        f"within {args.countdown} seconds without changing the selection. Anything else cancels.")
    try:
        answer = terminal.ask("> ").strip()
    finally:
        if isinstance(terminal, _Terminal):
            terminal.close()
    if answer != preview.preview_digest[:12]:
        out(json.dumps({"status": "cancelled", "written": False}))
        return 1
    deadline = clock() + args.countdown
    while not executor.matches(preview.surface):
        if clock() >= deadline:
            out(json.dumps({"status": "blocked", "reason": "desktop_focus_not_restored", "written": False}))
            return 2
        sleep(0.1)
    result = executor.apply(preview, confirmed_preview_digest=preview.preview_digest, replacement=replacement)
    out(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "succeeded" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

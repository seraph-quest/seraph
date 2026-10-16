"""Selected local desktop draft actions (C5.3, #1014).

One action only: replace the currently selected text in a supported editable
text control of the operator-selected app/window, after an exact preview, with
a literal readback. Scope is the selected window's focused control, never a
global desktop search; there is no coordinate fallback, hotkey or clipboard.

Before writing, the executor re-reads focus and requires the identical app,
window and control, the identical surrounding text and the exact selection
digest from the preview; any change aborts with no write. Terminals, password
fields, browsers (form submission), payment-like fields and system settings
are refused. A failed or mismatching write is reported as an unknown local
mutation with the current text for inspection, never reapplied.

Adapters advertise their actual readiness: Linux AT-SPI needs the
``gir1.2-atspi-2.0`` typelib and a session bus; macOS Accessibility is not
shipped yet and always reports unavailable. An unavailable adapter leaves the
browser and the rest of the daemon usable.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
import sys
from typing import Literal, Protocol

CAPABILITY = "desktop.draft.replace_selected_text.v1"
MAX_TEXT_BYTES = 64 * 1024
MAX_REPLACEMENT_BYTES = 8 * 1024
ALLOWED_ROLES = frozenset({"text", "entry", "paragraph", "document_text"})
DENIED_ROLES = frozenset({"password_text", "terminal"})
DENIED_APPS = re.compile(r"(firefox|chrom(e|ium)|brave|edge|safari|opera|vivaldi|epiphany|"
                         r"gnome-control-center|systemsettings|system settings|kcmshell|"
                         r"terminal|konsole|xterm|alacritty|kitty|wezterm|tilix|iterm)", re.IGNORECASE)
PAYMENT_LIKE = re.compile(r"(card|cvv|cvc|iban|account number|routing|expir|security code|passw)", re.IGNORECASE)


def _digest(value) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":"),
                                                            ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    """What an adapter observed for the focused control of the focused window."""
    app_id: str
    app_name: str
    window_id: str
    control_id: str
    role: str
    name: str
    editable: bool
    text: str
    selection_start: int
    selection_end: int


class TextAdapter(Protocol):
    def probe(self) -> dict: ...
    def focused(self, *, require_selection: bool = True) -> FocusedControl: ...
    def replace(self, control: FocusedControl, start: int, end: int, replacement: str) -> None: ...


# ---------------------------------------------------------------- platform adapters

class AtspiTextAdapter:
    """Linux AT-SPI2 adapter for the focused control of the active window."""

    def __init__(self):
        self._atspi = None
        self._reason = None
        try:
            import gi
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi
            self._atspi = Atspi
        except (ImportError, ValueError):
            self._reason = "atspi_typelib_missing"
        if self._reason is None and not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            self._reason = "atspi_session_bus_missing"

    def probe(self) -> dict:
        state = "ready" if self._reason is None else "unavailable"
        return {"capability": CAPABILITY, "platform": "linux", "adapter": "atspi", "state": state,
                "reason": self._reason}

    def _require(self):
        if self._reason is not None:
            raise DesktopDraftError(self._reason)
        return self._atspi

    def _active(self):
        Atspi = self._require()
        desktop = Atspi.get_desktop(0)
        for index in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(index)
            for window_index in range(app.get_child_count() if app else 0):
                window = app.get_child_at_index(window_index)
                if window is not None and window.get_state_set().contains(Atspi.StateType.ACTIVE):
                    return app, window
        raise DesktopDraftError("desktop_no_active_window")

    def _focused_in(self, node, depth=0):
        Atspi = self._require()
        if node is None or depth > 64:
            return None
        if node.get_state_set().contains(Atspi.StateType.FOCUSED):
            return node
        for index in range(min(node.get_child_count(), 512)):
            found = self._focused_in(node.get_child_at_index(index), depth + 1)
            if found is not None:
                return found
        return None

    def focused(self, *, require_selection: bool = True) -> FocusedControl:
        Atspi = self._require()
        app, window = self._active()
        control = self._focused_in(window)
        if control is None:
            raise DesktopDraftError("desktop_no_focused_control")
        text_iface = control.get_text_iface()
        if text_iface is None:
            raise DesktopDraftError("desktop_control_not_text")
        count = text_iface.get_character_count()
        text = text_iface.get_text(0, count)
        selections = text_iface.get_n_selections()
        if require_selection and selections != 1:
            raise DesktopDraftError("desktop_selection_required")
        selection = text_iface.get_selection(0) if selections == 1 else None
        start, end = (selection.start_offset, selection.end_offset) if selection is not None else (0, 0)
        states = control.get_state_set()
        return FocusedControl(app_id=f"{app.get_process_id()}:{app.get_name()}", app_name=app.get_name() or "",
                              window_id=f"{window.get_role_name()}:{window.get_name()}",
                              control_id=f"{control.get_role_name()}:{control.get_index_in_parent()}:{control.get_name()}",
                              role=(control.get_role_name() or "").replace(" ", "_"), name=control.get_name() or "",
                              editable=states.contains(Atspi.StateType.EDITABLE), text=text,
                              selection_start=start, selection_end=end)

    def replace(self, control: FocusedControl, start: int, end: int, replacement: str) -> None:
        self._require()
        # Last check at the write site: same control, text and selection.
        if self.focused() != control:
            raise DesktopDraftError("desktop_focus_changed")
        app, window = self._active()
        node = self._focused_in(window)
        editable = node.get_editable_text_iface() if node is not None else None
        if editable is None:
            raise DesktopDraftError("desktop_control_not_editable")
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
    if DENIED_APPS.search(control.app_name) or DENIED_APPS.search(control.app_id):
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
        return {"surface": surface, "control_role": control.role, "supported": reason is None, "reason": reason,
                "selection_digest": text_digest(selected), "selected_text": selected}

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
            # Readback needs the same control, not a selection (writes collapse it).
            after = self.adapter.focused(require_selection=False)
        except DesktopDraftError as exc:
            return self._unknown(exc.code)
        if (after.app_id, after.window_id, after.control_id) != (control.app_id, control.window_id, control.control_id) \
                or after.text != expected:
            return self._unknown("desktop_readback_mismatch")
        return {"status": "succeeded", "written": True, "readback_digest": text_digest(after.text),
                "replacement_digest": text_digest(replacement), "preview_digest": preview.preview_digest}

    def _unknown(self, reason: str) -> dict:
        """Never reapply: offer the current text for inspection instead."""
        try:
            current = self.adapter.focused(require_selection=False)
            inspect = {"control_id": current.control_id, "text": current.text[:MAX_TEXT_BYTES]}
        except DesktopDraftError as exc:
            inspect = {"error": exc.code}
        return {"status": "unknown_local_mutation", "reason": reason, "written": None,
                "reapply_offered": False, "inspect_current_text": inspect}


# ---------------------------------------------------------------- local operator CLI

def main(argv=None, *, adapter: TextAdapter | None = None, prompt=input, out=print) -> int:
    """Local-only flow: inspect, show the exact preview, confirm, write, read back."""
    import argparse
    parser = argparse.ArgumentParser(description="Seraph selected desktop draft actions")
    parser.add_argument("command", choices=["probe", "inspect", "replace"])
    parser.add_argument("--replacement-file", help="UTF-8 file with the exact replacement text")
    parser.add_argument("--paired-edge-id", default="local")
    args = parser.parse_args(argv)
    adapter = adapter or platform_adapter()
    probe = adapter.probe()
    if args.command == "probe":
        out(json.dumps(probe, sort_keys=True))
        return 0
    if probe["state"] != "ready":
        out(json.dumps({"status": "blocked", "reason": probe["reason"]}))
        return 2
    executor = DesktopDraftExecutor(adapter, paired_edge_id=args.paired_edge_id)
    try:
        current = executor.inspect()
        if args.command == "inspect":
            out(json.dumps({**current, "surface": current["surface"].__dict__}, sort_keys=True))
            return 0
        if not args.replacement_file:
            parser.error("replace requires --replacement-file")
        with open(args.replacement_file, encoding="utf-8") as handle:
            replacement = handle.read(MAX_REPLACEMENT_BYTES + 1)
        preview = executor.preview(current["surface"], replacement)
    except DesktopDraftError as exc:
        out(json.dumps({"status": "blocked", "reason": exc.code}))
        return 2
    out(f"Replace in {preview.surface.app_id} / {preview.surface.window_id} ({preview.control_role}):")
    out("--- selected ---\n" + preview.selected_text + "\n--- replacement ---\n" + preview.replacement)
    out(f"Type the preview digest prefix {preview.preview_digest[:12]} to write; anything else cancels.")
    if prompt("> ").strip() != preview.preview_digest[:12]:
        out(json.dumps({"status": "cancelled", "written": False}))
        return 1
    result = executor.apply(preview, confirmed_preview_digest=preview.preview_digest, replacement=replacement)
    out(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "succeeded" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

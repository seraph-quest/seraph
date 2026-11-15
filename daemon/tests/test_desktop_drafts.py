"""C5.3 (#1014): selected desktop draft replacement with a fixture adapter."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import desktop_drafts as drafts
from desktop_drafts import (
    AtspiTextAdapter, DesktopDraftError, DesktopDraftExecutor, DesktopSurfaceRef, DesktopWriteUncertain,
    FocusedControl, MacAccessibilityAdapter, text_digest,
)


def _control(**changes) -> FocusedControl:
    base = FocusedControl(app_id="4242:gedit", app_name="gedit", window_id="0/0:frame", window_title="notes.txt",
                          control_id="0/1:text", role="text", name="", editable=True,
                          text="Dear team, the draft is ready.", selection_start=11, selection_end=20)
    return replace(base, **changes)


class FixtureAdapter:
    """In-memory focused control; hooks simulate races between steps."""

    def __init__(self, control: FocusedControl):
        self.control = control
        self.writes: list[tuple[int, int, str]] = []
        self.before_write = None
        self.after_write = None
        self.fail_write = None

    def probe(self):
        return {"capability": drafts.CAPABILITY, "platform": "fixture", "adapter": "fixture", "state": "ready",
                "reason": None}

    def focused(self, *, require_selection=True):
        if require_selection and self.control.selection_start == self.control.selection_end:
            raise DesktopDraftError("desktop_selection_required")
        return self.control

    def replace(self, control, start, end, replacement):
        if self.before_write:
            self.before_write(self)
        if self.focused() != control:
            raise DesktopDraftError("desktop_focus_changed")
        if self.fail_write:
            raise self.fail_write
        self.writes.append((start, end, replacement))
        text = self.control.text[:start] + replacement + self.control.text[end:]
        cursor = start + len(replacement)
        self.control = replace(self.control, text=text, selection_start=cursor, selection_end=cursor)
        if self.after_write:
            self.after_write(self)


def _ready(control=None):
    adapter = FixtureAdapter(control or _control())
    executor = DesktopDraftExecutor(adapter, paired_edge_id="edge-1")
    surface = executor.inspect()["surface"]
    return adapter, executor, executor.preview(surface, "the report")


def test_replace_selected_text_after_exact_preview_with_readback():
    adapter, executor, preview = _ready()
    assert preview.selected_text == "the draft" and preview.replacement == "the report"
    assert preview.expected_selection_digest == text_digest("the draft")
    assert preview.replacement_artifact_ref == "sha256:" + text_digest("the report")
    assert preview.surface.paired_edge_id == "edge-1" and preview.surface.app_id == "4242:gedit"
    result = executor.apply(preview, confirmed_preview_digest=preview.preview_digest, replacement="the report")
    assert result["status"] == "succeeded" and result["written"] is True
    assert adapter.control.text == "Dear team, the report is ready."
    assert result["readback_digest"] == text_digest("Dear team, the report is ready.")
    assert adapter.writes == [(11, 20, "the report")]


def test_unconfirmed_or_changed_replacement_never_writes():
    adapter, executor, preview = _ready()
    assert executor.apply(preview, confirmed_preview_digest="0" * 64, replacement="the report")["reason"] \
        == "desktop_preview_not_confirmed"
    assert executor.apply(preview, confirmed_preview_digest=preview.preview_digest, replacement="other")["reason"] \
        == "desktop_replacement_changed"
    assert adapter.writes == []


@pytest.mark.parametrize("race,reason", [
    (lambda a: replace(a.control, window_id="0/1:frame"), "desktop_focus_changed"),
    (lambda a: replace(a.control, app_id="77:mousepad", app_name="mousepad"), "desktop_focus_changed"),
    (lambda a: replace(a.control, selection_start=12), "desktop_selection_changed"),
    (lambda a: replace(a.control, text="Dear team, the plans is ready."), "desktop_selection_changed"),
    (lambda a: replace(a.control, control_id="0/2:text"), "desktop_selection_changed"),
    (lambda a: replace(a.control, selection_start=0, selection_end=0), "desktop_selection_required"),
])
def test_focus_or_selection_change_after_preview_prevents_the_write(race, reason):
    adapter, executor, preview = _ready()
    adapter.control = race(adapter)
    result = executor.apply(preview, confirmed_preview_digest=preview.preview_digest, replacement="the report")
    assert result == {"status": "blocked", "reason": reason, "written": False}
    assert adapter.writes == []


def test_focus_change_at_the_write_site_prevents_the_write():
    adapter, executor, preview = _ready()
    adapter.before_write = lambda a: setattr(a, "control", replace(a.control, window_id="0/1:frame"))
    result = executor.apply(preview, confirmed_preview_digest=preview.preview_digest, replacement="the report")
    assert result == {"status": "blocked", "reason": "desktop_focus_changed", "written": False}
    assert adapter.writes == []


def test_unknown_local_mutation_offers_inspection_and_never_reapplies():
    adapter, executor, preview = _ready()
    adapter.after_write = lambda a: setattr(a, "control", replace(a.control, text=a.control.text + " typed"))
    result = executor.apply(preview, confirmed_preview_digest=preview.preview_digest, replacement="the report")
    assert result["status"] == "unknown_local_mutation" and result["reapply_offered"] is False
    assert result["inspect_current_text"]["untrusted_text"] == "Dear team, the report is ready. typed"
    assert len(adapter.writes) == 1

    adapter, executor, preview = _ready()
    adapter.fail_write = DesktopWriteUncertain("desktop_write_failed")
    result = executor.apply(preview, confirmed_preview_digest=preview.preview_digest, replacement="the report")
    assert result["status"] == "unknown_local_mutation" and result["reason"] == "desktop_write_failed"
    assert result["written"] is None and adapter.writes == []


@pytest.mark.parametrize("changes,reason", [
    ({"role": "password_text"}, "desktop_control_denied"),
    ({"role": "terminal"}, "desktop_control_denied"),
    ({"app_name": "gnome-terminal-server", "app_id": "1:gnome-terminal-server"}, "desktop_app_denied"),
    ({"app_name": "Firefox", "app_id": "1:Firefox"}, "desktop_app_denied"),
    ({"app_name": "Chromium", "app_id": "1:Chromium"}, "desktop_app_denied"),
    ({"app_name": "gnome-control-center", "app_id": "1:gnome-control-center"}, "desktop_app_denied"),
    ({"name": "Card number"}, "desktop_control_denied"),
    ({"name": "Security code (CVV)"}, "desktop_control_denied"),
    ({"role": "push_button"}, "desktop_control_unsupported"),
    ({"editable": False}, "desktop_control_unsupported"),
])
def test_denied_and_unsupported_control_types_are_blocked(changes, reason):
    adapter = FixtureAdapter(_control(**changes))
    executor = DesktopDraftExecutor(adapter, paired_edge_id="edge-1")
    inspected = executor.inspect()
    assert inspected["supported"] is False and inspected["reason"] == reason
    with pytest.raises(DesktopDraftError) as error:
        executor.preview(inspected["surface"], "x")
    assert error.value.code == reason and adapter.writes == []


def test_preview_is_bound_to_the_inspected_surface_and_bounds():
    adapter = FixtureAdapter(_control())
    executor = DesktopDraftExecutor(adapter, paired_edge_id="edge-1")
    surface = executor.inspect()["surface"]
    with pytest.raises(DesktopDraftError, match="desktop_paired_edge_changed"):
        executor.preview(DesktopSurfaceRef("edge-2", surface.app_id, surface.window_id, surface.snapshot_revision), "x")
    with pytest.raises(DesktopDraftError, match="desktop_replacement_bounds"):
        executor.preview(surface, "x" * (drafts.MAX_REPLACEMENT_BYTES + 1))
    adapter.control = replace(adapter.control, window_id="1/0:frame")
    with pytest.raises(DesktopDraftError, match="desktop_focus_changed"):
        executor.preview(surface, "x")


def test_platform_probes_report_actual_readiness(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", None)  # gir1.2-atspi-2.0 absent
    probe = AtspiTextAdapter().probe()
    assert probe["state"] == "unavailable" and probe["reason"] == "atspi_typelib_missing"
    with pytest.raises(DesktopDraftError, match="atspi_typelib_missing"):
        AtspiTextAdapter().focused()
    mac = MacAccessibilityAdapter().probe()
    assert mac["state"] == "unavailable" and mac["platform"] == "macos"
    monkeypatch.setattr(drafts.sys, "platform", "darwin")
    assert isinstance(drafts.platform_adapter(), MacAccessibilityAdapter)
    monkeypatch.setattr(drafts.sys, "platform", "win32")
    assert drafts.platform_adapter().probe()["reason"] == "desktop_platform_unsupported"


@pytest.mark.parametrize("setup,reason", [
    (dict(environ={}), "atspi_session_bus_missing"),
    (dict(a11y_enabled=lambda: False), "atspi_accessibility_disabled"),
    (dict(a11y_enabled=lambda: (_ for _ in ()).throw(RuntimeError("no bus"))), "atspi_accessibility_bus_unavailable"),
    (dict(init_result=-1), "atspi_init_failed"),
    (dict(desktop=None), "atspi_desktop_unavailable"),
    (dict(), None),
])
def test_atspi_probe_really_initialises_and_reaches_the_desktop(setup, reason):
    world = FakeDesktop()
    atspi = FakeAtspi(world, init_result=setup.get("init_result", 0))
    if "desktop" in setup:
        atspi.desktop = None
    adapter = AtspiTextAdapter(atspi=atspi, a11y_enabled=setup.get("a11y_enabled", lambda: True),
                               environ=setup.get("environ", {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"}))
    probe = adapter.probe()
    assert probe["reason"] == reason and probe["state"] == ("ready" if reason is None else "unavailable")
    if reason is None:
        assert atspi.init_calls == 1


def test_unavailable_platform_leaves_paired_edge_transport_usable(tmp_path, monkeypatch):
    from paired_edge import PairedEdgeTransport
    monkeypatch.setitem(sys.modules, "gi", None)
    transport = PairedEdgeTransport(origin="http://127.0.0.1:8000", credential=None, device_id="d", pairing_id="p",
                                    spool_path=tmp_path / "spool.json", allow_insecure_test_transport=True,
                                    desktop_adapter=AtspiTextAdapter())
    status = transport.status()
    assert status["desktop_draft"]["state"] == "unavailable"
    assert status["action_authority"] is False and status["count"] == 0


class FakeTerminal:
    def __init__(self, answer, on_ask=None):
        self.answer, self.on_ask, self.prompts = answer, on_ask, []

    def ask(self, prompt):
        self.prompts.append(prompt)
        if self.on_ask:
            self.on_ask()
        return self.answer() if callable(self.answer) else self.answer


def test_visible_preview_escapes_terminal_bidi_and_zero_width_spoofing():
    tricky = "ok\x1b[2K\rfake\x08\u202eevil\u200b\u2066x\u00a0\\n"
    shown = drafts.visible_text(tricky)
    assert "\x1b" not in shown and "\r" not in shown and "\x08" not in shown
    assert "\u202e" not in shown and "\u200b" not in shown and "\u2066" not in shown and "\u00a0" not in shown
    for marker in ("\\u{001B}", "\\u{000D}", "\\u{0008}", "\\u{202E}", "\\u{200B}", "\\u{2066}", "\\u{00A0}"):
        assert marker in shown
    assert shown.endswith("\\\\n")  # a literal backslash-n stays distinguishable from a newline
    non_ascii = sum(1 for char in tricky if ord(char) > 0x7E)
    assert drafts.text_summary(tricky) == (f"{len(tricky.encode())} bytes, {len(tricky)} code points, "
                                           f"{non_ascii} non-ASCII, 8 shown escaped")


@pytest.mark.parametrize("char", ["\ufe0f", "\U000e0100", "\u034f", "\u3164", "\u115f", "\u1160", "\uffa0",
                                  "\u180e", "\u200d"])
def test_visible_preview_escapes_invisible_non_control_characters(char):
    shown = drafts.visible_text("a" + char + "b")
    assert char not in shown and f"\\u{{{ord(char):04X}}}" in shown


def test_non_utf8_replacement_file_is_a_blocked_result(tmp_path):
    path = tmp_path / "replacement.txt"
    path.write_bytes(b"\xff\xfe not utf-8")
    lines: list[str] = []
    adapter = FixtureAdapter(_control())
    assert drafts.main(["replace", "--replacement-file", str(path)], adapter=adapter, terminal=FakeTerminal("x"),
                       out=lines.append, sleep=lambda _: None) == 2
    assert json.loads(lines[-1]) == {"status": "blocked", "reason": "desktop_replacement_not_utf8", "written": False}
    assert drafts.main(["replace", "--replacement-file", str(tmp_path / "missing.txt")], adapter=adapter,
                       terminal=FakeTerminal("x"), out=lines.append, sleep=lambda _: None) == 2
    assert json.loads(lines[-1])["reason"] == "desktop_replacement_unreadable" and adapter.writes == []


def test_local_cli_requires_typed_preview_confirmation(tmp_path):
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("the report\u202e", encoding="utf-8")
    lines: list[str] = []
    adapter = FixtureAdapter(_control())
    code = drafts.main(["replace", "--replacement-file", str(replacement)], adapter=adapter,
                       terminal=FakeTerminal("no"), out=lines.append, sleep=lambda _: None)
    assert code == 1 and adapter.writes == [] and '"cancelled"' in lines[-1]
    shown = "\n".join(lines)
    assert "the draft" in shown and "the report\\u{202E}" in shown and "\u202e" not in shown
    assert "untrusted" in shown and "13 bytes, 11 code points, 1 non-ASCII, 1 shown escaped" in shown
    prefix = next(line for line in lines if line.startswith("--- end ---")).split()[4]
    code = drafts.main(["replace", "--replacement-file", str(replacement)], adapter=adapter,
                       terminal=FakeTerminal(prefix), out=lines.append, sleep=lambda _: None)
    assert code == 0 and adapter.control.text == "Dear team, the report\u202e is ready."
    blocked: list[str] = []

    class Unavailable(FixtureAdapter):
        def probe(self):
            return {"state": "unavailable", "reason": "atspi_typelib_missing"}

    assert drafts.main(["inspect"], adapter=Unavailable(_control()), out=blocked.append) == 2
    assert "atspi_typelib_missing" in blocked[0]


def test_local_cli_refuses_confirmation_from_non_tty_stdin(tmp_path, monkeypatch):
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("x", encoding="utf-8")
    monkeypatch.setattr(drafts.sys.stdin, "isatty", lambda: False, raising=False)
    lines: list[str] = []
    adapter = FixtureAdapter(_control())
    assert drafts.main(["replace", "--replacement-file", str(replacement)], adapter=adapter,
                       out=lines.append, sleep=lambda _: None) == 2
    assert "desktop_confirmation_requires_tty" in lines[-1] and adapter.writes == []


# ---------------------------------------------------------------- end to end through a fake AT-SPI tree

class FakeStateType:
    ACTIVE, FOCUSED, EDITABLE = "active", "focused", "editable"


class FakeStateSet:
    def __init__(self, states):
        self.states = set(states)

    def contains(self, state):
        return state in self.states


class FakeSelection:
    def __init__(self, start, end):
        self.start_offset, self.end_offset = start, end


class FakeNode:
    def __init__(self, world, name, role, children=(), *, text=None, app=None, pid=0):
        self.world, self.name, self.role, self.children = world, name, role, list(children)
        self.text, self.selection, self.pid, self.parent = text, None, pid, None
        self.app = app  # an AT-SPI Application (bus_name) when the toolkit exposes it
        self.path = None  # the D-Bus object path when exposed
        self.editable = text is not None and role != "terminal"
        for child in self.children:
            child.parent = self

    # accessible
    def get_name(self): return self.name
    def get_role_name(self): return self.role
    def get_process_id(self): return self.pid
    def get_child_count(self): return len(self.children)
    def get_child_at_index(self, index): return self.children[index]

    def get_state_set(self):
        states = set()
        if self.role == "frame" and self.world.active is self:
            states.add(FakeStateType.ACTIVE)
        if self.world.focus is self:
            states.add(FakeStateType.FOCUSED)
        if self.editable:
            states.add(FakeStateType.EDITABLE)
        return FakeStateSet(states)

    def get_text_iface(self): return self if self.text is not None else None
    def get_editable_text_iface(self): return self if self.editable else None

    # text
    def get_character_count(self): return len(self.text)
    def get_text(self, start, end): return self.text[start:end]
    def get_n_selections(self): return 1 if self.selection and self.selection[0] != self.selection[1] else 0
    def get_selection(self, index): return FakeSelection(*self.selection)

    # editable text
    def delete_text(self, start, end):
        self.world.calls.append(("delete", start, end))
        self.text = self.text[:start] + self.text[end:]
        self.selection = (start, start)
        return True

    def insert_text(self, position, text, length):
        self.world.calls.append(("insert", position, text))
        self.text = self.text[:position] + text + self.text[position:]
        return True


class FakeDesktop:
    """A terminal running the CLI and a text editor holding the draft."""

    def __init__(self):
        self.calls = []
        self.focus = self.active = None
        self.terminal_view = FakeNode(self, "", "terminal", text="$ desktop_drafts.py replace")
        self.terminal = FakeNode(self, "Terminal", "frame", [FakeNode(self, "", "scroll_pane", [self.terminal_view])])
        self.editor_text = FakeNode(self, "", "text", text="Dear team, the draft is ready.")
        self.editor_text.selection = (11, 20)
        self.editor = FakeNode(self, "notes.txt", "frame", [FakeNode(self, "", "tool_bar"), self.editor_text])
        self.apps = [FakeNode(self, "gnome-terminal-server", "application", [self.terminal], pid=10),
                     FakeNode(self, "gedit", "application", [self.editor], pid=20)]
        self.focus_terminal()

    def focus_terminal(self):
        self.active, self.focus = self.terminal, self.terminal_view

    def focus_editor(self):
        self.active, self.focus = self.editor, self.editor_text
        self.editor.name = "notes.txt"

    def get_child_count(self): return len(self.apps)
    def get_child_at_index(self, index): return self.apps[index]


class FakeAtspi:
    StateType = FakeStateType

    def __init__(self, world, init_result=0):
        self.world, self.desktop, self.init_result, self.init_calls = world, world, init_result, 0

    def init(self):
        self.init_calls += 1
        return self.init_result

    def get_desktop(self, index):
        return self.desktop


def _atspi_adapter(world):
    return AtspiTextAdapter(atspi=FakeAtspi(world), a11y_enabled=lambda: True,
                            environ={"DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"})


class Script:
    """Drives operator focus changes from the CLI's own sleep calls."""

    def __init__(self, world, *, capture_after=2, refocus_after=3, on_refocus=None):
        self.world, self.capture_after, self.refocus_after, self.on_refocus = world, capture_after, refocus_after, on_refocus
        self.capture_sleeps = self.wait_sleeps = 0
        self.confirmed = False
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds
        if not self.confirmed:
            self.capture_sleeps += 1
            if self.capture_sleeps == self.capture_after:
                self.world.focus_editor()  # operator clicks into the editor during the countdown
        else:
            self.wait_sleeps += 1
            if self.wait_sleeps == self.refocus_after and self.on_refocus is not False:
                self.world.focus_editor()  # operator returns to the editor after confirming
                if self.on_refocus:
                    self.on_refocus(self.world)

    def clock(self):
        return self.now

    def confirm(self):
        self.confirmed = True
        self.world.focus_terminal()  # typing the confirmation moves focus to the terminal


def _cli(world, tmp_path, script, *, answer=None, replacement="the report"):
    path = tmp_path / "replacement.txt"
    path.write_text(replacement, encoding="utf-8")
    lines: list[str] = []
    prefix = {}

    def answer_fn():
        return answer if answer is not None else prefix["value"]

    def out(line):
        lines.append(line)
        if line.startswith("--- end ---"):
            prefix["value"] = line.split()[4]
    code = drafts.main(["replace", "--replacement-file", str(path), "--countdown", "3"],
                       adapter=_atspi_adapter(world), terminal=FakeTerminal(answer_fn, script.confirm),
                       out=out, sleep=script.sleep, clock=script.clock)
    return code, lines


def test_e2e_atspi_flow_captures_target_confirms_in_terminal_and_writes_after_refocus(tmp_path):
    world = FakeDesktop()
    script = Script(world)
    code, lines = _cli(world, tmp_path, script)
    assert code == 0, lines
    assert world.editor_text.text == "Dear team, the report is ready."
    assert world.calls == [("delete", 11, 20), ("insert", 11, "the report")]
    assert '"succeeded"' in lines[-1] and "20:gedit" in "\n".join(lines)


def test_e2e_title_change_after_write_still_reads_back_by_identity(tmp_path):
    world = FakeDesktop()
    original = FakeNode.insert_text

    def insert_and_retitle(node, position, text, length):
        node.world.editor.name = "*notes.txt"  # editors mark the window dirty
        return original(node, position, text, length)
    FakeNode.insert_text = insert_and_retitle
    try:
        code, lines = _cli(world, tmp_path, Script(world))
    finally:
        FakeNode.insert_text = original
    assert code == 0 and '"succeeded"' in lines[-1]


def test_e2e_capturing_the_terminal_itself_is_refused(tmp_path):
    world = FakeDesktop()
    world.terminal_view.selection = (0, 1)
    code, lines = _cli(world, tmp_path, Script(world, capture_after=99))
    assert code == 2 and "desktop_control_denied" in lines[-1] and world.calls == []


@pytest.mark.parametrize("change,reason", [
    (lambda w: setattr(w.editor_text, "selection", (11, 19)), "desktop_focus_not_restored"),
    (lambda w: setattr(w.editor_text, "text", "Dear team, the plans is ready."), "desktop_focus_not_restored"),
])
def test_e2e_selection_change_while_confirming_prevents_the_write(tmp_path, change, reason):
    world = FakeDesktop()
    code, lines = _cli(world, tmp_path, Script(world, on_refocus=change))
    assert code == 2 and reason in lines[-1] and '"written": false' in lines[-1] and world.calls == []
    assert world.editor_text.text != "Dear team, the report is ready."


def test_e2e_focus_never_restored_blocks_without_writing(tmp_path):
    world = FakeDesktop()
    code, lines = _cli(world, tmp_path, Script(world, on_refocus=False))
    assert code == 2 and "desktop_focus_not_restored" in lines[-1] and world.calls == []


def test_e2e_write_site_recheck_on_the_same_node_prevents_the_write(tmp_path):
    world = FakeDesktop()
    adapter = _atspi_adapter(world)
    world.focus_editor()
    executor = DesktopDraftExecutor(adapter, paired_edge_id="local")
    preview = executor.preview(executor.inspect()["surface"], "the report")
    control = adapter.focused()
    # Focus moves after the executor's checks but before the node is resolved for writing.
    world.focus_terminal()
    with pytest.raises(DesktopDraftError, match="desktop_focus_changed|desktop_selection_required"):
        adapter.replace(control, 11, 20, "the report")
    world.focus_editor()
    world.editor_text.selection = (12, 20)
    with pytest.raises(DesktopDraftError, match="desktop_focus_changed"):
        adapter.replace(control, 11, 20, "the report")
    assert world.calls == [] and preview.selected_text == "the draft"


class FakeApplication:
    bus_name = ":1.42"


def test_e2e_stable_object_reference_detects_a_replaced_control_at_the_same_position(tmp_path):
    world = FakeDesktop()
    world.editor_text.app, world.editor_text.path = FakeApplication(), "/org/a11y/atspi/accessible/7"
    world.editor.app, world.editor.path = FakeApplication(), "/org/a11y/atspi/accessible/2"
    adapter = _atspi_adapter(world)
    world.focus_editor()
    control = adapter.focused()
    assert control.control_id.endswith("@:1.42/org/a11y/atspi/accessible/7")
    executor = DesktopDraftExecutor(adapter, paired_edge_id="local")
    preview = executor.preview(executor.inspect()["surface"], "the report")
    # The toolkit rebuilt the view: same position, role, text and selection, different object.
    world.editor_text.path = "/org/a11y/atspi/accessible/9"
    result = executor.apply(preview, confirmed_preview_digest=preview.preview_digest, replacement="the report")
    assert result["status"] == "blocked" and result["written"] is False and world.calls == []

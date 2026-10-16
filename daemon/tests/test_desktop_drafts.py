"""C5.3 (#1014): selected desktop draft replacement with a fixture adapter."""
from __future__ import annotations

from dataclasses import replace
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
    base = FocusedControl(app_id="4242:gedit", app_name="gedit", window_id="frame:notes.txt",
                          control_id="text:0:", role="text", name="", editable=True,
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
    (lambda a: replace(a.control, window_id="frame:other.txt"), "desktop_focus_changed"),
    (lambda a: replace(a.control, app_id="77:mousepad", app_name="mousepad"), "desktop_focus_changed"),
    (lambda a: replace(a.control, selection_start=12), "desktop_selection_changed"),
    (lambda a: replace(a.control, text="Dear team, the plans is ready."), "desktop_selection_changed"),
    (lambda a: replace(a.control, control_id="text:1:"), "desktop_selection_changed"),
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
    adapter.before_write = lambda a: setattr(a, "control", replace(a.control, window_id="frame:other"))
    result = executor.apply(preview, confirmed_preview_digest=preview.preview_digest, replacement="the report")
    assert result == {"status": "blocked", "reason": "desktop_focus_changed", "written": False}
    assert adapter.writes == []


def test_unknown_local_mutation_offers_inspection_and_never_reapplies():
    adapter, executor, preview = _ready()
    adapter.after_write = lambda a: setattr(a, "control", replace(a.control, text=a.control.text + " typed"))
    result = executor.apply(preview, confirmed_preview_digest=preview.preview_digest, replacement="the report")
    assert result["status"] == "unknown_local_mutation" and result["reapply_offered"] is False
    assert result["inspect_current_text"]["text"] == "Dear team, the report is ready. typed"
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
    adapter.control = replace(adapter.control, window_id="frame:elsewhere")
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


def test_unavailable_platform_leaves_paired_edge_transport_usable(tmp_path, monkeypatch):
    from paired_edge import PairedEdgeTransport
    monkeypatch.setitem(sys.modules, "gi", None)
    transport = PairedEdgeTransport(origin="http://127.0.0.1:8000", credential=None, device_id="d", pairing_id="p",
                                    spool_path=tmp_path / "spool.json", allow_insecure_test_transport=True,
                                    desktop_adapter=AtspiTextAdapter())
    status = transport.status()
    assert status["desktop_draft"]["state"] == "unavailable"
    assert status["action_authority"] is False and status["count"] == 0


def test_local_cli_requires_typed_preview_confirmation(tmp_path):
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("the report", encoding="utf-8")
    lines: list[str] = []
    adapter = FixtureAdapter(_control())
    code = drafts.main(["replace", "--replacement-file", str(replacement)], adapter=adapter,
                       prompt=lambda _: "no", out=lines.append)
    assert code == 1 and adapter.writes == [] and '"cancelled"' in lines[-1]
    shown = "\n".join(lines)
    assert "the draft" in shown and "the report" in shown
    digest_prefix = next(line for line in lines if line.startswith("Type the preview digest")).split()[5]
    code = drafts.main(["replace", "--replacement-file", str(replacement)], adapter=adapter,
                       prompt=lambda _: digest_prefix, out=lines.append)
    assert code == 0 and adapter.control.text == "Dear team, the report is ready."
    blocked: list[str] = []

    class Unavailable(FixtureAdapter):
        def probe(self):
            return {"state": "unavailable", "reason": "atspi_typelib_missing"}

    assert drafts.main(["inspect"], adapter=Unavailable(_control()), out=blocked.append) == 2
    assert "atspi_typelib_missing" in blocked[0]

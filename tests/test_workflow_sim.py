"""End-to-end simulation of the loop with a fake printer, camera, Claude and human."""
from dataclasses import replace
from pathlib import Path

import pytest

from printloop import workflow as wf_mod
from printloop.approval import Answer
from printloop.config import Config
from printloop.design import DesignOutput
from printloop.detectors import DetectorResult, Signal
from printloop.fixer import FixProposal, SettingChange
from printloop.moonraker import PrintStatus


class FakePrinter:
    def __init__(self, script):
        self.script = list(script)  # states returned by successive status() calls once printing
        self.calls: list[str] = []
        self.state = "standby"
        self.busy_script: list = []

    def printer_info(self):
        return {"state": "ready"}

    def status(self):
        if self.state == "printing" and self.script:
            nxt = self.script.pop(0)
            if nxt in ("complete", "error"):
                self.state = nxt
        return PrintStatus(self.state, "x.gcode", 0.3, 600, "boom" if self.state == "error" else "", 5, 50, 210, 60)

    def busy_reason(self):
        return self.busy_script.pop(0) if self.busy_script else None

    def webcam_snapshot_url(self):
        return "http://cam/snap"

    def upload(self, p, remote_name=None):
        self.calls.append(f"upload:{p.name}")
        self.remote_names = getattr(self, "remote_names", []) + [remote_name]
        return remote_name or p.name

    def start_print(self, f):
        self.calls.append("start")
        self.state = "printing"

    def pause(self):
        self.calls.append("pause")
        self.state = "paused"

    def resume(self):
        self.calls.append("resume")
        self.state = "printing"

    def cancel_and_cool(self):
        self.calls.append("cancel_and_cool")
        self.state = "cancelled"


@pytest.fixture
def make_wf(tmp_path, monkeypatch):
    def fake_snapshot(url, d, label="snap"):
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{label}-{len(list(d.iterdir()))}.jpg"
        p.write_bytes(b"jpg")
        return p

    def fake_render(bin, scad):
        stl = scad.with_suffix(".stl")
        stl.write_text("solid")
        return stl, None

    def fake_slice(cfg, stl, overrides, out, tag):
        out.mkdir(parents=True, exist_ok=True)
        g = out / f"{tag}.gcode"
        g.write_text("; estimated printing time (normal mode) = 10m\n")
        return g

    def fake_claude(model, effort, system, text, schema, images=()):
        if schema is DesignOutput:
            return DesignOutput(scad_code="cube(10);", summary="a cube", assumptions=[], bounding_box_mm=[10, 10, 10])
        return FixProposal(diagnosis="warping", confidence="high", likely_false_positive=False,
                           setting_changes=[SettingChange(key="brim_width", value="5", reason="adhesion")],
                           new_scad_code=None, model_change_summary=None, operator_actions=["clean bed"])

    monkeypatch.setattr(wf_mod.camera, "snapshot", fake_snapshot)
    monkeypatch.setattr(wf_mod.design, "render", fake_render)
    monkeypatch.setattr(wf_mod.slicer, "slice_model", fake_slice)
    monkeypatch.setattr(wf_mod.slicer, "available_settings", lambda cfg: {"brim_width": "0"})
    monkeypatch.setattr("printloop.claude.ask_json", fake_claude)
    monkeypatch.setattr(wf_mod.shutil, "which", lambda b: "/bin/" + b)
    monkeypatch.setattr(wf_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(wf_mod.claude, "available", lambda: None)

    def make(printer_script, detector_script, answers):
        cfg = replace(Config.load(), runs_dir=tmp_path, thermal_protection_verified=True,
                      slack_webhook_url=None, slack_bot_token=None, approval_backend="cli",
                      check_interval_seconds=0, obico_ml_url="http://ml")
        w = wf_mod.Workflow(cfg, "a cube", tmp_path / "run")
        w.slack.enabled = True  # enabled, but posting is stubbed below
        w.slack.post = lambda *a, **k: None
        w.slack.post_image = lambda *a, **k: None
        w.printer = FakePrinter(printer_script)
        det_iter = iter(detector_script)
        w.detectors[0].check = lambda url: DetectorResult("obico", next(det_iter, Signal.OK), 0.0)
        asked = []

        def ask(question, options, images=None, timeout=None, requester_signoff=False):
            choice, comment = answers.pop(0)
            assert choice is None or choice in options, (choice, options)
            asked.append((question.splitlines()[0], choice))
            return Answer(choice, comment)

        w.approver.ask = ask
        w.asked = asked
        return w

    return make


def test_fail_fix_reprint_complete(make_wf):
    w = make_wf(
        printer_script=["printing", "printing", "complete"],
        detector_script=[Signal.OK, Signal.FAILURE, Signal.OK],
        answers=[("approve", ""), ("real", "corner lifted"), ("removed", ""), ("approve", "")],
    )
    w.run()
    calls = w.printer.calls
    assert calls.count("start") == 2
    assert calls.index("pause") < calls.index("cancel_and_cool") < len(calls) - calls[::-1].index("start") - 1
    assert any("fix" in c for c in calls if c.startswith("upload"))  # reprint used the fixed G-code
    assert "corner lifted" in " ".join(w.history)


def test_false_alarm_resumes(make_wf):
    w = make_wf(
        printer_script=["printing", "printing", "complete"],
        detector_script=[Signal.FAILURE, Signal.OK],
        answers=[("approve", ""), ("false", "just a shadow")],
    )
    w.run()
    assert w.printer.calls == ["upload:v1.gcode", "start", "pause", "resume"]


def test_pause_timeout_cancels_and_cools(make_wf):
    w = make_wf(
        printer_script=["printing", "complete"],
        detector_script=[Signal.FAILURE],
        answers=[("approve", ""), (None, ""), ("real", ""), ("removed", ""), ("same", "")],
    )
    w.run()
    assert w.printer.calls[:4] == ["upload:v1.gcode", "start", "pause", "cancel_and_cool"]
    # the follow-up question must not offer "false" (can't resume a cancelled print)
    assert w.asked[2] == ("The print was cancelled. Was it a real failure?", "real")


def test_printer_error_cannot_resume(make_wf):
    w = make_wf(
        printer_script=["error"],
        detector_script=[],
        answers=[("approve", ""), ("abort", "")],
    )
    w.run()
    assert w.printer.calls[-1] == "cancel_and_cool"


def test_preflight_requires_thermal_verification(make_wf):
    w = make_wf([], [], [])
    w.cfg = replace(w.cfg, thermal_protection_verified=False)
    with pytest.raises(SystemExit, match="THERMAL_PROTECTION_VERIFIED"):
        w.preflight()


def test_waits_when_printer_busy_before_start(make_wf):
    w = make_wf(
        printer_script=["complete"],
        detector_script=[],
        answers=[("approve", ""), ("retry", ""), ("retry", "")],
    )
    w.printer.busy_script = [None, "printing F7.gcode", "heaters are on"]  # first None = preflight
    w.run()
    assert [q for q, _ in w.asked].count(
        "The printer is busy (printing F7.gcode), so I can't start. Reply when it's done and the bed is clear.") == 1
    assert w.printer.calls.count("start") == 1


def test_busy_abort_never_starts(make_wf):
    w = make_wf(printer_script=[], detector_script=[], answers=[("approve", ""), ("abort", "")])
    w.printer.busy_script = [None, "printing F7.gcode"]
    w.run()
    assert "start" not in w.printer.calls


def test_lost_contact_alerts_once_then_reconnects(make_wf, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(wf_mod.time, "time", lambda: clock["t"])

    def tick(s):
        clock["t"] += 120  # each check is 2 min apart

    monkeypatch.setattr(wf_mod.time, "sleep", tick)
    w = make_wf(printer_script=["printing", "complete"], detector_script=[], answers=[("approve", "")])
    alerts = []
    w.alert = lambda text, image=None: alerts.append(text)
    real_status = w.printer.status
    outages = {"left": 5}  # 5 failed checks = 10 min offline

    def flaky_status():
        if w.printer.state == "printing" and outages["left"]:
            outages["left"] -= 1
            raise OSError("Network is unreachable")
        return real_status()

    w.printer.status = flaky_status
    w.run()
    lost = [a for a in alerts if "Lost contact" in a]
    back = [a for a in alerts if "Back in contact" in a]
    assert len(lost) == 1 and "unmonitored" in lost[0]
    assert len(back) == 1
    assert any("Print finished" in a for a in alerts)


def test_uploads_get_unique_remote_names(make_wf):
    w = make_wf(printer_script=["complete"], detector_script=[], answers=[("approve", "")])
    w.run()
    assert w.printer.remote_names == [f"printloop_{w.run_dir.name}_v1.gcode"]


def test_revise_images_are_added_to_reference_images(make_wf):
    w = make_wf(printer_script=[], detector_script=[], answers=[])
    w.slack.can_download = True

    def download(f, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\xff\xd8\xff")
        return dest

    w.slack.download = download
    w.add_reference_images([{"name": "side view.png", "mimetype": "image/png"},
                            {"name": "IMG_1", "mimetype": "image/jpeg"}])
    assert [p.name for p in w.reference_images] == ["ref1_01-side_view.png", "ref2_02-IMG_1.jpg"]
    w.add_reference_images([{"name": "more.png", "mimetype": "image/png"}])
    assert w.reference_images[-1].name == "ref3_03-more.png" and all(p.exists() for p in w.reference_images)

from pathlib import Path

import pytest

from printloop import fixer, slicer
from printloop.detectors import DetectorResult, ObicoDetector, Signal, Verdict, dual_rule

OK, FAIL, NA = Signal.OK, Signal.FAILURE, Signal.UNAVAILABLE


@pytest.mark.parametrize(
    "a,b,expected",
    [
        (FAIL, FAIL, Verdict.CONFIRMED),
        (FAIL, OK, Verdict.DISAGREE),
        (OK, FAIL, Verdict.DISAGREE),
        (OK, OK, Verdict.CONTINUE),
        (FAIL, NA, Verdict.SINGLE),
        (NA, FAIL, Verdict.SINGLE),
        (OK, NA, Verdict.CONTINUE),
        (NA, NA, Verdict.CONTINUE),
    ],
)
def test_dual_rule(a, b, expected):
    d = dual_rule([DetectorResult("ext", a), DetectorResult("k1c", b)])
    assert d.verdict is expected
    assert d.should_pause == (expected is not Verdict.CONTINUE)


class FakeResp:
    def __init__(self, dets):
        self.dets = dets

    def raise_for_status(self):
        pass

    def json(self):
        return {"detections": self.dets}


def test_obico_consecutive_and_high(monkeypatch):
    det = ObicoDetector("http://ml", None, threshold=0.4, high_threshold=0.8, consecutive=2)
    seq = iter([[["failure", 0.5, [0, 0, 1, 1]]], [["failure", 0.5, [0, 0, 1, 1]]], [], [["failure", 0.9, [0, 0, 1, 1]]]])
    monkeypatch.setattr("printloop.detectors.requests.get", lambda *a, **k: FakeResp(next(seq)))
    assert det.check("u").signal is OK  # first hit
    assert det.check("u").signal is FAIL  # second consecutive hit
    assert det.check("u").signal is OK  # clean frame resets
    assert det.check("u").signal is FAIL  # one very confident frame


def test_obico_unavailable_without_url():
    assert ObicoDetector(None, None, 0.4, 0.8, 2).check("u").signal is NA


def test_fix_review_rejects_unknown_keys_and_diffs():
    base = {"first_layer_temperature": "210", "brim_width": "0"}
    proposal = fixer.FixProposal(
        diagnosis="warping",
        confidence="high",
        likely_false_positive=False,
        setting_changes=[
            fixer.SettingChange(key="brim_width", value="5", reason="adhesion"),
            fixer.SettingChange(key="made_up_key", value="1", reason="?"),
        ],
        new_scad_code="cube(11);\n",
        model_change_summary="bigger",
        operator_actions=["clean bed"],
    )
    fix = fixer.review(proposal, "cube(10);\n", base, {"first_layer_temperature": "215"})
    assert fix.overrides == {"first_layer_temperature": "215", "brim_width": "5"}
    assert fix.rejected_keys == ["made_up_key"]
    assert "brim_width: 0  ->  5" in fix.settings_diff
    assert "+cube(11);" in fix.scad_diff
    assert "warping" in fixer.format_for_human(fix)


def test_schemas_are_strict():
    for model in (fixer.FixProposal,):
        schema = model.model_json_schema()
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_gcode_stats(tmp_path: Path):
    g = tmp_path / "x.gcode"
    g.write_text("G1 X0\n; filament used [mm] = 1234.5\n; total filament used [g] = 3.70\n"
                 "; estimated printing time (normal mode) = 42m 10s\n")
    assert slicer.gcode_stats(g) == {"estimated_time": "42m 10s", "filament_g": "3.70", "filament_mm": "1234.5"}


def test_ini_roundtrip(tmp_path: Path):
    ini = tmp_path / "p.ini"
    ini.write_text("# generated\nlayer_height = 0.2\nstart_gcode = G28\\nG1 Z5\n")
    assert slicer._read_ini(ini) == {"layer_height": "0.2", "start_gcode": "G28\\nG1 Z5"}

import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from printloop import design, slicer
from printloop.config import Config

ORCA = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
ROOT = Path(__file__).resolve().parents[1]


def test_orca_display_and_value_roundtrip():
    assert slicer._orca_display(["220"]) == "220"
    assert slicer._orca_display("auto_brim") == "auto_brim"
    assert slicer._orca_display(["0x0", "220x0"]) == '["0x0", "220x0"]'
    assert slicer._orca_value(["220"], "230") == ["230"]
    assert slicer._orca_value(["220"], '["230"]') == ["230"]
    assert slicer._orca_value(["0x0", "220x0"], "0x0, 200x0") == ["0x0", "200x0"]
    assert slicer._orca_value("5", 8) == "8"


@pytest.mark.skipif(not (Path(ORCA).exists() and shutil.which("openscad")), reason="OrcaSlicer/OpenSCAD not installed")
def test_render_and_slice_with_fix_overrides(tmp_path):
    cfg = replace(Config.load(), slicer="orca", slicer_bin=ORCA,
                  orca_machine=ROOT / "profiles/k1c_machine.json",
                  orca_process=ROOT / "profiles/k1c_process.json",
                  orca_filament=ROOT / "profiles/k1c_filament_pla.json")
    scad = tmp_path / "part.scad"
    scad.write_text("difference(){ cube([30,20,10]); translate([15,10,-1]) cylinder(d=8,h=12,$fn=64); }\n")
    stl, preview = design.render("openscad", scad)
    assert stl.stat().st_size > 0 and preview and preview.exists()

    base = slicer.available_settings(cfg)
    assert base["nozzle_temperature_initial_layer"] == "220" and base["brim_type"] == "auto_brim"

    overrides = {"nozzle_temperature_initial_layer": "225", "brim_type": "outer_only", "brim_width": "8"}
    gcode = slicer.slice_model(cfg, stl, overrides, tmp_path / "gcode", "fix1")
    text = gcode.read_text()
    assert "START_PRINT EXTRUDER_TEMP=225 BED_TEMP=50" in text
    assert re.search(r"^; brim_width = 8$", text, re.M) and re.search(r"^; brim_type = outer_only$", text, re.M)
    assert slicer.gcode_stats(gcode)["estimated_time"]

import shutil
import subprocess
from pathlib import Path

import pytest

from printloop import design

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def test_prepare_images_copies_and_rejects(tmp_path):
    src = tmp_path / "sketch.png"
    src.write_bytes(PNG)
    out = design.prepare_images([src], tmp_path / "run" / "reference")
    assert [p.name for p in out] == ["ref1_sketch.png"] and out[0].read_bytes() == PNG
    with pytest.raises(FileNotFoundError):
        design.prepare_images([tmp_path / "missing.jpg"], tmp_path / "r2")
    bad = tmp_path / "model.pdf"
    bad.write_text("x")
    with pytest.raises(ValueError):
        design.prepare_images([bad], tmp_path / "r3")


@pytest.mark.skipif(not shutil.which("sips"), reason="macOS sips not available")
def test_prepare_images_converts_heic(tmp_path):
    png = tmp_path / "a.png"
    png.write_bytes(PNG)
    heic = tmp_path / "photo.heic"
    if subprocess.run(["sips", "-s", "format", "heic", str(png), "--out", str(heic)], capture_output=True).returncode:
        pytest.skip("sips can't write HEIC here")
    out = design.prepare_images([heic], tmp_path / "ref")
    assert out[0].name == "ref1_photo.jpg" and out[0].read_bytes()[:3] == b"\xff\xd8\xff"


def test_generate_passes_reference_images_and_previous_preview(tmp_path, monkeypatch):
    seen = {}

    def fake_ask(model, effort, system, text, schema, images=()):
        seen["text"], seen["images"] = text, [Path(p).name for p in images]
        return design.DesignOutput(scad_code="cube(10);", summary="s", assumptions=[], bounding_box_mm=[10, 10, 10])

    def fake_render(bin, scad):
        return scad.with_suffix(".stl"), None

    monkeypatch.setattr(design.claude, "ask_json", fake_ask)
    monkeypatch.setattr(design, "render", fake_render)
    ref = tmp_path / "ref1_sketch.png"
    ref.write_bytes(PNG)
    prev = tmp_path / "model_v1.png"
    prev.write_bytes(PNG)

    design.generate("a knob", tmp_path, "opus", "high", "openscad", images=[ref])
    assert seen["images"] == ["ref1_sketch.png"] and "ref1_sketch.png" in seen["text"]

    design.generate("a knob", tmp_path, "opus", "high", "openscad", previous_scad="cube(5);",
                    feedback="rounder", version=2, images=[ref], previous_preview=prev)
    assert seen["images"] == ["ref1_sketch.png", "model_v1.png"]
    assert "render of that previous version" in seen["text"] and "rounder" in seen["text"]

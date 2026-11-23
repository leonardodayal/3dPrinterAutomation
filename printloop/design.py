"""Design request -> OpenSCAD source -> STL + preview PNG.

Claude writes parametric OpenSCAD; we render it with the OpenSCAD CLI and feed any
render error back to Claude (up to MAX_ATTEMPTS). CadQuery/Blender backends can be
added behind the same `render()` contract.
"""
from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from . import claude

MAX_ATTEMPTS = 3

K1C_CONTEXT = """\
Target printer: Creality K1C (CoreXY, enclosed, 0.4 mm nozzle, build volume 220 x 220 x 250 mm),
FDM, default material PLA. Units are millimetres."""

SYSTEM = f"""You design functional parts for FDM 3D printing by writing OpenSCAD code.

{K1C_CONTEXT}

Rules:
- Output a single self-contained .scad file (no `include`/`use` of external libraries).
- Put every meaningful dimension in a named parameter at the top with a comment.
- Model the part already in its best print orientation: largest flat face on z=0, no part below z=0.
- Prefer designs that print without supports: overhangs <= 45 degrees, bridges < 10 mm, chamfers instead of
  downward-facing fillets on the bed side.
- Walls >= 1.2 mm, small features >= 0.8 mm, clearance for fits 0.2-0.3 mm per side.
- Use $fn appropriate for the size (e.g. 64-128 for visible curves).
- Must fit the build volume.
- Reference images (photos, sketches, drawings) may be attached. Use them for shape, features and proportions.
  Take dimensions from the request text or from measurements written in the images. If you have to estimate a
  size from a photo, say so in `assumptions` and make that dimension an easy-to-change parameter.
"""


class DesignOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scad_code: str
    summary: str
    assumptions: list[str]
    bounding_box_mm: list[float]


@dataclass
class Design:
    scad: Path
    stl: Path
    preview: Path | None
    summary: str
    assumptions: list[str]


class RenderError(RuntimeError):
    pass


def render(openscad_bin: str, scad: Path) -> tuple[Path, Path | None]:
    stl = scad.with_suffix(".stl")
    r = subprocess.run([openscad_bin, "-o", str(stl), str(scad)], capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not stl.exists() or stl.stat().st_size == 0:
        raise RenderError((r.stderr or r.stdout)[-3000:])
    if "WARNING" in r.stderr and "not a valid 2-manifold" in r.stderr:
        raise RenderError("Mesh is not manifold:\n" + r.stderr[-3000:])

    preview = scad.with_suffix(".png")
    p = subprocess.run(
        [openscad_bin, "-o", str(preview), "--imgsize=1024,768", "--viewall", "--autocenter",
         "--colorscheme=Tomorrow", "--render=1", str(scad)],
        capture_output=True, text=True, timeout=600,
    )  # PNG export needs a display on a headless Pi: run under `xvfb-run` if this fails.
    return stl, (preview if p.returncode == 0 and preview.exists() else None)


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def prepare_images(paths: Sequence[Path], dest: Path) -> list[Path]:
    """Copy reference images into the run folder (kept with the run), converting HEIC photos to JPEG."""
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for i, src in enumerate(paths, 1):
        src = Path(src).expanduser()
        if not src.is_file():
            raise FileNotFoundError(f"Reference image not found: {src}")
        suffix = src.suffix.lower()
        target = dest / f"ref{i}_{src.stem}{suffix if suffix in IMAGE_SUFFIXES else '.jpg'}"
        if suffix in IMAGE_SUFFIXES:
            shutil.copy(src, target)
        elif suffix in (".heic", ".heif") and shutil.which("sips"):
            subprocess.run(["sips", "-s", "format", "jpeg", str(src), "--out", str(target)],
                           check=True, capture_output=True)
        else:
            raise ValueError(f"Unsupported image type {suffix} (use jpg, png, gif, webp or heic): {src}")
        out.append(target)
    return out


def generate(request: str, out_dir: Path, model: str, effort: str, openscad_bin: str,
             previous_scad: str | None = None, feedback: str | None = None, version: int = 1,
             images: Sequence[Path] = (), previous_preview: Path | None = None) -> Design:
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt = f"Design request:\n{request}"
    attached = list(images)
    if attached:
        prompt += ("\n\nReference images from the user (in this order): "
                   + ", ".join(p.name for p in attached))
    if previous_scad and feedback:
        prompt += f"\n\nThe user rejected the previous version with this feedback:\n{feedback}\n\nPrevious OpenSCAD:\n```openscad\n{previous_scad}\n```"
        if previous_preview and previous_preview.exists():
            attached.append(previous_preview)
            prompt += f"\n\n{previous_preview.name} is a render of that previous version."

    error: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        content = prompt if not error else prompt + f"\n\nYour last attempt failed to render:\n```\n{error}\n```\nFix it."
        out = claude.ask_json(model, effort, SYSTEM, content, DesignOutput, images=attached)
        scad = out_dir / f"model_v{version}.scad"
        scad.write_text(out.scad_code)
        try:
            stl, preview = render(openscad_bin, scad)
            return Design(scad, stl, preview, out.summary, out.assumptions)
        except RenderError as e:
            error = str(e)
            print(f"OpenSCAD render failed (attempt {attempt}/{MAX_ATTEMPTS}); asking Claude to fix it")
    raise RenderError(f"Could not produce a valid model after {MAX_ATTEMPTS} attempts:\n{error}")

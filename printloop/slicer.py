"""STL -> G-code with the base K1C profile plus per-iteration setting overrides.

Overrides are a flat {setting_key: value} dict layered on top of the base profile, so every
recovery attempt is an explicit, reviewable diff against a known-good profile.

- prusaslicer: SLICER_PROFILE is an .ini exported from PrusaSlicer (File > Export > Export Config)
  with the Creality K1C printer/filament/print presets selected.
- orca: ORCA_MACHINE_JSON / ORCA_PROCESS_JSON / ORCA_FILAMENT_JSON exported from OrcaSlicer or
  Creality Print. Verify the CLI flags against your Orca version (`orca-slicer --help`).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .bedview import finish_end_gcode
from .config import Config


class SliceError(RuntimeError):
    pass


def _read_ini(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith(("#", ";")):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def available_settings(cfg: Config) -> dict[str, str]:
    """Current base-profile values, so Claude proposes real keys with known starting values."""
    if cfg.slicer == "prusaslicer":
        if not cfg.slicer_profile:
            raise SliceError("Set SLICER_PROFILE to your exported K1C .ini")
        return _read_ini(cfg.slicer_profile)
    merged: dict[str, str] = {}
    for p in (cfg.orca_machine, cfg.orca_process, cfg.orca_filament):
        if p:
            for k, v in json.loads(p.read_text()).items():
                merged[k] = _orca_display(v)
    return merged


def _orca_display(v) -> str:
    """Orca stores most values as lists of strings (one per extruder): show ["220"] as 220."""
    if isinstance(v, list) and len(v) == 1:
        return str(v[0])
    return v if isinstance(v, str) else json.dumps(v)


def _orca_value(existing, v: str):
    """Write a plain override back in the shape Orca expects for that key."""
    v = str(v).strip()
    if isinstance(existing, list):
        if v.startswith("["):
            try:
                return [str(x) for x in json.loads(v)]
            except ValueError:
                pass
        return [v] if len(existing) <= 1 else [x.strip() for x in v.split(",")]
    return v


def slice_model(cfg: Config, stl: Path, overrides: dict[str, str], out_dir: Path, tag: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    gcode = out_dir / f"{stl.stem}_{tag}.gcode"
    if cfg.slicer == "prusaslicer":
        base = _read_ini(cfg.slicer_profile)  # type: ignore[arg-type]
        merged = {**base, **overrides}
        ini = out_dir / f"profile_{tag}.ini"
        ini.write_text("".join(f"{k} = {v}\n" for k, v in merged.items()))
        cmd = [cfg.slicer_bin, "--export-gcode", "--load", str(ini), "-o", str(gcode), str(stl)]
    elif cfg.slicer == "orca":
        files = {}
        for name, p in (("machine", cfg.orca_machine), ("process", cfg.orca_process), ("filament", cfg.orca_filament)):
            if not p:
                raise SliceError(f"Set ORCA_{name.upper()}_JSON")
            files[name] = json.loads(p.read_text())
        if cfg.finish_photo_lift:  # keep the bed near the camera for the finish photo (see bedview.py)
            files["machine"]["machine_end_gcode"] = finish_end_gcode(cfg.photo_park_xy)
        for k, v in overrides.items():
            # Put each override where the key already lives; default to the process profile.
            target = next((f for f in ("filament", "process", "machine") if k in files[f]), "process")
            files[target][k] = _orca_value(files[target].get(k), v)
        work = out_dir / f"orca_{tag}"
        work.mkdir(parents=True, exist_ok=True)
        paths = {}
        for name, data in files.items():
            paths[name] = work / f"{name}.json"
            paths[name].write_text(json.dumps(data, indent=2))
        # --arrange centres the part on the bed; --orient 0 keeps the orientation the design chose.
        cmd = [cfg.slicer_bin, "--slice", "0", "--arrange", "1", "--orient", "0", "--outputdir", str(work),
               "--load-settings", f"{paths['machine']};{paths['process']}",
               "--load-filaments", str(paths["filament"]), str(stl)]
    else:
        raise SliceError(f"Unknown SLICER={cfg.slicer}")

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    if cfg.slicer == "orca" and r.returncode == 0:
        produced = sorted((out_dir / f"orca_{tag}").glob("plate_*.gcode"))
        if produced:
            produced[0].rename(gcode)
    if r.returncode != 0 or not gcode.exists():
        raise SliceError(f"Slicer failed ({r.returncode}):\n{(r.stderr or r.stdout)[-3000:]}")
    return gcode


def gcode_stats(gcode: Path) -> dict[str, str]:
    """Pull estimated time and filament from the slicer's G-code comments."""
    stats: dict[str, str] = {}
    patterns = {
        "estimated_time": r";\s*(?:estimated printing time \(normal mode\)|model printing time|total estimated time)\s*[=:]\s*(.+)",
        "filament_g": r";\s*(?:total filament used \[g\]|filament used \[g\]|total filament weight \[g\])\s*[=:]\s*(.+)",
        "filament_mm": r";\s*filament used \[mm\]\s*[=:]\s*(.+)",
    }
    text = gcode.read_text(errors="ignore")
    head_tail = text[:20000] + text[-40000:]
    for key, pat in patterns.items():
        m = re.search(pat, head_tail, re.IGNORECASE)
        if m:
            stats[key] = m.group(1).strip()
    return stats

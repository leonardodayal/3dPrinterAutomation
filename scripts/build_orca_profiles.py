#!/usr/bin/env python3
"""Flatten OrcaSlicer system presets into standalone JSON files the Orca CLI can load.

Orca 2.5 ships its system profiles packed (.opc), and its CLI can't resolve `inherits` against
them. The same presets exist as plain JSON in the OrcaSlicer repo, so we resolve the inheritance
chain ourselves and write complete machine/process/filament files.

Usage:
  git clone --depth 1 --branch v2.5.2 --filter=blob:none --sparse \\
      https://github.com/OrcaSlicer/OrcaSlicer.git /tmp/orca-src
  (cd /tmp/orca-src && git sparse-checkout set resources/profiles/Creality resources/profiles/OrcaFilamentLibrary)
  python scripts/build_orca_profiles.py /tmp/orca-src/resources/profiles profiles/
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PRESETS = {
    "machine": "Creality K1C 0.4 nozzle",
    "process": "0.20mm Standard @Creality K1C",
    "filament_pla": "Generic PLA @K1C-all",
    "filament_petg": "Generic PETG @K1C-all",
}
DROP = {"inherits", "instantiation", "setting_id", "base_id"}


def index(profiles_root: Path) -> dict[tuple[str, str], dict]:
    """(type, name) -> raw json, across the vendor folders we checked out."""
    out: dict[tuple[str, str], dict] = {}
    for f in profiles_root.glob("*/*/*.json"):
        kind = f.parent.name  # machine | process | filament | machine_model...
        if kind not in ("machine", "process", "filament"):
            continue
        try:
            data = json.loads(f.read_text())
        except ValueError:
            continue
        if "name" in data:
            out[(kind, data["name"])] = data
    return out


def resolve(idx: dict, kind: str, name: str, seen: tuple[str, ...] = ()) -> dict:
    if name in seen:
        raise ValueError(f"inheritance loop: {' -> '.join(seen + (name,))}")
    try:
        data = idx[(kind, name)]
    except KeyError:
        raise KeyError(f"{kind} preset not found: {name!r}") from None
    parent = data.get("inherits")
    merged = resolve(idx, kind, parent, seen + (name,)) if parent else {}
    merged.update(data)
    return merged


def main(src: str, dest: str) -> None:
    idx = index(Path(src))
    out_dir = Path(dest)
    out_dir.mkdir(parents=True, exist_ok=True)
    for key, name in PRESETS.items():
        kind = key.split("_")[0]
        cfg = {k: v for k, v in resolve(idx, kind, name).items() if k not in DROP}
        cfg.update({"name": name, "from": "User", "type": kind})
        path = out_dir / f"k1c_{key}.json"
        path.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
        print(f"{path}: {len(cfg)} settings ({name})")


if __name__ == "__main__":
    main(*sys.argv[1:3])

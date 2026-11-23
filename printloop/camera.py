from __future__ import annotations

import time
from pathlib import Path

import requests


def snapshot(url: str, dest_dir: Path, label: str = "snap") -> Path:
    """Grab one JPEG frame from the printer camera and save it."""
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    if not r.headers.get("content-type", "image/jpeg").startswith("image/"):
        raise RuntimeError(f"Camera returned {r.headers.get('content-type')} instead of an image")
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}.jpg"
    path.write_bytes(r.content)
    return path

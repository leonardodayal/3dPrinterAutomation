"""CLI.

  python -m printloop run "a wall hook for a 25mm broom handle"
  python -m printloop run --scad my_part.scad "bracket"   # skip generation, start from your model
  python -m printloop run --image sketch.jpg --image photo.heic "replacement knob like the photo, 6mm D-shaft"
  python -m printloop serve         # take `print: ...` requests from the shared Slack channel
  python -m printloop preflight
  python -m printloop status
  python -m printloop test-alert
  python -m printloop cancel        # cancel + heaters off
  python -m printloop estop         # Klipper emergency stop (M112)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config
from .moonraker import Moonraker
from .workflow import Workflow


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="printloop", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="design -> approve -> print -> monitor -> fix loop")
    run.add_argument("request", help="what to design (also used as context when fixing)")
    run.add_argument("--scad", type=Path, help="start from an existing OpenSCAD file instead of generating one")
    run.add_argument("--image", type=Path, action="append", default=[], metavar="PATH",
                     help="reference photo/sketch for the design (repeatable; jpg, png, gif, webp, heic)")
    bv = sub.add_parser("bed-view", help="(watch the printer!) raise the bed for a photo, to tune BED_PHOTO_Z / "
                        "PHOTO_PARK_XY; Z must already be homed")
    bv.add_argument("--z", type=float, help="bed Z to try (default BED_PHOTO_Z, else 60)")
    bv.add_argument("--park", help="toolhead X,Y to try (default PHOTO_PARK_XY)")
    bv.add_argument("--lower", action="store_true", help="just lower the bed to the bottom")
    for name in ("serve", "preflight", "status", "test-alert", "cancel", "estop"):
        sub.add_parser(name)
    args = ap.parse_args(argv)
    cfg = Config.load()
    printer = Moonraker(cfg.moonraker_url, cfg.moonraker_api_key, web_url=cfg.moonraker_web_url)

    if args.cmd == "run":
        Workflow(cfg, args.request, images=args.image).run(scad=args.scad)
    elif args.cmd == "serve":
        from .serve import Server
        Server(cfg).serve_forever()
    elif args.cmd == "preflight":
        Workflow(cfg, "preflight").preflight()
        print("Preflight OK")
    elif args.cmd == "status":
        print(printer.printer_info())
        print(printer.status())
        print("snapshot url:", cfg.camera_snapshot_url or printer.webcam_snapshot_url())
    elif args.cmd == "test-alert":
        wf = Workflow(cfg, "test-alert")
        wf.snapshot_url = wf.snapshot_url or printer.webcam_snapshot_url()
        snap = wf.snap("test") if wf.snapshot_url else None
        wf.alert(":test_tube: printloop test alert: Slack is wired up.", snap)
    elif args.cmd == "bed-view":
        from . import camera
        from .bedview import BedView
        park = tuple(float(v) for v in args.park.split(",")) if args.park else cfg.photo_park_xy
        view = BedView(printer, cfg.runs_dir / "bed_state.json", park)
        why = view.lower() if args.lower else view.raise_for_photo(args.z or cfg.bed_photo_z or 60)
        if why:
            print(f"Didn't move: {why}")
            return 1
        url = cfg.camera_snapshot_url or printer.webcam_snapshot_url()
        if url and not args.lower:
            print("Snapshot:", camera.snapshot(url, cfg.runs_dir / "bed-view", "bed-view").resolve())
    elif args.cmd == "cancel":
        printer.cancel_and_cool()
        print("Cancelled; heaters off.")
    elif args.cmd == "estop":
        printer.emergency_stop()
        print("Emergency stop sent. Klipper needs FIRMWARE_RESTART before the next print.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Bed positions for photos. The K1C camera sits high at the front, so with the bed low (where the stock
END_PRINT leaves it) the photos show almost nothing, and the stock park spot puts the toolhead in front of the lens.

- Finish photo (FINISH_PHOTO_LIFT, on by default): printloop's own prints end with FINISH_END_GCODE instead of
  the stock END_PRINT. It does the same cleanup minus the bed drop and `M84`: the bed stops just
  below the finished part, the head parks at PHOTO_PARK_XY and the motors stay on (Z stays homed). The workflow
  takes the finish photo, then lowers the bed. Moving the bed DOWN can't hit anything.
- Approval photo (BED_PHOTO_Z, off by default): before asking to start a print, raise the bed to BED_PHOTO_Z so the
  bed photo is useful. That moves the bed TOWARD the nozzle, so it is guarded:
  * only when Z is still homed (after a printloop print). Never home just for a photo: homing drives the bed up
    into the nozzle, which is exactly what can't be done before knowing the bed is clear;
  * the gap is at least the tallest part printloop knows about (last finished part, current model) + PART_MARGIN;
  * never while the printer is busy.
  Something unknown and taller than the gap on the bed would still be hit: keep BED_PHOTO_Z generous.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PART_MARGIN = 15.0   # mm between the tallest known part and the nozzle when raising the bed
LOWER_MARGIN = 5.0   # lower the bed to axis max minus this
Z_SPEED = 600        # mm/min: slow, like the stock macros

# Stock END_PRINT minus END_PRINT_POINT's bed drop and the final M84 (which would un-home Z).
FINISH_END_GCODE = """; printloop end: bed stays just below the part for the finish photo, motors stay on
G91
G1 E-1.0 F180
G90
G1 Z{{min(max_layer_z + 10, printable_height)}} F600
G1 X{x:.1f} Y{y:.1f} F12000
Qmode_exit
EXCLUDE_OBJECT_RESET
PRINT_PREPARE_CLEAR
M220 S100
SET_VELOCITY_LIMIT ACCEL=5000 ACCEL_TO_DECEL=2500
TURN_OFF_HEATERS
M107 P1
M107 P2
WAIT_TEMP_START
"""


def finish_end_gcode(park_xy: tuple[float, float]) -> str:
    return FINISH_END_GCODE.format(x=park_xy[0], y=park_xy[1])


def gcode_max_z(gcode: Path) -> float | None:
    """Height of the printed part, from the slicer header or the highest Z move."""
    text = gcode.read_text(errors="ignore")
    if m := re.search(r";\s*max_z_height\s*[:=]\s*([\d.]+)", text[:200000]):
        return float(m.group(1))
    zs = [float(z) for z in re.findall(r"^G[01]\b[^;\n]*\bZ([\d.]+)", text, re.M)]
    return max(zs) if zs else None


class BedView:
    def __init__(self, printer, state_path: Path, park_xy: tuple[float, float], log=None):
        self.printer, self.state_path, self.park_xy, self.log = printer, state_path, park_xy, log

    # --- remembered height of the last finished part ----------------------
    def last_part_height(self) -> float:
        try:
            return float(json.loads(self.state_path.read_text()).get("part_height") or 0)
        except (OSError, ValueError):
            return 0.0

    def remember_part(self, height: float | None) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"part_height": height or 0}))

    def _note(self, event: str, **kw) -> None:
        if self.log:
            self.log.log(event, **kw)

    def _homed_z(self) -> tuple[dict, bool]:
        th = self.printer.toolhead()
        return th, "z" in (th.get("homed_axes") or "")

    # --- moves -------------------------------------------------------------
    def raise_for_photo(self, z: float, part_height: float = 0.0) -> str | None:
        """Move the bed up to Z=z (bigger gap if a known part needs it). Returns why it didn't move, or None."""
        if z <= 0:
            return "disabled"
        if busy := self.printer.busy_reason():
            return f"printer busy ({busy})"
        th, homed = self._homed_z()
        if not homed:
            return "Z isn't homed (e.g. after a touchscreen print), and homing could hit whatever is on the bed"
        target = max(z, max(part_height, self.last_part_height()) + PART_MARGIN)
        zmax = float((th.get("axis_maximum") or [0, 0, 255])[2])
        target = min(target, zmax - LOWER_MARGIN)
        x, y = self.park_xy
        park = f"G1 X{x:.1f} Y{y:.1f} F12000\n" if "xy" in (th.get("homed_axes") or "") else ""
        self.printer.gcode(f"G90\n{park}G1 Z{target:.1f} F{Z_SPEED}\nM400")
        self._note("bed_raised_for_photo", z=target)
        return None

    def lower(self) -> str | None:
        """Lower the bed to the bottom (always safe). Returns why it didn't move, or None."""
        # Not busy_reason(): the end G-code's cool-down timer can look "busy" although nothing prints.
        if (state := self.printer.status().state) in ("printing", "paused"):
            return f"printer is {state}"
        th, homed = self._homed_z()
        if not homed:
            return "Z isn't homed"
        zmax = float((th.get("axis_maximum") or [0, 0, 255])[2])
        cur = float((th.get("position") or [0, 0, 0])[2])
        target = zmax - LOWER_MARGIN
        if cur < target:
            self.printer.gcode(f"G90\nG1 Z{target:.1f} F{Z_SPEED}")
            self._note("bed_lowered", z=target)
        return None

    def settle(self) -> None:
        """Wait for queued moves (the end G-code) to finish before taking a photo."""
        self.printer.gcode("M400")

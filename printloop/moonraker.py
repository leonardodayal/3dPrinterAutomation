"""Thin Moonraker REST client for the K1C (Klipper)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

log = logging.getLogger(__name__)

# On the K1C (Creality Helper Script) the web UI and its /webcam/ proxy live on nginx port 4408.
DEFAULT_WEB_PORT = 4408


class MoonrakerError(RuntimeError):
    pass


@dataclass
class PrintStatus:
    state: str  # standby | printing | paused | complete | cancelled | error
    filename: str
    progress: float  # 0..1
    print_duration: float
    message: str
    current_layer: int | None
    total_layer: int | None
    extruder_temp: float | None
    bed_temp: float | None


class Moonraker:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 10, web_url: str | None = None):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        if api_key:
            self.session.headers["X-Api-Key"] = api_key
        self.timeout = timeout
        u = urlparse(self.base)
        self.web_url = (web_url or f"{u.scheme or 'http'}://{u.hostname}:{DEFAULT_WEB_PORT}").rstrip("/")

    def _req(self, method: str, path: str, **kw) -> dict:
        try:
            r = self.session.request(method, self.base + path, timeout=kw.pop("timeout", self.timeout), **kw)
        except requests.RequestException as e:
            raise MoonrakerError(f"{method} {path}: {e}") from e
        try:
            body = r.json()
        except ValueError:
            raise MoonrakerError(
                f"{method} {path}: HTTP {r.status_code}, not a Moonraker JSON response "
                f"(is MOONRAKER_URL pointing at the web UI instead of port 7125?): {r.text[:200]!r}"
            ) from None
        if not r.ok or (isinstance(body, dict) and "error" in body):
            err = body.get("error") if isinstance(body, dict) else None
            msg = err.get("message") if isinstance(err, dict) else r.text[:300]
            raise MoonrakerError(f"{method} {path}: HTTP {r.status_code} {msg}")
        if not isinstance(body, dict):
            raise MoonrakerError(f"{method} {path}: unexpected response {body!r:.200}")
        # Most endpoints wrap the payload in {"result": ...}; file upload does not.
        result = body.get("result", body)
        return result if isinstance(result, dict) else {"value": result}

    # --- status -----------------------------------------------------------
    def printer_info(self) -> dict:
        return self._req("GET", "/printer/info")

    def status(self) -> PrintStatus:
        res = self._req(
            "GET",
            "/printer/objects/query",
            params={"print_stats": "", "virtual_sdcard": "", "extruder": "", "heater_bed": ""},
        ).get("status") or {}
        ps, vsd = res.get("print_stats") or {}, res.get("virtual_sdcard") or {}
        info = ps.get("info") or {}
        return PrintStatus(
            state=ps.get("state", "unknown"),
            filename=ps.get("filename", ""),
            progress=float(vsd.get("progress") or 0.0),
            print_duration=float(ps.get("print_duration") or 0.0),
            message=ps.get("message", ""),
            current_layer=info.get("current_layer"),
            total_layer=info.get("total_layer"),
            extruder_temp=(res.get("extruder") or {}).get("temperature"),
            bed_temp=(res.get("heater_bed") or {}).get("temperature"),
        )

    def busy_reason(self) -> str | None:
        """Why the printer can't take a new job right now, or None if it's free.

        Prints started from the touchscreen or Creality's app heat up *before* Klipper's
        print_stats switches to "printing", so heater targets and idle_timeout count too.
        """
        res = self._req(
            "GET", "/printer/objects/query",
            params={"print_stats": "state,filename", "extruder": "target", "heater_bed": "target",
                    "idle_timeout": "state", "virtual_sdcard": "is_active"},
        ).get("status") or {}
        ps = res.get("print_stats") or {}
        if ps.get("state") in ("printing", "paused"):
            return f"{ps.get('state')} {ps.get('filename') or ''}".strip()
        if (res.get("virtual_sdcard") or {}).get("is_active"):
            return "a file is being printed"
        if (res.get("idle_timeout") or {}).get("state") == "Printing":
            return "Klipper is busy (idle_timeout=Printing)"
        heating = [f"{name} target {t:.0f}°C" for name in ("extruder", "heater_bed")
                   if (t := float((res.get(name) or {}).get("target") or 0)) > 0]
        if heating:
            return "heaters are on (" + ", ".join(heating) + "): a print may be starting"
        return None

    def time_left(self) -> str | None:
        """Human-readable time left on the current print, e.g. "32% done, ~50-70 min left (free ~20:05)".

        Uses both the slicer's estimate and the progress so far; prints often run slower than sliced.
        """
        import datetime
        st = self.status()
        if st.state not in ("printing", "paused") or not st.filename:
            return None
        try:
            est = float(self._req("GET", "/server/files/metadata", params={"filename": st.filename})
                        .get("estimated_time") or 0)
        except MoonrakerError:
            est = 0.0
        guesses = []
        if est:
            guesses.append(max(0.0, est - st.print_duration) / 60)
        if st.progress > 0.03:
            guesses.append(st.print_duration / st.progress * (1 - st.progress) / 60)
        done = f"{st.progress:.0%} done after {st.print_duration / 60:.0f} min"
        if not guesses:
            return f"{st.filename}: {done}"
        lo, hi = min(guesses), max(guesses)
        span = f"~{lo:.0f} min" if hi - lo < 5 else f"~{lo:.0f}-{hi:.0f} min"
        free = (datetime.datetime.now() + datetime.timedelta(minutes=hi)).strftime("%H:%M")
        return f"{st.filename}: {done}, {span} left (free around {free})"

    def webcam_snapshot_url(self) -> str | None:
        """Discover the snapshot URL configured in Moonraker. Relative paths resolve against the web UI."""
        try:
            cams = self._req("GET", "/server/webcams/list").get("webcams") or []
        except MoonrakerError:
            return None
        for cam in cams:
            if cam.get("enabled") is False:
                continue
            url = cam.get("snapshot_url")
            if url:
                return url if urlparse(url).scheme else urljoin(self.web_url + "/", url.lstrip("/"))
        return None

    # --- jobs -------------------------------------------------------------
    def upload(self, gcode: Path, remote_name: str | None = None) -> str:
        with gcode.open("rb") as f:
            res = self._req(
                "POST", "/server/files/upload", files={"file": (remote_name or gcode.name, f)},
                data={"root": "gcodes"}, timeout=300
            )
        try:
            return res["item"]["path"]
        except (KeyError, TypeError):
            raise MoonrakerError(f"Upload response missing item.path: {res!r:.300}") from None

    def start_print(self, filename: str) -> None:
        self._req("POST", "/printer/print/start", params={"filename": filename})

    def pause(self) -> None:
        self._req("POST", "/printer/print/pause")

    def resume(self) -> None:
        self._req("POST", "/printer/print/resume")

    def cancel(self) -> None:
        self._req("POST", "/printer/print/cancel")

    def gcode(self, script: str) -> None:
        self._req("POST", "/printer/gcode/script", json={"script": script}, timeout=60)

    def heaters_off(self) -> None:
        self.gcode("M104 S0\nM140 S0\nM107")

    def cancel_and_cool(self) -> None:
        """Cancel the job and make sure the heaters end up off.

        Each step is attempted independently. If Klipper is already shut down or in error,
        its heaters are disabled by Klipper itself, so failing commands are expected there.
        Otherwise, if heaters can't be turned off, fall back to an emergency stop.
        Raises only when none of that could be confirmed.
        """
        errors: list[str] = []
        for step in (self.cancel, self.heaters_off):
            try:
                step()
            except MoonrakerError as e:
                errors.append(f"{step.__name__}: {e}")
                log.warning("cancel_and_cool %s failed: %s", step.__name__, e)
        if not any(e.startswith("heaters_off") for e in errors):
            return
        try:
            state = self.printer_info().get("state")
        except MoonrakerError as e:
            state = None
            errors.append(f"printer_info: {e}")
        if state in ("shutdown", "error"):
            return  # Klipper has already disabled all heaters.
        try:
            self.emergency_stop()
            log.warning("cancel_and_cool fell back to emergency stop: %s", errors)
            return
        except MoonrakerError as e:
            errors.append(f"emergency_stop: {e}")
        raise MoonrakerError("Could not confirm heaters are off: " + "; ".join(errors))

    def emergency_stop(self) -> None:
        self._req("POST", "/printer/emergency_stop")

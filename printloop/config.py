"""All settings come from environment variables (or a .env file). Secrets never live in code."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value if value not in ("", None) else default


def _float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _bool(name: str, default: bool = False) -> bool:
    return (_env(name, "1" if default else "0") or "").lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    # Printer (Moonraker on the K1C, e.g. http://192.168.1.50:7125)
    moonraker_url: str
    moonraker_api_key: str | None
    moonraker_web_url: str | None  # web UI (nginx) base for relative webcam paths; default host:4408
    camera_snapshot_url: str | None  # auto-discovered from Moonraker when unset

    # Claude
    claude_model: str
    claude_effort: str

    # Design / slicing
    openscad_bin: str
    slicer: str  # "prusaslicer" | "orca"
    slicer_bin: str
    slicer_profile: Path | None  # PrusaSlicer .ini exported from your K1C profile
    orca_machine: Path | None
    orca_process: Path | None
    orca_filament: Path | None

    # Slack
    slack_backend: str  # auto | mcp | bot | webhook | none
    slack_mcp_channel_id: str | None  # channel/DM id used through your claude.ai Slack connector
    slack_webhook_url: str | None
    slack_bot_token: str | None
    slack_channel_id: str | None

    # Shared request channel for `printloop serve` (coworkers post `print: ...`)
    request_channel_id: str | None
    approver_user_ids: frozenset[str]  # who may approve prints / answer printer-side questions; empty = anyone
    serve_poll_seconds: int

    # Approvals: "cli" (terminal) or "slack" (reply in thread; needs bot token)
    approval_backend: str
    approval_poll_seconds: int

    # Monitoring
    check_interval_seconds: int
    pause_timeout_seconds: int  # paused + no human answer -> cancel and cool down
    obico_ml_url: str | None  # e.g. http://localhost:3333
    obico_ml_token: str | None
    obico_threshold: float
    obico_high_threshold: float
    obico_consecutive_hits: int
    k1c_detector: str  # "unavailable" until we can read its events (see detectors.py)

    # Free-text facts about this printer/bed that Claude should know when diagnosing
    printer_notes: str

    # Safety
    thermal_protection_verified: bool

    runs_dir: Path

    @classmethod
    def load(cls) -> "Config":
        def path(name: str) -> Path | None:
            v = _env(name)
            return Path(v).expanduser() if v else None

        return cls(
            moonraker_url=(_env("MOONRAKER_URL", "http://k1c.local:7125") or "").rstrip("/"),
            moonraker_api_key=_env("MOONRAKER_API_KEY"),
            moonraker_web_url=_env("MOONRAKER_WEB_URL"),
            camera_snapshot_url=_env("CAMERA_SNAPSHOT_URL"),
            claude_model=_env("CLAUDE_MODEL", "opus"),
            claude_effort=_env("CLAUDE_EFFORT", "high"),
            openscad_bin=_env("OPENSCAD_BIN", "openscad"),
            slicer=_env("SLICER", "prusaslicer"),
            slicer_bin=_env("SLICER_BIN", "prusa-slicer"),
            slicer_profile=path("SLICER_PROFILE"),
            orca_machine=path("ORCA_MACHINE_JSON"),
            orca_process=path("ORCA_PROCESS_JSON"),
            orca_filament=path("ORCA_FILAMENT_JSON"),
            slack_backend=_env("SLACK_BACKEND", "auto"),
            slack_mcp_channel_id=_env("SLACK_MCP_CHANNEL_ID"),
            slack_webhook_url=_env("SLACK_WEBHOOK_URL"),
            slack_bot_token=_env("SLACK_BOT_TOKEN"),
            slack_channel_id=_env("SLACK_CHANNEL_ID"),
            request_channel_id=_env("REQUEST_CHANNEL_ID"),
            approver_user_ids=frozenset(u.strip() for u in (_env("APPROVER_USER_IDS", "") or "").split(",") if u.strip()),
            serve_poll_seconds=_int("SERVE_POLL_SECONDS", 120),
            approval_backend=_env("APPROVAL_BACKEND", "cli"),
            approval_poll_seconds=_int("APPROVAL_POLL_SECONDS", 10),
            check_interval_seconds=_int("CHECK_INTERVAL_SECONDS", 120),
            pause_timeout_seconds=_int("PAUSE_TIMEOUT_SECONDS", 900),
            obico_ml_url=(_env("OBICO_ML_URL") or "").rstrip("/") or None,
            obico_ml_token=_env("OBICO_ML_TOKEN"),
            obico_threshold=_float("OBICO_THRESHOLD", 0.45),
            obico_high_threshold=_float("OBICO_HIGH_THRESHOLD", 0.8),
            obico_consecutive_hits=_int("OBICO_CONSECUTIVE_HITS", 2),
            k1c_detector=_env("K1C_DETECTOR", "unavailable"),
            printer_notes=_env("PRINTER_NOTES", "") or "",
            thermal_protection_verified=_bool("THERMAL_PROTECTION_VERIFIED"),
            runs_dir=Path(_env("RUNS_DIR", "runs")).expanduser(),
        )

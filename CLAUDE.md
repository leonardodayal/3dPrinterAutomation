# printloop: K1C design → print → monitor → recover

Automates a Creality K1C: Claude designs a part (OpenSCAD) → human approves in Slack → OrcaSlicer slices → Moonraker
prints → camera + Obico check every 2 min → on failure: pause, Slack alert with photo, Claude proposes a fix (settings
and/or model diff), human approves → reprint. Coworkers request prints in Slack `#3d-printer` (`print: …`).
README.md has the full design; this file is the short version for Claude sessions.

## Layout
- `printloop/workflow.py`: the state machine (design, approval, print, monitor, recovery, lost-contact alerts)
- `printloop/serve.py`: Slack request service (queue, one thread per job, `status` replies, state in `runs/serve_state.json`)
- `printloop/claude.py`: runs `claude -p` on the user's claude.ai subscription (never an API key; strips ANTHROPIC_API_KEY)
- `printloop/design.py` (OpenSCAD), `slicer.py` (OrcaSlicer CLI + `profiles/*.json`), `fixer.py`, `detectors.py`
  (Obico + dual rule), `moonraker.py`, `camera.py`, `slack.py` (bot / mcp / webhook backends), `approval.py`
- `printer/`: snapshot server that runs ON the printer (`/usr/data/printloop`, port 8090)
- `scripts/`: `setup-mac.sh`, `install-launchd.sh`, `obico-ml.sh`, `build_orca_profiles.py`
- `.env`: all settings and secrets (Slack bot token, Obico token). Never print, commit or paste its secrets.

## Commands
```bash
.venv/bin/python -m pytest -q                       # all tests must pass before restarting the service
.venv/bin/python -m printloop status | preflight | test-alert | cancel | estop
.venv/bin/python -m printloop bed-view [--z 60] [--park 219,0] [--lower]   # moves the bed: only with someone watching
.venv/bin/python -m printloop run "description" [--image photo.jpg] [--scad file.scad]
launchctl kickstart -k gui/$(id -u)/com.printloop.serve   # restart the background service after code changes
```

## Hardware / network facts
- K1C at 192.168.1.228, firmware 1.3.5.22, rooted. Moonraker :7125, Fluidd :4408 (Helper Script), our snapshot
  server :8090 (`/etc/init.d/S99printloop_snapshot`). Stock camera has no MJPEG URL; the snapshot server reads
  cam_app's frame buffer (`/var/run/mjpg_main_sock`).
- Root SSH: user `root`. **Never store or write down the password**; ask the user when SSH is needed.
- Glue stick is on the bed on purpose (white haze in photos is glue, not debris).
- The user also starts prints from the touchscreen/Creality Print; those heat up before Klipper says "printing".
  `Moonraker.busy_reason()` covers that. The workflow never starts on a busy printer.
- Obico ML API: Docker (Colima) container `obico-ml` on 127.0.0.1:3333, image `printloop/obico-ml-api:local`.
- Slack: bot "printloop" (U0CL5U06N8L), channel `#3d-printer` (C0CLWEDA6BS, private). Approvers: Leo (U0B6AMU6ZQ9),
  Goetz Neumann (U05L82Z8T5Y). Only approvers may answer approve/real/false/removed/same/retry.

## Rules
- Nothing prints without an explicit human `approve`. Don't add shortcuts around the approval gate.
- Check `busy_reason()` before touching printer services. Never restart Klipper, Moonraker or webrtc during a print.
  Restarting the snapshot server is OK.
- Only one machine may run `printloop serve` at a time (two would print every request twice).
- Run the tests before restarting the service. A restart during a print leaves that print unmonitored (the
  job's thread is told). Prefer restarting between jobs: check `runs/serve_state.json` for a `current` job.
- Don't post in Slack, especially to people, without the user's go-ahead, except the workflow's own messages.
- The project folder must not live in iCloud-synced Desktop/Documents on the host that runs the service.

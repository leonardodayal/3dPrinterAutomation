# printloop: K1C design → print → monitor → recover

Automates the loop in the implementation handoff for the Creality K1C:

```
 request ──► Claude writes OpenSCAD ──► render STL + preview ──► slice (K1C profile)
                     ▲                                                │
                     │ revise <feedback>                              ▼
                     └────────────── APPROVAL GATE (preview + bed photo) ──► upload + start via Moonraker
                                                                                   │
              every CHECK_INTERVAL_SECONDS: snapshot → Obico ML + K1C AI → dual rule
                                                                                   │
        continue ◄── neither flags ───────────────────────────────────────────────┤
                                                                                   ▼ any flag / Klipper error / external pause
                                        PAUSE ► Slack alert + snapshot + detector results
                                        Claude starts diagnosing in the background
                                                   │
                       false ─► resume     real ─► cancel + heaters off ─► "remove the print"
                                                   │ (no answer in PAUSE_TIMEOUT ─► cancel + heaters off anyway)
                                                   ▼ removed
                         APPROVAL GATE: diagnosis + settings diff + model diff + new slice estimate
                            approve / revise <feedback> / same / abort ─► reprint (max 5 attempts)
```

## Print requests from coworkers (Slack)

Run `python -m printloop serve` on the Mac next to the printer. Coworkers post a new message in the shared
channel (`REQUEST_CHANNEL_ID`) starting with `print`:

> print: a cable clip for a 6 mm cable, 3 of them

Attach photos or sketches to the request (or post just `print` with a photo) and Claude designs from them,
using any measurements written in them. Images posted later in the thread are used by the next `revise …`.

Each request gets a thread. The design preview, the approval, progress, failure alerts and the finish photo all go
there. Requests queue up because there's one printer. A new design prints only after **both** the requester and one
of `APPROVER_USER_IDS` reply `approve` (or react ✅ / 👍 on the approval message); an approver who requested a part
signs off alone. Only approvers can say `real`/`false`, `removed` and so on, because those confirm someone is at the
printer and the bed is clear. Anyone in the thread can `revise …`; the requester or an approver can `cancel` (same as
`abort`). Answers are acknowledged with a ✅ reaction rather than a reply.

Post `status` or `queue` as a new message anywhere in the channel to see the printer and the whole queue. Replying
`status` in a request's thread (or asking "how long…") gets that job's position, phase and time left. The queue and each job's phase are saved in `runs/serve_state.json`. After a restart, waiting jobs are picked up again. A job that was already printing is reported as unmonitored and isn't redone. The channel is checked every `SERVE_POLL_SECONDS` (120 by default).
With the `mcp` Slack backend, each check is one Claude Code call on the host's subscription.

## Dual detection rule

| External (Obico) | K1C built-in | Result |
|---|---|---|
| failure | failure | **confirmed**: pause, alert, propose fix |
| one flags, other doesn't | | **detectors disagree**: pause, alert, human decides |
| ok | ok | keep printing |
| failure | *unavailable* | **single detector**: pause, alert, human decides |

The K1C built-in detector is a stub (`printloop/detectors.py: K1CBuiltinDetector`) until we know whether its events can be read outside Creality Cloud. Until then the rule falls back to the "single detector" row. Keep the printer's own AI on **Notify only**. If it pauses the print anyway, the workflow treats the external pause as a possible failure.

## Setup (handoff build order)

1. **Moonraker on the K1C.** Root the printer and install Moonraker with the Creality Helper Script, then:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   cp .env.example .env    # set MOONRAKER_URL
   .venv/bin/python -m printloop status
   ```
2. **Detector.** Obico's open-source ML API runs locally in Docker (ARM-native on Apple Silicon):
   ```bash
   git clone --depth 1 https://github.com/TheSpaghettiDetective/obico-server.git tools/obico-server
   docker build -t printloop/obico-ml-api:local tools/obico-server/ml_api   # ~6 GB image
   scripts/obico-ml.sh            # starts it on 127.0.0.1:3333 with OBICO_ML_TOKEN from .env
   ```
   The container fetches `CAMERA_SNAPSHOT_URL` itself and gives up after 5 s, so the snapshot server must answer quickly.
   It restarts automatically while Docker (Colima: `colima start`) is running. Tune `OBICO_THRESHOLD` on a test
   print: `runs/<id>/events.jsonl` logs the score of every check.
3. **Slack.** A webhook only sends text. For alerts **with camera images**, create a Slack app with a bot token (`chat:write`, `files:write`, `channels:history`, `groups:history` for a private channel; for request images and ✅-reaction approvals also `files:read`, `reactions:read`, `reactions:write`), invite it to the channel, and set `SLACK_BOT_TOKEN` + `SLACK_CHANNEL_ID`. Run `python -m printloop test-alert` to check.
4. **Claude (your subscription, no API key).** Design and fix proposals run through the local Claude Code CLI (`claude -p`) on your claude.ai plan. Install Claude Code on the machine that runs the workflow and log in once with `claude auth login`. `ANTHROPIC_API_KEY` is stripped from its environment so it can't switch to API billing, and preflight checks that `claude auth status` reports `claude.ai`. Calls count against your plan's usage limits. If a limit is hit, the fix step reports the error and you can still `revise`, reprint `same`, or `abort`.
5. **Design + slicing.** `brew install --cask openscad@snapshot orcaslicer`. Use the OpenSCAD snapshot build: the
   2021 stable build is Intel-only and slow. OrcaSlicer is what Creality Print is built on and has official K1C presets.
   Its CLI can't read the packed system presets, so `scripts/build_orca_profiles.py` flattens the K1C machine/process/
   filament presets from the OrcaSlicer repo into `profiles/*.json` (already committed; see the script header to rebuild).
   Claude's fixes are applied as overrides on those files. On a headless Pi, OpenSCAD PNG previews need `xvfb-run`.
6. **Approvals.** `APPROVAL_BACKEND=cli` uses the terminal. `slack` posts each question and waits for a thread reply whose first word is the option, e.g. `approve` or `revise make the hook 2mm thicker`. Slack approval lets you answer from your phone.
7. **Safety.** Verify thermal runaway protection on the printer yourself, then set `THERMAL_PROTECTION_VERIFIED=1`. Preflight refuses to run without it.

```bash
.venv/bin/python -m printloop preflight
.venv/bin/python -m printloop run "wall hook for a 25 mm broom handle, 3 kg load"
.venv/bin/python -m printloop run --scad parts/bracket.scad "corner bracket"   # bring your own model
.venv/bin/python -m printloop run --image sketch.jpg --image old_part.heic "replacement knob like the photo, 6 mm D-shaft"
.venv/bin/python -m printloop cancel   # cancel + heaters off
.venv/bin/python -m printloop estop    # Klipper emergency stop
```

## Camera (stock K1C)

The stock firmware has no MJPEG snapshot URL; it only streams the camera over WebRTC. But `cam_app` keeps
its latest JPEG frame in a shared buffer that it hands out on `/var/run/mjpg_main_sock`.
`printer/snapshot_server.py` reads that buffer and serves it at `http://<printer>:8090/snapshot.jpg`.

Install or update it on the printer:
```bash
ssh root@<printer> mkdir -p /usr/data/printloop
scp -O printer/snapshot_server.py root@<printer>:/usr/data/printloop/
scp -O printer/S99printloop_snapshot root@<printer>:/etc/init.d/
ssh root@<printer> 'chmod +x /etc/init.d/S99printloop_snapshot && /etc/init.d/S99printloop_snapshot restart'
```
To remove it, stop it and delete those two files.

### Bed position for photos

The camera sits high at the front, so with the bed at the bottom (where the stock `END_PRINT` leaves it) photos show
almost nothing. See `printloop/bedview.py`.

- **Finish photo** (`FINISH_PHOTO_LIFT=1`, default): printloop's own prints end with a custom end G-code. It does the
  stock cleanup, but the bed stops 10 mm below the nozzle over the finished part, the head parks at `PHOTO_PARK_XY`
  (default `219,0`, front right) and the motors stay on. After the finish photo the bed is lowered. Prints from the
  touchscreen are unchanged.
- **Approval photo** (`BED_PHOTO_Z`, default `0` = off): raises the bed to that Z before the bed photo. This only
  happens when Z is still homed (it never homes to take a photo) and the printer isn't busy, and the gap always stays
  at least the tallest known part + 15 mm. An unknown taller object on the bed would still be hit.
- Tune both with someone watching the printer: `python -m printloop bed-view --z 60 --park 219,0` moves the bed and
  saves a snapshot path; `bed-view --lower` lowers it again.

## Prints started outside the workflow

The workflow never starts a job while the printer is busy, including prints started from the touchscreen
or Creality's app. Those heat up *before* Klipper reports "printing", so heater targets count as busy too.
Preflight refuses to run, and right before each print it checks again and asks you to `retry` once the
printer is free and the bed is clear.

## Safety behaviour

- No print starts without an explicit `approve`, for both new designs and every fix. The approval message includes a live photo of the bed.
- If a paused print gets no answer within `PAUSE_TIMEOUT_SECONDS` (15 min by default), the workflow cancels it and turns the heaters off.
- Cancelling always sends `M104 S0 / M140 S0` explicitly instead of relying on the `CANCEL_PRINT` macro.
- If the workflow crashes mid-print, it pauses the print and alerts Slack. Ctrl-C alerts that the printer is no longer monitored.
- Every approval, print start, failure, fix and cancellation is logged to `runs/<run>/events.jsonl` and `runs/all-events.jsonl`.
- Fixes are limited to settings that exist in your base profile. Unknown keys are dropped and listed in the alert.

## Layout

| File | Role |
|---|---|
| `printloop/workflow.py` | the state machine |
| `printloop/claude.py` | runs `claude -p` with a JSON schema on your subscription |
| `printloop/design.py` | Claude → OpenSCAD → STL/preview, with render-error retry |
| `printloop/fixer.py` | Claude failure diagnosis → settings/model diff |
| `printloop/detectors.py` | Obico ML client, K1C stub, dual rule |
| `printloop/moonraker.py`, `camera.py`, `slack.py`, `slicer.py`, `approval.py` | integrations |
| `tests/` | unit tests + full-loop simulation with a fake printer (`pytest`) |

## Open items (from the handoff)

- Wire up `K1CBuiltinDetector.check()` once we know where its events can be read.
- Pick the camera position (first layers vs. spool).
- CadQuery/Blender design backends are not implemented. OpenSCAD only for now.

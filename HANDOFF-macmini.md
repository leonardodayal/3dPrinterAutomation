# Handoff: make this Mac mini the always-on printloop host

**For Claude Code on the Mac mini.** Read `CLAUDE.md` first for the project overview and rules. Leo set this up on
his laptop on 2026-11-22. Everything works there: test prints, the Slack bot, coworker requests in `#3d-printer`.
Your job is to move the always-on part (`printloop serve` and the Obico detector) to this Mac mini, so the laptop
can leave the house. Work through the steps in order and tell Leo what you're doing. Ask him whenever a step needs
his hands: passwords, System Settings, or confirming the laptop has stopped.

## What already exists (don't redo it)
- **Printer side** (rooted K1C, Moonraker, Fluidd, snapshot server on :8090): done. Nothing to install on the printer.
- **Slack:** bot app + token (in `.env`), channel `#3d-printer`, pinned how-to post. Done.
- **Slicer profiles** in `profiles/`, built from OrcaSlicer v2.5.2. Done, copied with this folder.
- **The laptop** may still be running `printloop serve`. It must be stopped before you start the service here (step 6).

## Steps

### 1. Location
Run `pwd`. The folder must be **outside** `~/Documents` and `~/Desktop`. Both are iCloud-synced on Leo's Macs, and a
copy of the laptop's project folder exists under `~/Documents/Github/Other/3dPrinterAutomation`. **Don't run or modify
that synced copy.** Expected location: `~/3dPrinterAutomation`. If you're somewhere else, tell Leo and suggest moving.

### 2. Prerequisites (Leo)
- Homebrew installed (`brew --version`).
- `claude auth status` shows `"authMethod": "claude.ai"`. If not, Leo runs `claude auth login`.
- The mini is on the printer's network: `curl -s -m 5 http://192.168.1.228:7125/printer/info` returns JSON.

### 3. Install
```bash
bash scripts/setup-mac.sh
```
It installs python@3.12, Colima/Docker, OpenSCAD (snapshot build), OrcaSlicer, creates `.venv`, builds the Obico image
(~6 GB, 5-10 min, run it in the background) and starts the detector. Then it checks the printer, camera, Claude login
and sleep, runs the tests and runs preflight. Fix anything it reports before continuing. Expected results:
- tests: all pass, 1 skipped (the skip is a HEIC-conversion test)
- preflight: OK, or "Printer is busy" if something is printing (fine for now)

The OpenSCAD snapshot build changed some CLI flags (`--render=1`). If previews fail, check `printloop/design.py`.

### 4. Sleep
The mini must not sleep. The display may. Ask Leo to set **System Settings → Energy → "Prevent automatic sleeping
when the display is off"** on, or to run `sudo pmset -a sleep 0 displaysleep 10` in his own Terminal (sudo needs
his password). The service also runs under `caffeinate -i` (step 6), as a backstop.

### 5. Verify (no printing)
```bash
.venv/bin/python -m printloop status            # printer state + snapshot URL
.venv/bin/python -c "from printloop.config import Config; from printloop.detectors import ObicoDetector as O; c=Config.load(); print(O(c.obico_ml_url,c.obico_ml_token,c.obico_threshold,c.obico_high_threshold,c.obico_consecutive_hits).check(c.camera_snapshot_url))"
```
The Obico check should return `ok` with score 0 on a normal frame, in about 1-3 s. A 400 error that mentions a
"read timeout" means the container can't reach `192.168.1.228:8090` quickly. Check Colima networking.
`printloop test-alert` posts a photo to `#3d-printer`. Ask Leo before running it.

### 6. Switch over (Leo confirms)
1. Ask Leo to confirm the **laptop's** `printloop serve` is stopped, and no job is mid-print.
   On the laptop it ran inside a Claude Code session: stopping that background task, or closing the session, stops it.
2. Optional: Leo copies the laptop's `runs/serve_state.json` to `runs/` here, so already-handled Slack messages
   aren't seen again. Without it, the mini simply ignores everything posted before it starts. That's also fine.
3. Install the background service (starts now and at every login, restarts if it crashes, keeps the Mac awake):
   ```bash
   bash scripts/install-launchd.sh
   ```
4. Check: `tail -f runs/serve.log` shows `printloop serve: watching C0CLWEDA6BS every 30s (approvers: …)`.
   Ask Leo to post `print: status test` in `#3d-printer`. The bot should reply in its thread within ~30 s.
   Leo can reply `abort` to cancel that test job. Replying `status` in the thread should get an answer.

### 7. Reboot test (optional, with Leo)
After a reboot and login: `colima status` is running, `docker ps` shows `obico-ml`, `launchctl print
gui/$(id -u)/com.printloop.serve` shows `state = running`, and `runs/serve.log` has a fresh "watching" line.
Auto-login (System Settings → Users & Groups) makes this survive power cuts. That's Leo's call.

## Afterwards
- Code changes: edit, run `.venv/bin/python -m pytest -q`, then restart between jobs with
  `launchctl kickstart -k gui/$(id -u)/com.printloop.serve`.
- To stop the service: `bash scripts/install-launchd.sh uninstall`.
- Open items Leo knows about: Slack doesn't accept photos for requests yet; the K1C's built-in AI detector isn't
  wired up (`K1CBuiltinDetector`); the printer's root password is still the factory default (recommend `passwd`).

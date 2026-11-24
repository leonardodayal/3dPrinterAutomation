"""The loop: design -> approve -> print -> monitor -> (fail -> pause -> alert -> fix -> clear bed -> approve) -> repeat."""
from __future__ import annotations

import shutil
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import camera, claude, design, fixer, slicer
from .bedview import BedView, gcode_max_z
from .approval import Approver
from .config import Config
from .detectors import Decision, K1CBuiltinDetector, ObicoDetector, Verdict, dual_rule
from .eventlog import EventLog
from .moonraker import Moonraker, PrintStatus
from . import slack as slack_mod

MAX_ATTEMPTS = 5
LOST_CONTACT_SECONDS = 300      # alert after ~5 min without printer status
LOST_CONTACT_REMIND_SECONDS = 1800  # and remind every 30 min while it lasts


class Abort(Exception):
    pass


@dataclass
class Plan:
    """Everything needed to (re)print: the model plus slicer overrides."""
    scad: Path
    stl: Path
    preview: Path | None
    overrides: dict[str, str] = field(default_factory=dict)
    gcode: Path | None = None
    stats: dict[str, str] = field(default_factory=dict)


@dataclass
class Prepared:
    """A fix that has already been rendered and sliced in the background."""
    fix: fixer.ReviewedFix
    plan: Plan
    note: str = ""


def _fmt_status(st: PrintStatus) -> str:
    layer = f", layer {st.current_layer}/{st.total_layer}" if st.current_layer else ""
    return (f"state={st.state}, progress={st.progress:.0%}{layer}, elapsed={st.print_duration / 60:.0f} min, "
            f"nozzle={st.extruder_temp}°C, bed={st.bed_temp}°C, file={st.filename}")


class Workflow:
    def __init__(self, cfg: Config, request: str, run_dir: Path | None = None, images: list[Path] | None = None,
                 slack: slack_mod.Slack | None = None, requester: str | None = None, on_phase=None,
                 should_stop=None):
        """should_stop(): True once the job was cancelled in Slack (checked before designing/asking)."""
        self.cfg = cfg
        self.request = request
        self.run_dir = run_dir or cfg.runs_dir / time.strftime("%Y%m%d-%H%M%S")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log = EventLog(self.run_dir, cfg.runs_dir / "all-events.jsonl")
        self.printer = Moonraker(cfg.moonraker_url, cfg.moonraker_api_key, web_url=cfg.moonraker_web_url)
        self.slack = slack or slack_mod.from_config(cfg)
        self.requester = requester
        self.phase = "starting"
        self._on_phase = on_phase
        self._should_stop = should_stop
        self.approver = Approver(cfg.approval_backend, self.slack, self.log, cfg.approval_poll_seconds,
                                 approvers=set(cfg.approver_user_ids), requester=requester)
        self.detectors = [
            ObicoDetector(cfg.obico_ml_url, cfg.obico_ml_token, cfg.obico_threshold,
                          cfg.obico_high_threshold, cfg.obico_consecutive_hits),
            K1CBuiltinDetector(cfg.k1c_detector),
        ]
        self.snapshot_url: str | None = cfg.camera_snapshot_url
        self.base_settings: dict[str, str] = {}
        self.history: list[str] = []
        self.pool = ThreadPoolExecutor(max_workers=1)

        self.reference_images = design.prepare_images(images or [], self.run_dir / "reference")

    # ------------------------------------------------------------------ setup
    def preflight(self) -> None:
        problems = []
        if not self.cfg.thermal_protection_verified:
            problems.append("THERMAL_PROTECTION_VERIFIED is not set. Verify Klipper's verify_heater/thermal runaway "
                            "protection on the K1C by hand, then set THERMAL_PROTECTION_VERIFIED=1.")
        try:
            info = self.printer.printer_info()
            if info.get("state") != "ready":
                problems.append(f"Klipper state is {info.get('state')!r}: {info.get('state_message', '')}")
            if busy := self.printer.busy_reason():
                problems.append(f"Printer is busy: {busy}. Wait for it to finish (and clear the bed).")
        except Exception as e:
            problems.append(f"Moonraker not reachable at {self.cfg.moonraker_url}: {e}")
        if not self.snapshot_url:
            self.snapshot_url = self.printer.webcam_snapshot_url()
        if not self.snapshot_url:
            problems.append("No camera: set CAMERA_SNAPSHOT_URL")
        else:
            try:
                camera.snapshot(self.snapshot_url, self.run_dir / "snapshots", "preflight")
            except Exception as e:
                problems.append(f"Camera snapshot failed ({self.snapshot_url}): {e}")
        for binary in (self.cfg.openscad_bin, self.cfg.slicer_bin):
            if not shutil.which(binary) and not Path(binary).exists():
                problems.append(f"Not found: {binary}")
        try:
            self.base_settings = slicer.available_settings(self.cfg)
        except Exception as e:
            problems.append(f"Slicer profile: {e}")
        if problem := claude.available():
            problems.append(problem)
        if not self.slack.enabled:
            problems.append("Slack not configured (SLACK_MCP_CHANNEL_ID, SLACK_BOT_TOKEN + SLACK_CHANNEL_ID, or SLACK_WEBHOOK_URL)")
        if not self.cfg.obico_ml_url:
            print("WARNING: OBICO_ML_URL not set: no external failure detector; only Klipper errors and "
                  "external pauses will be caught.")
        if problems:
            raise SystemExit("Preflight failed:\n  - " + "\n  - ".join(problems))
        self.log.log("preflight_ok", snapshot_url=self.snapshot_url, slicer=self.cfg.slicer)

    # ----------------------------------------------------------------- phases
    def run(self, scad: Path | None = None) -> None:
        self.preflight()
        try:
            plan = self.design_phase(scad)
            for attempt in range(1, MAX_ATTEMPTS + 1):
                self.start_print(plan, attempt)
                outcome, plan = self.monitor_attempt(plan, attempt)
                if outcome == "complete":
                    return
            self.alert(f":octagonal_sign: Gave up after {MAX_ATTEMPTS} attempts. Human attention needed.")
            self.log.log("max_attempts_reached")
        except Abort as e:
            self.log.log("workflow_aborted", reason=str(e))
            self.alert(f":no_entry: Print workflow aborted: {e}")
        except KeyboardInterrupt:
            self.log.log("workflow_stopped", reason="keyboard interrupt")
            self.alert(":double_vertical_bar: Print workflow stopped by operator. The printer is NO LONGER MONITORED.")
            raise
        except Exception as e:
            # Never leave a print running unmonitored: try to pause it and shout.
            self.log.log("workflow_crashed", error=repr(e))
            try:
                if self.printer.status().state == "printing":
                    self.printer.pause()
                    self.log.log("print_paused", reason="workflow crashed")
            except Exception:
                pass
            self.alert(f":boom: Print workflow crashed: `{e!r}`. Print paused if it was running; check the printer.")
            raise
        finally:
            self.set_phase("done")
            self.pool.shutdown(wait=False, cancel_futures=True)

    def set_phase(self, phase: str) -> None:
        """designing | awaiting_approval | printing | recovering | done (reported to `printloop serve`)."""
        if phase in ("designing", "awaiting_approval") and self._should_stop and self._should_stop():
            raise Abort("cancelled in Slack")
        self.phase = phase
        if self._on_phase:
            try:
                self._on_phase(phase)
            except Exception as e:
                self.log.log("phase_callback_error", error=repr(e))

    def design_phase(self, scad: Path | None) -> Plan:
        feedback, version, previous, previous_preview = None, 1, None, None
        while True:
            self.set_phase("designing")
            if scad and version == 1:
                dest = self.run_dir / "model_v1.scad"
                shutil.copy(scad, dest)
                stl, preview = design.render(self.cfg.openscad_bin, dest)
                d = design.Design(dest, stl, preview, f"User-supplied model {scad.name}", [])
            else:
                print(f"Asking Claude for design v{version}...")
                d = design.generate(self.request, self.run_dir, self.cfg.claude_model, self.cfg.claude_effort,
                                    self.cfg.openscad_bin, previous, feedback, version,
                                    images=self.reference_images, previous_preview=previous_preview)
            self.log.log("design_generated", version=version, scad=d.scad, summary=d.summary,
                         reference_images=[p.name for p in self.reference_images])
            plan = Plan(d.scad, d.stl, d.preview)
            self.slice(plan, f"v{version}")

            bed = self.bed_snap("bed-before-print", plan)
            self.set_phase("awaiting_approval")
            question = (
                (f"<@{self.requester}> " if self.requester else "")
                + f"*Design v{version}* for: _{self.request}_\n{d.summary}\n"
                + ("Assumptions:\n" + "\n".join(f"• {a}" for a in d.assumptions) + "\n" if d.assumptions else "")
                + f"Slicer estimate: {plan.stats or 'n/a'}\n"
                + f"Files: `{d.scad}`, `{plan.gcode}`\n"
                + ("The last image is the bed right now: an approver's `approve` confirms it is clear and starts the print."
                   if bed else "")
            )
            ans = self.approver.ask(
                question,
                {"approve": "print this on the K1C now",
                 "revise": "revise it, e.g. `revise make the base 5mm wider` (attach photos/sketches if useful)",
                 "abort": "stop the workflow"},
                images=[p for p in (d.preview, bed) if p],
                requester_signoff=True,
            )
            if ans.choice == "approve":
                return plan
            if ans.choice == "abort":
                raise Abort("rejected at design approval")
            self.add_reference_images(ans.files)
            previous, feedback, version = d.scad.read_text(), ans.comment or "Please improve it.", version + 1
            previous_preview = d.preview

    def slice(self, plan: Plan, tag: str) -> None:
        plan.gcode = slicer.slice_model(self.cfg, plan.stl, plan.overrides, self.run_dir / "gcode", tag)
        plan.stats = slicer.gcode_stats(plan.gcode)
        self.log.log("sliced", gcode=plan.gcode, overrides=plan.overrides, stats=plan.stats)

    def wait_until_free(self) -> None:
        """Never start on top of a print someone started from the screen/app in the meantime."""
        while busy := self.printer.busy_reason():
            self.log.log("printer_busy", reason=busy)
            try:
                left = self.printer.time_left()
            except Exception:
                left = None
            ans = self.approver.ask(
                f"The printer is busy ({busy}{'; ' + left if left else ''}), so I can't start. "
                "Reply when it's done and the bed is clear.",
                {"retry": "check again and start", "abort": "stop the workflow"},
            )
            if ans.choice == "abort":
                raise Abort("printer busy")

    def start_print(self, plan: Plan, attempt: int) -> None:
        assert plan.gcode is not None
        self.wait_until_free()
        # Unique per run, so a new job never overwrites an earlier job's file on the printer.
        remote = self.printer.upload(plan.gcode, f"printloop_{self.run_dir.name}_{plan.gcode.name}")
        self.printer.start_print(remote)
        self.log.log("print_started", attempt=attempt, file=remote, overrides=plan.overrides)
        self.set_phase("printing")
        self.alert(f":printer: Attempt {attempt} started: `{remote}` (est. {plan.stats.get('estimated_time', '?')})")
        for d in self.detectors:
            d.reset()

    # ------------------------------------------------------------- monitoring
    def monitor_attempt(self, plan: Plan, attempt: int) -> tuple[str, Plan]:
        seen_printing = False
        lost_since: float | None = None
        last_lost_alert = 0.0
        while True:
            time.sleep(self.cfg.check_interval_seconds)
            try:
                st = self.printer.status()
            except Exception as e:
                now = time.time()
                lost_since = lost_since or now
                gone = now - lost_since
                self.log.log("status_error", error=str(e), lost_for_s=round(gone))
                due = (not last_lost_alert and gone >= LOST_CONTACT_SECONDS) or (
                    last_lost_alert and now - last_lost_alert >= LOST_CONTACT_REMIND_SECONDS)
                if due:
                    last_lost_alert = now
                    self.log.log("lost_contact", lost_for_s=round(gone))
                    self.alert(
                        f":satellite_antenna: *Lost contact with the printer* for {gone / 60:.0f} min "
                        f"(attempt {attempt}, last error: `{str(e)[:150]}`).\n"
                        "The print may still be running *unmonitored* and I can't pause it. Check the printer, "
                        "its Wi-Fi and the host computer's network. I'll keep trying and tell you when it's back.")
                continue
            if lost_since is not None:
                gone = time.time() - lost_since
                self.log.log("contact_restored", lost_for_s=round(gone))
                if last_lost_alert:
                    self.alert(f":white_check_mark: Back in contact with the printer after {gone / 60:.0f} min: "
                               f"{_fmt_status(st)}. Monitoring resumed.", self.snap("reconnected"))
                lost_since, last_lost_alert = None, 0.0
            if st.state == "printing":
                seen_printing = True
            if st.state == "complete":
                snap = self.finish_snap(plan)
                self.log.log("print_complete", attempt=attempt, status=_fmt_status(st))
                self.alert(f":white_check_mark: Print finished on attempt {attempt}. {_fmt_status(st)}", snap)
                self.lower_bed()
                return "complete", plan
            if st.state in ("cancelled", "standby") and seen_printing:
                self.log.log("print_cancelled_externally", status=_fmt_status(st))
                raise Abort(f"print was {st.state} outside the workflow")
            if st.state == "error":
                decision = Decision(Verdict.PRINTER_ERROR)
            elif st.state == "paused":
                # Paused by someone else (or the K1C AI not set to "Notify only"): a human decides.
                decision = Decision(Verdict.SINGLE, [])
                self.log.log("paused_externally", status=_fmt_status(st))
            elif st.state == "printing":
                snap = self.snap("check")
                results = [d.check(self.snapshot_url) for d in self.detectors]
                decision = dual_rule(results)
                self.log.log("check", progress=round(st.progress, 3), verdict=decision.verdict.value,
                             results=[(r.name, r.signal.value, r.score) for r in results])
                if not decision.should_pause:
                    continue
            else:
                continue  # standby before start / heating

            next_plan = self.handle_failure(plan, st, decision, attempt)
            if next_plan is None:  # false alarm, print resumed
                for d in self.detectors:
                    d.reset()
                continue
            return "failed", next_plan

    # -------------------------------------------------------------- recovery
    def handle_failure(self, plan: Plan, st: PrintStatus, decision: Decision, attempt: int) -> Plan | None:
        self.set_phase("recovering")
        if st.state == "printing":
            self.printer.pause()
        snap = self.snap("failure")
        status_text = _fmt_status(st)
        det_text = "\n".join(f"• {r.name}: {r.signal.value} {r.detail}" for r in decision.results) or "• (no detector results)"
        headline = {
            Verdict.CONFIRMED: ":rotating_light: *Print failure CONFIRMED* (both detectors agree)",
            Verdict.DISAGREE: ":warning: *Possible print failure: detectors disagree*",
            Verdict.SINGLE: ":warning: *Possible print failure* (single detector / external pause)",
            Verdict.PRINTER_ERROR: f":rotating_light: *Printer error*: {st.message}",
        }[decision.verdict]
        self.log.log("failure_detected", attempt=attempt, verdict=decision.verdict.value, status=status_text, snapshot=snap)
        self.alert(f"{headline}\nPrint paused. {status_text}\nDetectors:\n{det_text}", snap)

        # Start working on a fix immediately, while the human looks at the printer.
        future = self.pool.submit(self.prepare_fix, plan, snap, det_text, status_text, None)

        can_resume = decision.verdict is not Verdict.PRINTER_ERROR
        options = {"real": "real failure: cancel the print and cool down"}
        if can_resume:
            options["false"] = "false alarm: resume printing"
        options["abort"] = "cancel and stop the whole workflow"
        # The alert above already carried the snapshot, so don't attach it again.
        ans = self.approver.ask("Check the snapshot above. Is this a real failure?", options,
                                timeout=self.cfg.pause_timeout_seconds)
        if ans.choice is None:
            self.printer.cancel_and_cool()
            self.log.log("print_cancelled", reason="pause timeout")
            self.alert(f":fire_extinguisher: No answer in {self.cfg.pause_timeout_seconds // 60} min: cancelled the print and turned heaters off.")
            options.pop("false", None)
            ans = self.approver.ask("The print was cancelled. Was it a real failure?", options)
        if ans.choice == "false":
            self.printer.resume()
            self.set_phase("printing")
            self.log.log("print_resumed", reason="false alarm", comment=ans.comment)
            self.history.append(f"Attempt {attempt}: detector false alarm at {st.progress:.0%} ({ans.comment})")
            future.cancel()
            return None
        if ans.choice == "abort":
            self.printer.cancel_and_cool()
            raise Abort("operator aborted after failure")

        self.printer.cancel_and_cool()
        self.log.log("print_cancelled", reason="confirmed failure", comment=ans.comment)
        self.history.append(f"Attempt {attempt} failed at {st.progress:.0%} ({decision.verdict.value}). "
                            f"Operator notes: {ans.comment or '-'}")
        if ans.comment:  # human context is valuable: redo the proposal with it
            future.cancel()
            future = self.pool.submit(self.prepare_fix, plan, snap, det_text, status_text, ans.comment)

        ans = self.approver.ask(
            "Remove the failed print and clear the bed. I'm working on a fix in the meantime.",
            {"removed": "the bed is clear", "abort": "stop the workflow"},
        )
        if ans.choice == "abort":
            raise Abort("operator aborted while clearing the bed")
        self.log.log("bed_cleared", comment=ans.comment)
        return self.approve_fix(plan, future, snap, det_text, status_text, attempt)

    def prepare_fix(self, plan: Plan, snap: Path, det_text: str, status_text: str, feedback: str | None) -> Prepared:
        history = self.history + ([f"Operator feedback on the proposed fix: {feedback}"] if feedback else [])
        scad_code = plan.scad.read_text()
        proposal = fixer.propose(self.cfg.claude_model, self.cfg.claude_effort, snap, det_text, status_text,
                                 scad_code, self.base_settings, plan.overrides, history, self.cfg.printer_notes)
        fix = fixer.review(proposal, scad_code, self.base_settings, plan.overrides)
        tag = f"fix{len(self.history)}-{int(time.time())}"
        note = ""
        new = Plan(plan.scad, plan.stl, plan.preview, fix.overrides)
        if proposal.new_scad_code:
            scad = self.run_dir / f"model_{tag}.scad"
            scad.write_text(proposal.new_scad_code)
            try:
                new.stl, new.preview = design.render(self.cfg.openscad_bin, scad)
                new.scad = scad
            except design.RenderError as e:
                note = f":warning: The proposed model change didn't render, so it was dropped: {str(e)[:300]}"
                fix.scad_diff = ""
        self.slice(new, tag)
        return Prepared(fix, new, note)

    def approve_fix(self, plan: Plan, future: Future, snap: Path, det_text: str, status_text: str, attempt: int) -> Plan:
        while True:
            if not future.done():
                print("Waiting for Claude's fix proposal...")
            try:
                prepared: Prepared = future.result()
            except Exception as e:
                self.log.log("fix_failed", error=str(e))
                prepared = None
                text = f":warning: Couldn't prepare a fix automatically ({e})."
            else:
                self.log.log("fix_proposed", diagnosis=prepared.fix.proposal.diagnosis,
                             overrides=prepared.plan.overrides, model_changed=bool(prepared.fix.scad_diff))
                text = (f"*Proposed fix for attempt {attempt + 1}*\n\n{fixer.format_for_human(prepared.fix)}\n\n"
                        f"Slicer estimate: {prepared.plan.stats or 'n/a'}\n{prepared.note}")
            bed = self.bed_snap("bed-before-reprint", prepared.plan if prepared else plan)
            options = {}
            if prepared:
                options["approve"] = "apply this fix and start the reprint (confirms the bed is clear)"
            options.update({
                "revise": "ask Claude to rethink, e.g. `revise the part popped off, add a brim`",
                "same": "reprint with no changes",
                "abort": "stop the workflow",
            })
            images = [p for p in (prepared.plan.preview if prepared and prepared.fix.scad_diff else None, bed) if p]
            ans = self.approver.ask(text, options, images)
            if ans.choice == "approve" and prepared:
                self.log.log("fix_approved", overrides=prepared.plan.overrides, scad=prepared.plan.scad)
                self.history.append(f"Fix applied for attempt {attempt + 1}: {prepared.fix.proposal.diagnosis} "
                                    f"| settings {prepared.fix.settings_diff or 'none'} | model changed: {bool(prepared.fix.scad_diff)}")
                return prepared.plan
            if ans.choice == "same":
                self.log.log("fix_skipped")
                return plan
            if ans.choice == "abort":
                raise Abort("operator rejected the fix")
            future = self.pool.submit(self.prepare_fix, plan, snap, det_text, status_text, ans.comment or "Try a different approach.")

    # ----------------------------------------------------------------- utils
    def add_reference_images(self, files: list[dict]) -> None:
        """Download images posted in Slack (with a `revise`) and use them for the next design."""
        if not files:
            return
        if not self.slack.can_download:
            self.alert("I can't read images with this Slack setup, so I'll go by the text only.")
            return
        raw = self.run_dir / "reference-incoming"
        got = []
        for i, f in enumerate(files, len(self.reference_images) + 1):
            try:
                got.append(self.slack.download(f, raw / slack_mod.local_name(f, i)))
            except Exception as e:
                self.log.log("image_download_failed", name=f.get("name"), error=str(e))
                self.alert(f":warning: Couldn't use image `{f.get('name')}`: {e}")
        try:
            new = design.prepare_images(got, self.run_dir / "reference", start=len(self.reference_images) + 1)
        except (ValueError, FileNotFoundError, OSError) as e:
            self.alert(f":warning: Couldn't use the new images: {e}")
            return
        self.reference_images += new
        self.log.log("reference_images_added", images=[p.name for p in new])

    @property
    def bedview(self) -> BedView:
        # Built on use so it always drives the current self.printer (tests swap in a fake one).
        return BedView(self.printer, self.cfg.runs_dir / "bed_state.json", self.cfg.photo_park_xy, self.log)

    def _height(self, plan: Plan) -> float:
        try:
            return (gcode_max_z(plan.gcode) or 0.0) if plan.gcode else 0.0
        except OSError:
            return 0.0

    def bed_snap(self, label: str, plan: Plan) -> Path | None:
        """Bed photo for an approval question, with the bed raised toward the camera if that's safe."""
        if self.cfg.bed_photo_z > 0:
            try:
                if why := self.bedview.raise_for_photo(self.cfg.bed_photo_z, self._height(plan)):
                    self.log.log("bed_not_raised", reason=why)
            except Exception as e:
                self.log.log("bed_raise_error", error=repr(e))
        return self.snap(label)

    def finish_snap(self, plan: Plan) -> Path | None:
        """The finish photo is taken before the bed goes down (the end G-code leaves it near the camera)."""
        if self.cfg.finish_photo_lift:
            try:
                self.bedview.settle()
            except Exception as e:
                self.log.log("settle_error", error=repr(e))
        snap = self.snap("complete")
        try:
            self.bedview.remember_part(self._height(plan))
        except OSError:
            pass
        return snap

    def lower_bed(self) -> None:
        if not self.cfg.finish_photo_lift:
            return
        try:
            if why := self.bedview.lower():
                self.log.log("bed_not_lowered", reason=why)
        except Exception as e:
            self.log.log("bed_lower_error", error=repr(e))

    def snap(self, label: str) -> Path | None:
        try:
            return camera.snapshot(self.snapshot_url, self.run_dir / "snapshots", label)
        except Exception as e:
            self.log.log("snapshot_error", error=str(e))
            return None

    def alert(self, text: str, image: Path | None = None) -> None:
        try:
            if image:
                self.slack.post_image(image, text)
            else:
                self.slack.post(text)
        except Exception as e:
            self.log.log("slack_error", error=str(e))
        print(text)

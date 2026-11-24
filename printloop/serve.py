"""`printloop serve`: take print requests from a shared Slack channel and run them on this printer.

Coworkers post a top-level message in REQUEST_CHANNEL_ID starting with `print` (e.g.
`print: a cable clip for a 6 mm cable`), optionally with photos/sketches attached (needs the bot's
files:read scope). Requests are queued (one printer), and each job runs the normal workflow with every
message kept in the thread under the request. A design prints only after `approve` (or ✅) from both
the requester and one of APPROVER_USER_IDS; only approvers give the other printer-side answers
(real/false, removed...). Anyone can `revise` (with more images if useful); the requester or an
approver can `cancel`.

`status` / `queue` as a new message anywhere in the channel lists the printer and the queue; `status`
in a job's thread gives that job's position / phase / time left.

The queue and the running job's phase are saved in runs/serve_state.json, so a restart picks up
waiting jobs again. A job that was already printing is NOT restarted (that would design and print it
twice); its thread is told that the print is no longer monitored.

The channel is checked every SERVE_POLL_SECONDS. With the mcp Slack backend each check is one
Claude Code call on your subscription, so keep it at a minute or more; with the bot backend it's free.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field

from . import slack as slack_mod
from .slack import image_files
from .config import Config
from .moonraker import Moonraker
from .workflow import Workflow

REQUEST_RE = re.compile(r"^\s*(?:<@\w+>\s*)?print\b[\s:,-]*(?P<what>.+)", re.IGNORECASE | re.DOTALL)
STATUS_RE = re.compile(r"^\s*(status|eta)\b|how long", re.IGNORECASE)
CHANNEL_STATUS_RE = re.compile(r"^\s*(?:<@\w+>\s*)?(status|queue|eta)\s*[?!.]*\s*$", re.IGNORECASE)
QUEUE_RE = re.compile(r"^\s*queue\b", re.IGNORECASE)
CANCEL_RE = re.compile(r"^\s*(cancel|abort)\b", re.IGNORECASE)
IMAGE_ONLY_REQUEST = "the part shown in the attached image(s)"
HELP = ("Post a new message here starting with `print`, e.g. `print: a cable clip for a 6 mm cable`. "
        "Attach photos or sketches if they help (measurements in them are used). "
        "I'll design it, ask you and an approver to `approve` it in the thread, print it and report back. "
        "Post `status` any time to see the printer and the queue; reply `cancel` in your request's thread to drop it.")
PHASE_TEXT = {
    "queued": "waiting in line",
    "waiting_for_printer": "waiting for the printer to be free",
    "starting": "starting",
    "designing": "Claude is designing it",
    "awaiting_approval": "waiting for an approver to reply `approve` (or `revise …`)",
    "printing": "printing",
    "recovering": "paused after a possible failure, waiting for an answer here",
    "done": "finished",
}
RESUMABLE = {"queued", "waiting_for_printer", "starting", "designing", "awaiting_approval"}


@dataclass
class Job:
    ts: str
    user: str
    request: str
    phase: str = "queued"
    seen_ts: str = ""  # newest thread reply already looked at for `status`
    files: list = field(default_factory=list)  # images attached to the request (Slack file dicts)
    cancelled: bool = False


def parse_request(text: str, has_images: bool = False) -> str | None:
    m = REQUEST_RE.match(text or "")
    if not m:
        if has_images and re.match(r"^\s*(?:<@\w+>\s*)?print\b[\s:,.!-]*$", text or "", re.IGNORECASE):
            return IMAGE_ONLY_REQUEST  # `print` + a photo
        return None
    what = m.group("what").strip()
    return what if len(what) >= 3 else None


class Server:
    def __init__(self, cfg: Config, slack: slack_mod.Slack | None = None, printer: Moonraker | None = None,
                 workflow_factory=Workflow):
        if not cfg.request_channel_id:
            raise SystemExit("Set REQUEST_CHANNEL_ID (the shared Slack channel id) in .env")
        if cfg.approval_backend != "slack":
            raise SystemExit("printloop serve needs APPROVAL_BACKEND=slack")
        self.cfg = cfg
        self.slack = slack or slack_mod.from_config(cfg, cfg.request_channel_id)
        self.printer = printer or Moonraker(cfg.moonraker_url, cfg.moonraker_api_key, web_url=cfg.moonraker_web_url)
        self.workflow_factory = workflow_factory
        self.state_path = cfg.runs_dir / "serve_state.json"
        self.queue: deque[Job] = deque()
        self.current: Job | None = None
        self.worker: threading.Thread | None = None
        self.lock = threading.RLock()
        # `status` needs a thread read per tracked job each poll: only do that when reads are free (bot API).
        self.answer_status = isinstance(self.slack, slack_mod.BotSlack) or getattr(self.slack, "free_reads", False)
        self.last_ts = self._load_state()

    # --- state -------------------------------------------------------------
    def _load_state(self) -> str:
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return f"{time.time():.6f}"  # first start: ignore the channel's history
        for j in data.get("queue", []):
            self.queue.append(Job(**j))
        if cur := data.get("current"):
            job = Job(**cur)
            if job.cancelled:
                job.phase = "done"
            if job.phase in RESUMABLE:
                job.phase = "queued"
                self.queue.appendleft(job)
                self._post_safe(job.ts, "I restarted. Picking this request back up now.")
            elif job.phase != "done":
                self._post_safe(job.ts, ":warning: I restarted while this job was "
                                f"{PHASE_TEXT.get(job.phase, job.phase)}. The print is *no longer monitored* by me. "
                                "Please check the printer. Post a new `print:` request if it needs redoing.")
        return data.get("last_ts") or f"{time.time():.6f}"

    def _save_state(self) -> None:
        # Called from the poll loop and the job thread: serialise the whole write, then swap atomically.
        with self.lock:
            data = {"last_ts": self.last_ts, "queue": [asdict(j) for j in self.queue],
                    "current": asdict(self.current) if self.current else None}
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(self.state_path)

    def _post_safe(self, thread_ts: str, text: str) -> None:
        try:
            self.slack.post(text, thread_ts=thread_ts)
        except Exception as e:
            print(f"slack post failed: {e!r}")

    # --- one polling step --------------------------------------------------
    def poll_once(self) -> None:
        for msg in self.slack.channel_messages(self.last_ts):
            self.last_ts = max(self.last_ts, msg["ts"], key=float)
            if self.slack.is_own(msg):
                continue
            text, images = msg.get("text", ""), image_files(msg)
            if CHANNEL_STATUS_RE.match(text):
                self._post_safe(msg["ts"], self._overview_text())
                continue
            what = parse_request(text, bool(images))
            if what is None:
                if re.match(r"^\s*print\b", text, re.I):
                    self.slack.post(HELP, thread_ts=msg["ts"])
                continue
            job = Job(msg["ts"], msg.get("user", ""), what, seen_ts=msg["ts"], files=images)
            with self.lock:
                self.queue.append(job)
                ahead = len(self.queue) - 1 + (1 if self.current else 0)
            pics = f" (+{len(images)} image{'s' if len(images) != 1 else ''})" if images else ""
            if images and not self.slack.can_download:
                pics += ". I can't read images with this Slack setup, so I'll go by the text"
            self.slack.post(
                f"<@{job.user}> got it: _{what}_{pics}. "
                + ("Starting now: I'll design it and ask for approval in this thread."
                   if not ahead else f"You're #{ahead + 1} in line; I'll start here when the printer is free.")
                + " Reply `status` or `cancel` here any time.",
                thread_ts=job.ts)
        self._save_state()
        if self.answer_status:
            self._answer_status_requests()
        self._start_next()

    def _start_next(self) -> None:
        with self.lock:
            if self.worker and self.worker.is_alive():
                return
            if self.current and self.current.phase != "done":
                self.current.phase = "done"
            self.current = None
            if self.queue:
                self.current = self.queue.popleft()
                self.worker = threading.Thread(target=self._run_job, args=(self.current,), daemon=True)
                self.worker.start()
        self._save_state()

    def _set_phase(self, job: Job, phase: str) -> None:
        job.phase = phase
        self._save_state()

    def _download_images(self, job: Job, thread: slack_mod.Slack) -> list:
        if not job.files or not self.slack.can_download:
            return []
        dest, paths = self.cfg.runs_dir / "incoming" / job.ts, []
        for i, f in enumerate(job.files, 1):
            try:
                paths.append(self.slack.download(f, dest / slack_mod.local_name(f, i)))
            except Exception as e:
                thread.post(f":warning: Couldn't read image `{f.get('name')}` ({e}); going on without it.")
        return paths

    def _run_job(self, job: Job) -> None:
        thread = slack_mod.ThreadSlack(self.slack, job.ts)
        try:
            self._set_phase(job, "waiting_for_printer")
            self._wait_for_printer(thread, job)
            if job.cancelled:
                return
            kw = {"images": images} if (images := self._download_images(job, thread)) else {}
            wf = self.workflow_factory(self.cfg, job.request, slack=thread, requester=job.user,
                                       on_phase=lambda p: self._set_phase(job, p),
                                       should_stop=lambda: job.cancelled, **kw)
            wf.run()
        except SystemExit as e:  # preflight problems
            thread.post(f":warning: Couldn't start this job:\n{e}")
        except Exception as e:  # the workflow already alerted and paused if a print was running
            thread.post(f":boom: This job stopped with an error: `{e!r}`")
        finally:
            self._set_phase(job, "done")

    def _busy_or_unreachable(self) -> str | None:
        try:
            return self.printer.busy_reason()
        except Exception as e:  # printer offline / host off its network: wait instead of failing the job
            return f"can't reach it from this computer: {str(e)[:120]}"

    def _time_left(self) -> str | None:
        try:
            return self.printer.time_left()
        except Exception:
            return None

    def _wait_for_printer(self, thread: slack_mod.Slack, job: Job | None = None) -> None:
        told = False
        while not (job and job.cancelled) and (busy := self._busy_or_unreachable()):
            if not told:
                left = self._time_left()
                thread.post(f"The printer is busy ({left or busy}). I'll start as soon as it's free.")
                told = True
            time.sleep(self.cfg.serve_poll_seconds)

    # --- `status` in job threads --------------------------------------------
    def _status_text(self, job: Job) -> str:
        with self.lock:
            position = next((i for i, j in enumerate(self.queue, 1) if j.ts == job.ts), None)
        if position is not None:
            ahead = position - 1 + (1 if self.current else 0)
            text = f"You're #{ahead + 1} in line ({ahead} job{'s' if ahead != 1 else ''} ahead)."
        else:
            text = f"This job is {PHASE_TEXT.get(job.phase, job.phase)}."
        if job.phase in ("queued", "waiting_for_printer", "printing"):
            busy = self._busy_or_unreachable()
            left = self._time_left()
            if left:
                text += f" Printer: {left}."
            elif busy:
                text += f" Printer: {busy}."
            else:
                text += " The printer is free."
        return text

    def _overview_text(self) -> str:
        """Printer + queue, for `status` / `queue` posted anywhere in the channel."""
        busy, left = self._busy_or_unreachable(), self._time_left()
        lines = [f"*Printer:* {left or busy or 'free'}"]
        with self.lock:
            cur, queued = self.current, list(self.queue)
        if cur and cur.phase != "done":
            lines.append(f"*Now:* _{cur.request}_ for <@{cur.user}>: {PHASE_TEXT.get(cur.phase, cur.phase)}")
        lines += [f"{i}. _{j.request}_ for <@{j.user}>" for i, j in enumerate(queued, 1)]
        if not queued:
            lines.append("Nobody is waiting." if cur else "Nothing queued. Post `print: …` to request a part.")
        return "\n".join(lines)

    def _cancel(self, job: Job, user: str) -> str | None:
        """Handle `cancel` in a job thread; returns the reply (None: the job's open question handles it)."""
        approvers = self.cfg.approver_user_ids
        if user != job.user and approvers and user not in approvers:
            return f"<@{user}> only <@{job.user}> or an approver can cancel this request."
        with self.lock:
            if job in self.queue:
                self.queue.remove(job)
                job.phase, job.cancelled = "done", True
                return "Cancelled: removed from the queue."
        if job.phase in ("awaiting_approval", "recovering"):
            return None  # the open question in this thread treats `cancel` as `abort`
        if job.phase == "printing":
            return ("It's already printing, so I won't stop it from here. An approver can cancel it "
                    "at the printer (or with `printloop cancel` on the host).")
        if job.phase == "done":
            return None
        job.cancelled = True
        return "Cancelling: I'll stop before the next step."

    def _answer_status_requests(self) -> None:
        with self.lock:
            jobs = ([self.current] if self.current else []) + list(self.queue)
        for job in jobs:
            try:
                replies = self.slack.thread_replies(job.ts)
            except Exception as e:
                print(f"status check failed for {job.ts}: {e!r}")
                continue
            for m in replies:
                if float(m["ts"]) <= float(job.seen_ts or job.ts) or self.slack.is_own(m):
                    continue
                job.seen_ts = m["ts"]
                text, user = m.get("text", ""), m.get("user", "")
                if QUEUE_RE.match(text):
                    self._post_safe(job.ts, self._overview_text())
                elif STATUS_RE.search(text):
                    self._post_safe(job.ts, f"<@{user}> {self._status_text(job)}")
                elif CANCEL_RE.match(text) and (reply := self._cancel(job, user)):
                    self._post_safe(job.ts, reply)
        self._save_state()

    # --- main loop ---------------------------------------------------------
    def serve_forever(self) -> None:
        print(f"printloop serve: watching {self.cfg.request_channel_id} every {self.cfg.serve_poll_seconds}s "
              f"(approvers: {', '.join(sorted(self.cfg.approver_user_ids)) or 'anyone'}; "
              f"queued: {len(self.queue)})")
        while True:
            try:
                self.poll_once()
            except Exception as e:  # a Slack hiccup must not kill the service
                print(f"poll failed: {e!r}")
            time.sleep(self.cfg.serve_poll_seconds)

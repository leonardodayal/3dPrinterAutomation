import threading
import time
from dataclasses import replace

import pytest

from printloop import slack as slack_mod
from printloop.approval import Approver
from printloop.config import Config
from printloop.serve import Server, parse_request

OWNER, COWORKER = "U0OWNER", "U0COWORK"


class FakeSlack(slack_mod.Slack):
    has_bot = enabled = True

    def __init__(self):
        self.channel = []   # top-level messages
        self.threads = {}   # root ts -> replies
        self.posts = []     # (thread_ts, text)
        self.reacted = {}   # ts -> [{name, users}]
        self._n = 1000

    def _ts(self):
        self._n += 1
        return f"1795400000.{self._n:06d}"

    def post(self, text, thread_ts=None):
        ts = self._ts()
        msg = {"ts": ts, "user": OWNER, "text": slack_mod.MARKER + " · " + text}
        self.posts.append((thread_ts, text))
        if thread_ts:
            self.threads.setdefault(thread_ts, []).append(msg)
        else:
            self.channel.append(msg)
        return ts

    def post_image(self, image, comment, thread_ts=None):
        return self.post(comment, thread_ts)

    def thread_replies(self, ts):
        return list(self.threads.get(ts, []))

    def channel_messages(self, oldest):
        return [m for m in self.channel if float(m["ts"]) > float(oldest or 0)]

    can_download = True

    def reactions(self, ts):
        return self.reacted.get(ts, [])

    def react(self, ts, name):
        return False  # ack in text so tests can see it

    def download(self, file, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\xff\xd8\xff")
        return dest

    def say(self, user, text, thread_ts=None, files=None):
        msg = {"ts": self._ts(), "user": user, "text": text, "files": files or []}
        (self.threads.setdefault(thread_ts, []) if thread_ts else self.channel).append(msg)
        return msg["ts"]


class FakePrinter:
    def __init__(self):
        self.busy = []

    def busy_reason(self):
        return self.busy.pop(0) if self.busy else None


class Log:
    def log(self, *a, **k):
        pass


@pytest.fixture
def cfg(tmp_path):
    return replace(Config.load(), runs_dir=tmp_path, request_channel_id="C0PRINTS", approval_backend="slack",
                   approver_user_ids=frozenset({OWNER}), serve_poll_seconds=0)


@pytest.mark.parametrize("text,want", [
    ("print: a cable clip for 6mm cable", "a cable clip for 6mm cable"),
    ("Print a phone stand", "a phone stand"),
    ("<@U123> print - hook for a 25mm tube", "hook for a 25mm tube"),
    ("printing is cool", None),
    ("can you print a thing", None),
    ("print", None),
])
def test_parse_request(text, want):
    assert parse_request(text) == want


def test_poll_queues_acks_and_runs_jobs_in_threads(cfg):
    s, ran, gate = FakeSlack(), [], threading.Event()

    class FakeWorkflow:
        def __init__(self, cfg, request, slack, requester, on_phase=None, **k):
            self.request, self.slack, self.requester, self.on_phase = request, slack, requester, on_phase

        def run(self):
            ran.append((self.request, self.requester))
            self.on_phase("designing")
            self.slack.post("design ready")
            gate.wait(5)

    srv = Server(cfg, slack=s, printer=FakePrinter(), workflow_factory=FakeWorkflow)
    srv.last_ts = "0"
    a = s.say(COWORKER, "print: a cable clip")
    s.say(COWORKER, "lunch anyone?")
    b = s.say(OWNER, "print a phone stand")
    srv.poll_once()

    assert [t for t, _ in s.posts[:2]] == [a, b]                 # each request acknowledged in its own thread
    assert "Starting now" in s.posts[0][1] and "#2 in line" in s.posts[1][1]
    srv.worker.join(1)
    assert ran == [("a cable clip", COWORKER)]
    assert (a, "design ready") in s.posts                         # job output stays in the request's thread

    gate.set(); srv.worker.join(2)
    srv.poll_once()                                               # no new messages; next job starts
    srv.worker.join(2)
    assert ran[-1] == ("a phone stand", OWNER)
    assert (cfg.runs_dir / "serve_state.json").exists() and float(srv.last_ts) >= float(b)


def test_bare_print_gets_help_and_own_messages_ignored(cfg):
    s = FakeSlack()
    srv = Server(cfg, slack=s, printer=FakePrinter(), workflow_factory=lambda *a, **k: None)
    srv.last_ts = "0"
    s.post("print: this is our own status message")  # our own post must never become a job
    q = s.say(COWORKER, "print")
    srv.poll_once()
    assert not srv.queue and srv.current is None
    assert s.posts[-1][0] == q and "Post a new message here" in s.posts[-1][1]


def test_thread_slack_routes_into_root_and_filters_replies():
    s = FakeSlack()
    root = s.say(COWORKER, "print: x")
    t = slack_mod.ThreadSlack(s, root)
    q = t.post("question?")
    early = s.say(COWORKER, "before", thread_ts=root)  # noqa: F841  (posted after q, so it counts)
    assert all(ts == root for ts, _ in s.posts)
    assert [m["text"] for m in t.thread_replies(q)] == ["before"]


def test_only_approvers_can_approve_but_anyone_can_revise(cfg, monkeypatch):
    monkeypatch.setattr("printloop.approval.time.sleep", lambda s: None)
    s = FakeSlack()
    root = s.say(COWORKER, "print: x")
    t = slack_mod.ThreadSlack(s, root)
    ap = Approver("slack", t, Log(), 0, approvers={OWNER})

    orig = t.thread_replies
    script = iter([lambda: s.say(COWORKER, "approve", thread_ts=root), lambda: s.say(OWNER, "approve bed clear", thread_ts=root)])

    def replies(ts):
        f = next(script, None)
        if f:
            f()
        return orig(ts)

    t.thread_replies = replies
    ans = ap.ask("Print?", {"approve": "go", "revise": "change", "abort": "stop"})
    assert (ans.choice, ans.who, ans.comment) == ("approve", OWNER, "bed clear")
    assert any("only an approver" in text for _, text in s.posts)
    assert any(f"<@{OWNER}>" in text and "can give `approve`" in text for _, text in s.posts)

    script = iter([lambda: s.say(COWORKER, "revise make it taller", thread_ts=root)])
    ans = ap.ask("Print?", {"approve": "go", "revise": "change", "abort": "stop"})
    assert (ans.choice, ans.who) == ("revise", COWORKER)


class StatusPrinter(FakePrinter):
    def time_left(self):
        return "F7_body_v2.gcode: 32% done after 34 min, ~47-71 min left (free around 20:17)"


def test_busy_message_includes_time_left(cfg):
    s, p = FakeSlack(), StatusPrinter()
    p.busy = ["printing F7_body_v2.gcode"]
    srv = Server(cfg, slack=s, printer=p, workflow_factory=lambda *a, **k: type("W", (), {"run": lambda self: None})())
    root = s.say(COWORKER, "print: x")
    srv._wait_for_printer(slack_mod.ThreadSlack(s, root))
    assert any("~47-71 min left" in text for _, text in s.posts)


def test_status_reply_and_queue_position(cfg):
    s, gate = FakeSlack(), threading.Event()

    class SlowWorkflow:
        def __init__(self, *a, on_phase=None, **k):
            self.on_phase = on_phase

        def run(self):
            self.on_phase("printing")
            gate.wait(5)

    p = StatusPrinter()
    srv = Server(cfg, slack=s, printer=p, workflow_factory=SlowWorkflow)
    srv.answer_status = True
    srv.last_ts = "0"
    a = s.say(COWORKER, "print: first")
    b = s.say(COWORKER, "print: second")
    srv.poll_once()
    srv.worker.join(0.2)
    s.say(COWORKER, "status?", thread_ts=a)
    s.say(OWNER, "how long until mine?", thread_ts=b)
    s.say(COWORKER, "thanks", thread_ts=b)
    srv.poll_once()
    replies_a = [t for ts, t in s.posts if ts == a]
    replies_b = [t for ts, t in s.posts if ts == b]
    assert any("This job is printing" in t and "~47-71 min left" in t for t in replies_a)
    assert any("#2 in line" in t for t in replies_b)
    n = len(s.posts)
    srv.poll_once()                       # already-answered questions aren't answered twice
    assert len(s.posts) == n
    gate.set(); srv.worker.join(2)


def test_restart_requeues_waiting_jobs_and_warns_about_running_print(cfg):
    s = FakeSlack()
    started = []

    class W:
        def __init__(self, cfg, request, **k):
            started.append(request)

        def run(self):
            pass

    import json
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    (cfg.runs_dir / "serve_state.json").write_text(json.dumps({
        "last_ts": "5", "current": {"ts": "1.1", "user": COWORKER, "request": "was printing", "phase": "printing"},
        "queue": [{"ts": "1.2", "user": COWORKER, "request": "waiting one", "phase": "queued"}]}))
    srv = Server(cfg, slack=s, printer=FakePrinter(), workflow_factory=W)
    assert [j.request for j in srv.queue] == ["waiting one"]
    assert any(ts == "1.1" and "no longer monitored" in t for ts, t in s.posts)
    srv.poll_once(); srv.worker.join(2)
    assert started == ["waiting one"]

    (cfg.runs_dir / "serve_state.json").write_text(json.dumps({
        "last_ts": "5", "current": {"ts": "2.1", "user": OWNER, "request": "mid-design", "phase": "awaiting_approval"},
        "queue": []}))
    srv2 = Server(cfg, slack=FakeSlack(), printer=FakePrinter(), workflow_factory=W)
    assert [(j.request, j.phase) for j in srv2.queue] == [("mid-design", "queued")]


def test_state_survives_round_trip(cfg):
    s = FakeSlack()
    gate = threading.Event()

    class W:
        def __init__(self, *a, on_phase=None, **k):
            self.on_phase = on_phase

        def run(self):
            self.on_phase("designing")
            gate.wait(5)

    srv = Server(cfg, slack=s, printer=FakePrinter(), workflow_factory=W)
    srv.last_ts = "0"
    s.say(COWORKER, "print: one"); s.say(COWORKER, "print: two")
    srv.poll_once(); srv.worker.join(0.2)
    import json
    state = json.loads((cfg.runs_dir / "serve_state.json").read_text())
    assert state["current"]["request"] == "one" and state["current"]["phase"] == "designing"
    assert [j["request"] for j in state["queue"]] == ["two"]
    gate.set(); srv.worker.join(2)


PHOTO = {"id": "F1", "name": "knob.jpg", "mimetype": "image/jpeg", "url": "https://files.slack.com/x"}


def test_request_images_are_downloaded_and_passed_on(cfg):
    s, got = FakeSlack(), {}

    class W:
        def __init__(self, cfg, request, images=None, **k):
            got.update(request=request, images=images)

        def run(self):
            pass

    srv = Server(cfg, slack=s, printer=FakePrinter(), workflow_factory=W)
    srv.last_ts = "0"
    s.say(COWORKER, "print", files=[PHOTO, {"id": "F2", "name": "notes.pdf", "mimetype": "application/pdf"}])
    srv.poll_once(); srv.worker.join(2)
    assert got["request"] == "the part shown in the attached image(s)"
    assert [p.name for p in got["images"]] == ["01-knob.jpg"] and got["images"][0].exists()
    assert "(+1 image)" in s.posts[0][1]


def test_channel_status_and_queue_without_a_thread(cfg):
    s, gate = FakeSlack(), threading.Event()

    class W:
        def __init__(self, *a, on_phase=None, **k):
            self.on_phase = on_phase

        def run(self):
            self.on_phase("designing")
            gate.wait(5)

    srv = Server(cfg, slack=s, printer=StatusPrinter(), workflow_factory=W)
    srv.last_ts = "0"
    s.say(COWORKER, "print: first"); s.say(OWNER, "print: second")
    srv.poll_once(); srv.worker.join(0.2)
    q = s.say(COWORKER, "status?")
    s.say(COWORKER, "what's the status of lunch")   # chatter, not a command
    srv.poll_once()
    replies = [t for ts, t in s.posts if ts == q]
    assert len(replies) == 1 and "~47-71 min left" in replies[0]
    assert "_first_" in replies[0] and "Claude is designing it" in replies[0] and "1. _second_" in replies[0]
    assert len([p for p in s.posts if p[0] not in (q,)]) == 2     # only the two request acks otherwise
    gate.set(); srv.worker.join(2)


def test_cancel_queued_job_only_by_requester_or_approver(cfg):
    s, gate = FakeSlack(), threading.Event()

    class W:
        def __init__(self, *a, **k):
            pass

        def run(self):
            gate.wait(5)

    srv = Server(cfg, slack=s, printer=FakePrinter(), workflow_factory=W)
    srv.answer_status = True
    srv.last_ts = "0"
    s.say(OWNER, "print: first")
    b = s.say(COWORKER, "print: second")
    srv.poll_once(); srv.worker.join(0.2)
    s.say("U0STRANGER", "cancel", thread_ts=b)
    srv.poll_once()
    assert [j.request for j in srv.queue] == ["second"]
    s.say(COWORKER, "cancel please", thread_ts=b)
    srv.poll_once()
    assert not srv.queue
    assert any(ts == b and "removed from the queue" in t for ts, t in s.posts)
    gate.set(); srv.worker.join(2)


def test_cancel_while_waiting_for_printer_stops_before_designing(cfg):
    s, started = FakeSlack(), []
    class Busy(StatusPrinter):
        def busy_reason(self):
            time.sleep(0.01)
            return "printing x"

    srv = Server(cfg, slack=s, printer=Busy(), workflow_factory=lambda *a, **k: started.append(1))
    srv.answer_status = True
    srv.last_ts = "0"
    a = s.say(COWORKER, "print: thing")
    srv.poll_once(); srv.worker.join(0.1)
    s.say(COWORKER, "cancel", thread_ts=a)
    srv.poll_once(); srv.worker.join(2)
    assert not srv.worker.is_alive() and not started


def _ask_in_thread(cfg, script, requester=COWORKER, approvers=(OWNER,)):
    import printloop.approval as ap_mod
    s = FakeSlack()
    root = s.say(requester, "print: x")
    t = slack_mod.ThreadSlack(s, root)
    ap = Approver("slack", t, Log(), 0, approvers=set(approvers), requester=requester)
    orig, steps = t.thread_replies, iter(script)

    def replies(ts):
        f = next(steps, None)
        if f:
            f(s, root, ts)
        return orig(ts)

    t.thread_replies = replies
    ans = ap.ask("Print?", {"approve": "go", "revise": "change", "abort": "stop"}, requester_signoff=True)
    return ans, s


def test_design_needs_requester_and_approver(cfg, monkeypatch):
    monkeypatch.setattr("printloop.approval.time.sleep", lambda s: None)
    ans, s = _ask_in_thread(cfg, [
        lambda s, root, q: s.say(OWNER, "approve bed clear", thread_ts=root),
        lambda s, root, q: s.say(COWORKER, "approve", thread_ts=root),
    ])
    assert ans.choice == "approve" and set(ans.who.split(",")) == {OWNER, COWORKER} and ans.comment == "bed clear"
    assert any(f"Still waiting for <@{COWORKER}>" in t for _, t in s.posts)
    assert any("*and* from" in t for _, t in s.posts)


def test_reaction_counts_as_approve_and_requester_alone_is_not_enough(cfg, monkeypatch):
    monkeypatch.setattr("printloop.approval.time.sleep", lambda s: None)

    def react(users):
        def f(s, root, q):
            s.reacted[q] = [{"name": "white_check_mark", "users": users}]
        return f

    ans, s = _ask_in_thread(cfg, [react([COWORKER]), lambda *a: None, react([COWORKER, OWNER])])
    assert ans.choice == "approve" and set(ans.who.split(",")) == {COWORKER, OWNER}
    assert sum("Still waiting" in t for _, t in s.posts) == 1


def test_requester_who_is_approver_signs_off_alone(cfg, monkeypatch):
    monkeypatch.setattr("printloop.approval.time.sleep", lambda s: None)
    ans, _ = _ask_in_thread(cfg, [lambda s, root, q: s.say(OWNER, "approve", thread_ts=root)], requester=OWNER)
    assert (ans.choice, ans.who) == ("approve", OWNER)


def test_revise_brings_thread_images_and_cancel_means_abort(cfg, monkeypatch):
    monkeypatch.setattr("printloop.approval.time.sleep", lambda s: None)
    ans, s = _ask_in_thread(cfg, [
        lambda s, root, q: s.say(COWORKER, "", thread_ts=root, files=[PHOTO]),
        lambda s, root, q: s.say("U0OTHER", "revise like this photo", thread_ts=root),
    ])
    assert (ans.choice, ans.comment, ans.files) == ("revise", "like this photo", [PHOTO])
    assert not any("To answer, start" in t for _, t in s.posts)   # image-only message isn't nagged

    ans, s = _ask_in_thread(cfg, [
        lambda s, root, q: s.say("U0OTHER", "cancel", thread_ts=root),
        lambda s, root, q: s.say(COWORKER, "cancel", thread_ts=root),
    ])
    assert (ans.choice, ans.who) == ("abort", COWORKER)
    assert any("can stop this job" in t for _, t in s.posts)

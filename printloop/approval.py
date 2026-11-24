"""Human-in-the-loop gate. Nothing prints without an explicit answer from here.

Backends:
- cli:   prompt in the terminal running the workflow.
- slack: post the question and poll its thread for a reply whose first word is one of the
         options (e.g. "approve", "revise make the wall 3mm thicker"). A ✅ / 👍 reaction on the
         question counts as `approve` (needs the reactions:read scope). Needs the Slack bot token.

Starting a print from a new design can require two sign-offs (`requester_signoff=True`): the person
who asked for the part (it's what they wanted) and an approver (someone at the printer: the bed is
clear). If the requester is an approver, their one `approve` covers both.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .eventlog import EventLog
from .slack import Slack, image_files


@dataclass
class Answer:
    choice: str | None  # None = timed out
    comment: str = ""
    who: str = ""
    files: list[dict] = field(default_factory=list)  # images posted in the thread while the question was open


# Answers anyone in the thread may give. Everything else (approve, real, false, removed, same, retry)
# asserts something about the physical printer, so it must come from an approver.
OPEN_CHOICES = {"revise", "abort"}
# Messages answered by `printloop serve` itself; don't treat them as attempts to answer a question.
PASSIVE = re.compile(r"^\s*(status|eta|queue)\b|how long", re.IGNORECASE)
SYNONYMS = {"cancel": "abort", "approved": "approve", "lgtm": "approve"}
APPROVE_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check", "+1", "thumbsup"}


class Approver:
    def __init__(self, backend: str, slack: Slack, log: EventLog, poll_seconds: int = 10,
                 approvers: set[str] | None = None, requester: str | None = None):
        """approvers: Slack user ids allowed to give printer-side answers; empty/None = anyone.
        requester: Slack user id of whoever asked for the part (may abort, and signs off designs)."""
        if backend == "slack" and not slack.has_bot:
            raise ValueError("APPROVAL_BACKEND=slack needs a Slack backend that can read replies (mcp or bot)")
        self.backend, self.slack, self.log, self.poll = backend, slack, log, poll_seconds
        self.approvers = set(approvers or ())
        self.requester = requester or None

    def ask(
        self,
        question: str,
        options: dict[str, str],
        images: list[Path] | None = None,
        timeout: float | None = None,
        requester_signoff: bool = False,
    ) -> Answer:
        """options: {keyword: description}. Returns the chosen keyword (lowercase).
        requester_signoff: `approve` needs the requester AND an approver (Slack only)."""
        opts_text = "\n".join(f"  • `{k}` — {v}" for k, v in options.items())
        self.log.log("approval_requested", question=question.splitlines()[0], options=list(options))
        ans = (
            self._ask_slack(question, opts_text, options, images, timeout, requester_signoff)
            if self.backend == "slack"
            else self._ask_cli(question, opts_text, options, images, timeout)
        )
        self.log.log("approval_answered", choice=ans.choice, comment=ans.comment, who=ans.who)
        return ans

    # --- terminal ----------------------------------------------------------
    def _ask_cli(self, question, opts_text, options, images, timeout) -> Answer:
        # Images/alerts still go to Slack so the human can see them on their phone.
        for img in images or []:
            if self.slack.enabled:
                self.slack.post_image(img, f"Preview for approval: {img.name}")
        print("\n" + "=" * 70 + f"\n{question}\n")
        for img in images or []:
            print(f"  image: {img.resolve()}")
        print(f"\nReply with one of:\n{opts_text}\n(anything after the keyword is passed on as a comment)")
        if timeout is not None:
            return self._cli_with_timeout(options, timeout)
        while True:
            raw = input("> ").strip()
            word, _, rest = raw.partition(" ")
            if word.lower() in options:
                return Answer(word.lower(), rest.strip(), "terminal")
            print(f"Please start your reply with one of: {', '.join(options)}")

    def _cli_with_timeout(self, options, timeout) -> Answer:
        import select
        import sys

        deadline = time.time() + timeout
        while (remaining := deadline - time.time()) > 0:
            sys.stdout.write("> ")
            sys.stdout.flush()
            ready, _, _ = select.select([sys.stdin], [], [], remaining)
            if not ready:
                break
            raw = sys.stdin.readline().strip()
            word, _, rest = raw.partition(" ")
            if word.lower() in options:
                return Answer(word.lower(), rest.strip(), "terminal")
            print(f"Please start your reply with one of: {', '.join(options)}")
        print("\n(no answer before timeout)")
        return Answer(None)

    # --- slack -------------------------------------------------------------
    def _is_approver(self, user: str) -> bool:
        return not self.approvers or user in self.approvers

    def _refusal(self, user: str, word: str, two_party: bool) -> str | None:
        """Why `user` may not give `word`, or None if they may."""
        if self._is_approver(user):
            return None
        if word == "abort":  # open to anyone only when there's no requester (CLI runs)
            return None if not self.requester or user == self.requester else \
                f"<@{user}> only <@{self.requester}> or an approver can stop this job."
        if word in OPEN_CHOICES:
            return None
        if word == "approve" and two_party and user == self.requester:
            return None
        return f"<@{user}> only an approver at the printer can say `{word}`."

    def _who_text(self, options, two_party: bool) -> str:
        react = " (or a :white_check_mark: reaction on this message)" if "approve" in options else ""
        if two_party:
            if self.requester and self._is_approver(self.requester) or not self.approvers:
                return f"\n_Printing needs `approve`{react} from <@{self.requester}>._"
            return (f"\n_Printing needs `approve`{react} from <@{self.requester}> (it's what you wanted) *and* from "
                    + " or ".join(f"<@{u}>" for u in sorted(self.approvers)) + " (someone at the printer)._")
        if self.approvers and set(options) - OPEN_CHOICES - {"abort"}:
            return ("\n_Only " + ", ".join(f"<@{u}>" for u in sorted(self.approvers))
                    + " can give " + ", ".join(f"`{o}`" for o in options if o not in OPEN_CHOICES | {"abort"})
                    + f"{react} (someone has to be at the printer)._")
        return ""

    def _ack(self, msg_ts: str | None, thread_ts: str, text: str) -> None:
        """Acknowledge an answer with a reaction on it (quiet); fall back to a reply."""
        if msg_ts and self.slack.react(msg_ts, "white_check_mark"):
            return
        self.slack.post(text, thread_ts=thread_ts)

    def _ask_slack(self, question, opts_text, options, images, timeout, signoff=False) -> Answer:
        two_party = bool(signoff and self.requester and "approve" in options)
        if images:  # one message for all images instead of one per image
            self.slack.post_images(images, "Images for the question below: " + ", ".join(p.name for p in images))
        ts = self.slack.post(f":raising_hand: *Approval needed*\n{question}\n\nReply *in this thread* with:\n"
                             f"{opts_text}{self._who_text(options, two_party)}")
        seen: set = set()
        files: list[dict] = []
        approvals: dict[str, str] = {}  # user -> comment, for two-party sign-off
        nagged = False
        deadline = None if timeout is None else time.time() + timeout
        while deadline is None or time.time() < deadline:
            events = []  # (user, word, comment, message ts or None for a reaction)
            for msg in self.slack.thread_replies(ts):
                if msg["ts"] in seen or self.slack.is_own(msg):
                    continue
                seen.add(msg["ts"])
                files += image_files(msg)
                text = msg.get("text", "").strip()
                if PASSIVE.search(text):
                    continue
                word, _, rest = text.partition(" ")
                word = word.lower().strip("`*_.,!:")
                word = SYNONYMS.get(word, word)
                if word in options:
                    events.append((msg.get("user", ""), word, rest.strip(), msg["ts"]))
                elif image_files(msg) and not text:
                    self._ack(msg["ts"], ts, "Image noted: reply `revise …` to use it.")
                elif not nagged:  # shared threads have chatter: remind once, not on every message
                    self.slack.post(f"To answer, start your reply with one of: {', '.join(options)}", thread_ts=ts)
                    nagged = True
            if "approve" in options:
                for r in self.slack.reactions(ts):
                    if r.get("name", "").split("::")[0] in APPROVE_REACTIONS:
                        for user in r.get("users", []):
                            if ("reaction", user) not in seen:
                                seen.add(("reaction", user))
                                events.append((user, "approve", "", None))
            for user, word, comment, msg_ts in events:
                if why := self._refusal(user, word, two_party):
                    self.slack.post(why, thread_ts=ts)
                    continue
                if word == "approve" and two_party:
                    approvals[user] = comment
                    have_requester = self.requester in approvals
                    have_approver = any(self._is_approver(u) for u in approvals)
                    if not (have_requester and have_approver):
                        missing = (f"<@{self.requester}>" if not have_requester else
                                   " or ".join(f"<@{u}>" for u in sorted(self.approvers)))
                        self.slack.post(f"<@{user}> approved. Still waiting for {missing}.", thread_ts=ts)
                        continue
                    self._ack(msg_ts, ts, "Approved by " + " and ".join(f"<@{u}>" for u in approvals) + ".")
                    return Answer("approve", "; ".join(c for c in approvals.values() if c), ",".join(approvals), files)
                self._ack(msg_ts, ts, f"Got it: `{word}`")
                return Answer(word, comment, user, files)
            time.sleep(self.poll)
        self.slack.post("(timed out waiting for an answer)", thread_ts=ts)
        return Answer(None, files=files)

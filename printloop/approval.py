"""Human-in-the-loop gate. Nothing prints without an explicit answer from here.

Backends:
- cli:   prompt in the terminal running the workflow.
- slack: post the question and poll its thread for a reply whose first word is one of the
         options (e.g. "approve", "reject make the wall 3mm thicker"). Needs the Slack bot token.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from .eventlog import EventLog
from .slack import Slack


@dataclass
class Answer:
    choice: str | None  # None = timed out
    comment: str = ""
    who: str = ""


# Answers anyone in the thread may give. Everything else (approve, real, false, removed, same, retry)
# asserts something about the physical printer, so it must come from an approver.
OPEN_CHOICES = {"revise", "abort"}
# Messages answered by `printloop serve` itself; don't treat them as attempts to answer a question.
PASSIVE = re.compile(r"^\s*(status|eta)\b|how long", re.IGNORECASE)


class Approver:
    def __init__(self, backend: str, slack: Slack, log: EventLog, poll_seconds: int = 10,
                 approvers: set[str] | None = None):
        """approvers: Slack user ids allowed to give printer-side answers; empty/None = anyone."""
        if backend == "slack" and not slack.has_bot:
            raise ValueError("APPROVAL_BACKEND=slack needs a Slack backend that can read replies (mcp or bot)")
        self.backend, self.slack, self.log, self.poll = backend, slack, log, poll_seconds
        self.approvers = set(approvers or ())

    def ask(
        self,
        question: str,
        options: dict[str, str],
        images: list[Path] | None = None,
        timeout: float | None = None,
    ) -> Answer:
        """options: {keyword: description}. Returns the chosen keyword (lowercase)."""
        opts_text = "\n".join(f"  • `{k}` — {v}" for k, v in options.items())
        self.log.log("approval_requested", question=question.splitlines()[0], options=list(options))
        ans = (
            self._ask_slack(question, opts_text, options, images, timeout)
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
    def _ask_slack(self, question, opts_text, options, images, timeout) -> Answer:
        for img in images or []:
            self.slack.post_image(img, f"Preview: {img.name}")
        who = ""
        if self.approvers and set(options) - OPEN_CHOICES:
            who = ("\n_Only " + ", ".join(f"<@{u}>" for u in sorted(self.approvers))
                   + " can give " + ", ".join(f"`{o}`" for o in options if o not in OPEN_CHOICES)
                   + " (someone has to be at the printer)._")
        ts = self.slack.post(f":raising_hand: *Approval needed*\n{question}\n\nReply *in this thread* with:\n{opts_text}{who}")
        seen: set[str] = set()
        nagged = False
        deadline = None if timeout is None else time.time() + timeout
        while deadline is None or time.time() < deadline:
            for msg in self.slack.thread_replies(ts):
                if msg["ts"] in seen or self.slack.is_own(msg):
                    continue
                seen.add(msg["ts"])
                word, _, rest = msg.get("text", "").strip().partition(" ")
                word = word.lower().strip("`*_.,!")
                user = msg.get("user", "")
                if PASSIVE.search(msg.get("text", "")):
                    continue
                if word in options:
                    if self.approvers and word not in OPEN_CHOICES and user not in self.approvers:
                        self.slack.post(f"<@{user}> only an approver at the printer can say `{word}`.", thread_ts=ts)
                        continue
                    self.slack.post(f"Got it: `{word}`", thread_ts=ts)
                    return Answer(word, rest.strip(), user)
                if not nagged:  # shared threads have chatter: remind once, not on every message
                    self.slack.post(f"To answer, start your reply with one of: {', '.join(options)}", thread_ts=ts)
                    nagged = True
            time.sleep(self.poll)
        self.slack.post("(timed out waiting for an answer)", thread_ts=ts)
        return Answer(None)

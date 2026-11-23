from pathlib import Path

import pytest

from printloop import slack
from printloop.approval import Approver


class Log:
    def log(self, *a, **k):
        pass


def test_mcp_post_and_thread(monkeypatch):
    calls = []

    def fake_ask(model, effort, system, text, schema, images=(), mcp_tools=(), timeout=None):
        calls.append((text, list(mcp_tools)))
        if schema is slack._Sent:
            return slack._Sent(ok=True, ts="1763851234.123456", error=None)
        return slack._Thread(messages=[
            slack._Msg(ts="1763851234.123456", user="U1", text="parent"),
            slack._Msg(ts="1763851300.000001", user="U0B6AMU6ZQ9", text=slack.MARKER + " · Got it"),
            slack._Msg(ts="1763851400.000002", user="U05L82Z8T5Y", text="approve looks good"),
        ])

    monkeypatch.setattr(slack.claude, "ask_json", fake_ask)
    s = slack.McpSlack("D0B62G6DMPV")
    assert s.post("hello") == "1763851234.123456"
    text, tools = calls[0]
    assert "BEGIN\n" + slack.MARKER + " · hello\nEND" in text and "D0B62G6DMPV" in text
    assert tools == ["mcp__claude_ai_Slack__slack_send_message"]
    replies = s.thread_replies("1763851234.123456")
    assert [m["ts"] for m in replies] == ["1763851300.000001", "1763851400.000002"]
    assert [s.is_own(m) for m in replies] == [True, False]


def test_mcp_image_upload(monkeypatch, tmp_path):
    img = tmp_path / "snap.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"0" * 100)
    steps = []

    def fake_ask(model, effort, system, text, schema, images=(), mcp_tools=(), timeout=None):
        steps.append(mcp_tools[0].rsplit("__", 1)[1])
        if schema is slack._UploadUrl:
            return slack._UploadUrl(upload_url="https://files.slack.com/upload/v1/ABC", file_id="F123")
        return slack._Sent(ok=True, ts=None, error=None)

    posted = {}

    class R:
        def raise_for_status(self):
            pass

    def fake_post(url, data=None, headers=None, timeout=None):
        posted.update(url=url, size=len(data), ctype=headers["Content-Type"])
        return R()

    monkeypatch.setattr(slack.claude, "ask_json", fake_ask)
    monkeypatch.setattr(slack.requests, "post", fake_post)
    slack.McpSlack("D0B62G6DMPV").post_image(img, "alert")
    assert steps == ["slack_get_file_upload_url", "slack_complete_file_upload"]
    assert posted == {"url": "https://files.slack.com/upload/v1/ABC", "size": 103, "ctype": "image/jpeg"}


def test_mcp_rejects_user_id_as_channel():
    with pytest.raises(ValueError):
        slack.McpSlack("U05L82Z8T5Y")


def test_approver_ignores_own_messages(monkeypatch):
    class FakeSlack(slack.Slack):
        has_bot = enabled = True
        posts = []

        def post(self, text, thread_ts=None):
            self.posts.append(text)
            return "1.0"

        def post_image(self, *a, **k):
            pass

        def thread_replies(self, ts):
            return [{"ts": "2", "user": "me", "text": slack.MARKER + " · Please start your reply with approve"},
                    {"ts": "3", "user": "goetz", "text": "Approve, bed is clear"}]

    monkeypatch.setattr("printloop.approval.time.sleep", lambda s: None)
    ans = Approver("slack", FakeSlack(), Log(), 0).ask("Print?", {"approve": "go", "abort": "stop"})
    assert (ans.choice, ans.comment, ans.who) == ("approve", "bed is clear", "goetz")


@pytest.mark.parametrize("text,own", [
    (slack.MARKER + " · Got it", True),
    (":printer: _printloop_ · :test_tube: Test alert", True),   # as read back through the connector
    ("🖨 *printloop* · Please start your reply", True),
    ("approve", False),
    ("ok printloop · works", False),
    ("real the part came loose", False),
])
def test_is_own_matches_rendered_prefix(text, own):
    assert slack.NoSlack().is_own({"text": text}) is own

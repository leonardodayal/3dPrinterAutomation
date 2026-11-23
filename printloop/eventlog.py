"""Append-only JSONL audit log: every approval, print start, failure and cancellation."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class EventLog:
    def __init__(self, run_dir: Path, global_log: Path):
        self.paths = [run_dir / "events.jsonl", global_log]
        for p in self.paths:
            p.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_dir.name

    def log(self, event: str, **fields: Any) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "run": self.run_id, "event": event, **fields}
        line = json.dumps(record, default=str)
        for p in self.paths:
            with p.open("a") as f:
                f.write(line + "\n")
        print(f"[{record['ts']}] {event} {json.dumps(fields, default=str)[:300]}")

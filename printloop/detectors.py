"""Failure detection with the handoff's dual-detector rule.

External detector: Obico's ML API (self-hosted, `ml_api` container from obico-server,
https://github.com/TheSpaghettiDetective/obico-server). It fetches the snapshot URL
itself and returns [[label, confidence, [x, y, w, h]], ...].

K1C built-in AI: whether its events can be read outside Creality Cloud is still an open
question in the handoff. Until it is answered, this detector reports UNAVAILABLE and the
rule degrades to "single detector" (still pause + alert; a human decides).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import requests


class Signal(str, Enum):
    OK = "ok"
    FAILURE = "failure"
    UNAVAILABLE = "unavailable"


@dataclass
class DetectorResult:
    name: str
    signal: Signal
    score: float | None = None
    detail: str = ""


class Verdict(str, Enum):
    CONTINUE = "continue"
    CONFIRMED = "confirmed_failure"  # both detectors agree
    DISAGREE = "detectors_disagree"  # exactly one of two flags a failure
    SINGLE = "single_detector_failure"  # only one detector is available and it flags a failure
    PRINTER_ERROR = "printer_error"  # Klipper itself reports an error


@dataclass
class Decision:
    verdict: Verdict
    results: list[DetectorResult] = field(default_factory=list)

    @property
    def should_pause(self) -> bool:
        return self.verdict is not Verdict.CONTINUE


def dual_rule(results: list[DetectorResult]) -> Decision:
    """Both -> confirmed; one of two -> disagree; neither -> continue."""
    available = [r for r in results if r.signal is not Signal.UNAVAILABLE]
    flagged = [r for r in available if r.signal is Signal.FAILURE]
    if not flagged:
        return Decision(Verdict.CONTINUE, results)
    if len(available) >= 2 and len(flagged) == len(available):
        return Decision(Verdict.CONFIRMED, results)
    if len(available) >= 2:
        return Decision(Verdict.DISAGREE, results)
    return Decision(Verdict.SINGLE, results)


class ObicoDetector:
    name = "obico"

    def __init__(self, url: str | None, token: str | None, threshold: float, high_threshold: float, consecutive: int):
        self.url, self.token = url, token
        self.threshold, self.high, self.consecutive = threshold, high_threshold, consecutive
        self.hits = 0

    def reset(self) -> None:
        self.hits = 0

    def check(self, snapshot_url: str) -> DetectorResult:
        if not self.url:
            return DetectorResult(self.name, Signal.UNAVAILABLE, detail="OBICO_ML_URL not set")
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            r = requests.get(f"{self.url}/p/", params={"img": snapshot_url}, headers=headers, timeout=30)
            r.raise_for_status()
            detections = r.json().get("detections", [])
        except (requests.RequestException, ValueError) as e:
            return DetectorResult(self.name, Signal.UNAVAILABLE, detail=f"ML API error: {e}")

        score = sum(float(d[1]) for d in detections)
        self.hits = self.hits + 1 if score >= self.threshold else 0
        failed = score >= self.high or self.hits >= self.consecutive
        detail = f"{len(detections)} detection(s), score {score:.2f} (threshold {self.threshold}, hits {self.hits}/{self.consecutive})"
        return DetectorResult(self.name, Signal.FAILURE if failed else Signal.OK, score, detail)


class K1CBuiltinDetector:
    """Placeholder for the printer's own AI detection (set to "Notify only" on the printer).

    Implement `check()` once we know where its events surface (open question in the handoff).
    Returning UNAVAILABLE keeps the workflow correct in the meantime.
    """

    name = "k1c_builtin"

    def __init__(self, mode: str):
        self.mode = mode

    def reset(self) -> None:
        pass

    def check(self, snapshot_url: str) -> DetectorResult:
        return DetectorResult(self.name, Signal.UNAVAILABLE, detail=f"mode={self.mode}; event source not wired up yet")

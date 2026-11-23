"""After a failure: Claude looks at the snapshot + context and proposes a fix with a diff."""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from . import claude
from .design import K1C_CONTEXT

SYSTEM = f"""You are an expert FDM print-failure analyst.

{K1C_CONTEXT}

You get a camera snapshot taken when a failure detector fired, the detector outputs, print progress, the OpenSCAD
source of the part, the slicer settings currently in use, and the history of earlier attempts.

Diagnose the most likely cause (e.g. bed adhesion / warping, spaghetti from a detached part, layer shift,
under-extrusion or clog, stringing, unsupported overhang, tall thin part wobbling, or a false positive).
Then propose the smallest change likely to fix it:
- `setting_changes` must use keys that exist in the provided slicer settings, with values in the same format.
- Only change the model (`new_scad_code`, full file) when geometry is the root cause, e.g. add a brim/mouse ears,
  chamfer the bed edge, reorient, widen the base, add support-free overhang angles. Otherwise return null.
- `operator_actions` are physical things the human should do while clearing the bed (clean the plate with IPA,
  check filament path, dry filament...).
- Don't repeat a change from an earlier attempt that already failed the same way.
- If the snapshot does not show a failure, say so and set `likely_false_positive` true.
"""


class SettingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    value: str
    reason: str


class FixProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diagnosis: str
    confidence: str  # low | medium | high
    likely_false_positive: bool
    setting_changes: list[SettingChange]
    new_scad_code: str | None
    model_change_summary: str | None
    operator_actions: list[str]


@dataclass
class ReviewedFix:
    proposal: FixProposal
    overrides: dict[str, str]  # full override set for the next attempt
    rejected_keys: list[str]
    scad_diff: str
    settings_diff: str


def propose(
    model: str, effort: str, snapshot: Path | None, detector_summary: str, status_summary: str,
    scad_code: str, base_settings: dict[str, str], overrides: dict[str, str], history: list[str],
    printer_notes: str = "",
) -> FixProposal:
    current = {**base_settings, **overrides}
    # Long values (custom G-code blocks etc.) are truncated to keep the prompt focused.
    settings_text = "\n".join(f"{k} = {v if len(v) <= 200 else v[:200] + '...'}" for k, v in sorted(current.items()))
    text = (
        (f"Notes about this printer from its owner:\n{printer_notes}\n\n" if printer_notes else "")
        + f"Detectors:\n{detector_summary}\n\nPrinter status at detection:\n{status_summary}\n\n"
        f"Earlier attempts:\n{chr(10).join(history) or '(none: this was the first print)'}\n\n"
        f"Current overrides on top of the base profile: {overrides or '(none)'}\n\n"
        f"OpenSCAD source:\n```openscad\n{scad_code}\n```\n\nFull slicer settings in use:\n```\n{settings_text}\n```"
    )
    if not snapshot:
        text = "(No camera snapshot was available.)\n\n" + text
    return claude.ask_json(model, effort, SYSTEM, text, FixProposal, images=[snapshot] if snapshot else [])


def review(proposal: FixProposal, scad_code: str, base_settings: dict[str, str], overrides: dict[str, str]) -> ReviewedFix:
    new_overrides = dict(overrides)
    rejected, lines = [], []
    for ch in proposal.setting_changes:
        if ch.key not in base_settings:
            rejected.append(ch.key)
            continue
        old = new_overrides.get(ch.key, base_settings[ch.key])
        new_overrides[ch.key] = ch.value
        lines.append(f"- {ch.key}: {old}  ->  {ch.value}   ({ch.reason})")
    scad_diff = ""
    if proposal.new_scad_code:
        scad_diff = "".join(difflib.unified_diff(
            scad_code.splitlines(keepends=True), proposal.new_scad_code.splitlines(keepends=True),
            fromfile="model (current)", tofile="model (proposed)",
        ))
    return ReviewedFix(proposal, new_overrides, rejected, scad_diff, "\n".join(lines))


def format_for_human(fix: ReviewedFix) -> str:
    p = fix.proposal
    parts = [f"*Diagnosis* ({p.confidence} confidence): {p.diagnosis}"]
    if p.likely_false_positive:
        parts.append(":warning: Claude thinks this may have been a false positive.")
    parts.append("*Slicer setting changes:*\n" + (f"```\n{fix.settings_diff}\n```" if fix.settings_diff else "_none_"))
    if fix.rejected_keys:
        parts.append(f"_Ignored unknown setting keys: {', '.join(fix.rejected_keys)}_")
    if fix.scad_diff:
        parts.append(f"*Model change:* {p.model_change_summary or ''}\n```diff\n{fix.scad_diff[:6000]}\n```")
    else:
        parts.append("*Model change:* _none_")
    if p.operator_actions:
        parts.append("*Before reprinting, please:*\n" + "\n".join(f"• {a}" for a in p.operator_actions))
    return "\n\n".join(parts)

"""Pure planning helpers for the Hypit-style project runner.

Keeping shot planning deterministic lets the eventual provider runner reuse a
completed node instead of submitting another paid generation request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from video_projects import fingerprint


@dataclass(frozen=True)
class PlannedShot:
    key: str
    index: int
    start: float
    end: float
    duration: float
    contract: str
    dependency_fingerprint: str


def plan_shots(analysis: dict[str, Any], duration: float) -> list[PlannedShot]:
    raw = analysis.get("shots") if isinstance(analysis, dict) else []
    output: list[PlannedShot] = []
    for index, shot in enumerate(raw or [], 1):
        if not isinstance(shot, dict):
            continue
        start = float(shot.get("start_seconds") or shot.get("start") or 0)
        end = float(shot.get("end_seconds") or shot.get("end") or duration)
        start = max(0.0, min(start, duration))
        end = max(start, min(end, duration))
        if end <= start:
            continue
        output.append(PlannedShot(
            key=f"shot_{index:03d}", index=index, start=start, end=end,
            duration=round(end - start, 3),
            contract=str(shot.get("contract") or shot.get("text") or ""),
            dependency_fingerprint=fingerprint({"shot": shot}),
        ))
    if not output and duration > 0:
        output.append(PlannedShot(
            key="shot_001", index=1, start=0.0, end=duration,
            duration=round(duration, 3), contract="",
            dependency_fingerprint=fingerprint({"duration": duration}),
        ))
    return output


def reusable_nodes(
    planned: list[PlannedShot], existing: dict[str, dict[str, Any]],
) -> tuple[list[PlannedShot], list[PlannedShot]]:
    """Return (reusable, invalidated) nodes by dependency fingerprint."""
    reusable, invalidated = [], []
    for shot in planned:
        current = existing.get(shot.key) or {}
        if (
            current.get("dependency_fingerprint") == shot.dependency_fingerprint
            and current.get("status") in {"ready", "completed"}
            and current.get("output_json")
        ):
            reusable.append(shot)
        else:
            invalidated.append(shot)
    return reusable, invalidated


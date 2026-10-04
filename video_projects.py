"""Small durable project layer for Hypit-style video editing.

The existing task queue remains responsible for paid execution.  This module
only owns the editable project envelope and its Run/node bookkeeping so the
editor can grow from one-shot generation into incremental production without
changing the public video-generation APIs.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from database import connect, public_id


def fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def create_for_editor(
    db,
    *,
    merchant_id: str,
    task_id: str,
    title: str,
    brief: str,
    input_objects: list[dict],
    prompt: str,
    duration: int,
    ratio: str,
) -> tuple[str, str]:
    """Create a project and its first Run inside the task transaction."""
    now = time.time()
    project_id = public_id("prj")
    run_id = public_id("run")
    run_number = 1
    source = next((item for item in input_objects if item.get("field") == "videos"), None)
    source_fingerprint = fingerprint({
        "source": source or {},
        "images": [item for item in input_objects if item.get("field") == "images"],
        "prompt": prompt,
        "duration": duration,
        "ratio": ratio,
    })
    db.execute(
        """INSERT INTO video_projects
        (id,merchant_id,title,brief,status,source_task_id,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?)""",
        (project_id, merchant_id, title[:100], brief[:30000], "analyzing", task_id, now, now),
    )
    db.execute(
        """INSERT INTO video_project_runs
        (id,project_id,task_id,run_number,input_fingerprint,status,created_at)
        VALUES(?,?,?,?,?,?,?)""",
        (run_id, project_id, task_id, run_number, source_fingerprint, "queued", now),
    )
    return project_id, run_id


def update_analysis(db, project_id: str, analysis: dict) -> None:
    """Persist the analyzer output as the project's reference truth."""
    from video_project_engine import plan_shots

    now = time.time()
    shots = analysis.get("shots") if isinstance(analysis, dict) else []
    planned = plan_shots(analysis, float(
        (analysis or {}).get("duration_seconds") or (analysis or {}).get("duration") or 0
    )) if isinstance(analysis, dict) else []
    timeline = {
        "duration": (analysis.get("duration_seconds") or analysis.get("duration"))
        if isinstance(analysis, dict) else None,
        "shots": shots if isinstance(shots, list) else [],
    }
    db.execute(
        """UPDATE video_projects
        SET status='planned',analysis_json=?,timeline_json=?,updated_at=?
        WHERE id=?""",
        (json.dumps(analysis, ensure_ascii=False), json.dumps(timeline, ensure_ascii=False), now, project_id),
    )
    if not isinstance(shots, list):
        return
    planned_by_index = {item.index: item for item in planned}
    for index, shot in enumerate(shots, 1):
        if not isinstance(shot, dict):
            continue
        key = f"shot_{index:03d}"
        plan = planned_by_index.get(index)
        start = plan.start if plan else (shot.get("start") or shot.get("start_seconds"))
        end = plan.end if plan else (shot.get("end") or shot.get("end_seconds"))
        db.execute(
            """INSERT INTO video_project_nodes
            (id,project_id,node_type,node_key,source_start,source_end,
             dependency_fingerprint,status,output_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(project_id,node_key) DO UPDATE SET
              source_start=excluded.source_start,source_end=excluded.source_end,
              dependency_fingerprint=excluded.dependency_fingerprint,
              output_json=excluded.output_json,updated_at=excluded.updated_at""",
            (
                public_id("node"), project_id, "shot", key,
                float(start) if start is not None else None,
                float(end) if end is not None else None,
                plan.dependency_fingerprint if plan else fingerprint(shot),
                "planned", json.dumps(shot, ensure_ascii=False), now, now,
            ),
        )


def update_keyframes(db, project_id: str, frames: list[dict], *, status: str = "ready") -> None:
    """Attach generated image anchors to the corresponding shot nodes."""
    now = time.time()
    for frame in frames:
        try:
            shot_index = int(frame.get("shot_index") or 0)
        except (TypeError, ValueError):
            continue
        if shot_index <= 0:
            continue
        key = f"shot_{shot_index:03d}"
        node = db.execute(
            "SELECT id,output_json FROM video_project_nodes WHERE project_id=? AND node_key=?",
            (project_id, key),
        ).fetchone()
        if not node:
            continue
        output = json.loads(node["output_json"] or "{}")
        output.setdefault("reference_frames", []).append(frame)
        db.execute(
            """UPDATE video_project_nodes SET status=?,output_json=?,updated_at=?
            WHERE id=?""",
            (status, json.dumps(output, ensure_ascii=False), now, node["id"]),
        )
    db.execute("UPDATE video_projects SET updated_at=? WHERE id=?", (now, project_id))


def mark_run(db, task_id: str, *, status: str, result: dict | None = None) -> None:
    now = time.time()
    row = db.execute(
        "SELECT id,project_id FROM video_project_runs WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not row:
        return
    result_json = json.dumps(result or {}, ensure_ascii=False)
    db.execute(
        """UPDATE video_project_runs SET status=?,result_json=?,finished_at=?
        WHERE id=?""",
        (status, result_json, now if status in {"completed", "failed", "cancelled"} else None, row["id"]),
    )
    db.execute(
        """UPDATE video_projects SET current_run_id=?,status=?,updated_at=?
        WHERE id=?""",
        (row["id"], "completed" if status == "completed" else status, now, row["project_id"]),
    )
    if status in {"failed", "cancelled"}:
        db.execute(
            """UPDATE video_project_nodes SET status=?,updated_at=?
            WHERE project_id=? AND run_id=? AND status IN ('planned','generating','running')""",
            (status, now, row["project_id"], row["id"]),
        )


def project_view(db, project_id: str, merchant_id: str) -> dict | None:
    row = db.execute(
        "SELECT * FROM video_projects WHERE id=? AND merchant_id=?",
        (project_id, merchant_id),
    ).fetchone()
    if not row:
        return None
    project = dict(row)
    project["analysis"] = json.loads(project.pop("analysis_json") or "{}")
    project["timeline"] = json.loads(project.pop("timeline_json") or "{}")
    project["treatment"] = json.loads(project.pop("treatment_json") or "{}")
    project["runs"] = [dict(item) for item in db.execute(
        "SELECT * FROM video_project_runs WHERE project_id=? ORDER BY run_number DESC",
        (project_id,),
    ).fetchall()]
    for run in project["runs"]:
        run["selected_nodes"] = json.loads(run.pop("selected_nodes_json") or "[]")
        run["result"] = json.loads(run.pop("result_json") or "{}")
    project["nodes"] = [dict(item) for item in db.execute(
        "SELECT * FROM video_project_nodes WHERE project_id=? ORDER BY node_key",
        (project_id,),
    ).fetchall()]
    for node in project["nodes"]:
        node["output"] = json.loads(node.pop("output_json") or "{}")
    return project

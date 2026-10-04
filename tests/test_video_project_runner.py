import asyncio
import json
import shutil
import subprocess

import commerce
import database
import gongji
import object_storage
import scheduler
import local_storage
from video_project_runner import dispatch_project_take, poll_project_take
from video_projects import create_for_editor


def test_dispatch_creates_a_source_trim_and_independent_take_job(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(database, "DB", tmp_path / "data" / "tasks.sqlite3")
    database.init_db()
    source_path = tmp_path / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
         "color=c=red:s=320x180:r=24:d=10", "-an", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(source_path)],
        check=True, capture_output=True,
    )

    submitted = {}
    uploaded = {}

    def copy_input(item, target, *, output=False):
        shutil.copyfile(source_path, target)

    def upload_path(path, key, content_type):
        uploaded.update(key=key, size=path.stat().st_size, content_type=content_type)

    async def create_job(job_id, merchant_id, worker_input, quality, backend, **kwargs):
        submitted.update(job_id=job_id, worker_input=worker_input, workload=kwargs["workload"])
        return 12345

    monkeypatch.setattr(commerce, "copy_asset", copy_input)
    monkeypatch.setattr(object_storage, "upload_path", upload_path)
    monkeypatch.setattr(object_storage, "presigned_url", lambda key, **kwargs: f"https://storage/{key}")
    monkeypatch.setattr(object_storage, "bucket_name", lambda: "test-bucket")
    monkeypatch.setattr(scheduler, "_input_url", lambda row, item: f"https://storage/{item['key']}")
    monkeypatch.setattr(gongji, "create_job", create_job)

    payload = {
        "mode": "reference", "tier": "720p", "duration": 10, "ratio": "16:9",
        "prompt": "compiled project edit", "seed": 100, "_transfer_backend": "cos",
        "_original_prompt": "replace person", "_tool": "remix",
        "_remix": {"roles": ["person"], "audio": "original", "has_audio": False,
                   "source_seconds": 10, "analysis": {"duration_seconds": 10, "shots": [
                       {"start_seconds": 0, "end_seconds": 4, "text": "opening action"},
                       {"start_seconds": 4, "end_seconds": 10, "text": "closing action"},
                   ]}},
    }
    with database.transaction() as db:
        db.execute("""INSERT INTO merchants
            (id,username,password_hash,recovery_hash,created_at,updated_at)
            VALUES('merchant','merchant','x','x',0,0)""")
        db.execute("""INSERT INTO merchant_tasks
            (id,merchant_id,kind,quality,duration,ratio,title,status,payload,input_objects,
             price_milli,failure_price_milli,created_at,queued_at)
            VALUES('task','merchant','reference','720p',10,'16:9','test','dispatching',?,'[]',1000,100,0,0)""",
                   (json.dumps(payload),))
        inputs = [
            {"id": "source", "storage": "local", "key": "source", "field": "videos", "filename": "source.mp4"},
            {"id": "person", "storage": "local", "key": "person", "field": "images", "filename": "person.jpg"},
        ]
        project_id, run_id = create_for_editor(
            db, merchant_id="merchant", task_id="task", title="test", brief="replace",
            input_objects=inputs, prompt="replace", duration=10, ratio="16:9",
        )
        payload["_project"] = {"project_id": project_id, "run_id": run_id, "mode": "hypit_project"}
        db.execute("UPDATE merchant_tasks SET payload=?,input_objects=? WHERE id='task'",
                   (json.dumps(payload), json.dumps(inputs)))

    with database.connect() as db:
        row = dict(db.execute("SELECT * FROM merchant_tasks WHERE id='task'").fetchone())
    asyncio.run(dispatch_project_take(row))

    assert submitted["job_id"] == "task-take-001"
    assert submitted["workload"] == "remix"
    assert submitted["worker_input"]["duration"] == 5
    assert len(submitted["worker_input"]["media"]) == 2
    assert uploaded["content_type"] == "video/mp4"
    with database.connect() as db:
        task = db.execute("SELECT status,remote_id FROM merchant_tasks WHERE id='task'").fetchone()
        node = db.execute("SELECT status,output_json FROM video_project_nodes WHERE project_id=? AND node_key='take_001'",
                          (project_id,)).fetchone()
    assert tuple(task) == ("running", "12345")
    assert node["status"] == "running"
    assert json.loads(node["output_json"])["job_id"] == "task-take-001"

    async def completed_job_status(remote_id):
        return {"status": "completed", "billing": 0.2}

    monkeypatch.setattr(gongji, "job_status", completed_job_status)
    monkeypatch.setattr(local_storage, "list_task_videos", lambda *args: [{
        "name": "00-output.mp4", "storage": "local",
        "path": "merchant/task-take-001/00-output.mp4", "media": "video", "size_bytes": 4096,
    }])
    with database.connect() as db:
        running_row = dict(db.execute("SELECT * FROM merchant_tasks WHERE id='task'").fetchone())
    asyncio.run(poll_project_take(running_row))
    with database.connect() as db:
        task = db.execute("SELECT status,remote_id,provider_billing FROM merchant_tasks WHERE id='task'").fetchone()
        node = db.execute("SELECT status,output_json FROM video_project_nodes WHERE project_id=? AND node_key='take_001'",
                          (project_id,)).fetchone()
        refreshed = json.loads(db.execute("SELECT payload FROM merchant_tasks WHERE id='task'").fetchone()[0])
    assert task["status"] == "queued" and task["remote_id"] is None
    assert task["provider_billing"] == 0.2
    assert node["status"] == "ready"
    assert json.loads(node["output_json"])["artifact"]["path"].endswith("00-output.mp4")
    assert refreshed["_project"]["take_cursor"] == 1

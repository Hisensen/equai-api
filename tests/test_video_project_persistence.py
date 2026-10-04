import database
from video_projects import create_for_editor, mark_run, project_view, update_analysis


def test_project_keeps_analysis_timeline_and_run_result(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    monkeypatch.setattr(database, "DB", tmp_path / "tasks.sqlite3")
    database.init_db()
    with database.transaction() as db:
        db.execute(
            """INSERT INTO merchants
            (id,username,password_hash,recovery_hash,created_at,updated_at)
            VALUES('merchant','merchant','x','x',0,0)"""
        )
        db.execute(
            """INSERT INTO merchant_tasks
            (id,merchant_id,kind,quality,duration,ratio,title,status,payload,
             price_milli,failure_price_milli,created_at,queued_at)
            VALUES('task','merchant','reference','720p',15,'16:9','test','queued','{}',1000,100,0,0)"""
        )
        project_id, run_id = create_for_editor(
            db, merchant_id="merchant", task_id="task", title="test edit",
            brief="replace person", input_objects=[{"field": "videos", "key": "source.mp4"}],
            prompt="replace person", duration=15, ratio="16:9",
        )
        update_analysis(db, project_id, {
            "duration_seconds": 15,
            "shots": [
                {"start_seconds": 0, "end_seconds": 7, "text": "opening"},
                {"start_seconds": 7, "end_seconds": 15, "text": "closing"},
            ],
        })
        mark_run(db, "task", status="completed", result={"output": "final.mp4"})

    with database.connect() as db:
        project = project_view(db, project_id, "merchant")
    assert project["analysis"]["shots"][0]["text"] == "opening"
    assert project["timeline"]["duration"] == 15
    assert len(project["nodes"]) == 2
    assert project["runs"][0]["id"] == run_id
    assert project["runs"][0]["result"] == {"output": "final.mp4"}
    assert project["status"] == "completed"

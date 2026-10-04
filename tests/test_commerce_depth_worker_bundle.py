from pathlib import Path


def test_depth_worker_failure_report_includes_required_job_id():
    source = Path(__file__).parents[1] / "commerce-depth-worker/app/commerce_depth_job.py"
    text = source.read_text(encoding="utf-8")

    assert '"job_id": os.environ.get("H3_JOB_ID", "")' in text
    assert 'Path(sys.argv[3]).with_suffix(".error").write_text(' in text
    assert 'detail_path = manifest.with_suffix(".error")' in text
    assert '_configured_model_dir.is_relative_to(Path("/models"))' in text


def test_commerce_video_profile_inherits_solattn(monkeypatch):
    import gongji

    monkeypatch.setenv("GONGJI_720_SOLATTN", "tau:1.3:0.0:1.0")
    monkeypatch.delenv("GONGJI_REMIX_720_SOLATTN", raising=False)
    assert gongji._profile("720p", "remix")["solattn"] == "tau:1.3:0.0:1.0"

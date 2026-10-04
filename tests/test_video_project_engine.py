from video_project_engine import plan_shots, reusable_nodes
from video_project_runner import plan_take_groups, shot_prompt, take_generation_prompt


def test_plan_shots_normalizes_and_clamps_timeline():
    shots = plan_shots(
        {"shots": [
            {"start_seconds": -1, "end_seconds": 4, "text": "open"},
            {"start_seconds": 4, "end_seconds": 20, "text": "close"},
        ]},
        15,
    )
    assert [(item.key, item.start, item.end) for item in shots] == [
        ("shot_001", 0.0, 4.0), ("shot_002", 4.0, 15.0)
    ]


def test_reusable_nodes_only_keep_matching_completed_outputs():
    planned = plan_shots({"shots": [{"start": 0, "end": 5}]}, 5)
    reusable, invalidated = reusable_nodes(
        planned,
        {"shot_001": {
            "dependency_fingerprint": planned[0].dependency_fingerprint,
            "status": "ready",
            "output_json": "{\"video\":\"cached.mp4\"}",
        }},
    )
    assert [item.key for item in reusable] == ["shot_001"]
    assert invalidated == []


def test_shot_prompt_is_scoped_to_one_take():
    value = shot_prompt("close-up, hand reaches for product", "keep original audio", 4.5, "16:9")
    assert "independent shot Take" in value
    assert "4.500s" in value
    assert "close-up" in value


def test_take_groups_follow_timeline_and_h3_duration_limits():
    groups = plan_take_groups({"shots": [
        {"start_seconds": 0, "end_seconds": 4, "text": "opening"},
        {"start_seconds": 4, "end_seconds": 9, "text": "middle"},
        {"start_seconds": 9, "end_seconds": 15, "text": "closing"},
    ]}, 15)
    assert len(groups) == 2
    assert groups[0]["start"] == 0
    assert groups[-1]["end"] == 15
    assert all(5 <= item["request_duration"] <= 15 for item in groups)


def test_editor_projects_split_unbounded_source_video_into_bounded_takes():
    groups = plan_take_groups({"shots": [{"start_seconds": 0, "end_seconds": 75, "text": "one continuous shot"}]}, 75)
    assert groups[0]["start"] == 0
    assert groups[-1]["end"] == 75
    assert all(item["duration"] <= 9 for item in groups)
    assert all(5 <= item["request_duration"] <= 15 for item in groups)
    assert all(left["end"] == right["start"] for left, right in zip(groups, groups[1:]))


def test_take_prompt_carries_local_source_range_and_contract():
    value = take_generation_prompt("compiled edit", "close-up hand action", "keep sleeve", 2, 7, 5)
    assert "2.000s to 7.000s" in value
    assert "close-up hand action" in value
    assert "keep sleeve" in value

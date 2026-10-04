from unittest.mock import patch

import video_project_api


def test_project_media_refreshes_expired_local_worker_input_link():
    stored = {
        "id": "ast_example",
        "storage": "local",
        "url": "https://equai.vip/internal/worker/tasks/task_example/inputs/ast_example?expires=1&signature=expired",
    }
    with patch("transfer_storage.signed_input_url", return_value="https://equai.vip/fresh") as sign:
        assert video_project_api._sign(stored, "task_example") == "https://equai.vip/fresh"
    sign.assert_called_once_with("task_example", "ast_example", expires=24 * 3600)

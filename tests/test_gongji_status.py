from __future__ import annotations

import asyncio
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

class FakeClient:
    def __init__(
        self, job_state: str, node_status: str = "End", reason=None,
        *, succeeded_pods: int = 1, failed_pods: int = 0,
    ):
        self.job_state = job_state
        self.node_status = node_status
        self.reason = reason
        self.succeeded_pods = succeeded_pods
        self.failed_pods = failed_pods

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, url, **_):
        if url.endswith("/api/deployment/task/points"):
            data = {
                "results": [{
                    "status": self.node_status,
                    "runing_time": 42,
                    "billing_value": 12000,
                    "containers": [{"reason": self.reason}],
                }],
            }
        else:
            data = {"job_status": {
                "status": self.job_state,
                "succeeded_pods": self.succeeded_pods,
                "failed_pods": self.failed_pods,
            }}
        return httpx.Response(200, json={"code": "0000", "data": data})


class GongjiStatusTest(unittest.TestCase):
    def test_gpu_media_uses_tls_valid_direct_origin(self) -> None:
        import gongji

        original = {"media": [
            {"url": "https://equai.vip/internal/worker/tasks/t/inputs/a?signature=abc"},
            {"url": "https://other.example/image.png"},
        ]}
        with patch.dict(os.environ, {
            "H3_TRANSFER_BASE_URL": "https://equai.vip",
            "H3_TRANSFER_HOST_IP": "129.204.50.9",
        }):
            direct = gongji._direct_local_media_urls(original, "local")
            untouched = gongji._direct_local_media_urls(original, "cos")
        self.assertEqual(
            direct["media"][0]["url"],
            "https://129.204.50.9/internal/worker/tasks/t/inputs/a?signature=abc",
        )
        self.assertEqual(direct["media"][1], original["media"][1])
        self.assertEqual(untouched, original)
        self.assertEqual(original["media"][0]["url"].split("/")[2], "equai.vip")

    def status(
        self, job_state: str, node_status: str = "End", reason=None,
        *, succeeded_pods: int = 1, failed_pods: int = 0,
    ) -> dict:
        import gongji

        fake = FakeClient(
            job_state, node_status, reason,
            succeeded_pods=succeeded_pods, failed_pods=failed_pods,
        )
        with (
            patch.object(gongji, "TOKEN", "test-token"),
            patch.object(gongji.httpx, "AsyncClient", return_value=fake),
        ):
            return asyncio.run(gongji.job_status(123))

    def test_ambiguous_end_uses_failed_job_state(self) -> None:
        result = self.status("Failed")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "Failed")

    def test_ambiguous_end_accepts_complete_job_state(self) -> None:
        result = self.status("Complete")
        self.assertEqual(result["status"], "completed")

    def test_end_with_completed_container_uses_complete_job_state(self) -> None:
        result = self.status("Complete", "End", "Completed")
        self.assertEqual(result["status"], "completed")

    def test_conflicting_failed_node_and_completed_reason_uses_job_state(self) -> None:
        result = self.status("Complete", "Failed", "Completed")
        self.assertEqual(result["status"], "completed")

    def test_manual_terminal_with_failed_pod_is_not_misreported_completed(self) -> None:
        result = self.status("Manual", succeeded_pods=0, failed_pods=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "Manual")

    def test_unknown_terminal_without_successful_pod_is_failed(self) -> None:
        result = self.status("Manual", succeeded_pods=0, failed_pods=0)
        self.assertEqual(result["status"], "failed")

    def test_complete_with_only_failed_pods_is_failed(self) -> None:
        result = self.status("Complete", succeeded_pods=0, failed_pods=1)
        self.assertEqual(result["status"], "failed")

    def test_stale_running_node_with_manual_terminal_job_is_failed(self) -> None:
        result = self.status(
            "Manual", "Running", succeeded_pods=0, failed_pods=0,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "Manual")

    def test_stale_running_node_with_completed_job_is_completed(self) -> None:
        result = self.status("Complete", "Running", succeeded_pods=1)
        self.assertEqual(result["status"], "completed")

    def test_succeeded_node_is_completed(self) -> None:
        result = self.status("Complete", "Succeeded", "Completed")
        self.assertEqual(result["status"], "completed")

    def test_attention_mode_can_be_selected_per_quality_tier(self) -> None:
        import gongji

        with patch.dict(os.environ, {
            "GONGJI_720_SOLATTN": "tau:1.3:0.0:1.0",
            "GONGJI_1080_SOLATTN": "off",
            "GONGJI_2K_SOLATTN": "off",
        }):
            self.assertNotEqual(gongji._profile("720p")["solattn"], "off")
            self.assertEqual(gongji._profile("1080p")["solattn"], "off")
            self.assertEqual(gongji._profile("2k")["solattn"], "off")

    def test_remix_profile_uses_dedicated_region_and_inherits_worker(self) -> None:
        import gongji

        with patch.dict(os.environ, {
            "GONGJI_720_IMAGE": "registry.example/validated-720:latest",
            "GONGJI_720_STORAGE_ID": "2593",
            "GONGJI_720_REGION_NAME": "福建四区",
            "GONGJI_720_GPU_NAME": "4090",
            "GONGJI_REMIX_720_REGION_NAME": "浙江四区",
            "GONGJI_REMIX_720_STORAGE_ID": "6001",
            "GONGJI_REMIX_720_GPU_NAME": "4090-48G",
            "GONGJI_REMIX_720_GPU_COUNT": "1",
        }, clear=False):
            standard = gongji._profile("720p")
            remix = gongji._profile("720p", "remix")
        self.assertEqual(standard["region"], "福建四区")
        self.assertEqual(remix["region"], "浙江四区")
        self.assertEqual(remix["storage_id"], 6001)
        self.assertEqual(remix["gpu"], "4090-48G")
        self.assertEqual(remix["image"], standard["image"])

    def test_unknown_resource_pool_is_rejected(self) -> None:
        import gongji

        with self.assertRaises(gongji.GongjiError):
            gongji._profile("720p", "unknown")

    def test_remix_analysis_uses_the_remix_resource_pool(self) -> None:
        import gongji

        create = AsyncMock(return_value=123)
        with patch.object(gongji, "create_job", create), patch.dict(
            os.environ, {"GONGJI_QWEN_IMAGE": "registry.example/qwen:latest"},
        ):
            task_id = asyncio.run(gongji.create_remix_analysis_job(
                "vid_test", "merchant_test", {"source": {}}, "cos",
            ))
        self.assertEqual(task_id, 123)
        self.assertEqual(create.await_args.kwargs["workload"], "remix")
        self.assertEqual(create.await_args.kwargs["task_prefix"], "qwen-remix")

    def test_commerce_remix_uses_one_worker_job_with_runtime_overlay(self) -> None:
        import gongji

        from tempfile import TemporaryDirectory

        async def invoke(root: str) -> None:
            runtime = Path(root) / "runtime"
            runtime.mkdir()
            overlay = runtime / "commerce-depth-worker.tar.gz"
            model = runtime / "depth-anything-v2-small.tar.gz"
            overlay.write_bytes(b"overlay")
            model.write_bytes(b"model")
            create = AsyncMock(return_value=321)
            with patch.object(gongji, "create_job", create), patch(
                "transfer_storage.signed_runtime_url",
                side_effect=lambda job, path, expires, **kwargs: f"{kwargs.get('base_url', 'https://internal')}/{job}/{path}?expires={expires}",
            ), patch.dict(os.environ, {
                "H3_LOCAL_RUNTIME_DIR": str(runtime),
                "H3_LOCAL_COMMERCE_DEPTH_OVERLAY_PATH": str(overlay),
                "H3_LOCAL_COMMERCE_DEPTH_MODEL_PATH": str(model),
                "H3_COMMERCE_TRANSFER_BASE_URL": "https://129.204.50.9",
                "H3_TRANSFER_SECRET": "test-transfer-secret-0123456789abcdef",
            }, clear=False):
                task_id = await gongji.create_commerce_remix_job(
                    "job-1", "merchant-1", {"_commerce_depth_worker": True}, "720p", "local",
                )
            self.assertEqual(task_id, 321)
            self.assertEqual(create.await_args.kwargs["task_prefix"], "h3-commerce")
            self.assertEqual(create.await_args.kwargs["workload"], "remix")
            self.assertIn("commerce-depth-worker.tar.gz", create.await_args.kwargs["start_script"]["args"][1])
            self.assertEqual(len(create.await_args.kwargs["extra_env"]), 4)
            self.assertEqual(create.await_args.kwargs["extra_env"][0]["value"], "1")
            self.assertEqual(
                create.await_args.kwargs["extra_env"][3]["value"],
                "/tmp/h3-commerce-depth/depth-anything-v2-small",
            )
            start_script = create.await_args.kwargs["start_script"]["args"][1]
            subprocess.run(["bash", "-n", "-c", start_script], check=True, timeout=10)
            subprocess.run(["sh", "-n", "-c", start_script], check=True, timeout=10)
            self.assertIn("report_failure", start_script)
            with patch.dict(os.environ, {"H3_TRANSFER_SECRET": "test-transfer-secret-0123456789abcdef"}):
                targets = gongji._output_upload_targets(
                    "job-1", "merchant-1", 3600, "local", base_url="https://129.204.50.9",
                )
                result_target = gongji._result_upload_target(
                    "job-1", "merchant-1", 3600, "local", base_url="https://129.204.50.9",
                )
            self.assertTrue(all(target["url"].startswith("https://129.204.50.9/") for target in targets))
            self.assertTrue(result_target["url"].startswith("https://129.204.50.9/"))

        with TemporaryDirectory() as root:
            asyncio.run(invoke(root))


if __name__ == "__main__":
    unittest.main()

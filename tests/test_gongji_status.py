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


class FakeCreateClient:
    def __init__(self):
        self.payload = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def post(self, _url, **kwargs):
        self.payload = kwargs["json"]
        return httpx.Response(200, json={"code": "0000", "data": {"task_id": 321}})


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

    def test_digital_human_uses_baked_image_without_runtime_download(self) -> None:
        import gongji

        async def check(mode: str) -> tuple:
            profile = {**gongji._profile("720p"), "image": "digital-human-image"}
            with (
                patch.dict(os.environ, {
                    "GONGJI_DIGITAL_HUMAN_IMAGE": "digital-human-image",
                    "GONGJI_DIGITAL_HUMAN_OVERLAY_KEY": "digital-human-worker.tar.gz",
                    "GONGJI_LONG_IMAGE": "existing-long-image",
                    "GONGJI_LONG_OVERLAY_KEY": "h3-long-worker.tar.gz",
                }),
                patch.object(gongji, "_select_available_profile", new_callable=AsyncMock,
                             return_value=(profile, {"resource": {}, "mark": "mark"})),
                patch.object(gongji, "_runtime_bundle_url", return_value="https://runtime.test/bundle") as runtime_bundle_url,
                patch.object(gongji, "create_job", new_callable=AsyncMock, return_value=42) as create_job,
            ):
                result = await gongji.create_long_job(
                    "test-job", "test-merchant", {"mode": mode}, "local"
                )
            self.assertEqual(result, 42)
            selected_profile = create_job.await_args.kwargs.get("selected_profile")
            return (
                create_job.await_args.kwargs["service_image"],
                selected_profile["image"] if selected_profile else None,
                runtime_bundle_url.call_count,
                create_job.await_args.kwargs.get("start_script"),
                create_job.await_args.kwargs["extra_env"],
            )

        self.assertEqual(
            asyncio.run(check("digital_human_workflow")),
            (
                "digital-human-image", "digital-human-image", 0, None,
                [{"name": "H3_LONG_MAX_DURATION_SECONDS", "value": "45"}],
            ),
        )
        long_video = asyncio.run(check("long_text"))
        self.assertEqual(long_video[0:3], ("existing-long-image", None, 1))
        self.assertIn("H3_LONG_OVERLAY_URL", long_video[4][1]["name"])
        self.assertEqual(long_video[4][1]["value"], "https://runtime.test/bundle")
        self.assertEqual(long_video[3]["command"], ["/bin/bash"])

    def test_digital_human_skips_automatic_standard_overlay(self) -> None:
        import gongji

        client = FakeCreateClient()
        with (
            patch.object(gongji, "TOKEN", "test-token"),
            patch.object(gongji, "_resource", new_callable=AsyncMock, return_value={"resource": {}, "mark": "mark"}),
            patch.object(gongji, "_output_upload_targets", return_value=[]),
            patch.object(gongji, "_result_upload_target", return_value={"key": "_job_result.json", "url": "https://upload.invalid"}),
            patch.object(gongji.httpx, "AsyncClient", return_value=client),
            patch.object(gongji.os.path, "isfile", return_value=True),
            patch.dict(os.environ, {
                "GONGJI_720_IMAGE": "standard-image",
                "GONGJI_720_STORAGE_ID": "2593",
                "GONGJI_STANDARD_OVERLAY_KEY": "standard-worker.tar.gz",
                "H3_LOCAL_RUNTIME_OVERLAY_PATH": "/data/runtime/h3-worker.tar.gz",
            }),
        ):
            result = asyncio.run(gongji.create_job(
                "dh-job", "merchant", {"mode": "digital_human_workflow"}, "720p", "local",
                service_image="baked-dh-image",
            ))

        service = client.payload["services"][0]
        env_names = {item["name"] for item in service["env"]}
        self.assertEqual(result, 321)
        self.assertEqual(service["service_image"], "baked-dh-image")
        self.assertNotIn("start_script", service)
        self.assertNotIn("H3_STANDARD_OVERLAY_URL", env_names)
        self.assertNotIn("H3_LONG_OVERLAY_URL", env_names)

    def test_baked_standard_worker_skips_tencent_runtime_download(self) -> None:
        import gongji

        client = FakeCreateClient()
        with (
            patch.object(gongji, "TOKEN", "test-token"),
            patch.object(gongji, "_select_available_profile", new_callable=AsyncMock, return_value=(
                {
                    **gongji._profile("1080p"),
                    "image": "worker:standard-v9-r50-1080-baked-runtime-20261007-v1",
                },
                {"resource": {}, "mark": "mark"},
            )),
            patch.object(gongji, "_output_upload_targets", return_value=[]),
            patch.object(gongji, "_result_upload_target", return_value={"key": "_job_result.json", "url": "https://upload.invalid"}),
            patch.object(gongji.httpx, "AsyncClient", return_value=client),
            patch.object(gongji.os.path, "isfile", return_value=True),
            patch.object(gongji, "_runtime_bundle_url", return_value="https://runtime.test/bundle") as runtime_bundle_url,
            patch.dict(os.environ, {
                "GONGJI_1080_IMAGE": "worker:standard-v9-r50-1080-baked-runtime-20261007-v1",
                "GONGJI_1080_STORAGE_ID": "2593",
                "GONGJI_STANDARD_OVERLAY_KEY": "standard-worker.tar.gz",
                "H3_LOCAL_RUNTIME_OVERLAY_PATH": "/data/runtime/h3-worker.tar.gz",
            }),
        ):
            result = asyncio.run(gongji.create_job(
                "highres-job", "merchant", {"mode": "first_last_frame"}, "1080p", "local",
            ))

        service = client.payload["services"][0]
        env_names = {item["name"] for item in service["env"]}
        self.assertEqual(result, 321)
        self.assertIn("baked-runtime", service["service_image"])
        self.assertEqual(runtime_bundle_url.call_count, 0)
        self.assertNotIn("start_script", service)
        self.assertNotIn("H3_STANDARD_OVERLAY_URL", env_names)

    def test_sage_attention_compatibility_is_scoped_to_5090(self) -> None:
        import gongji

        async def submit(gpu_name: str) -> str:
            client = FakeCreateClient()
            profile = {**gongji._profile("2k"), "gpu": gpu_name, "image": "test-worker"}
            with (
                patch.object(gongji, "TOKEN", "test-token"),
                patch.object(gongji, "_select_available_profile", new_callable=AsyncMock,
                             return_value=(profile, {"resource": {}, "mark": "mark"})),
                patch.object(gongji, "_output_upload_targets", return_value=[]),
                patch.object(gongji, "_result_upload_target", return_value={"key": "result", "url": "https://upload.invalid"}),
                patch.object(gongji.httpx, "AsyncClient", return_value=client),
                patch.dict(os.environ, {
                    "GONGJI_2K_IMAGE": "test-worker",
                    "GONGJI_2K_STORAGE_ID": "2593",
                }, clear=False),
            ):
                os.environ.pop("H3_USE_SAGE_ATTENTION", None)
                os.environ.pop("H3_5090_USE_SAGE_ATTENTION", None)
                await gongji.create_job("job", "merchant", {"mode": "video"}, "2k")
            return next(
                item["value"] for item in client.payload["services"][0]["env"]
                if item["name"] == "H3_USE_SAGE_ATTENTION"
            )

        self.assertEqual(asyncio.run(submit("4090-48G")), "1")
        self.assertEqual(asyncio.run(submit("5090")), "0")

    def test_all_baked_standard_tiers_are_marked_as_self_contained(self) -> None:
        import gongji

        for quality in ("720", "1080", "2k"):
            self.assertTrue(gongji._image_has_baked_standard_runtime(
                f"harbor.suanleme.cn/h3platform/h3-video-worker:standard-{quality}-baked-runtime-v1"
            ))
        self.assertFalse(gongji._image_has_baked_standard_runtime(
            "harbor.suanleme.cn/h3platform/h3-video-worker:standard-v9-r50-4090-720-ref1-6step-io"
        ))

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

    def test_music_job_keeps_the_music_image_entrypoint(self) -> None:
        import gongji

        create = AsyncMock(return_value=456)
        with patch.object(gongji, "create_job", create), patch.dict(os.environ, {
            "GONGJI_MUSIC_IMAGE": "registry.example/music-worker:test",
            "GONGJI_MUSIC_TIMEOUT_SEC": "3600",
        }, clear=False):
            task_id = asyncio.run(gongji.create_music_job(
                "mus_test", "merchant_test", {"lyrics": "test"}, "local",
            ))
        self.assertEqual(task_id, 456)
        self.assertEqual(create.await_args.kwargs["service_image"], "registry.example/music-worker:test")
        self.assertEqual(create.await_args.kwargs["start_script"], {
            "command": ["python3"], "args": ["/app/worker.py"],
        })
        self.assertEqual(create.await_args.kwargs["task_prefix"], "h3-music")

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


class DigitalHumanImageRoutingTest(unittest.TestCase):
    """A missing digital-human image must fail loudly, never borrow the long-video one.

    The silent fallback is what produced the 2026-10-06 failures: digital-human
    jobs ran on the r38 long-video worker, where 16:9 requested a model file the
    current Comfy build does not list and 9:16 was rejected by its 16:9-only
    validation. Both only ever surfaced as "GPU 任务失败: Error".
    """

    def _submit(self, mode: str) -> str:
        import asyncio

        import gongji

        try:
            asyncio.run(gongji.create_long_job("job", "merchant", {"mode": mode}))
        except gongji.GongjiError as exc:
            return str(exc)
        return ""

    def test_digital_human_refuses_to_borrow_the_long_video_image(self) -> None:
        with patch.dict(os.environ, {"GONGJI_DIGITAL_HUMAN_IMAGE": "",
                                     "GONGJI_LONG_IMAGE": "harbor/long:r38"}):
            self.assertIn("数字人", self._submit("digital_human_workflow"))

    def test_long_video_still_uses_its_own_image(self) -> None:
        with patch.dict(os.environ, {"GONGJI_DIGITAL_HUMAN_IMAGE": "",
                                     "GONGJI_LONG_IMAGE": ""}):
            self.assertIn("长视频", self._submit("long_text"))

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

import httpx

import gongji


def resource(region: str, inventory: int, gpu: str, cpu: int = 16) -> dict:
    return {
        "region_name": region,
        "inventory": inventory,
        "mark": {
            "resource": {"gpu_name": gpu, "gpu_count": 1, "cpu_cores": cpu},
            "mark": f"mark-{region}",
        },
    }


class FakeResourceClient:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.created_payload = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, _url, **_kwargs):
        return httpx.Response(200, json={
            "code": "0000", "data": {"results": [{"region_map": {
                str(index): row for index, row in enumerate(self.rows)
            }}]},
        })

    async def post(self, _url, **kwargs):
        self.created_payload = kwargs["json"]
        return httpx.Response(200, json={"code": "0000", "data": {"task_id": 123}})


class GongjiResourceFailoverTest(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "image": "zhejiang-worker", "storage_id": 2593,
            "region": "浙江四区", "gpu": "4090", "gpu_count": 1,
            "cpu_cores": None, "comfy_args": "", "solattn": "tau:1.3:0.0:1.0",
            "estimated_sec": 600, "timeout_sec": 2400,
        }

    def _create(self, rows: list[dict], env: dict[str, str]):
        client = FakeResourceClient(rows)
        patches = (
            patch.object(gongji, "TOKEN", "test-token"),
            patch.object(gongji, "_profile", return_value=self.profile),
            patch.object(gongji, "_output_upload_targets", return_value=[]),
            patch.object(gongji, "_result_upload_target", return_value={"key": "result", "url": "https://upload.invalid"}),
            patch.object(gongji.httpx, "AsyncClient", return_value=client),
            patch.dict(os.environ, env, clear=False),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return client

    def test_uses_primary_when_both_regions_have_inventory(self):
        client = self._create([
            resource("浙江四区", 2, "4090"),
            resource("天津一区", 4, "5090"),
        ], {
            "GONGJI_720_BACKUP_REGION_NAME": "天津一区",
            "GONGJI_720_BACKUP_GPU_NAME": "5090",
            "GONGJI_720_BACKUP_IMAGE": "tianjin-worker",
            "GONGJI_720_BACKUP_STORAGE_ID": "3001",
        })

        result = asyncio.run(gongji.create_job("job-1", "merchant", {}, "720p"))

        self.assertEqual(result, 123)
        self.assertEqual(client.created_payload["resources"][0]["region_name"], "浙江四区")

    def test_falls_back_to_tianjin_only_when_primary_has_no_inventory(self):
        client = self._create([
            resource("浙江四区", 0, "4090"),
            resource("天津一区", 1, "5090"),
        ], {
            "GONGJI_720_BACKUP_REGION_NAME": "天津一区",
            "GONGJI_720_BACKUP_GPU_NAME": "5090",
            "GONGJI_720_BACKUP_CPU_CORES": "16",
            "GONGJI_720_BACKUP_IMAGE": "tianjin-worker",
            "GONGJI_720_BACKUP_STORAGE_ID": "3001",
        })

        asyncio.run(gongji.create_job("job-2", "merchant", {}, "720p"))

        payload = client.created_payload
        self.assertEqual(payload["resources"][0]["region_name"], "天津一区")
        self.assertEqual(payload["resources"][0]["resource"]["gpu_name"], "5090")
        service = payload["services"][0]
        self.assertEqual(service["service_image"], "tianjin-worker")
        self.assertEqual(service["storage_config"][0]["storage_id"], 3001)
        env = {item["name"]: item["value"] for item in service["env"]}
        self.assertEqual(env["H3_SOLATTN"], "tau:1.3:0.0:1.0")

    def test_5090_backup_can_explicitly_opt_in_to_solattn(self):
        client = self._create([
            resource("浙江四区", 0, "4090"),
            resource("天津一区", 1, "5090"),
        ], {
            "GONGJI_720_BACKUP_REGION_NAME": "天津一区",
            "GONGJI_720_BACKUP_GPU_NAME": "5090",
            "GONGJI_720_BACKUP_CPU_CORES": "16",
            "GONGJI_720_BACKUP_IMAGE": "tianjin-worker",
            "GONGJI_720_BACKUP_STORAGE_ID": "3001",
            "GONGJI_720_BACKUP_SOLATTN": "tau:1.3:0.0:1.0",
        })

        asyncio.run(gongji.create_job("job-5090-solattn", "merchant", {}, "720p"))

        env = {
            item["name"]: item["value"]
            for item in client.created_payload["services"][0]["env"]
        }
        self.assertEqual(env["H3_SOLATTN"], "tau:1.3:0.0:1.0")

    def test_no_inventory_returns_retry_message_without_creating_job(self):
        client = self._create([
            resource("浙江四区", 0, "4090"),
            resource("天津一区", 0, "5090"),
        ], {
            "GONGJI_720_BACKUP_REGION_NAME": "天津一区",
            "GONGJI_720_BACKUP_GPU_NAME": "5090",
            "GONGJI_720_BACKUP_IMAGE": "tianjin-worker",
            "GONGJI_720_BACKUP_STORAGE_ID": "3001",
        })

        with self.assertRaisesRegex(gongji.NoGpuAvailableError, "^暂时无GPU，稍后重试$"):
            asyncio.run(gongji.create_job("job-3", "merchant", {}, "720p"))

        self.assertIsNone(client.created_payload)

    def test_commerce_remix_can_use_its_own_tianjin_backup_profile(self):
        image = (
            "harborpush.suanleme.cn/h3platform/h3-video-worker:"
            "standard-v9-r50-5090-2k-ref1-6step-denoise02-baked-runtime-20261014-v12"
        )
        client = self._create([
            resource("浙江四区", 0, "4090"),
            resource("天津一区", 1, "5090"),
        ], {
            "GONGJI_REMIX_720_BACKUP_REGION_NAME": "天津一区",
            "GONGJI_REMIX_720_BACKUP_GPU_NAME": "5090",
            "GONGJI_REMIX_720_BACKUP_CPU_CORES": "16",
            "GONGJI_REMIX_720_BACKUP_IMAGE": image,
            "GONGJI_REMIX_720_BACKUP_STORAGE_ID": "3001",
        })

        asyncio.run(gongji.create_job(
            "commerce-job", "merchant", {}, "720p", task_prefix="h3-commerce",
            workload="remix", start_script={"command": ["/bin/sh"]},
        ))

        payload = client.created_payload
        self.assertEqual(payload["resources"][0]["region_name"], "天津一区")
        self.assertEqual(payload["services"][0]["service_image"], image)
        self.assertEqual(payload["services"][0]["storage_config"][0]["storage_id"], 3001)

    def test_commerce_5090_fallback_does_not_change_standard_video_profile(self):
        client = self._create([
            resource("浙江四区", 1, "4090"),
            resource("天津一区", 1, "5090"),
        ], {
            "GONGJI_REMIX_720_BACKUP_REGION_NAME": "天津一区",
            "GONGJI_REMIX_720_BACKUP_GPU_NAME": "5090",
            "GONGJI_REMIX_720_BACKUP_IMAGE": "tianjin-commerce-worker",
            "GONGJI_REMIX_720_BACKUP_STORAGE_ID": "3001",
        })

        asyncio.run(gongji.create_job("video-job", "merchant", {}, "720p"))

        self.assertEqual(client.created_payload["resources"][0]["region_name"], "浙江四区")
        self.assertEqual(client.created_payload["resources"][0]["resource"]["gpu_name"], "4090")
        self.assertEqual(client.created_payload["services"][0]["service_image"], "zhejiang-worker")

    def test_digital_human_uses_its_own_tianjin_5090_image_as_fallback(self):
        client = self._create([
            resource("浙江四区", 0, "4090"),
            resource("天津一区", 1, "5090"),
        ], {
            "GONGJI_DIGITAL_HUMAN_IMAGE": "zhejiang-dh-worker",
            "GONGJI_DIGITAL_HUMAN_BACKUP_REGION_NAME": "天津一区",
            "GONGJI_DIGITAL_HUMAN_BACKUP_GPU_NAME": "5090",
            "GONGJI_DIGITAL_HUMAN_BACKUP_CPU_CORES": "16",
            "GONGJI_DIGITAL_HUMAN_BACKUP_IMAGE": "tianjin-dh-worker",
            "GONGJI_DIGITAL_HUMAN_BACKUP_STORAGE_ID": "3001",
        })

        result = asyncio.run(gongji.create_long_job(
            "dh-fallback", "merchant", {"mode": "digital_human_workflow"}, "local",
        ))

        payload = client.created_payload
        self.assertEqual(result, 123)
        self.assertEqual(payload["resources"][0]["region_name"], "天津一区")
        self.assertEqual(payload["resources"][0]["resource"]["gpu_name"], "5090")
        service = payload["services"][0]
        self.assertEqual(service["service_image"], "tianjin-dh-worker")
        self.assertEqual(service["storage_config"][0]["storage_id"], 3001)
        self.assertNotIn("start_script", service)

    def test_digital_human_keeps_zhejiang_4090_as_primary(self):
        client = self._create([
            resource("浙江四区", 1, "4090"),
            resource("天津一区", 1, "5090"),
        ], {
            "GONGJI_DIGITAL_HUMAN_IMAGE": "zhejiang-dh-worker",
            "GONGJI_DIGITAL_HUMAN_BACKUP_REGION_NAME": "天津一区",
            "GONGJI_DIGITAL_HUMAN_BACKUP_GPU_NAME": "5090",
            "GONGJI_DIGITAL_HUMAN_BACKUP_IMAGE": "tianjin-dh-worker",
            "GONGJI_DIGITAL_HUMAN_BACKUP_STORAGE_ID": "3001",
        })

        asyncio.run(gongji.create_long_job(
            "dh-primary", "merchant", {"mode": "digital_human_workflow"}, "local",
        ))

        self.assertEqual(client.created_payload["resources"][0]["region_name"], "浙江四区")
        self.assertEqual(client.created_payload["resources"][0]["resource"]["gpu_name"], "4090")
        self.assertEqual(client.created_payload["services"][0]["service_image"], "zhejiang-dh-worker")


if __name__ == "__main__":
    unittest.main()

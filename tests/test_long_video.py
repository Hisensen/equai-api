from __future__ import annotations

import unittest
import json
import sys
import types
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from long_video_api import (
    LongVideoRequest,
    digital_human_segment_count, segment_count, validate_long_request,
)
from worker_long.long_video import _digital_human_workflow_graph, long_chunk_workflow, long_workflow, segment_lengths


class LongVideoApiTest(unittest.TestCase):
    def test_active_digital_human_graph_matches_workflow_verbatim(self) -> None:
        workflow_path = Path(__file__).parents[1] / "worker_long/workflows/digital_human_image_audio_v1.json"
        original = json.loads(workflow_path.read_text())
        graph = _digital_human_workflow_graph(
            "digital_human_image_audio_v1", "portrait.png", "voice.wav", 1.0
        )
        self.assertEqual(original["136"]["inputs"]["prompt"], graph["136"]["inputs"]["prompt"])
        self.assertEqual(original["276"]["inputs"]["prompt"], graph["276"]["inputs"]["prompt"])
        self.assertEqual(original["261"]["inputs"]["seed"], graph["261"]["inputs"]["seed"])
        self.assertEqual(original["284"]["inputs"]["seed"], graph["284"]["inputs"]["seed"])
        self.assertEqual(1.0, original["115"]["inputs"]["megapixels"])
        self.assertEqual(1.0, graph["115"]["inputs"]["megapixels"])
        self.assertEqual("16:9 (Widescreen)", graph["115"]["inputs"]["aspect_ratio"])
        portrait_graph = _digital_human_workflow_graph(
            "digital_human_image_audio_v1", "portrait.png", "voice.wav", 1.0, "9:16"
        )
        self.assertEqual("9:16 (Portrait Widescreen)", portrait_graph["115"]["inputs"]["aspect_ratio"])
        self.assertEqual(1.0, portrait_graph["115"]["inputs"]["megapixels"])
        extra_prompt = "保持人物衣着和背景不变，轻轻点头。"
        prompted_graph = _digital_human_workflow_graph(
            "digital_human_image_audio_v1", "portrait.png", "voice.wav", 1.0,
            "9:16", "template\n\nAdditional user direction (verbatim):\n" + extra_prompt,
        )
        self.assertEqual(prompted_graph["136"]["inputs"]["prompt"], prompted_graph["276"]["inputs"]["prompt"])
        self.assertIn(extra_prompt, prompted_graph["136"]["inputs"]["prompt"])
        self.assertEqual(
            "official/minimax_h3_video_vae_int8_convrot.safetensors",
            graph["119"]["inputs"]["vae_name"],
        )
        self.assertEqual(
            "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
            graph["128"]["inputs"]["clip_name"],
        )
        self.assertEqual("portrait.png", graph["268"]["inputs"]["image"])
        self.assertEqual("voice.wav", graph["232"]["inputs"]["audio"])
        for node in ("261", "284"):
            self.assertEqual("minimax_h3_latent_upscaler_3d_fp16.safetensors", graph[node]["inputs"]["upscaler_model"])
        self.assertNotIn("195", graph)
        self.assertEqual("CreateVideo", graph["9000"]["class_type"])
        self.assertEqual(["122", 0], graph["9000"]["inputs"]["images"])
        self.assertEqual(["232", 0], graph["9000"]["inputs"]["audio"])
        self.assertEqual("SaveVideo", graph["9001"]["class_type"])
    def test_segment_count_matches_validated_motion_context_examples(self) -> None:
        self.assertEqual(segment_count(15), 1)
        self.assertEqual(segment_count(15.024), 1)
        self.assertEqual(segment_count(15.7), 1)
        self.assertEqual(segment_count(15.8), 2)
        self.assertEqual(segment_count(23), 2)
        self.assertEqual(segment_count(32), 3)
        self.assertEqual(segment_count(60), 4)
        self.assertEqual(segment_count(20), 2)
        self.assertEqual(segment_count(30), 2)
        self.assertEqual(segment_count(45), 3)
        self.assertEqual(segment_count(90), 6)

    def test_prompt_count_must_match_duration(self) -> None:
        body = LongVideoRequest(
            duration=45,
            ratio="16:9",
            segment_prompts=["first", "second"],
        )
        with self.assertRaises(HTTPException) as caught:
            validate_long_request(body)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("需要 3 段", str(caught.exception.detail))

    def test_exact_prompt_count_is_accepted(self) -> None:
        body = LongVideoRequest(
            duration=45,
            ratio="9:16",
            segment_prompts=[f"segment {index}" for index in range(3)],
        )
        self.assertEqual(validate_long_request(body), 3)

    def test_duration_above_configured_maximum_is_rejected(self) -> None:
        body = LongVideoRequest(
            duration=61,
            ratio="16:9",
            segment_prompts=["segment"] * 5,
        )
        with self.assertRaises(HTTPException) as caught:
            validate_long_request(body)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("16–60 秒", str(caught.exception.detail))

    def test_worker_frame_plan_covers_and_only_slightly_overrenders(self) -> None:
        for duration in (1, 8, 15, 16, 20, 23, 30, 32, 45, 60, 90, 180):
            lengths, rendered = segment_lengths(duration)
            self.assertEqual(len(lengths), segment_count(duration))
            self.assertGreaterEqual(rendered, duration * 24)
            self.assertLess(rendered - duration * 24, 17)
            self.assertTrue(all((length - 5) % 17 == 0 for length in lengths))

    def test_worker_uses_one_h3_segment_for_audio_up_to_15_seconds(self) -> None:
        for duration in (1, 8, 15):
            lengths, _ = segment_lengths(duration)
            self.assertEqual(1, len(lengths))

    def test_worker_rejects_retired_dynamic_avatar_mode(self) -> None:
        runpod = types.ModuleType("runpod")
        network = types.ModuleType("network_bootstrap")
        network.install_transfer_host_mapping = lambda: None
        storage = types.ModuleType("object_storage")
        storage.upload_outputs = lambda *_args, **_kwargs: None
        audio = types.ModuleType("audio_utils")
        audio._preserve_source_audio = lambda *_args, **_kwargs: None
        with patch.dict(sys.modules, {
            "runpod": runpod, "network_bootstrap": network,
            "object_storage": storage, "audio_utils": audio,
        }):
            from worker_long.handler import _payload
            with self.assertRaises(ValueError):
                _payload({
                    "mode": "long_avatar", "avatar_mode": "audio", "duration": 8,
                    "segment_prompts": ["old prompt"], "ratio": "9:16",
                })
    def test_worker_follows_user_visible_15_second_boundaries(self) -> None:
        self.assertEqual(2, len(segment_lengths(23)[0]))
        self.assertEqual(3, len(segment_lengths(32)[0]))
        self.assertEqual(4, len(segment_lengths(60)[0]))

    def test_digital_human_pricing_counts_full_audio_in_15_second_units(self) -> None:
        for duration, expected_segments in (
            (1, 1), (15, 1), (15.8, 1), (16, 2),
            (30, 2), (31, 3), (45.8, 3), (46, 4), (60, 4),
        ):
            self.assertEqual(expected_segments, digital_human_segment_count(duration))


    def test_worker_concatenates_inside_comfy_before_single_encode(self) -> None:
        graph, lengths, _ = long_workflow(
            ["first", "continue"], 30, "16:9", 7
        )
        self.assertEqual(2, len(lengths))
        self.assertEqual("SolAttnMiniMax", graph["5"]["class_type"])
        self.assertEqual(["5", 0], graph["6"]["inputs"]["model"])
        self.assertEqual(
            1,
            sum(node["class_type"] == "SaveVideo" for node in graph.values()),
        )
        self.assertTrue(
            any(node["class_type"] == "ImageBatch" for node in graph.values())
        )
        self.assertTrue(
            any(node["class_type"] == "AudioConcat" for node in graph.values())
        )

    def test_45_seconds_stays_in_one_graph(self) -> None:
        graph, lengths, _ = long_workflow(
            ["first", "continue", "finish"], 45, "16:9", 7
        )
        self.assertEqual(3, len(lengths))
        self.assertEqual(
            1,
            sum(node["class_type"] == "SaveVideo" for node in graph.values()),
        )
        self.assertEqual(
            2,
            sum(node["class_type"] == "ImageBatch" for node in graph.values()),
        )
        self.assertFalse(
            any(node["class_type"] in {"SaveLatent", "LoadLatent"}
                for node in graph.values())
        )
        self.assertEqual("first", graph["100"]["inputs"]["prompt"])
        self.assertIn(
            "For the first two seconds, hold the exact closing framing",
            graph["120"]["inputs"]["prompt"],
        )
        self.assertTrue(graph["120"]["inputs"]["prompt"].endswith("continue"))
        self.assertIn(
            "For the first two seconds, hold the exact closing framing",
            graph["140"]["inputs"]["prompt"],
        )
        self.assertTrue(graph["140"]["inputs"]["prompt"].endswith("finish"))

    def test_first_frame_only_anchors_the_first_segment(self) -> None:
        graph, lengths, _ = long_workflow(
            ["speak first", "continue speaking"], 25, "9:16", 7,
            first_frame="speaker.png",
        )
        self.assertEqual(2, len(lengths))
        self.assertEqual("LoadImage", graph["7"]["class_type"])
        self.assertEqual("speaker.png", graph["7"]["inputs"]["image"])
        self.assertEqual(["7", 0], graph["100"]["inputs"]["first_frame"])
        self.assertNotIn("first_frame", graph["120"]["inputs"])

    def test_memory_bounded_chunk_persists_final_av_latent(self) -> None:
        graph = long_chunk_workflow(
            ["first", "continue"], [362, 379], "16:9", 7
        )
        self.assertEqual("SolAttnMiniMax", graph["5"]["class_type"])
        self.assertEqual(["5", 0], graph["6"]["inputs"]["model"])
        self.assertEqual(
            1, sum(node["class_type"] == "SaveVideo" for node in graph.values())
        )
        self.assertEqual(
            2, sum(node["class_type"] == "SaveLatent" for node in graph.values())
        )
        self.assertTrue(
            any(node["class_type"] == "ImageBatch" for node in graph.values())
        )

    def test_45_second_chunk_accepts_three_segments_and_first_frame(self) -> None:
        graph = long_chunk_workflow(
            ["first", "continue", "finish"], [362, 379, 396],
            "9:16", 7, first_frame="speaker.png",
        )
        self.assertEqual("LoadImage", graph["10"]["class_type"])
        self.assertEqual(["10", 0], graph["100"]["inputs"]["first_frame"])
        self.assertNotIn("first_frame", graph["130"]["inputs"])
        self.assertNotIn("first_frame", graph["160"]["inputs"])
        self.assertEqual(
            2,
            sum(node["class_type"] == "ImageBatch" for node in graph.values()),
        )

    def test_continuation_chunk_loads_prior_av_latent(self) -> None:
        graph = long_chunk_workflow(
            ["continue", "finish"], [379, 396], "16:9", 7,
            segment_offset=2, video_latent="video.latent",
            audio_latent="audio.latent",
        )
        self.assertEqual("LoadLatent", graph["7"]["class_type"])
        self.assertEqual("LoadLatent", graph["8"]["class_type"])
        self.assertEqual("LTXVConcatAVLatent", graph["9"]["class_type"])
        self.assertEqual(18, graph["112"]["inputs"]["trim_frames"])





    def test_workflow_resolution_helper_aligns_to_canvas_multiple(self) -> None:
        from worker_long.long_video import megapixel_dimensions

        self.assertEqual((1344, 736), megapixel_dimensions("16:9", 1.0))
        with self.assertRaises(ValueError):
            megapixel_dimensions("4:3", 1.0)


    def test_retired_selflift_mode_is_rejected(self) -> None:
        runpod = types.ModuleType("runpod")
        network = types.ModuleType("network_bootstrap")
        network.install_transfer_host_mapping = lambda: None
        storage = types.ModuleType("object_storage")
        storage.upload_outputs = lambda *_args, **_kwargs: None
        audio = types.ModuleType("audio_utils")
        audio._preserve_source_audio = lambda *_args, **_kwargs: None
        with patch.dict(sys.modules, {
            "runpod": runpod, "network_bootstrap": network,
            "object_storage": storage, "audio_utils": audio,
        }):
            from worker_long.handler import _payload
            with self.assertRaises(ValueError):
                _payload({
                    "mode": "long_avatar_selflift", "avatar_mode": "audio",
                    "duration": 8, "segment_prompts": ["old prompt"],
                })
    def test_digital_human_workflow_rejects_unknown_id_and_resolution(self) -> None:
        with self.assertRaises(ValueError):
            _digital_human_workflow_graph("old", "portrait.png", "voice.wav", 1.0)
        with self.assertRaises(ValueError):
            _digital_human_workflow_graph("digital_human_image_audio_v1", "portrait.png", "voice.wav", 0.7)
        with self.assertRaises(ValueError):
            _digital_human_workflow_graph("digital_human_image_audio_v1", "portrait.png", "voice.wav", 1.0, "4:3")

    def test_worker_payload_accepts_both_digital_human_ratios_at_fixed_one_mp(self) -> None:
        runpod = types.ModuleType("runpod")
        network = types.ModuleType("network_bootstrap")
        network.install_transfer_host_mapping = lambda: None
        storage = types.ModuleType("object_storage")
        storage.upload_outputs = lambda *_args, **_kwargs: None
        audio = types.ModuleType("audio_utils")
        audio._preserve_source_audio = lambda *_args, **_kwargs: None
        with patch.dict(sys.modules, {
            "runpod": runpod, "network_bootstrap": network,
            "object_storage": storage, "audio_utils": audio,
        }):
            from worker_long.handler import _payload
            for ratio in ("16:9", "9:16"):
                mode, payload = _payload({
                    "mode": "digital_human_workflow",
                    "workflow_id": "digital_human_image_audio_v1",
                    "duration": 5,
                    "ratio": ratio,
                    "megapixels": 1.0,
                    "prompt": "原样保留这段附加要求。",
                })
                self.assertEqual("digital_human_workflow", mode)
                self.assertEqual(ratio, payload["ratio"])
                self.assertEqual(1.0, payload["megapixels"])
                self.assertEqual("原样保留这段附加要求。", payload["prompt"])



if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()

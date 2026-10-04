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
    LongVideoRequest, digital_human_render_duration,
    digital_human_segment_count, segment_count, validate_long_request,
)
from worker_long.long_video import avatar_segment_lengths, long_chunk_workflow, long_workflow, segment_lengths
from video_api import _digital_human_prompts


class LongVideoApiTest(unittest.TestCase):
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

    def test_worker_accepts_single_prompt_avatar_audio_payload(self) -> None:
        runpod = types.ModuleType("runpod")
        network = types.ModuleType("network_bootstrap")
        network.install_transfer_host_mapping = lambda: None
        storage = types.ModuleType("object_storage")
        storage.upload_outputs = lambda *_args, **_kwargs: None
        audio = types.ModuleType("audio_utils")
        audio._preserve_source_audio = lambda *_args, **_kwargs: None
        audio._split_avatar_audio = lambda *_args, **_kwargs: None
        import worker_long.long_video as long_video_module
        with patch.dict(sys.modules, {
            "runpod": runpod, "network_bootstrap": network,
            "object_storage": storage, "audio_utils": audio,
            "long_video": long_video_module,
        }):
            from worker_long.handler import _payload

            mode, payload = _payload({
                "mode": "long_avatar", "avatar_mode": "audio", "duration": 8,
                "segment_prompts": ["Speak the uploaded audio"],
                "ratio": "9:16", "seed": 7,
            })
        self.assertEqual("long_avatar", mode)
        self.assertEqual(8, payload["duration"])
        self.assertEqual(1, len(json.loads(payload["prompts_json"])))

    def test_worker_follows_user_visible_15_second_boundaries(self) -> None:
        self.assertEqual(2, len(segment_lengths(23)[0]))
        self.assertEqual(3, len(segment_lengths(32)[0]))
        self.assertEqual(4, len(segment_lengths(60)[0]))

    def test_digital_human_drops_subsecond_tail_at_each_15_second_boundary(self) -> None:
        for duration, expected_duration, expected_segments in (
            (15, 15, 1), (15.024, 15, 1), (15.8, 15, 1),
            (15.999, 15, 1), (16, 16, 2),
            (30.5, 30, 2), (30.999, 30, 2), (31, 31, 3),
            (45.8, 45, 3), (46, 46, 4), (60, 60, 4),
        ):
            self.assertEqual(expected_duration, digital_human_render_duration(duration))
            self.assertEqual(expected_segments, digital_human_segment_count(duration))
            lengths, rendered = avatar_segment_lengths(duration)
            self.assertEqual(expected_segments, len(lengths))
            self.assertLess(abs(rendered - expected_duration * 24), 17)
            self.assertEqual(expected_segments, len(_digital_human_prompts(duration, "9:16")))

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

    def test_digital_human_uses_person_audio_and_keeps_latent_continuity(self) -> None:
        profile = types.SimpleNamespace(
            fl_model="fl.safetensors", ref_model="ref.safetensors",
            text_encoder="text.safetensors", video_vae="video.safetensors",
            audio_vae="audio.safetensors", ref_lora="ref-lora.safetensors",
        )
        module = types.ModuleType("h3_profiles")
        module.get_profile = lambda _name: profile
        with patch.dict(sys.modules, {"h3_profiles": module}):
            graph, lengths, _ = long_workflow(
                ["Speak the opening", "Continue speaking"], 30, "9:16", 17,
                avatar_image="person.png", audio_segments=["voice-1.wav", "voice-2.wav"],
            )
        self.assertEqual(2, len(lengths))
        self.assertEqual("MiniMaxH3Easy", graph["101"]["class_type"])
        first = graph["101"]["inputs"]
        self.assertEqual("reference", first["mode"])
        self.assertEqual("image", first["media_type_1"])
        self.assertEqual("audio", first["media_type_2"])
        self.assertEqual("person.png", graph["100"]["inputs"]["image"])
        self.assertEqual("voice-1.wav", graph["103"]["inputs"]["audio"])
        self.assertEqual("Speak the opening", first["prompt"])
        self.assertNotIn("130", graph)
        self.assertEqual("voice-2.wav", graph["133"]["inputs"]["audio"])
        self.assertEqual(["100", 0], graph["131"]["inputs"]["media_1"])
        self.assertEqual(["133", 0], graph["131"]["inputs"]["media_2"])
        self.assertEqual("SolAttnMiniMax", graph["129"]["class_type"])
        self.assertEqual(["101", 0], graph["129"]["inputs"]["model"])
        self.assertEqual(["129", 0], graph["105"]["inputs"]["model"])
        self.assertEqual(["105", 0], graph["106"]["inputs"]["model"])
        self.assertEqual(["106", 0], graph["108"]["inputs"]["model"])
        self.assertEqual(graph["108"]["inputs"]["model"], graph["109"]["inputs"]["model"])
        self.assertEqual(["111", 0], graph["134"]["inputs"]["context_latent"])
        self.assertEqual("MiniMaxH3MotionContext", graph["134"]["class_type"])
        self.assertEqual(1, sum(node["class_type"] == "SaveVideo" for node in graph.values()))

    def test_digital_human_continuation_chunk_keeps_audio_refs_and_saves_latents(self) -> None:
        from worker_long.long_video import _avatar_long_workflow

        profile = types.SimpleNamespace(
            fl_model="fl.safetensors", ref_model="ref.safetensors",
            text_encoder="text.safetensors", video_vae="video.safetensors",
            audio_vae="audio.safetensors", ref_lora="ref-lora.safetensors",
        )
        module = types.ModuleType("h3_profiles")
        module.get_profile = lambda _name: profile
        with patch.dict(sys.modules, {"h3_profiles": module}):
            graph, _, _ = _avatar_long_workflow(
                ["Continue speaking"], [362], 362, "9:16", 17,
                "person.png", ["voice-4.wav"], 15,
                "prior-video.latent", "prior-audio.latent", 3,
                persist_latent=True,
            )
        self.assertEqual("MiniMaxH3Easy", graph["101"]["class_type"])
        self.assertEqual("voice-4.wav", graph["103"]["inputs"]["audio"])
        self.assertNotIn("1001", graph)
        self.assertEqual(["102", 1], graph["104"]["inputs"]["latent"])
        self.assertEqual(["102", 1], graph["112"]["inputs"]["latent_image"])
        self.assertEqual(["4", 0], graph["104"]["inputs"]["context_latent"])
        self.assertEqual(18, graph["122"]["inputs"]["trim_frames"])
        self.assertEqual("SaveLatent", graph["9011"]["class_type"])
        self.assertEqual("SaveLatent", graph["9012"]["class_type"])

    def test_chunked_avatar_uses_current_audio_and_prior_motion(self) -> None:
        from worker_long.long_video import _avatar_long_workflow

        profile = types.SimpleNamespace(
            fl_model="fl.safetensors", ref_model="ref.safetensors",
            text_encoder="text.safetensors", video_vae="video.safetensors",
            audio_vae="audio.safetensors", ref_lora="ref-lora.safetensors",
        )
        module = types.ModuleType("h3_profiles")
        module.get_profile = lambda _name: profile
        with patch.dict(sys.modules, {"h3_profiles": module}):
            graph, _, _ = _avatar_long_workflow(
                ["Continue singing"], [345], 345, "9:16", 17,
                "person.png", ["voice-2.wav"], 345 / 24,
                "prior-video.latent", "prior-audio.latent", 1,
                persist_latent=True, selflift=True,
            )
        self.assertEqual(345 / 24, graph["101"]["inputs"]["seconds"])
        self.assertEqual(["103", 0], graph["1001"]["inputs"]["audio"])
        self.assertEqual(["1004", 0], graph["112"]["inputs"]["latent_image"])
        self.assertEqual(["4", 0], graph["104"]["inputs"]["context_latent"])

    def test_digital_human2_uses_only_the_selflift_avatar_sampler(self) -> None:
        from worker_long.long_video import _avatar_long_workflow

        profile = types.SimpleNamespace(
            fl_model="fl.safetensors", ref_model="ref.safetensors",
            text_encoder="text.safetensors", video_vae="video.safetensors",
            audio_vae="audio.safetensors", ref_lora="ref-lora.safetensors",
        )
        module = types.ModuleType("h3_profiles")
        module.get_profile = lambda _name: profile
        with patch.dict(sys.modules, {"h3_profiles": module}):
            graph, _, _ = _avatar_long_workflow(
                ["<Picture 1> speaks <Audio 1>"], [362], 362, "9:16", 17,
                "person.png", ["voice.wav"], 15, selflift=True,
            )
        samplers = [node for node in graph.values()
                    if node.get("class_type") in {"SelfLiftAvatarH3Sampler", "SamplerCustomAdvanced"}]
        self.assertEqual(["SelfLiftAvatarH3Sampler"], [node["class_type"] for node in samplers])
        self.assertEqual(6, samplers[0]["inputs"]["transition_step"])
        self.assertEqual(0.5, samplers[0]["inputs"]["lowres_scale"])
        self.assertEqual(0.6, samplers[0]["inputs"]["rho"])

    def test_digital_human2_audio_mask_is_attached_to_each_segment(self) -> None:
        from worker_long.long_video import _avatar_long_workflow

        profile = types.SimpleNamespace(
            fl_model="fl.safetensors", ref_model="ref.safetensors",
            text_encoder="text.safetensors", video_vae="video.safetensors",
            audio_vae="audio.safetensors", ref_lora="ref-lora.safetensors",
        )
        module = types.ModuleType("h3_profiles")
        module.get_profile = lambda _name: profile
        with patch.dict(sys.modules, {"h3_profiles": module}):
            graph, _, _ = _avatar_long_workflow(
                ["one", "two"], [362, 345], 685, "9:16", 17,
                "person.png", ["voice-1.wav", "voice-2.wav"], 27.6,
                selflift=True,
            )
        self.assertEqual("SetLatentNoiseMask", graph["1103"]["class_type"])
        self.assertEqual("LTXVConcatAVLatent", graph["1104"]["class_type"])
        self.assertEqual(["1104", 0], graph["134"]["inputs"]["latent"])
        self.assertEqual(["1004", 0], graph["111"]["inputs"]["latent_image"])
        self.assertEqual(["1104", 0], graph["142"]["inputs"]["latent_image"])
        for base, audio, output in ((1000, "103", "102"), (1100, "133", "132")):
            self.assertEqual([audio, 0], graph[str(base + 1)]["inputs"]["audio"])
            self.assertEqual([output, 3], graph[str(base + 1)]["inputs"]["vae"])
            self.assertEqual(0.0, graph[str(base + 2)]["inputs"]["value"])
            self.assertEqual([str(base + 1), 0], graph[str(base + 3)]["inputs"]["samples"])
            self.assertEqual([str(base + 2), 0], graph[str(base + 3)]["inputs"]["mask"])
            self.assertEqual([str(base + 3), 0], graph[str(base + 4)]["inputs"]["audio_latent"])

    def test_digital_human2_failure_is_not_retried_as_digital_human1(self) -> None:
        runpod = types.ModuleType("runpod")
        network = types.ModuleType("network_bootstrap")
        network.install_transfer_host_mapping = lambda: None
        storage = types.ModuleType("object_storage")
        storage.upload_outputs = lambda *_args, **_kwargs: None
        audio = types.ModuleType("audio_utils")
        audio._preserve_source_audio = lambda *_args, **_kwargs: None
        audio._split_avatar_audio = lambda *_args, **_kwargs: None
        import worker_long.long_video as long_video_module
        with patch.dict(sys.modules, {
            "runpod": runpod, "network_bootstrap": network,
            "object_storage": storage, "audio_utils": audio,
            "long_video": long_video_module,
        }):
            from worker_long import handler as worker_handler

            with tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                payload = {
                    "mode": "long_avatar_selflift", "avatar_mode": "audio",
                    "duration": 8, "source_audio_duration": 8,
                }
                prepared = [
                    {"field": "images", "filename": "person.png", "path": folder / "person.png"},
                    {"field": "audios", "filename": "voice.wav", "path": folder / "voice.wav"},
                ]
                with patch.object(worker_handler, "_payload", return_value=("long_avatar_selflift", payload)), \
                     patch.object(worker_handler, "_prepare_media", return_value=(folder, prepared)), \
                     patch.object(worker_handler, "_run_long_single_window", side_effect=RuntimeError("selflift graph failed")) as run_single, \
                     patch.object(worker_handler, "_run_long_chunked") as run_chunked, \
                     patch.object(worker_handler, "_report_failure"):
                    with self.assertRaises(RuntimeError):
                        worker_handler.handler({
                            "id": "dh2-isolation-test",
                            "input": {
                                "media": [], "source_audio_duration": 8,
                            },
                        })
                run_single.assert_called_once()
                self.assertEqual("long_avatar_selflift", run_single.call_args.args[0])
                run_chunked.assert_not_called()

    def test_avatar_solattn_can_be_disabled_without_changing_reference_mode(self) -> None:
        profile = types.SimpleNamespace(
            fl_model="fl.safetensors", ref_model="ref.safetensors",
            text_encoder="text.safetensors", video_vae="video.safetensors",
            audio_vae="audio.safetensors", ref_lora="ref-lora.safetensors",
        )
        module = types.ModuleType("h3_profiles")
        module.get_profile = lambda _name: profile
        with patch.dict(sys.modules, {"h3_profiles": module}), patch.dict("os.environ", {"H3_SOLATTN": "off"}):
            graph, _, _ = long_workflow(
                ["<Picture 1> performs <Audio 1>"], 15, "9:16", 17,
                avatar_image="person.png", audio_segments=["voice.wav"],
            )
        self.assertEqual("reference", graph["101"]["inputs"]["mode"])
        self.assertNotIn("129", graph)
        self.assertEqual(["104", 0], graph["105"]["inputs"]["model"])

    def test_digital_human_prompts_use_minimax_picture_and_audio_references(self) -> None:
        prompts = _digital_human_prompts(16, "9:16", "温柔、微笑")
        self.assertEqual(2, len(prompts))
        for prompt in prompts:
            self.assertIn("<Subject 1> 是 <Picture 1> 中的人物", prompt)
            self.assertNotIn("<Subject 2>", prompt)
            self.assertIn("口型严格与 <Audio 1> 同步", prompt)
            self.assertEqual(1, prompt.count("温柔、微笑"))
            self.assertLess(prompt.index("subject_definitions:"), prompt.index("summary:"))
            self.assertLess(prompt.index("summary:"), prompt.index("retention_analysis:"))
            self.assertLess(prompt.index("retention_analysis:"), prompt.index("detailed_description:"))
            self.assertLess(prompt.index("detailed_description:"), prompt.index("overall_soundscape:"))
            self.assertLess(prompt.index("overall_soundscape:"), prompt.index("non_diegetic_music:"))
        self.assertEqual(prompts[0], prompts[1])
        self.assertEqual(prompts, _digital_human_prompts(16, "16:9", "温柔、微笑"))
        self.assertNotIn("segment", prompts[1])

    def test_blank_digital_human_direction_adds_no_style_or_camera_defaults(self) -> None:
        prompt = _digital_human_prompts(8, "9:16")[0]
        self.assertIn("subject_definitions:", prompt)
        self.assertIn("口型严格与 <Audio 1> 同步", prompt)
        self.assertNotIn("direct-to-camera", prompt)
        self.assertNotIn("subtle breathing", prompt)
        self.assertNotIn("User instructions", prompt)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()

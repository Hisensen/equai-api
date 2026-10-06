"""The optional direction is merged into the pinned Ref2VA template, not bolted on.

Two properties matter more than the wording of the merge. Without a direction the
template must go out byte for byte and cost nothing. With one, a merge that loses
the six-section skeleton, drops a reference label, or fails outright must fall back
to the literal append rather than fail a paid task.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("H3_DATA_DIR", tempfile.mkdtemp(prefix="equai-dhprompt-"))

import digital_human_prompt as dh


def run(coro):
    return asyncio.run(coro)


def _well_formed(direction: str = "") -> str:
    body = f" {direction}" if direction else ""
    return (
        "subject_definitions:\n<Subject 1> is the person in <Picture 1>; <Audio 1> is the speech.\n\n"
        "summary:\nOne continuous talking-head shot.\n\n"
        "retention_analysis:\n<Subject 1> keeps identity from <Picture 1>; <Audio 1> reused exactly.\n\n"
        f"detailed_description:\n[Shot 1] Locked medium shot.{body}\n\n"
        "overall_soundscape:\nUse the supplied <Audio 1> window as the only sound.\n\n"
        "non_diegetic_music:\nN/A"
    )


class NoDirectionTest(unittest.TestCase):
    def test_template_is_used_unchanged(self) -> None:
        base = dh.base_prompt()
        self.assertEqual(dh.compose_digital_human_workflow_prompt(""), base)
        self.assertEqual(dh.compose_digital_human_workflow_prompt("   "), base)

    def test_no_direction_never_calls_the_prompt_service(self) -> None:
        base = dh.base_prompt()
        with patch("prompt_service._runtime_settings",
                   side_effect=AssertionError("must not contact the prompt service")):
            result = run(dh.merge_direction(base, ""))
        self.assertEqual(result["prompt"], base)
        self.assertFalse(result["provider_used"])


class MergeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = dh.base_prompt()

    def _merge(self, reply: str, status: int = 200):
        class Response:
            status_code = status

        with patch("prompt_service._runtime_settings",
                   return_value={"base_url": "https://example", "api_key": "k"}), \
                patch("prompt_service._response_content", return_value=reply), \
                patch("prompt_service._provider_error", return_value="boom"), \
                patch("httpx.AsyncClient.post", return_value=Response()):
            return run(dh.merge_direction(self.base, "自然微笑，轻轻点头"))

    def test_a_well_formed_merge_is_used(self) -> None:
        result = self._merge(_well_formed("He smiles naturally and nods gently."))
        self.assertTrue(result["provider_used"])
        self.assertIn("nods gently", result["prompt"])
        self.assertNotIn("Additional user direction", result["prompt"])

    def test_code_fences_and_preamble_are_stripped(self) -> None:
        result = self._merge("Sure!\n```text\n" + _well_formed("Smiling.") + "\n```")
        self.assertTrue(result["provider_used"])
        self.assertTrue(result["prompt"].startswith("subject_definitions:"))

    def test_a_merge_missing_a_section_falls_back(self) -> None:
        broken = _well_formed().replace("retention_analysis:", "notes:")
        result = self._merge(broken)
        self.assertFalse(result["provider_used"])
        self.assertIn("自然微笑", result["prompt"])

    def test_a_merge_that_drops_a_reference_label_falls_back(self) -> None:
        result = self._merge(_well_formed().replace("<Audio 1>", "the audio"))
        self.assertFalse(result["provider_used"])
        self.assertIn("自然微笑", result["prompt"])

    def test_out_of_order_sections_fall_back(self) -> None:
        text = _well_formed()
        head, tail = text.split("summary:", 1)
        result = self._merge("summary:" + tail + "\n\n" + head)
        self.assertFalse(result["provider_used"])

    def test_provider_error_falls_back_instead_of_raising(self) -> None:
        result = self._merge("", status=500)
        self.assertFalse(result["provider_used"])
        self.assertIn("自然微笑", result["prompt"])

    def test_unconfigured_service_falls_back(self) -> None:
        import prompt_service
        with patch("prompt_service._runtime_settings",
                   side_effect=prompt_service.PromptServiceError("未配置")):
            result = run(dh.merge_direction(self.base, "点头"))
        self.assertFalse(result["provider_used"])
        self.assertIn("点头", result["prompt"])

    def test_fallback_keeps_the_template_structure(self) -> None:
        result = self._merge("garbage")
        self.assertTrue(dh._usable(result["prompt"], self.base))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest
from pathlib import Path

from digital_human_prompt import compose_digital_human_workflow_prompt


class DigitalHumanPromptTest(unittest.TestCase):
    def setUp(self) -> None:
        workflow = Path(__file__).parents[1] / "worker_long/workflows/digital_human_image_audio_v1.json"
        self.graph = json.loads(workflow.read_text(encoding="utf-8"))
        self.base = self.graph["136"]["inputs"]["prompt"]

    def test_empty_extra_prompt_uses_exact_workflow_template(self) -> None:
        self.assertEqual(self.base, self.graph["276"]["inputs"]["prompt"])
        self.assertEqual(self.base, compose_digital_human_workflow_prompt(""))
        self.assertEqual(self.base, compose_digital_human_workflow_prompt("  \n  "))

    def test_extra_prompt_is_appended_verbatim_inside_h3_description(self) -> None:
        extra = "人物自然微笑，轻轻点头；不要改变衣服和背景。"
        actual = compose_digital_human_workflow_prompt(extra)
        marker = "\n\noverall_soundscape:"
        expected = self.base.replace(
            marker,
            f"\n\nAdditional user direction (verbatim):\n{extra}\n\noverall_soundscape:",
            1,
        )
        self.assertEqual(expected, actual)
        self.assertEqual(1, actual.count(extra))
        self.assertIn("detailed_description:", actual)
        self.assertNotIn("\nuser_direction:", actual)


if __name__ == "__main__":
    unittest.main()

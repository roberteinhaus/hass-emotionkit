"""Consistency of the shipped blueprints.

They are YAML with Home Assistant's `!input` tag; these checks read them as
text so that no YAML library is needed. Whether Home Assistant accepts them is
checked in ../tests_ha.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest

from _support import INTEGRATION_ROOT

from custom_components.emotionkit import const

BLUEPRINT_DIR = INTEGRATION_ROOT / "blueprints" / "automation" / "emotionkit"
SOURCE_BASE = "https://github.com/roberteinhaus/hass-emotionkit/blob/main/blueprints/automation/emotionkit/"

# `key:` followed by a line `name:` one level deeper: an input or an input section.
INPUT_KEY = re.compile(r"^(\s+)(\w+):\s*\n\1  name:", re.MULTILINE)


def blueprints() -> list[Path]:
    return sorted(BLUEPRINT_DIR.glob("*.yaml"))


def event_types_in(text: str) -> set[str]:
    """Event types named in select options and in `event_type:` lists."""
    found = set(re.findall(r"^\s+value:\s*(\w+)\s*$", text, re.MULTILINE))
    for block in re.finditer(r"^(\s+)event_type:\s*\n((?:\1  - \w+\s*\n)+)", text, re.MULTILINE):
        found |= set(re.findall(r"-\s*(\w+)", block.group(2)))
    return found


class BlueprintsTest(unittest.TestCase):
    def test_blueprints_exist(self) -> None:
        self.assertGreaterEqual(len(blueprints()), 5)

    def test_metadata(self) -> None:
        for path in blueprints():
            with self.subTest(blueprint=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("blueprint:\n"))
                self.assertIn("  domain: automation\n", text)
                self.assertIn(f"  source_url: {SOURCE_BASE}{path.name}\n", text)
                self.assertRegex(text, r'min_version: "\d{4}\.\d+\.\d+"')
                self.assertRegex(text, r"(?m)^mode: (single|restart|queued|parallel)$")
                self.assertNotIn("\t", text)

    def test_only_known_event_types(self) -> None:
        known = set(const.ALL_EVENT_TYPES)
        for path in blueprints():
            with self.subTest(blueprint=path.name):
                used = event_types_in(path.read_text(encoding="utf-8"))
                self.assertFalse(used - known, f"unknown event types: {sorted(used - known)}")

    def test_event_selects_offer_every_event_type(self) -> None:
        known = set(const.ALL_EVENT_TYPES)
        for path in blueprints():
            text = path.read_text(encoding="utf-8")
            offered = set(re.findall(r"^\s+value:\s*(\w+)\s*$", text, re.MULTILINE))
            if not offered:
                continue
            with self.subTest(blueprint=path.name):
                self.assertEqual(offered, known)

    def test_every_referenced_input_is_defined(self) -> None:
        for path in blueprints():
            with self.subTest(blueprint=path.name):
                text = path.read_text(encoding="utf-8")
                head, _, body = text.partition("\nmode:")
                defined = {match.group(2) for match in INPUT_KEY.finditer(head)}
                used = set(re.findall(r"!input\s+(\w+)", body))
                self.assertTrue(used, "a blueprint without inputs in its body is not a blueprint")
                self.assertFalse(used - defined, f"undefined inputs: {sorted(used - defined)}")

    def test_no_input_inside_a_template(self) -> None:
        # `!input` is a YAML tag; inside {{ … }} it is not substituted.
        for path in blueprints():
            with self.subTest(blueprint=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIsNone(re.search(r"\{\{[^}]*!input", text))

    def test_readme_lists_every_blueprint(self) -> None:
        readme = (INTEGRATION_ROOT / "README.md").read_text(encoding="utf-8")
        for path in blueprints():
            with self.subTest(blueprint=path.name):
                self.assertIn(f"`{path.name}`", readme)


if __name__ == "__main__":
    unittest.main()

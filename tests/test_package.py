"""Consistency of what gets shipped: translations, manifest, module layout."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import sys
from typing import Any
import unittest

import _support
from _support import INTEGRATION_ROOT, PACKAGE_DIR, PURE_MODULES

from custom_components.emotionkit import const

TRANSLATIONS = PACKAGE_DIR / "translations"
FORBIDDEN_IN_PURE_MODULES = ("homeassistant", "aiomqtt", "aiohttp", "voluptuous", "paho")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def key_paths(node: Any, prefix: str = "") -> set[str]:
    """Return the dotted paths of all leaves of a translation file."""
    if isinstance(node, dict):
        paths: set[str] = set()
        for key, value in node.items():
            paths |= key_paths(value, f"{prefix}.{key}" if prefix else key)
        return paths
    return {prefix}


def leaves(node: Any, prefix: str = "") -> dict[str, str]:
    if isinstance(node, dict):
        result: dict[str, str] = {}
        for key, value in node.items():
            result.update(leaves(value, f"{prefix}.{key}" if prefix else key))
        return result
    return {prefix: node}


class TranslationsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.strings = load(PACKAGE_DIR / "strings.json")
        cls.en = load(TRANSLATIONS / "en.json")
        cls.de = load(TRANSLATIONS / "de.json")

    def test_strings_is_the_english_source(self) -> None:
        self.assertEqual(self.strings, self.en)
        self.assertEqual(
            (PACKAGE_DIR / "strings.json").read_bytes(),
            (TRANSLATIONS / "en.json").read_bytes(),
        )
        # A crude but effective check that the source is not German.
        text = json.dumps(self.strings, ensure_ascii=False)
        for german in ("Gerät", "ä", "ö", "ü", "ß", " und ", " der ", " nicht "):
            self.assertNotIn(german, text)

    def test_identical_key_sets(self) -> None:
        self.assertEqual(key_paths(self.strings), key_paths(self.en))
        self.assertEqual(key_paths(self.strings), key_paths(self.de))

    def test_all_values_are_non_empty_strings(self) -> None:
        for name, data in (("en", self.en), ("de", self.de)):
            for path, value in leaves(data).items():
                with self.subTest(file=name, key=path):
                    self.assertIsInstance(value, str)
                    self.assertTrue(value.strip())

    def test_german_is_actually_translated(self) -> None:
        en, de = leaves(self.en), leaves(self.de)
        # Terms that are the same in both languages.
        same_on_purpose = {"Kill", "Ace"}
        identical = [
            path for path in en if en[path] == de[path] and en[path] not in same_on_purpose
        ]
        self.assertEqual(identical, [])

    def test_placeholders_match(self) -> None:
        pattern = re.compile(r"\{(\w+)\}")
        en, de = leaves(self.en), leaves(self.de)
        for path, text in en.items():
            with self.subTest(key=path):
                self.assertEqual(set(pattern.findall(text)), set(pattern.findall(de[path])))
        progress = self.en["config"]["progress"]["wait_for_claim"]
        self.assertIn("{claim_code}", progress)
        self.assertIn("{url}", progress)

    def test_every_trigger_type_is_translated(self) -> None:
        expected = set(const.ALL_EVENT_TYPES)
        for name, data in (("en", self.en), ("de", self.de)):
            with self.subTest(file=name):
                self.assertEqual(set(data["device_automation"]["trigger_type"]), expected)
                event_states = data["entity"]["event"][const.EVENT_ENTITY_KEY][
                    "state_attributes"
                ]["event_type"]["state"]
                self.assertEqual(set(event_states), expected)

    def test_every_entity_is_translated(self) -> None:
        for name, data in (("en", self.en), ("de", self.de)):
            with self.subTest(file=name):
                entity = data["entity"]
                self.assertEqual(set(entity["sensor"]), set(const.SENSOR_KEYS))
                self.assertEqual(set(entity["binary_sensor"]), set(const.BINARY_SENSOR_KEYS))
                self.assertEqual(set(entity["event"]), {const.EVENT_ENTITY_KEY})
                for platform in ("sensor", "binary_sensor", "event"):
                    for key, item in entity[platform].items():
                        self.assertIn("name", item, f"{platform}.{key}")
                self.assertEqual(
                    set(entity["sensor"]["round_phase"]["state"]), set(const.ROUND_PHASES)
                )

    def test_required_sections_exist(self) -> None:
        # The parts that were missing before.
        config = self.en["config"]
        self.assertIn("reconfigure", config["step"])
        self.assertIn("reauth_confirm", config["step"])
        self.assertIn("invalid_broker", config["error"])
        self.assertIn("reconfigure_successful", config["abort"])
        self.assertIn("reauth_successful", config["abort"])
        self.assertIn("idle", self.en["device_automation"]["trigger_type"])
        self.assertIn("init", self.en["options"]["step"])
        self.assertEqual(
            set(self.en["options"]["step"]["init"]["data"]),
            {const.CONF_IDLE_TIMEOUT, const.CONF_ALL_PLAYERS},
        )

    def test_keys_used_by_the_code_exist(self) -> None:
        errors = set(self.en["config"]["error"])
        # Error keys the claim flow can produce (api.py and config_flow.py).
        for key in (
            "cannot_connect",
            "timeout_connect",
            "server_busy",
            "invalid_response",
            "claim_timeout",
            "claim_failed",
            "invalid_broker",
            "unknown",
        ):
            self.assertIn(key, errors)
        steps = set(self.en["config"]["step"])
        flow_source = (PACKAGE_DIR / "config_flow.py").read_text(encoding="utf-8")
        for step_id in re.findall(r"async def async_step_(\w+)\(", flow_source):
            if step_id in ("init", "reauth", "finish"):
                # init: options flow. reauth and finish never show a form.
                continue
            with self.subTest(step=step_id):
                self.assertIn(step_id, steps)
        for step_id in re.findall(r'(?<!next_)step_id="(\w+)"', flow_source):
            if step_id != "init":
                self.assertIn(step_id, steps)
        # Every step a progress step hands over to must exist in the flow.
        for step_id in re.findall(r'next_step_id="(\w+)"', flow_source):
            self.assertIn(f"async def async_step_{step_id}(", flow_source)
        for action in re.findall(r'progress_action="(\w+)"', flow_source):
            self.assertIn(action, self.en["config"]["progress"])
        for reason in re.findall(r"async_abort\(reason=(\w+)\)", flow_source):
            self.assertEqual(reason, "ERROR_CLAIM_FAILED")
        self.assertIn("claim_failed", self.en["config"]["abort"])

    def test_icons_cover_the_entities(self) -> None:
        icons = load(PACKAGE_DIR / "icons.json")["entity"]
        self.assertEqual(set(icons["sensor"]), set(const.SENSOR_KEYS))
        self.assertEqual(set(icons["binary_sensor"]), set(const.BINARY_SENSOR_KEYS))
        self.assertEqual(set(icons["event"]), {const.EVENT_ENTITY_KEY})
        for path, value in leaves(icons).items():
            with self.subTest(icon=path):
                self.assertRegex(value, r"^mdi:[a-z0-9-]+$")


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load(PACKAGE_DIR / "manifest.json")
        cls.hacs = load(INTEGRATION_ROOT / "hacs.json")

    def test_manifest(self) -> None:
        manifest = self.manifest
        self.assertEqual(manifest["domain"], const.DOMAIN)
        self.assertIs(manifest["config_flow"], True)
        # Year.Month.Release
        self.assertRegex(manifest["version"], r"^20\d\d\.(?:[1-9]|1[0-2])\.\d+$")
        for key in ("documentation", "issue_tracker"):
            self.assertTrue(
                manifest[key].startswith("https://github.com/roberteinhaus/hass-emotionkit"),
                key,
            )
            self.assertNotIn("gitlab", manifest[key])
        # HACS requires these keys.
        for key in ("codeowners", "documentation", "domain", "issue_tracker", "name", "version"):
            self.assertIn(key, manifest)

    def test_aiomqtt_is_bounded(self) -> None:
        requirement = next(r for r in self.manifest["requirements"] if r.startswith("aiomqtt"))
        self.assertIn(">=2.5.1", requirement)
        self.assertIn("<3", requirement)

    def test_hacs_minimum_version(self) -> None:
        version = tuple(int(part) for part in self.hacs["homeassistant"].split("."))
        # OptionsFlowWithReload exists since Home Assistant 2025.8.
        self.assertGreaterEqual(version, (2025, 8, 0))
        self.assertEqual(self.hacs["name"], "EmotionKit")
        self.assertLessEqual(
            set(self.hacs),
            {
                "name",
                "content_in_root",
                "zip_release",
                "filename",
                "hide_default_branch",
                "country",
                "homeassistant",
                "hacs",
                "persistent_directory",
                "render_readme",
            },
        )

    def test_all_json_files_are_valid(self) -> None:
        files = [*PACKAGE_DIR.rglob("*.json"), INTEGRATION_ROOT / "hacs.json"]
        self.assertGreaterEqual(len(files), 6)
        for path in files:
            with self.subTest(file=path.name):
                load(path)


class ReadmeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.readme = (INTEGRATION_ROOT / "README.md").read_text(encoding="utf-8")

    def test_lists_every_trigger(self) -> None:
        for trigger_type in const.ALL_EVENT_TYPES:
            with self.subTest(trigger=trigger_type):
                self.assertIn(f"`{trigger_type}`", self.readme)

    def test_uses_current_automation_syntax(self) -> None:
        self.assertIn("triggers:", self.readme)
        self.assertIn("actions:", self.readme)
        self.assertIn("- trigger: device", self.readme)
        self.assertIn("- action: ", self.readme)
        lines = self.readme.splitlines()
        for outdated in ("trigger:", "action:"):
            # The old singular top-level keys.
            self.assertNotIn(outdated, lines)
        for outdated in ("platform: device", "service:"):
            self.assertNotIn(outdated, self.readme)

    def test_states_the_minimum_version_of_hacs_json(self) -> None:
        minimum = load(INTEGRATION_ROOT / "hacs.json")["homeassistant"]
        self.assertIn(minimum.rsplit(".", 1)[0], self.readme)


class LayoutTest(unittest.TestCase):
    def test_no_tests_are_shipped(self) -> None:
        shipped = [p.name for p in PACKAGE_DIR.rglob("*.py")]
        self.assertEqual([name for name in shipped if name.startswith(("test_", "conftest"))], [])

    def test_expected_platforms_exist(self) -> None:
        for name in (
            "__init__",
            "binary_sensor",
            "config_flow",
            "coordinator",
            "device_trigger",
            "diagnostics",
            "entity",
            "event",
            "sensor",
        ):
            self.assertTrue((PACKAGE_DIR / f"{name}.py").is_file(), name)

    def test_logic_modules_do_not_import_home_assistant(self) -> None:
        for name in PURE_MODULES:
            tree = ast.parse((PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8"))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])
            with self.subTest(module=name):
                self.assertEqual(imported & set(FORBIDDEN_IN_PURE_MODULES), set())

    def test_logic_modules_only_import_each_other(self) -> None:
        for name in PURE_MODULES:
            tree = ast.parse((PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level > 0:
                    with self.subTest(module=name, imports=node.module):
                        self.assertIn(node.module, PURE_MODULES)

    @unittest.skipIf(sys.version_info < (3, 12), "the Home Assistant side uses 3.12 syntax")
    def test_every_python_file_compiles(self) -> None:
        files = [*PACKAGE_DIR.rglob("*.py"), *Path(_support.__file__).parent.glob("*.py")]
        self.assertGreater(len(files), 20)
        for path in files:
            with self.subTest(file=path.name):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_secrets_are_not_logged(self) -> None:
        # No log call may mention a variable holding a secret.
        suspicious = re.compile(
            r"_LOGGER\.\w+\([^)]*\b(password|_password|device_token|token|claim_code|code|credentials)\b[^)]*\)",
            re.DOTALL,
        )
        for path in PACKAGE_DIR.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for match in suspicious.finditer(source):
                call = match.group(0)
                # Words inside the message text are fine; arguments are not.
                arguments = re.sub(r'"[^"]*"', '""', call)
                with self.subTest(file=path.name, call=call[:60]):
                    self.assertIsNone(
                        re.search(
                            r"\b(password|_password|device_token|token|claim_code|code|credentials)\b",
                            arguments,
                        )
                    )


if __name__ == "__main__":
    unittest.main()

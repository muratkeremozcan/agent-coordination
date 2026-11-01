"""Schemas stay in step with the ledger, the entrypoint is checkable from its repository
path without host registration, and nothing personal or host-specific ships in the package."""

import json
import os
import re
import subprocess
import unittest

import _guard  # noqa: F401  (must load first: it installs the real-state guard)
import coordinator_state as cs
from helpers import REPO_ROOT, Ledger, TempRoot

SKILL = REPO_ROOT / "skills" / "coordinate" / "SKILL.md"
SCHEMAS = REPO_ROOT / "schemas"


def schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text())


class SchemaDriftTests(unittest.TestCase):
    def test_every_schema_parses_and_names_its_draft(self):
        for path in SCHEMAS.glob("*.json"):
            with self.subTest(path.name):
                self.assertIn("2020-12", json.loads(path.read_text())["$schema"])

    def test_run_record_schema_requires_exactly_the_ledgers_required_keys(self):
        self.assertEqual(set(schema("run-record.v3.schema.json")["required"]), cs._REQUIRED_RECORD_KEYS)
        self.assertEqual(
            schema("run-record.v3.schema.json")["properties"]["schema_version"]["const"], cs.SCHEMA_VERSION
        )

    def test_enumerations_match_the_ledger(self):
        self.assertEqual(
            schema("authority.v1.schema.json")["items"]["properties"]["action"]["enum"], list(cs.AUTHORITY_ACTIONS)
        )
        review = schema("result-receipt.v2.schema.json")["properties"]["review"]["items"]["properties"]["status"]
        self.assertEqual(set(review["enum"]), cs.REVIEW_STATUSES)
        provisioning = schema("workspace-receipt.v1.schema.json")["properties"]["provisioning"]["properties"]["status"]
        self.assertEqual(set(provisioning["enum"]), cs.PROVISIONING_STATUSES)
        self.assertEqual(set(schema("landing.v1.schema.json")["properties"]["method"]["enum"]), cs.LANDING_METHODS)

    def test_a_real_record_carries_every_schema_required_workstream_key(self):
        led = Ledger(TempRoot(self).root)
        led.add()
        ws = led.snapshot()["workstreams"][0]
        required = schema("run-record.v3.schema.json")["properties"]["workstreams"]["items"]["required"]
        self.assertEqual([k for k in required if k not in ws], [])


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.text = SKILL.read_text()
        match = re.match(r"^---\n(.*?)\n---\n", self.text, re.S)
        self.assertIsNotNone(match, "SKILL.md needs YAML frontmatter")
        self.frontmatter = dict(line.split(": ", 1) for line in match.group(1).splitlines() if ": " in line)

    def test_frontmatter_names_the_skill_and_its_triggers(self):
        self.assertEqual(self.frontmatter["name"], "coordinate")
        for phrase in ("coordinate", "parallelize", "delegate", "babysit", "run overnight", "run a fleet"):
            self.assertIn(phrase, self.frontmatter["description"])

    def test_every_referenced_repository_file_exists(self):
        # References load on their own after compaction, so their paths are checked too.
        for prose in [SKILL, *sorted((SKILL.parent / "references").glob("*.md"))]:
            text = prose.read_text()
            for target in re.findall(r"\]\(([^)#]+)\)", text):
                self.assertTrue((prose.parent / target).is_file(), f"{prose.name}: {target}")
            for path in re.findall(r"`((?:scripts|adapters|profiles)/[\w./-]+)`", text):
                self.assertTrue((REPO_ROOT / path).exists(), f"{prose.name}: {path}")

    def test_the_entrypoint_says_where_repository_paths_resolve(self):
        self.assertTrue((SKILL.parent / ".." / ".." / "scripts" / "coordinator_state.py").resolve().is_file())
        self.assertIn("resolve from this repository's root, two\ndirectories above this file", self.text)

    def test_the_three_bindings_are_distinct_and_codex_never_defaults_to_claude(self):
        for binding in ("`herdr-codex`", "`codex-subagent`", "`peer-session`"):
            self.assertIn(binding, self.text)
        self.assertIn("A Codex coordinator dispatches Codex", self.text)
        self.assertIn("HERDR_ENV=1", self.text)

    def test_the_two_level_architecture_and_owner_continuity_are_stated(self):
        self.assertIn("One coordinator", self.text)
        self.assertIn("keep their\n   session through review and every fix", self.text)

    def test_the_skill_is_not_registered_with_any_host(self):
        # lstat raises no audit event and writes nothing, so the real host directories can be
        # checked without the guard firing.
        for host in (".agents", ".claude", ".codex"):
            self.assertFalse(os.path.lexists(os.path.join(_guard.REAL_HOME, host, "skills", "coordinate")))
        self.assertNotIn("install", self.frontmatter)


class SanitizationTests(unittest.TestCase):
    """Personal history and host-specific paths stay out of everything the package ships."""

    FORBIDDEN = [
        r"/Users/",
        r"/home/[a-z]",
        r"\bsanctum\b",
        r"_bmad/memory",
        r"\.runtime/coordinator",
        r"bmad-testarch",
        r"\b20[0-9]{2}-[01][0-9]-[0-3][0-9]\b",
        r"Co-Authored-By",
        r"Claude-Session",
    ]
    # Provenance and compatibility docs name the source and its dates; the state-root guard
    # restates the protected layout it refuses.
    EXEMPT = {"docs/source-provenance.md", "docs/compatibility.md", "scripts/state_root.py"}

    def tracked_files(self) -> list[str]:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return [
                str(p.relative_to(REPO_ROOT)) for p in REPO_ROOT.rglob("*") if p.is_file() and ".git" not in p.parts
            ]
        return result.stdout.splitlines()

    def test_no_personal_or_host_specific_text_ships(self):
        offenders = []
        for relative in self.tracked_files():
            if relative in self.EXEMPT or relative.startswith("tests/") or relative.endswith(".json"):
                continue
            text = (REPO_ROOT / relative).read_text(errors="ignore")
            for pattern in self.FORBIDDEN:
                if re.search(pattern, text):
                    offenders.append(f"{relative}: {pattern}")
        self.assertEqual(offenders, [])

    def test_no_absolute_home_paths_anywhere(self):
        offenders = [
            relative
            for relative in self.tracked_files()
            if "/Users/" in (REPO_ROOT / relative).read_text(errors="ignore")
            and relative != "tests/test_schemas_and_entrypoint.py"
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

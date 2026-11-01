"""The skill's prose names commands, flags and evidence shapes that the coordinator copies
into real calls. Each one must be accepted by the CLI or ledger check it names, so a renamed
flag or a tightened evidence rule cannot leave the skill teaching a call that is refused."""

import argparse
import json
import re
import unittest

import _guard  # noqa: F401  (must load first: it installs the real-state guard)
import coordinator_state as cs
import coordinator_watch
import forge_github
import peer_sessions_receipt
import workspace_treehouse
from helpers import REPO_ROOT, SHA_B, Ledger, TempRoot, iso, verification

SKILL_DIR = REPO_ROOT / "skills" / "coordinate"
PROSE = [SKILL_DIR / "SKILL.md", *sorted((SKILL_DIR / "references").glob("*.md"))]
# Flags every ledger subcommand requires; prose leaves them implicit.
IMPLICIT = {"--state-root", "--run-id", "--session-id"}


class _Captured(Exception):
    def __init__(self, parser: argparse.ArgumentParser):
        self.parser = parser


def _parser_of(main) -> argparse.ArgumentParser:
    """The parser a CLI builds inside `main`, captured before it parses anything."""

    def capture(self, *args, **kwargs):
        raise _Captured(self)

    original = argparse.ArgumentParser.parse_args
    argparse.ArgumentParser.parse_args = capture
    try:
        main([])
    except _Captured as captured:
        return captured.parser
    finally:
        argparse.ArgumentParser.parse_args = original
    raise AssertionError(f"{main.__module__}.main parsed nothing")


PARSERS = {
    "coordinator_state.py": cs.build_parser(),
    "workspace_treehouse.py": workspace_treehouse.build_parser(),
    "forge_github.py": _parser_of(forge_github.main),
    "peer_sessions_receipt.py": _parser_of(peer_sessions_receipt.main),
    "coordinator_watch.py": _parser_of(coordinator_watch.main),
}


def _subcommands(parser: argparse.ArgumentParser) -> dict:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def _flags(parser: argparse.ArgumentParser) -> set[str]:
    return {flag for action in parser._actions for flag in action.option_strings}


def _required(parser: argparse.ArgumentParser) -> set[str]:
    return {action.option_strings[0] for action in parser._actions if action.required and action.option_strings}


def _spans(text: str):
    """Command-shaped text: each line of a fenced block, then every inline code span."""
    for block in re.findall(r"```[^\n]*\n(.*?)```", text, re.S):
        yield from (line for line in block.replace("\\\n", " ").splitlines() if line.strip())
    for span in re.findall(r"`([^`]+)`", re.sub(r"```.*?```", "", text, flags=re.S)):
        yield span.replace("\n", " ")


def command_problems(name: str, text: str) -> list[str]:
    ledger = _subcommands(PARSERS["coordinator_state.py"])
    problems = []
    for span in _spans(text):
        tokens = span.split()
        script = next((i for i, t in enumerate(tokens) if t.rsplit("/", 1)[-1] in PARSERS), None)
        if script is not None:
            tool, rest, named = tokens[script].rsplit("/", 1)[-1], tokens[script + 1 :], True
        elif tokens and tokens[0] in ledger:
            tool, rest, named = "coordinator_state.py", tokens, False
        else:
            continue
        parser = PARSERS[tool]
        subcommands = _subcommands(parser)
        if subcommands:
            if not rest or rest[0].startswith("-"):
                continue
            if rest[0] not in subcommands:
                problems.append(f"{name}: `{span}`: {tool} has no subcommand {rest[0]!r}")
                continue
            parser, rest = subcommands[rest[0]], rest[1:]
        used = {t.split("=", 1)[0] for t in rest if t.startswith("--")}
        problems += [f"{name}: `{span}`: {tool} has no {flag}" for flag in sorted(used - _flags(parser))]
        if named and used:
            missing = _required(parser) - used - IMPLICIT
            problems += [f"{name}: `{span}`: {tool} also requires {flag}" for flag in sorted(missing)]
    return problems


def documented_shape(text: str, marker: str) -> dict:
    """Parse a `{key: value, bare_key, ...}` shape from prose: the keys it names and the
    literal values it fixes. Bare keys map to None for the caller to fill."""
    shapes = [s for s in re.findall(r"`(\{[^`]+\})`", text.replace("\n", " ")) if marker in s]
    assert len(shapes) == 1, f"expected one documented shape containing {marker!r}, found {shapes}"
    shape = {}
    for part in shapes[0].strip("{}").split(","):
        key, _, value = (piece.strip() for piece in part.partition(":"))
        literal = {"true": True, "false": False}.get(value, int(value) if value.isdigit() else value)
        shape[key] = literal if value else None
    return shape


def fill(shape: dict, values: dict) -> dict:
    return {key: values[key] if fixed is None else fixed for key, fixed in shape.items()}


class ProseCommandTests(unittest.TestCase):
    def test_every_command_the_skill_names_parses(self):
        problems = [p for path in PROSE for p in command_problems(path.name, path.read_text())]
        self.assertEqual(problems, [])

    def test_the_check_rejects_unknown_flags_subcommands_and_missing_required_flags(self):
        bad = (
            "`ack-due --key <k> --next-check <t>` `snapshot --req <id>` "
            "`adapters/workspace_treehouse.py fetch --repo-path x` "
            "`adapters/peer_sessions_receipt.py --session s --request-id r`"
        )
        problems = command_problems("bad.md", bad)
        self.assertEqual(len(problems), 4, problems)
        self.assertIn("has no --next-check", problems[0])
        self.assertIn("has no --req", problems[1])
        self.assertIn("no subcommand 'fetch'", problems[2])
        self.assertIn("also requires --receipt", problems[3])


class DocumentedEvidenceShapeTests(unittest.TestCase):
    DELIVERY = (SKILL_DIR / "references" / "delivery.md").read_text()

    def test_the_documented_published_branch_evidence_passes_the_ledger_check(self):
        shape = documented_shape(self.DELIVERY, "kind: branch")
        evidence = fill(shape, {"remote": "origin", "remote_ref": "refs/heads/story", "oid": SHA_B, "read_at": iso()})
        self.assertEqual(cs._branch_evidence_problems({}, {}, evidence, SHA_B), [])

    def test_the_documented_completion_and_cleanup_calls_are_accepted(self):
        led = Ledger(TempRoot(self).root)
        led.add(kind="ship")
        led.ship_ready("ws-1")
        led.status("ws-1", "working")
        led.result("ws-1", "req-done", head_sha=SHA_B, generation=led.ws("ws-1")["assignment_generation"])
        led.status("ws-1", "review-ready")
        entry = fill(
            documented_shape(self.DELIVERY, "command, exit_code"),
            {"command": "python3 -B -m unittest discover -s tests", "exit_code": 0, "head_sha": SHA_B},
        )
        led.do(
            "update-workstream",
            "--id",
            "ws-1",
            "--status",
            "completed",
            "--verification",
            json.dumps(entry),
            "--verification",
            verification(SHA_B),
        )
        evidence = fill(
            documented_shape(self.DELIVERY, "clean_worktree"),
            {
                "detail": "git status --porcelain --ignored empty; head on a retained ref",
                "checked_at": iso(),
                "worktree": led.ws("ws-1")["worktree"],
                "head_sha": SHA_B,
            },
        )
        led.do(
            "update-workstream",
            "--id",
            "ws-1",
            "--cleanup-disposition",
            "eligible",
            "--cleanup-evidence",
            json.dumps(evidence),
        )
        self.assertEqual(led.ws("ws-1")["cleanup_disposition"], "eligible")


if __name__ == "__main__":
    unittest.main()

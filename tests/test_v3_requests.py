"""Schema v3 requests and authority: stored bodies, user words, amendments, deliverables and close."""

import json
import os
import unittest

import _guard  # noqa: F401  (must load first: it installs the real-state guard)
import coordinator_state as cs
from helpers import SHA_A, SHA_B, Ledger, TempRoot, all_authority, ok, verification

LANDING = {"executor": "coordinator", "method": "merge", "target_ref": "main", "merged_sha": "9" * 40, "evidence": "x"}


class RequestsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TempRoot(self)
        self.root = self.tmp.root

    def scout(self, led: Ledger) -> str:
        ws = led.add()["workstreams"][-1]["id"]
        led.assign(ws)
        led.status(ws, "working")
        return ws

    def completed_ship(self, led: Ledger, *extra: str) -> str:
        extra = extra or ("--deliverable", "pr", "--forge", "none")
        ws = led.add("ship", None, None, ["local-edit", "push", "pr-create"], "ship", *extra)["workstreams"][-1]["id"]
        led.ship_ready(ws)
        led.status(ws, "working")
        led.complete(ws)
        return ws


class BodyTests(RequestsCase):
    def test_a_follow_up_body_is_stored_once_by_digest_and_printed_for_a_rebrief(self):
        led = Ledger(self.root)
        ws = self.scout(led)
        led.issue(ws, "req-2", "follow-up", "--body", "Add the missing test.")
        request = led.ws(ws)["requests"][-1]
        path = cs.body_path(self.root, led.run_id, request["body_digest"])
        self.assertEqual(path.read_text(), "Add the missing test.")
        shown = ok(self.root, ["snapshot", "--run-id", led.run_id, "--request", "req-2", "--id", ws])
        self.assertEqual(shown["body"], "Add the missing test.")
        brief = ok(self.root, ["snapshot", "--run-id", led.run_id, "--request", "req-2", "--id", ws])
        self.assertEqual(brief["body_path"], str(path))

    def test_a_follow_up_without_a_body_is_refused(self):
        led = Ledger(self.root)
        ws = self.scout(led)
        err = led.refuse("issue-request", "--id", ws, "--request-id", "r", "--purpose", "follow-up")
        self.assertIn("needs a stored body", err)

    def test_a_tampered_body_blocks_completion(self):
        led = Ledger(self.root)
        ws = self.scout(led)
        led.issue(ws, "req-2", "follow-up", "--body", "Fix it.")
        led.result(ws, "req-2", issue=False, base_sha=None)
        led.status(ws, "review-ready")
        path = cs.body_path(self.root, led.run_id, led.ws(ws)["requests"][-1]["body_digest"])
        os.chmod(path, 0o644)
        path.write_text("Something else.")
        err = led.refuse("update-workstream", "--id", ws, "--status", "completed", "--verification", verification(None))
        self.assertIn("does not match its digest", err)

    def test_a_symlinked_body_directory_is_refused(self):
        led = Ledger(self.root)
        ws = self.scout(led)
        elsewhere = self.tmp.base / "elsewhere"
        elsewhere.mkdir()
        (self.root / "runs" / led.run_id).symlink_to(elsewhere)
        err = led.refuse("issue-request", "--id", ws, "--request-id", "r", "--purpose", "follow-up", "--body", "x")
        self.assertIn("symlink", err)
        self.assertEqual(list(elsewhere.iterdir()), [])


class IntentTests(RequestsCase):
    def test_user_words_and_references_are_stored_verbatim(self):
        led = Ledger(self.root)
        refs = json.dumps([{"ref": "docs/spec.md", "digest": "0" * 64}])
        ws = led.add("scout", None, None, None, "s", "--user-words", "make it fast", "--user-references", refs)
        stored = ws["workstreams"][-1]
        self.assertEqual((stored["user_words"], stored["user_references"][0]["ref"]), ("make it fast", "docs/spec.md"))

    def test_an_amendment_supersedes_the_result_before_it(self):
        led = Ledger(self.root)
        ws = self.scout(led)
        led.result(ws, base_sha=None)
        led.do("amend-intent", "--id", ws, "--user-words", "also cover Windows", "--request-id", "req-1")
        self.assertIn("amendment postdates", led.refuse("update-workstream", "--id", ws, "--status", "review-ready"))
        led.issue(ws, "req-2", "amend", "--body", "Also cover Windows.")
        led.result(ws, "req-2", issue=False, base_sha=None)
        led.status(ws, "review-ready")


class AuthorityAmendmentTests(RequestsCase):
    def test_a_later_merge_approval_permits_landing_only_the_named_workstream(self):
        led = Ledger(self.root, authority=all_authority(merge=False))
        ws = self.completed_ship(led)
        other = self.completed_ship(led)
        self.assertIn("withholds merge", led.refuse("record-landing", "--id", ws, "--landing", json.dumps(LANDING)))
        self.assertIn(
            "amend-authority first",
            led.refuse("amend-permissions", "--id", ws, "--add", "merge", "--user-words", "merge that one"),
        )
        led.do("amend-authority", "--action", "merge", "--to", "granted", "--user-words", "go ahead and merge the PR")
        led.do("amend-permissions", "--id", ws, "--add", "merge", "--user-words", "go ahead and merge the PR")
        grants = led.snapshot()["authority"]
        self.assertEqual(grants["amendments"][0]["from"], "withheld")
        self.assertNotIn("merge", led.ws(other)["permitted_operations"])
        led.do(
            "record-denial",
            "--action",
            "merge",
            "--workstream",
            ws,
            "--executor",
            "coordinator",
            "--command",
            "gh pr merge 1",
            "--denial",
            "denied by policy",
            "--executed",
            "no",
        )
        self.assertIn("unresolved", led.refuse("record-landing", "--id", ws, "--landing", json.dumps(LANDING)))
        led.do(
            "resolve-denial", "--id", "den-1", "--resolution", "user approved", "--evidence", "e", "--executed", "no"
        )
        led.do("record-landing", "--id", ws, "--landing", json.dumps(LANDING))
        self.assertIn(
            "not permitted to merge", led.refuse("record-landing", "--id", other, "--landing", json.dumps(LANDING))
        )

    def test_terminal_workstreams_gain_only_post_completion_permissions(self):
        led = Ledger(self.root)
        ws = self.completed_ship(led)
        err = led.refuse("amend-permissions", "--id", ws, "--add", "subagent-create", "--user-words", "w")
        self.assertIn("only ['cleanup', 'merge', 'release']", err)


class DeliverableAndCloseTests(RequestsCase):
    def test_a_permission_never_implies_a_deliverable(self):
        led = Ledger(self.root)
        err = led.refuse(
            "add-workstream",
            "--title",
            "t",
            "--kind",
            "ship",
            "--repository",
            "r",
            "--scope",
            "s",
            "--brief",
            "b",
            "--acceptance-criteria",
            '["x"]',
            "--delivery-authority",
            "d",
            "--permitted-operations",
            '["local-edit"]',
            "--deliverable",
            "pr",
            "--forge-repo",
            "o/r",
            "--target-branch",
            "main",
        )
        self.assertIn("needs workstream permission(s) ['pr-create', 'push']", err)
        err = led.refuse(
            "add-workstream",
            "--title",
            "t",
            "--kind",
            "ship",
            "--repository",
            "r",
            "--scope",
            "s",
            "--brief",
            "b",
            "--acceptance-criteria",
            '["x"]',
            "--delivery-authority",
            "d",
            "--permitted-operations",
            '["local-edit"]',
        )
        self.assertIn("deliverable must be one of", err)

    def test_close_needs_an_integration_disposition_from_the_existing_instruction(self):
        led = Ledger(self.root)
        ws = self.completed_ship(led)
        led.do(
            "update-workstream",
            "--id",
            ws,
            "--cleanup-disposition",
            "retained",
            "--cleanup-evidence",
            '{"reason": "kept"}',
        )
        self.assertIn("without an integration disposition", led.refuse("close", "--outcome", "done"))
        led.do(
            "record-disposition",
            "--id",
            ws,
            "--disposition",
            "left-for-review",
            "--artifact",
            "story-ws-1",
            "--follow-up-owner",
            "the user",
            "--source-instruction",
            "open a PR but do not merge",
        )
        self.assertEqual(led.do("close", "--outcome", "done")["status"], "completed")

    def test_a_landed_deliverable_must_land_before_close(self):
        led = Ledger(self.root)
        perms = ["local-edit", "push", "pr-create", "merge"]
        extra = ("--deliverable", "landed", "--forge", "none")
        ws = led.add("ship", None, None, perms, "ship", *extra)["workstreams"][-1]["id"]
        led.ship_ready(ws)
        led.status(ws, "working")
        led.complete(ws)
        err = led.refuse(
            "record-disposition",
            "--id",
            ws,
            "--disposition",
            "left-for-review",
            "--artifact",
            "a",
            "--follow-up-owner",
            "u",
            "--source-instruction",
            "s",
        )
        self.assertIn("landed or superseded", err)
        self.assertIn(
            "no landing record",
            led.refuse(
                "record-disposition",
                "--id",
                ws,
                "--disposition",
                "landed",
                "--artifact",
                "a",
                "--follow-up-owner",
                "u",
                "--source-instruction",
                "s",
            ),
        )

    def test_a_migrated_ship_without_a_deliverable_cannot_complete_until_one_is_set(self):
        led = Ledger(self.root)
        ws = led.add("ship")["workstreams"][-1]["id"]
        path = self.root / "runs" / f"{led.run_id}.json"
        record = json.loads(path.read_text())
        record["workstreams"][0]["deliverable"] = None
        path.write_text(json.dumps(record))
        led.ship_ready(ws)
        led.status(ws, "working")
        led.result(ws, head_sha=SHA_B, base_sha=SHA_A)
        led.status(ws, "review-ready")
        err = led.refuse("update-workstream", "--id", ws, "--status", "completed", "--verification", verification())
        self.assertIn("set-deliverable first", err)
        led.do("set-deliverable", "--id", ws, "--deliverable", "local-branch", "--user-words", "a local branch is fine")
        led.status(ws, "completed", "--verification", verification())


if __name__ == "__main__":
    unittest.main()

"""Delivery proofs: the read-only GitHub reader, publication and landing predicates, retained
original commits, the agent co-author check and remote base freshness."""

import json
import os
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import _guard  # noqa: F401  (must load first: it installs the real-state guard)
import coordinator_state as cs
import forge_github as fg
import workspace_treehouse as wt
from helpers import REPO_ROOT, SHA_A, Ledger, TempRoot, git, make_repo, verification

FAKE_GH = REPO_ROOT / "tests" / "fixtures" / "fake_gh.py"
HEAD = "1" * 40
MERGE = "2" * 40
APP = 15368


def check_run(name: str, state: str = "SUCCESS", app: int = APP, at: str = "2026-10-31T10:00:00Z") -> dict:
    status = "COMPLETED" if state in {"SUCCESS", "FAILURE", "NEUTRAL"} else "IN_PROGRESS"
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": state if status == "COMPLETED" else None,
        "startedAt": at,
        "checkSuite": {"app": {"databaseId": app, "slug": "github-actions"}},
    }


def facts(**fields) -> dict:
    pr = {
        "number": 7,
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": HEAD,
        "headRefName": "story-ws-1",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "merged": False,
        "mergedAt": None,
        "mergeCommit": None,
    }
    pr.update(fields)
    return {"repository": {"nameWithOwner": "o/r", "pullRequest": pr}}


def checks_page(nodes: list[dict], more: str | None = None) -> dict:
    contexts = {"pageInfo": {"hasNextPage": more is not None, "endCursor": more}, "nodes": nodes}
    commit = {"oid": HEAD, "statusCheckRollup": {"contexts": contexts}}
    return {"repository": {"pullRequest": {"commits": {"nodes": [{"commit": commit}]}}}}


def threads_page(resolved: list[bool], more: str | None = None) -> dict:
    threads = {
        "pageInfo": {"hasNextPage": more is not None, "endCursor": more},
        "nodes": [{"isResolved": r} for r in resolved],
    }
    return {"repository": {"pullRequest": {"reviewThreads": threads}}}


def scenario() -> dict:
    return {
        "rest": {
            "repos/o/r": [200, {"full_name": "o/r"}],
            "repos/o/r/branches/main": [200, {"name": "main"}],
            "repos/o/r/branches/main/protection": [
                200,
                {"required_status_checks": {"checks": [{"context": "test", "app_id": APP}]}},
            ],
            "repos/o/r/rules/branches/main?per_page=100&page=1": [200, []],
            f"repos/o/r/compare/{MERGE}...main": [200, {"status": "ahead"}],
        },
        "graphql": {
            "PrFacts": [facts()],
            "Checks": {"": checks_page([check_run("test"), check_run("lint", app=99)])},
            "Threads": {"": threads_page([True])},
        },
    }


class ForgeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TempRoot(self)
        self.root = self.tmp.root
        self.scenario_path = self.tmp.base / "gh-scenario.json"
        env = {"COORDINATE_GH_BIN": str(FAKE_GH), "FAKE_GH_SCENARIO": str(self.scenario_path)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, data: dict, kind: str = "pr") -> dict:
        self.scenario_path.write_text(json.dumps(data))
        for counter in self.tmp.base.glob("gh-scenario.*.count"):
            counter.unlink()
        reader = fg.pr_evidence if kind == "pr" else fg.landing_evidence
        return reader("o/r", 7, "main", HEAD)


class ForgeReaderTests(ForgeCase):
    def test_a_green_pr_reads_complete_and_certain(self):
        ev = self.read(scenario())
        self.assertEqual((ev["uncertain"], ev["complete"], ev["head_stable"]), (False, True, True))
        self.assertEqual(ev["checks"]["required_passing"], [f"test (app {APP})"])
        self.assertEqual(ev["review_threads"]["unresolved"], 0)

    def test_red_pending_and_missing_required_checks_are_reported(self):
        data = scenario()
        data["graphql"]["Checks"][""] = checks_page([check_run("test", "FAILURE"), check_run("lint", "QUEUED", 99)])
        ev = self.read(data)
        self.assertEqual(ev["checks"]["required_not_passing"], [f"test (app {APP})"])
        self.assertEqual((ev["checks"]["failing"], ev["checks"]["pending"]), (["test"], ["lint"]))
        data["graphql"]["Checks"][""] = checks_page([check_run("lint", app=99)])
        self.assertEqual(self.read(data)["checks"]["required_missing"], [f"test (app {APP})"])

    def test_a_success_from_the_wrong_producer_does_not_satisfy_an_app_bound_check(self):
        data = scenario()
        data["graphql"]["Checks"][""] = checks_page([check_run("test", app=4242)])
        self.assertEqual(self.read(data)["checks"]["required_missing"], [f"test (app {APP})"])

    def test_the_latest_run_per_producer_wins(self):
        data = scenario()
        old = check_run("test", "FAILURE", at="2026-10-31T09:00:00Z")
        data["graphql"]["Checks"][""] = checks_page([old, check_run("test", at="2026-10-31T11:00:00Z")])
        self.assertEqual(self.read(data)["checks"]["failing"], [])

    def test_checks_and_threads_paginate(self):
        data = scenario()
        data["graphql"]["Checks"] = {
            "": checks_page([check_run("lint", app=99)], more="c2"),
            "c2": checks_page([check_run("test")]),
        }
        data["graphql"]["Threads"] = {"": threads_page([True], more="t2"), "t2": threads_page([False])}
        ev = self.read(data)
        self.assertEqual(ev["checks"]["count"], 2)
        self.assertEqual(ev["review_threads"]["unresolved"], 1)

    def test_an_unprotected_branch_is_absence_and_an_unreadable_one_is_uncertain(self):
        data = scenario()
        data["rest"]["repos/o/r/branches/main/protection"] = [404, {"message": "Branch not protected"}]
        ev = self.read(data)
        self.assertEqual((ev["uncertain"], ev["required"]), (False, []))
        data["rest"]["repos/o/r/branches/main/protection"] = [403, {"message": "Resource not accessible"}]
        self.assertTrue(self.read(data)["uncertain"])
        data["rest"]["repos/o/r/branches/main/protection"] = [404, {"message": "Not Found"}]
        self.assertTrue(self.read(data)["uncertain"])

    def test_denied_rulesets_and_an_unreadable_repository_are_uncertain(self):
        data = scenario()
        data["rest"]["repos/o/r/rules/branches/main?per_page=100&page=1"] = [403, {"message": "no"}]
        self.assertTrue(self.read(data)["uncertain"])
        data = scenario()
        del data["rest"]["repos/o/r"]
        self.assertTrue(self.read(data)["uncertain"])

    def test_ruleset_required_checks_are_read(self):
        data = scenario()
        rule = {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "e2e", "integration_id": APP}]},
        }
        data["rest"]["repos/o/r/rules/branches/main?per_page=100&page=1"] = [200, [rule]]
        self.assertIn(f"e2e (app {APP})", self.read(data)["checks"]["required_missing"])

    def test_a_head_that_moves_between_reads_is_uncertain(self):
        data = scenario()
        data["graphql"]["PrFacts"] = [facts(), facts(headRefOid="3" * 40)]
        ev = self.read(data)
        self.assertEqual((ev["head_stable"], ev["uncertain"]), (False, True))

    def test_landing_evidence_reports_merge_facts_and_containment(self):
        data = scenario()
        merged = facts(state="MERGED", merged=True, mergedAt="2026-10-31T12:00:00Z", mergeCommit={"oid": MERGE})
        data["graphql"]["PrFacts"] = [merged]
        ev = self.read(data, "landing")
        self.assertEqual((ev["merged"], ev["merge_commit"], ev["target_contains_merge_commit"]), (True, MERGE, True))
        data["rest"][f"repos/o/r/compare/{MERGE}...main"] = [200, {"status": "diverged"}]
        self.assertFalse(self.read(data, "landing")["target_contains_merge_commit"])


class ShipCase(ForgeCase):
    def ship(self, led: Ledger, deliverable: str = "pr", *extra: str) -> str:
        perms = ["local-edit", "push", "pr-create", "merge"]
        args = ("--deliverable", deliverable, "--forge-repo", "o/r", "--target-branch", "main", *extra)
        ws = led.add("ship", None, None, perms, "ship", *args)["workstreams"][-1]["id"]
        led.ship_ready(ws)
        led.status(ws, "working")
        led.result(ws, head_sha=HEAD, base_sha=SHA_A)
        led.status(ws, "review-ready")
        return ws

    def refusal(self, led: Ledger, ws: str, data: dict) -> str:
        return led.refuse("record-publication", "--id", ws, "--evidence", json.dumps(self.read(data)))


class PublicationPredicateTests(ShipCase):
    def test_a_green_pr_binds_and_completion_then_passes(self):
        led = Ledger(self.root)
        ws = self.ship(led)
        self.assertIn(
            "no PR evidence",
            led.refuse(
                "update-workstream",
                "--id",
                ws,
                "--status",
                "completed",
                "--verification",
                json.dumps({"command": "c", "check": "agent-coauthors", "exit_code": 0, "head_sha": HEAD}),
            ),
        )
        led.do("record-publication", "--id", ws, "--evidence", json.dumps(self.read(scenario())))
        self.assertEqual(led.ws(ws)["pr"], 7)
        led.status(
            ws,
            "completed",
            "--verification",
            json.dumps({"command": "c", "check": "agent-coauthors", "exit_code": 0, "head_sha": HEAD}),
        )

    def test_pending_ci_unresolved_threads_and_drafts_block(self):
        led = Ledger(self.root)
        ws = self.ship(led)
        data = scenario()
        data["graphql"]["Checks"][""] = checks_page([check_run("test"), check_run("lint", "QUEUED", 99)])
        self.assertIn("checks pending", self.refusal(led, ws, data))
        data = scenario()
        data["graphql"]["Threads"][""] = threads_page([False])
        self.assertIn("unresolved review thread", self.refusal(led, ws, data))
        data = scenario()
        data["graphql"]["PrFacts"] = [facts(isDraft=True)]
        self.assertIn("draft", self.refusal(led, ws, data))

    def test_evidence_for_another_repository_base_or_pr_is_refused(self):
        led = Ledger(self.root)
        ws = self.ship(led)
        data = scenario()
        data["graphql"]["PrFacts"] = [facts(baseRefName="develop")]
        self.assertIn("is not the target", self.refusal(led, ws, data))
        ev = self.read(scenario())
        led.do("record-publication", "--id", ws, "--evidence", json.dumps(ev))
        other = {**ev, "pr": 8}
        self.assertIn("bound PR", led.refuse("record-publication", "--id", ws, "--evidence", json.dumps(other)))
        foreign = {**ev, "repo": "o/other"}
        self.assertIn(
            "evidence repository", led.refuse("record-publication", "--id", ws, "--evidence", json.dumps(foreign))
        )

    def test_empty_checks_pass_only_with_no_ci_and_no_required_checks(self):
        data = scenario()
        data["rest"]["repos/o/r/branches/main/protection"] = [404, {"message": "Branch not protected"}]
        data["graphql"]["Checks"][""] = checks_page([])
        led = Ledger(self.root)
        ws = self.ship(led)
        self.assertIn("no checks ran", self.refusal(led, ws, data))
        ci_free = self.ship(led, "pr", "--no-ci")
        data["graphql"]["PrFacts"] = [facts(headRefName=f"story-{ci_free}")]
        led.do("record-publication", "--id", ci_free, "--evidence", json.dumps(self.read(data)))

    def test_stale_evidence_is_refused(self):
        led = Ledger(self.root)
        ws = self.ship(led)
        ev = self.read(scenario())
        ev["read_at"] = (cs._now() - timedelta(hours=2)).isoformat(timespec="seconds")
        self.assertIn("older than", led.refuse("record-publication", "--id", ws, "--evidence", json.dumps(ev)))


class LandingPredicateTests(ShipCase):
    def completed(self, led: Ledger) -> tuple[str, dict]:
        ws = self.ship(led, "landed")
        pre = self.read(scenario())
        led.do("record-publication", "--id", ws, "--evidence", json.dumps(pre))
        led.status(
            ws,
            "completed",
            "--verification",
            json.dumps({"command": "c", "check": "agent-coauthors", "exit_code": 0, "head_sha": HEAD}),
        )
        return ws, pre

    def landing(self, pre: dict, **post_fields) -> dict:
        data = scenario()
        merged_at = cs._now_iso()
        data["graphql"]["PrFacts"] = [
            facts(state="MERGED", merged=True, mergedAt=merged_at, mergeCommit={"oid": MERGE})
        ]
        post = self.read(data, "landing")
        post.update(post_fields)
        return {
            "executor": "coordinator",
            "method": "squash",
            "target_ref": "main",
            "merged_sha": MERGE,
            "evidence": "gh pr merge 7 --match-head-commit",
            "retained_ref": "refs/coordinate/retained/x",
            "pr": 7,
            "pre_merge": pre,
            "forge": post,
        }

    def test_a_proven_landing_is_recorded(self):
        led = Ledger(self.root)
        ws, pre = self.completed(led)
        led.do("record-landing", "--id", ws, "--landing", json.dumps(self.landing(pre)))
        self.assertEqual(led.ws(ws)["landing"]["landed_head"], HEAD)

    def test_a_different_merged_head_or_missing_containment_is_refused(self):
        led = Ledger(self.root)
        ws, pre = self.completed(led)
        err = led.refuse("record-landing", "--id", ws, "--landing", json.dumps(self.landing(pre, source_head="4" * 40)))
        self.assertIn("is not the verified head", err)
        err = led.refuse(
            "record-landing", "--id", ws, "--landing", json.dumps(self.landing(pre, target_contains_merge_commit=False))
        )
        self.assertIn("does not contain the merge commit", err)

    def test_chronology_is_enforced(self):
        led = Ledger(self.root)
        ws, pre = self.completed(led)
        early = (cs._now() - timedelta(hours=1)).isoformat(timespec="seconds")
        err = led.refuse("record-landing", "--id", ws, "--landing", json.dumps(self.landing(pre, merged_at=early)))
        self.assertIn("chronology", err)

    def test_a_queued_merge_stays_pending_and_surfaces_in_due(self):
        led = Ledger(self.root)
        ws, _ = self.completed(led)
        led.do("record-landing-pending", "--id", ws, "--command", "gh pr merge 7", "--outcome", "queued")
        kinds = {i["kind"] for i in cs.due_items(self.root, led.snapshot())}
        self.assertIn("landing-pending", kinds)


class RetainHeadTests(unittest.TestCase):
    """A real remote with a pull ref, a squash merge, and a create-only retained ref."""

    def setUp(self):
        self.tmp = TempRoot(self)
        self.root = self.tmp.root
        base = self.tmp.base
        self.repo = base / "work"
        self.base_sha = make_repo(self.repo)
        git(self.repo, "switch", "-q", "-c", "story-ws-1")
        (self.repo / "story.txt").write_text("story\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "story")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.remote = base / "o" / "r.git"
        self.remote.parent.mkdir()
        git(base, "init", "-q", "--bare", str(self.remote))
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-q", "origin", "main", "story-ws-1")
        git(self.remote, "update-ref", "refs/pull/7/head", self.head)
        self.led = Ledger(self.root)
        perms = ["local-edit", "push", "pr-create"]
        self.led.add(
            "ship", None, None, perms, "ship", "--deliverable", "pr", "--forge-repo", "o/r", "--target-branch", "main"
        )
        self.led.assign(
            "ws-1", "worker-1", "--worktree", str(self.repo), "--branch", "story-ws-1", "--base-sha", self.base_sha
        )
        common = str(Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve())
        self.led.receipt(
            "ws-1",
            path=str(self.repo),
            branch="story-ws-1",
            repository_common_dir=common,
            expected_base_sha=self.base_sha,
            observed_head_sha=self.base_sha,
        )
        self.led.status("ws-1", "working")
        self.complete()

    def complete(self) -> None:
        led, head = self.led, self.head
        led.result("ws-1", "req-done", head_sha=head, base_sha=self.base_sha)
        led.status("ws-1", "review-ready")
        with mock.patch.object(cs, "_publication_problems", return_value=[]):
            # Publication proof is covered by the predicate tests; this class proves retention.
            led.status("ws-1", "completed", "--verification", verification(head))

    def retain(self, **overrides) -> dict:
        args = dict(
            run_id=self.led.run_id,
            ws_id="ws-1",
            repo_path=self.repo,
            remote="origin",
            expected_remote_url=str(self.remote),
            pr=7,
            expect_head=self.head,
        )
        args.update(overrides)
        return wt.retain_head(self.root, **args)

    def test_a_retained_ref_keeps_the_tested_commits_after_a_squash_and_branch_deletion(self):
        out = self.retain()
        self.assertTrue(out["created"])
        git(self.repo, "switch", "-q", "main")
        git(self.repo, "merge", "-q", "--squash", "story-ws-1")
        git(self.repo, "commit", "-q", "-m", "squash")
        git(self.repo, "branch", "-q", "-D", "story-ws-1")
        self.assertEqual(git(self.repo, "rev-parse", out["ref"]), self.head)
        self.assertFalse(self.retain()["created"])

    def test_a_conflicting_ref_is_left_unchanged(self):
        ref = f"refs/coordinate/retained/{self.led.run_id}/ws-1/g1-a1-{self.head}"
        git(self.repo, "update-ref", ref, self.base_sha)
        with self.assertRaisesRegex(wt.AdapterError, "left unchanged"):
            self.retain()
        self.assertEqual(git(self.repo, "rev-parse", ref), self.base_sha)

    def test_the_wrong_remote_or_repository_is_refused(self):
        with self.assertRaisesRegex(wt.AdapterError, "does not name o/r"):
            self.retain(expected_remote_url="/elsewhere/x/y.git")
        git(self.repo, "remote", "add", "fork", str(self.tmp.base / "fork" / "o" / "r.git"))
        with self.assertRaisesRegex(wt.AdapterError, "points at"):
            self.retain(remote="fork")
        other = self.tmp.base / "other"
        make_repo(other)
        with self.assertRaisesRegex(wt.AdapterError, "not the workstream's repository"):
            self.retain(repo_path=other)

    def test_a_pull_ref_holding_another_head_is_refused(self):
        git(self.remote, "update-ref", "refs/pull/7/head", self.base_sha)
        with self.assertRaisesRegex(wt.AdapterError, "holds"):
            self.retain()

    def test_retention_needs_the_cleanup_grant(self):
        self.led.do("amend-authority", "--action", "cleanup", "--to", "withheld", "--user-words", "keep everything")
        with self.assertRaisesRegex(wt.AdapterError, "withholds cleanup"):
            self.retain()
        self.assertEqual(git(self.repo, "for-each-ref", "refs/coordinate"), "")

    def test_retention_grants_nothing(self):
        before = (self.root / "runs" / f"{self.led.run_id}.json").read_bytes()
        self.retain()
        self.assertEqual((self.root / "runs" / f"{self.led.run_id}.json").read_bytes(), before)
        self.assertIsNone(self.led.ws("ws-1")["cleanup_disposition"])


class CoauthorAndFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TempRoot(self)
        self.repo = self.tmp.base / "repo"
        self.base = make_repo(self.repo)

    def commit(self, message: str) -> str:
        (self.repo / f"f{len(message)}.txt").write_text(message)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def test_agent_trailers_fail_and_human_coauthors_pass(self):
        human = self.commit("feat: x\n\nCo-authored-by: Aidan Smith <aidan@anthropic.com>")
        self.assertEqual(wt.check_coauthors(self.repo, self.base, human)["exit_code"], 0)
        agent = self.commit("fix: y\n\nCo-authored-by: Claude <noreply@anthropic.com>")
        entry = wt.check_coauthors(self.repo, self.base, agent)
        self.assertEqual((entry["exit_code"], entry["head_sha"]), (1, agent))
        self.assertEqual(len(entry["offending"]), 1)

    def test_fetch_remote_refreshes_a_remote_tracking_base_and_refuses_others(self):
        remote = self.tmp.base / "remote.git"
        git(self.tmp.base, "init", "-q", "--bare", str(remote))
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")
        clone = self.tmp.base / "clone"
        git(self.tmp.base, "clone", "-q", "-b", "main", str(remote), str(clone))
        (clone / "new.txt").write_text("new\n")
        git(clone, "add", "-A")
        git(clone, "commit", "-q", "-m", "moved")
        git(clone, "push", "-q", "origin", "main")
        moved = git(clone, "rev-parse", "HEAD")
        self.assertNotEqual(git(self.repo, "rev-parse", "origin/main"), moved)
        wt._fetch_remote_base(self.repo, "origin/main")
        self.assertEqual(git(self.repo, "rev-parse", "origin/main"), moved)
        with self.assertRaisesRegex(wt.AdapterError, "remote-tracking base"):
            wt._fetch_remote_base(self.repo, "main")
        git(self.repo, "remote", "set-url", "origin", str(self.tmp.base / "gone.git"))
        with self.assertRaisesRegex(wt.AdapterError, "nothing was reserved"):
            wt._fetch_remote_base(self.repo, "origin/main")


if __name__ == "__main__":
    unittest.main()

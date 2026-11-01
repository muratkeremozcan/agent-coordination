"""Run profiles resolve policy explicitly and refuse contradictions of package invariants."""

import tomllib
import unittest

import _guard  # noqa: F401  (must load first: it installs the real-state guard)
import run_profile
from helpers import DEFAULT_PROFILE, EXAMPLE_PROFILE, TempRoot


def mutated(**changes) -> dict:
    data = tomllib.loads(DEFAULT_PROFILE.read_text())
    for dotted, value in changes.items():
        section, key = dotted.split("__")
        if value is ...:
            del data[section][key]
        else:
            data[section][key] = value
    return data


class ProfileTests(unittest.TestCase):
    def test_a_profile_without_the_newer_keys_resolves_to_documented_defaults(self):
        data = tomllib.loads(DEFAULT_PROFILE.read_text())
        del data["supervision"]
        del data["delivery"]["evidence_max_age_minutes"]
        del data["delivery"]["forbid_agent_coauthors"]
        resolved = run_profile.validate_profile(data)
        self.assertEqual(resolved["supervision"]["result_window_minutes"], 90)
        self.assertEqual(resolved["delivery"]["forbid_agent_coauthors"], True)
        self.assertEqual(resolved, run_profile.validate_profile(tomllib.loads(DEFAULT_PROFILE.read_text())))

    def test_newer_keys_are_validated_when_present(self):
        with self.assertRaises(run_profile.ProfileError):
            run_profile.validate_profile(mutated(supervision__ack_window_minutes=0))
        with self.assertRaises(run_profile.ProfileError):
            run_profile.validate_profile(mutated(supervision__surprise=1))

    def test_shipped_profiles_validate_with_stable_digests(self):
        for path in (DEFAULT_PROFILE, EXAMPLE_PROFILE):
            first = run_profile.load_profile(path)
            self.assertEqual(first, run_profile.load_profile(path))
            self.assertEqual(len(first[1]), 64)

    def test_invariants_cannot_be_overridden(self):
        cases = {
            "topology__state_writer": "workers",
            "topology__one_writable_assignment_per_workspace": False,
            "context__retain_owner_through_fixes": False,
            "cleanup__workspaces": "on-idle",
            "runtime__explicit_choice_wins": False,
        }
        for dotted, value in cases.items():
            with self.subTest(dotted), self.assertRaises(run_profile.ProfileError):
                run_profile.validate_profile(mutated(**{dotted: value}))

    def test_lane_reuse_belongs_to_the_lanes_topology(self):
        with self.assertRaisesRegex(run_profile.ProfileError, "lane_reuse must be 'never'"):
            run_profile.validate_profile(mutated(topology__lane_reuse="verified-boundary"))

    def test_a_reviewer_cannot_be_required_and_allowed_to_be_absent(self):
        data = mutated(review__allow_unavailable=["independent"])
        with self.assertRaisesRegex(run_profile.ProfileError, "both required and allowed"):
            run_profile.validate_profile(data)

    def test_unknown_missing_and_mistyped_keys_are_refused(self):
        with self.assertRaisesRegex(run_profile.ProfileError, "unknown key"):
            run_profile.validate_profile(mutated(limits__watchdog_minutes=12))
        with self.assertRaisesRegex(run_profile.ProfileError, "missing key"):
            run_profile.validate_profile(mutated(limits__max_active_workstreams=...))
        with self.assertRaisesRegex(run_profile.ProfileError, "positive integer"):
            run_profile.validate_profile(mutated(limits__max_active_workstreams=True))
        with self.assertRaisesRegex(run_profile.ProfileError, "unsupported profile version"):
            run_profile.validate_profile(mutated(profile__version=2))

    def test_invalid_toml_is_reported_with_the_path(self):
        tmp = TempRoot(self)
        bad = tmp.base / "bad.toml"
        bad.write_text("[profile\n")
        with self.assertRaisesRegex(run_profile.ProfileError, "not valid TOML"):
            run_profile.load_profile(bad)


if __name__ == "__main__":
    unittest.main()

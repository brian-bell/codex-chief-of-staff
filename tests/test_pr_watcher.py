from __future__ import annotations

import json
import copy
import sys
import tracemalloc
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_chief_of_staff.pr_watcher import (
    RetryPolicy,
    classify_github_response,
    observe_with_retry,
)


FIXTURES = ROOT / "tests" / "fixtures" / "github-pr"
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


class PullRequestWatcherTest(unittest.TestCase):
    def load(self, name: str = "merge-ready.json") -> dict:
        return json.loads((FIXTURES / name).read_text())

    def test_complete_green_pull_request_is_merge_ready(self) -> None:
        payload = json.loads((FIXTURES / "merge-ready.json").read_text())

        result = classify_github_response(
            "example/project", 17, payload, clock=lambda: NOW
        )

        self.assertEqual(result["classification"], "merge-ready")
        self.assertEqual(result["repository"], "example/project")
        self.assertEqual(result["pull_request"], 17)
        self.assertEqual(result["observed_head_sha"], "a" * 40)
        self.assertEqual(result["observation_time"], "2026-08-29T12:00:00Z")
        self.assertEqual(result["checks"][0]["name"], "test")
        self.assertEqual(result["reviews"][0]["id"], "PRR_1")
        self.assertEqual(
            result["counts"],
            {"conversation_comments": 2, "review_threads": 0, "reviews": 1},
        )
        self.assertRegex(result["fingerprint"], r"^sha256:[0-9a-f]{64}$")

    def test_null_status_check_rollup_means_no_checks(self) -> None:
        payload = self.load()
        repository = payload["data"]["repository"]
        repository["pullRequest"]["statusCheckRollup"] = None

        result = classify_github_response(
            "example/project", 17, payload, clock=lambda: NOW
        )

        self.assertEqual(result["classification"], "checks-pending")

        rule = repository["branchProtectionRules"]["nodes"][0]
        rule["requiresStatusChecks"] = False
        rule["requiredStatusCheckContexts"] = []

        result = classify_github_response(
            "example/project", 17, payload, clock=lambda: NOW
        )

        self.assertEqual(result["classification"], "merge-ready")
        self.assertEqual(result["checks"], [])
        self.assertFalse(result["partial_response"]["active"])

    def test_changed_head_precedes_other_conditions_and_invalidates_verdict(self) -> None:
        payload = self.load()
        pr = payload["data"]["repository"]["pullRequest"]
        pr["mergeable"] = "CONFLICTING"
        pr["statusCheckRollup"]["contexts"]["nodes"][0]["conclusion"] = "FAILURE"

        result = classify_github_response(
            "example/project",
            17,
            payload,
            expected_head="b" * 40,
            verdict_head="b" * 40,
            clock=lambda: NOW,
        )

        self.assertEqual(result["classification"], "stale-head")
        self.assertFalse(result["verdict_applies"])
        self.assertIn("expected head", result["reason"])

    def test_every_supplied_authoritative_head_is_checked_independently(self) -> None:
        current = "a" * 40
        old = "b" * 40
        cases = (
            (current, old, "stale-head", False),
            (old, current, "stale-head", True),
            (old, old, "stale-head", False),
            (current, current, "merge-ready", True),
        )
        for expected, verdict, classification, verdict_applies in cases:
            with self.subTest(expected=expected, verdict=verdict):
                result = classify_github_response(
                    "example/project",
                    17,
                    self.load(),
                    expected_head=expected,
                    verdict_head=verdict,
                    clock=lambda: NOW,
                )
                self.assertEqual(result["classification"], classification)
                self.assertEqual(result["verdict_applies"], verdict_applies)

        absent = {"data": {"repository": {"pullRequest": None}}}
        result = classify_github_response(
            "example/project",
            17,
            absent,
            expected_head=current,
            verdict_head=current,
            clock=lambda: NOW,
        )
        self.assertEqual(result["classification"], "product-gate")
        self.assertFalse(result["verdict_applies"])

    def test_saved_github_responses_cover_each_non_stale_classification(self) -> None:
        cases = self.load("classification-cases.json")
        seen = {"merge-ready"}
        for case in cases:
            with self.subTest(classification=case["classification"]):
                payload = self.load()
                pr = payload["data"]["repository"]["pullRequest"]
                self._merge(pr, copy.deepcopy(case["overrides"]))
                if case.get("payloadErrors"):
                    payload["errors"] = copy.deepcopy(case["payloadErrors"])
                result = classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
                self.assertEqual(result["classification"], case["classification"])
                seen.add(result["classification"])
        self.assertEqual(
            seen,
            {
                "blocking-review-feedback",
                "checks-pending",
                "conflict",
                "deterministic-check-failure",
                "merge-ready",
                "plausible-transient-failure",
                "product-gate",
            },
        )

    def test_retry_policy_only_retries_explicit_transient_failures(self) -> None:
        deterministic = self.load()
        transient = self.load()
        pending = self.load()
        cases = self.load("classification-cases.json")
        by_name = {case["classification"]: case["overrides"] for case in cases}
        self._merge(
            deterministic["data"]["repository"]["pullRequest"],
            copy.deepcopy(by_name["deterministic-check-failure"]),
        )
        transient["errors"] = [{"path": [], "type": "RATE_LIMITED"}]
        self._merge(
            pending["data"]["repository"]["pullRequest"],
            copy.deepcopy(by_name["checks-pending"]),
        )

        for payload, expected in (
            (deterministic, "deterministic-check-failure"),
            (pending, "checks-pending"),
        ):
            calls = []
            sleeps = []
            result = observe_with_retry(
                lambda _repo, _pr: calls.append(1) or payload,
                "example/project",
                17,
                policy=RetryPolicy(max_retries=2, backoff_seconds=(1.0, 2.0)),
                clock=lambda: NOW,
                sleeper=sleeps.append,
            )
            self.assertEqual(result["classification"], expected)
            self.assertEqual(len(calls), 1)
            self.assertEqual(sleeps, [])

        calls = []
        sleeps = []
        exhausted = observe_with_retry(
            lambda _repo, _pr: calls.append(1) or transient,
            "example/project",
            17,
            policy=RetryPolicy(max_retries=2, backoff_seconds=(1.0, 2.0)),
            clock=lambda: NOW,
            sleeper=sleeps.append,
        )
        self.assertEqual(exhausted["classification"], "plausible-transient-failure")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertEqual(
            exhausted["retry"],
            {"attempts": 3, "delays_seconds": [1.0, 2.0], "exhausted": True},
        )

    def test_incomplete_forbidden_and_malformed_responses_are_conservative(self) -> None:
        partial = self.load()
        repository = partial["data"]["repository"]
        del repository["pullRequest"]["reviewThreads"]
        del repository["branchProtectionRules"]
        partial["errors"] = [
            {
                "type": "FORBIDDEN",
                "path": ["repository", "pullRequest", "reviewThreads"],
                "message": "Resource not accessible by integration",
            }
        ]
        result = classify_github_response(
            "example/project", 17, partial, expected_head="a" * 40, clock=lambda: NOW
        )
        self.assertEqual(result["classification"], "product-gate")
        self.assertTrue(result["partial_response"]["active"])
        self.assertEqual(result["permissions"]["review_threads"], "unavailable")
        self.assertEqual(result["permissions"]["branch_protection"], "unavailable")
        self.assertNotIn("Resource not accessible", json.dumps(result))

        malformed = classify_github_response(
            "example/project",
            17,
            {"data": {"repository": {"pullRequest": "not-an-object"}}},
            clock=lambda: NOW,
        )
        self.assertEqual(malformed["classification"], "product-gate")
        self.assertIsNone(malformed["observed_head_sha"])
        self.assertTrue(malformed["partial_response"]["active"])

    def test_malformed_graphql_error_shapes_are_conservative(self) -> None:
        malformed_errors = (
            None,
            {},
            "RATE_LIMITED",
            7,
            [None],
            ["FORBIDDEN"],
            [{}],
            [{"path": []}],
            [{"type": 7, "path": []}],
            [{"type": "RATE_LIMITED", "path": "repository"}],
            [{"type": "RATE_LIMITED", "path": [-1]}],
            [{"type": "RATE_LIMITED", "path": [True]}],
            [{"type": "RATE_LIMITED", "extensions": None}],
            [{"type": "RATE_LIMITED", "extensions": "bad"}],
            [{"type": "RATE_LIMITED", "locations": [{"line": True, "column": 1}]}],
        )
        for errors in malformed_errors:
            with self.subTest(errors=errors):
                payload = self.load()
                payload["errors"] = errors
                result = classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
                self.assertEqual(result["classification"], "product-gate")
                self.assertTrue(result["partial_response"]["active"])
                self.assertIn("errors", result["partial_response"]["missing_fields"])

        valid = self.load()
        valid["errors"] = [
            {
                "extensions": {"code": "RATE_LIMITED"},
                "locations": [{"column": 2, "line": 1}],
                "message": "untrusted secret text",
                "path": ["repository", "pullRequest", 0],
                "type": "RATE_LIMITED",
            }
        ]
        result = classify_github_response(
            "example/project", 17, valid, clock=lambda: NOW
        )
        self.assertEqual(result["classification"], "plausible-transient-failure")
        self.assertNotIn("untrusted secret text", json.dumps(result))

    def test_mixed_graphql_errors_are_nontransient_and_order_independent(self) -> None:
        mixtures = (
            ("RATE_LIMITED", "FORBIDDEN"),
            ("RATE_LIMITED", "INTERNAL"),
            ("RATE_LIMITED", "NOT_FOUND"),
        )
        for types in mixtures:
            outputs = []
            errors = [
                {"path": ["repository", index], "type": error_type}
                for index, error_type in enumerate(types)
            ]
            for ordered in (errors, list(reversed(errors))):
                payload = self.load()
                payload["errors"] = ordered
                calls = []
                delays = []
                result = observe_with_retry(
                    lambda _repository, _pull_request: calls.append(1) or payload,
                    "example/project",
                    17,
                    policy=RetryPolicy(max_retries=2, backoff_seconds=(1.0, 2.0)),
                    clock=lambda: NOW,
                    sleeper=delays.append,
                )
                self.assertEqual(result["classification"], "product-gate")
                self.assertEqual(len(calls), 1)
                self.assertEqual(delays, [])
                outputs.append(result)
            self.assertEqual(outputs[0]["fingerprint"], outputs[1]["fingerprint"])
            self.assertEqual(outputs[0]["partial_response"]["errors"], outputs[1]["partial_response"]["errors"])

        transient = self.load()
        transient["errors"] = [
            {"path": ["repository", 0], "type": "RATE_LIMITED"},
            {"path": ["repository", 1], "type": "RATE_LIMITED"},
        ]
        calls = []
        delays = []
        result = observe_with_retry(
            lambda _repository, _pull_request: calls.append(1) or transient,
            "example/project",
            17,
            policy=RetryPolicy(max_retries=2, backoff_seconds=(1.0, 2.0)),
            clock=lambda: NOW,
            sleeper=delays.append,
        )
        self.assertEqual(result["classification"], "plausible-transient-failure")
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, [1.0, 2.0])

    def test_provider_error_evidence_is_bounded_without_changing_semantics(self) -> None:
        oversized = "secret-prefix-" + ("x" * 1_000_000) + "-secret-suffix"
        errors = [
            {
                "extensions": {"detail": oversized},
                "locations": [{"column": 2, "line": 1} for _ in range(1_000)],
                "message": oversized,
                "path": [oversized, "pullRequest", index],
                "type": "RATE_LIMITED",
            }
            for index in range(40)
        ]
        outputs = []
        for ordered in (errors, list(reversed(errors))):
            payload = self.load()
            payload["errors"] = ordered
            outputs.append(
                classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
            )
        for result in outputs:
            encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
            self.assertEqual(result["classification"], "plausible-transient-failure")
            self.assertLessEqual(len(result["partial_response"]["errors"]), 16)
            self.assertEqual(result["partial_response"]["error_count"], 40)
            self.assertTrue(result["partial_response"]["errors_truncated"])
            self.assertTrue(
                all(
                    len(part) <= 80
                    for error in result["partial_response"]["errors"]
                    for part in error["path"]
                )
            )
            self.assertLess(len(encoded), 10_000)
            self.assertNotIn("secret-suffix", encoded)
        self.assertEqual(outputs[0]["partial_response"]["errors"], outputs[1]["partial_response"]["errors"])
        self.assertEqual(outputs[0]["fingerprint"], outputs[1]["fingerprint"])

        payload = self.load()
        payload["errors"] = errors
        calls = []
        delays = []
        result = observe_with_retry(
            lambda _repository, _pull_request: calls.append(1) or payload,
            "example/project",
            17,
            policy=RetryPolicy(max_retries=1, backoff_seconds=(1.0,)),
            clock=lambda: NOW,
            sleeper=delays.append,
        )
        self.assertEqual(result["classification"], "plausible-transient-failure")
        self.assertEqual(len(calls), 2)
        self.assertEqual(delays, [1.0])

        mixed = self.load()
        mixed["errors"] = [*errors, {"path": ["repository"], "type": "INTERNAL"}]
        calls = []
        delays = []
        result = observe_with_retry(
            lambda _repository, _pull_request: calls.append(1) or mixed,
            "example/project",
            17,
            policy=RetryPolicy(max_retries=2, backoff_seconds=(1.0, 2.0)),
            clock=lambda: NOW,
            sleeper=delays.append,
        )
        self.assertEqual(result["classification"], "product-gate")
        self.assertEqual(len(calls), 1)
        self.assertEqual(delays, [])

    def test_integer_error_paths_have_fixed_canonical_bounds(self) -> None:
        integers = (
            int("7" * 79),
            int("8" * 80),
            int("9" * 81),
            (10 ** 100_000) + 123456789,
        )
        errors = [
            {"path": ["repository", value], "type": "RATE_LIMITED"}
            for value in integers
        ]
        outputs = []
        for ordered in (errors, list(reversed(errors))):
            payload = self.load()
            payload["errors"] = ordered
            outputs.append(
                classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
            )
        for result in outputs:
            self.assertEqual(result["classification"], "plausible-transient-failure")
            self.assertEqual(result["partial_response"]["error_count"], 4)
            self.assertFalse(result["partial_response"]["errors_truncated"])
            numeric_parts = [
                error["path"][1]
                for error in result["partial_response"]["errors"]
            ]
            self.assertTrue(all(len(part) <= 80 for part in numeric_parts))
            self.assertIn("7" * 79, numeric_parts)
            self.assertIn("8" * 80, numeric_parts)
            self.assertEqual(len(set(numeric_parts)), 4)
            self.assertEqual(sum(part.startswith("<int:") for part in numeric_parts), 2)
        self.assertEqual(outputs[0]["partial_response"]["errors"], outputs[1]["partial_response"]["errors"])
        self.assertEqual(outputs[0]["fingerprint"], outputs[1]["fingerprint"])

    def test_oversized_integer_error_paths_digest_every_bit(self) -> None:
        bit_length = 8_192
        common = (
            (0xFEDCBA9876543210 << (bit_length - 64))
            | 0x0123456789ABCDEF
        )
        first_integer = common | (1 << 2_000)
        second_integer = common | (1 << 3_000)

        individual = []
        for value in (first_integer, second_integer):
            payload = self.load()
            payload["errors"] = [
                {"path": ["repository", value], "type": "RATE_LIMITED"}
            ]
            individual.append(
                classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
            )

        first_token = individual[0]["partial_response"]["errors"][0]["path"][1]
        second_token = individual[1]["partial_response"]["errors"][0]["path"][1]
        self.assertNotEqual(first_token, second_token)
        self.assertLessEqual(len(first_token), 80)
        self.assertLessEqual(len(second_token), 80)
        self.assertNotEqual(individual[0]["fingerprint"], individual[1]["fingerprint"])

        combined = []
        errors = [
            {"path": ["repository", first_integer], "type": "RATE_LIMITED"},
            {"path": ["repository", second_integer], "type": "RATE_LIMITED"},
        ]
        for ordered in (errors, list(reversed(errors))):
            payload = self.load()
            payload["errors"] = ordered
            combined.append(
                classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
            )
        self.assertEqual(combined[0], combined[1])

    def test_oversized_integer_normalization_keeps_output_and_working_memory_bounded(self) -> None:
        value = (10 ** 100_000) | (1 << 100_000) | 1
        payload = self.load()
        payload["errors"] = [
            {"path": ["repository", value], "type": "RATE_LIMITED"}
        ]

        tracemalloc.start()
        try:
            result = classify_github_response(
                "example/project", 17, payload, clock=lambda: NOW
            )
            retained_bytes, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        token = result["partial_response"]["errors"][0]["path"][1]
        binary_bytes = (value.bit_length() + 7) // 8
        self.assertEqual(len(token), 77)
        self.assertTrue(token.startswith("<int:sha256:"))
        self.assertLess(retained_bytes, 20_000)
        self.assertLess(peak_bytes, binary_bytes * 3)

    def test_source_list_order_does_not_change_output_or_fingerprint(self) -> None:
        first = self.load()
        pr = first["data"]["repository"]["pullRequest"]
        pr["reviews"]["nodes"].append(
            {"author": {"login": "another"}, "id": "PRR_0", "state": "APPROVED", "submittedAt": "2026-08-28T11:00:00Z"}
        )
        pr["statusCheckRollup"]["contexts"]["nodes"].append(
            {"__typename": "CheckRun", "completedAt": "2026-08-28T12:04:00Z", "conclusion": "SUCCESS", "detailsUrl": "https://github.com/example/project/actions/runs/2", "name": "lint", "startedAt": "2026-08-28T12:00:00Z", "status": "COMPLETED"}
        )
        second = copy.deepcopy(first)
        second_pr = second["data"]["repository"]["pullRequest"]
        second_pr["reviews"]["nodes"].reverse()
        second_pr["statusCheckRollup"]["contexts"]["nodes"].reverse()

        one = classify_github_response("example/project", 17, first, clock=lambda: NOW)
        two = classify_github_response("example/project", 17, second, clock=lambda: NOW)
        self.assertEqual(one, two)
        self.assertEqual(one["fingerprint"], two["fingerprint"])

    def test_precedence_is_stale_conflict_review_product_pending_failure_transient_ready(self) -> None:
        cases = self.load("classification-cases.json")
        overrides = {case["classification"]: case["overrides"] for case in cases}
        combinations = (
            ("conflict", ("conflict", "blocking-review-feedback", "product-gate", "checks-pending")),
            ("blocking-review-feedback", ("blocking-review-feedback", "product-gate", "checks-pending")),
            ("product-gate", ("product-gate", "checks-pending", "deterministic-check-failure")),
            ("checks-pending", ("checks-pending", "deterministic-check-failure")),
        )
        for expected, names in combinations:
            payload = self.load()
            pr = payload["data"]["repository"]["pullRequest"]
            for name in reversed(names):
                self._merge(pr, copy.deepcopy(overrides[name]))
            result = classify_github_response(
                "example/project", 17, payload, clock=lambda: NOW
            )
            self.assertEqual(result["classification"], expected)

    def test_required_review_and_check_evidence_prevent_false_merge_ready(self) -> None:
        review_missing = self.load()
        review_missing["data"]["repository"]["pullRequest"]["reviewDecision"] = None
        review_result = classify_github_response(
            "example/project", 17, review_missing, clock=lambda: NOW
        )
        self.assertEqual(review_result["classification"], "merge-ready")

        check_missing = self.load()
        check_connection = check_missing["data"]["repository"]["pullRequest"]["statusCheckRollup"]["contexts"]
        check_connection["nodes"] = []
        check_connection["totalCount"] = 0
        check_result = classify_github_response(
            "example/project", 17, check_missing, clock=lambda: NOW
        )
        self.assertEqual(check_result["classification"], "checks-pending")
        self.assertIn("test", check_result["reason"])

    def test_closed_author_waiting_and_rate_limit_are_explicit(self) -> None:
        closed = self.load()
        closed["data"]["repository"]["pullRequest"]["state"] = "CLOSED"
        closed_result = classify_github_response(
            "example/project", 17, closed, clock=lambda: NOW
        )
        self.assertEqual(closed_result["classification"], "product-gate")
        self.assertIn("state:closed", closed_result["product_gate"]["signals"])

        waiting = self.load()
        waiting["data"]["repository"]["pullRequest"]["labels"]["nodes"] = [
            {"name": "waiting-for-author"}
        ]
        waiting_result = classify_github_response(
            "example/project", 17, waiting, clock=lambda: NOW
        )
        self.assertEqual(waiting_result["classification"], "product-gate")
        self.assertTrue(waiting_result["author_waiting"]["active"])

        rate_limited = self.load()
        rate_limited["data"]["rateLimit"]["remaining"] = 0
        rate_result = classify_github_response(
            "example/project", 17, rate_limited, clock=lambda: NOW
        )
        self.assertEqual(rate_result["classification"], "merge-ready")
        self.assertEqual(rate_result["rate_limit"]["remaining"], 0)

    def test_transient_retry_can_recover_and_never_echoes_check_messages(self) -> None:
        transient = self.load()
        transient["errors"] = [
            {"message": "secret token content", "path": [], "type": "RATE_LIMITED"}
        ]
        direct = classify_github_response(
            "example/project", 17, transient, clock=lambda: NOW
        )
        self.assertEqual(direct["classification"], "plausible-transient-failure")
        self.assertNotIn("secret token content", json.dumps(direct))
        responses = [transient, self.load()]
        sleeps = []
        result = observe_with_retry(
            lambda _repo, _pr: responses.pop(0),
            "example/project",
            17,
            policy=RetryPolicy(max_retries=2, backoff_seconds=(1.0, 2.0)),
            clock=lambda: NOW,
            sleeper=sleeps.append,
        )
        self.assertEqual(result["classification"], "merge-ready")
        self.assertEqual(result["retry"]["attempts"], 2)
        self.assertFalse(result["retry"]["exhausted"])
        self.assertEqual(sleeps, [1.0])
        self.assertNotIn("secret token content", json.dumps(result))

    def test_nonclean_merge_state_and_required_review_are_product_gates(self) -> None:
        for field, value in (
            ("mergeStateStatus", "BLOCKED"),
            ("reviewDecision", "REVIEW_REQUIRED"),
        ):
            with self.subTest(field=field):
                payload = self.load()
                payload["data"]["repository"]["pullRequest"][field] = value
                result = classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
                self.assertEqual(result["classification"], "product-gate")

    def test_explicit_transient_acquisition_without_pr_data_retries(self) -> None:
        transient = {
            "data": {"rateLimit": None, "repository": {"pullRequest": None}},
            "errors": [{"path": [], "type": "RATE_LIMITED"}],
        }
        calls = []
        result = observe_with_retry(
            lambda _repo, _pr: calls.append(1) or transient,
            "example/project",
            17,
            policy=RetryPolicy(max_retries=1, backoff_seconds=(0.0,)),
            clock=lambda: NOW,
            sleeper=lambda _delay: None,
        )
        self.assertEqual(result["classification"], "plausible-transient-failure")
        self.assertEqual(len(calls), 2)
        self.assertTrue(result["retry"]["exhausted"])

    def test_untrusted_check_prose_never_triggers_retry(self) -> None:
        for phrase in (
            "hosted runner lost communication",
            "runner lost communication",
            "service unavailable",
            "connection reset",
            "rate limit",
        ):
            for field in ("title", "summary"):
                with self.subTest(phrase=phrase, field=field):
                    payload = self.load()
                    node = payload["data"]["repository"]["pullRequest"]["statusCheckRollup"]["contexts"]["nodes"][0]
                    node["conclusion"] = "FAILURE"
                    node[field] = phrase
                    calls = []
                    delays = []
                    result = observe_with_retry(
                        lambda _repo, _pr: calls.append(1) or payload,
                        "example/project",
                        17,
                        policy=RetryPolicy(max_retries=2, backoff_seconds=(1.0, 2.0)),
                        clock=lambda: NOW,
                        sleeper=delays.append,
                    )
                    self.assertEqual(result["classification"], "deterministic-check-failure")
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(delays, [])

    def test_semantic_fingerprint_excludes_clock_retry_and_rate_telemetry(self) -> None:
        first_payload = self.load()
        second_payload = self.load()
        second_payload["data"]["rateLimit"] = {
            "cost": 99,
            "remaining": 0,
            "resetAt": "2030-01-01T00:00:00Z",
        }
        first = classify_github_response(
            "example/project",
            17,
            first_payload,
            clock=lambda: NOW,
            retry_evidence={"attempts": 1, "delays_seconds": [], "exhausted": False},
        )
        second = classify_github_response(
            "example/project",
            17,
            second_payload,
            clock=lambda: datetime(2027, 1, 1, tzinfo=timezone.utc),
            retry_evidence={"attempts": 3, "delays_seconds": [1.0, 2.0], "exhausted": True},
        )
        self.assertEqual(second["classification"], "merge-ready")
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_semantic_fingerprint_changes_for_meaningful_state(self) -> None:
        baseline = classify_github_response(
            "example/project", 17, self.load(), clock=lambda: NOW
        )["fingerprint"]
        mutations = (
            lambda payload: payload["data"]["repository"]["pullRequest"].update(headRefOid="c" * 40),
            lambda payload: payload["data"]["repository"]["pullRequest"].update(mergeable="CONFLICTING"),
            lambda payload: payload["data"]["repository"]["pullRequest"]["labels"]["nodes"].append({"name": "hold"}),
            lambda payload: payload["data"]["repository"]["pullRequest"]["statusCheckRollup"]["contexts"]["nodes"][0].update(conclusion="FAILURE"),
            lambda payload: payload["data"]["repository"]["pullRequest"]["reviews"]["nodes"][0].update(state="CHANGES_REQUESTED"),
            lambda payload: payload["data"]["repository"]["pullRequest"]["reviews"].pop("pageInfo"),
        )
        for mutate in mutations:
            payload = self.load()
            mutate(payload)
            changed = classify_github_response(
                "example/project", 17, payload, clock=lambda: NOW
            )["fingerprint"]
            self.assertNotEqual(changed, baseline)

    def test_check_types_terminal_states_and_empty_rollups_are_total(self) -> None:
        cases = self.load("check-cases.json")
        for case in cases["check_runs"]:
            with self.subTest(kind="CheckRun", case=case):
                payload = self.load()
                node = payload["data"]["repository"]["pullRequest"]["statusCheckRollup"]["contexts"]["nodes"][0]
                node["status"] = case["status"]
                node["conclusion"] = case["conclusion"]
                result = classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
                self.assertEqual(result["classification"], case["expected"])

        for case in cases["status_contexts"]:
            with self.subTest(kind="StatusContext", case=case):
                payload = self.load()
                pr = payload["data"]["repository"]["pullRequest"]
                pr["statusCheckRollup"]["contexts"]["nodes"] = [
                    {
                        "__typename": "StatusContext",
                        "context": "test",
                        "createdAt": "2026-08-28T12:05:00Z",
                        "id": "SC_1",
                        "state": case["state"],
                        "targetUrl": "https://example.test/status",
                    }
                ]
                result = classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
                self.assertEqual(result["classification"], case["expected"])

        for case in cases["empty_rollups"]:
            with self.subTest(kind="rollup", case=case):
                payload = self.load()
                pr = payload["data"]["repository"]["pullRequest"]
                pr["statusCheckRollup"]["contexts"]["nodes"] = []
                pr["statusCheckRollup"]["contexts"]["totalCount"] = 0
                pr["statusCheckRollup"]["state"] = case["state"]
                payload["data"]["repository"]["branchProtectionRules"]["nodes"][0]["requiresStatusChecks"] = False
                payload["data"]["repository"]["branchProtectionRules"]["nodes"][0]["requiredStatusCheckContexts"] = []
                result = classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
                self.assertEqual(result["classification"], case["expected"])

    def test_rollup_state_is_authoritative_and_consistent_with_children(self) -> None:
        expected = {
            "SUCCESS": {
                "success": "merge-ready",
                "pending": "checks-pending",
                "failure": "deterministic-check-failure",
            },
            "PENDING": {
                "success": "checks-pending",
                "pending": "checks-pending",
                "failure": "checks-pending",
            },
            "EXPECTED": {
                "success": "checks-pending",
                "pending": "checks-pending",
                "failure": "checks-pending",
            },
            "FAILURE": {
                "success": "deterministic-check-failure",
                "pending": "deterministic-check-failure",
                "failure": "deterministic-check-failure",
            },
            "ERROR": {
                "success": "deterministic-check-failure",
                "pending": "deterministic-check-failure",
                "failure": "deterministic-check-failure",
            },
            "FUTURE_VALUE": {
                "success": "product-gate",
                "pending": "product-gate",
                "failure": "product-gate",
            },
        }
        child_states = {
            "success": ("COMPLETED", "SUCCESS"),
            "pending": ("IN_PROGRESS", None),
            "failure": ("COMPLETED", "FAILURE"),
        }
        for rollup_state, outcomes in expected.items():
            for child_outcome, classification in outcomes.items():
                with self.subTest(rollup=rollup_state, child=child_outcome):
                    payload = self.load()
                    rollup = payload["data"]["repository"]["pullRequest"]["statusCheckRollup"]
                    node = rollup["contexts"]["nodes"][0]
                    node["status"], node["conclusion"] = child_states[child_outcome]
                    rollup["state"] = rollup_state
                    result = classify_github_response(
                        "example/project", 17, payload, clock=lambda: NOW
                    )
                    self.assertEqual(result["classification"], classification)
                    self.assertEqual(result["checks"][0]["outcome"], child_outcome)

        payload = self.load()
        rollup = payload["data"]["repository"]["pullRequest"]["statusCheckRollup"]
        rollup["state"] = "PENDING"
        rollup["contexts"]["nodes"][0]["conclusion"] = "FAILURE"
        result = classify_github_response(
            "example/project", 17, payload, clock=lambda: NOW
        )
        self.assertEqual(result["classification"], "checks-pending")
        self.assertEqual(result["checks"][0]["outcome"], "failure")

    def test_duplicate_check_reruns_choose_latest_attempt_independent_of_order(self) -> None:
        def check(conclusion, status, started, database_id):
            return {
                "__typename": "CheckRun",
                "completedAt": started if status == "COMPLETED" else None,
                "conclusion": conclusion,
                "databaseId": database_id,
                "detailsUrl": "https://example.test/check",
                "name": "test",
                "startedAt": started,
                "status": status,
            }

        scenarios = (
            ([check("FAILURE", "COMPLETED", "2026-08-28T10:00:00Z", 1), check("SUCCESS", "COMPLETED", "2026-08-28T11:00:00Z", 2)], "merge-ready"),
            ([check("SUCCESS", "COMPLETED", "2026-08-28T10:00:00Z", 1), check("FAILURE", "COMPLETED", "2026-08-28T11:00:00Z", 2)], "deterministic-check-failure"),
            ([check("SUCCESS", "COMPLETED", "2026-08-28T10:00:00Z", 1), check(None, "IN_PROGRESS", "2026-08-28T11:00:00Z", 2)], "checks-pending"),
            ([check("SUCCESS", "COMPLETED", "2026-08-28T10:00:00Z", 1), check("FAILURE", "COMPLETED", "2026-08-28T10:00:00Z", 1)], "deterministic-check-failure"),
        )
        for nodes, expected in scenarios:
            outputs = []
            for ordered in (nodes, list(reversed(nodes))):
                payload = self.load()
                contexts = payload["data"]["repository"]["pullRequest"]["statusCheckRollup"]["contexts"]
                contexts["nodes"] = ordered
                contexts["totalCount"] = len(ordered)
                outputs.append(
                    classify_github_response(
                        "example/project", 17, payload, clock=lambda: NOW
                    )
                )
            self.assertEqual(outputs[0]["classification"], expected)
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(len(outputs[0]["checks"]), 1)

        optional = self.load()
        contexts = optional["data"]["repository"]["pullRequest"]["statusCheckRollup"]["contexts"]
        contexts["nodes"].extend(
            [
                check("FAILURE", "COMPLETED", "2026-08-28T09:00:00Z", 10) | {"name": "optional"},
                check("SUCCESS", "COMPLETED", "2026-08-28T12:00:00Z", 11) | {"name": "optional"},
            ]
        )
        contexts["totalCount"] = 3
        self.assertEqual(
            classify_github_response(
                "example/project", 17, optional, clock=lambda: NOW
            )["classification"],
            "merge-ready",
        )

        missing_name = self.load()
        del missing_name["data"]["repository"]["pullRequest"]["statusCheckRollup"]["contexts"]["nodes"][0]["name"]
        self.assertEqual(
            classify_github_response(
                "example/project", 17, missing_name, clock=lambda: NOW
            )["classification"],
            "product-gate",
        )

    def test_same_name_check_run_and_status_context_remain_distinct(self) -> None:
        check_run = {
            "__typename": "CheckRun",
            "completedAt": "2026-08-28T10:00:00Z",
            "conclusion": "FAILURE",
            "databaseId": 1,
            "detailsUrl": "https://checks.example.test/run/1",
            "name": "test",
            "startedAt": "2026-08-28T09:59:00Z",
            "status": "COMPLETED",
        }
        status_context = {
            "__typename": "StatusContext",
            "context": "test",
            "createdAt": "2026-08-28T11:00:00Z",
            "id": "SC_1",
            "state": "SUCCESS",
            "targetUrl": "https://status.example.test/context/1",
        }
        outputs = []
        for nodes in ([check_run, status_context], [status_context, check_run]):
            payload = self.load()
            contexts = payload["data"]["repository"]["pullRequest"]["statusCheckRollup"]["contexts"]
            contexts["nodes"] = nodes
            contexts["totalCount"] = 2
            outputs.append(
                classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
            )
        self.assertEqual(outputs[0]["classification"], "deterministic-check-failure")
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(
            {item["type"] for item in outputs[0]["checks"]},
            {"CheckRun", "StatusContext"},
        )

    def test_exact_duplicate_check_ties_use_all_emitted_semantic_details(self) -> None:
        def check(host, annotations):
            return {
                "__typename": "CheckRun",
                "completedAt": "2026-08-28T11:00:00Z",
                "conclusion": "SUCCESS",
                "databaseId": 9,
                "detailsUrl": f"https://{host}/run/9",
                "name": "test",
                "output": {"annotationsCount": annotations},
                "startedAt": "2026-08-28T10:59:00Z",
                "status": "COMPLETED",
            }

        first = check("alpha.example.test", 1)
        second = check("zeta.example.test", 2)
        outputs = []
        for nodes in ([first, second], [second, first]):
            payload = self.load()
            contexts = payload["data"]["repository"]["pullRequest"]["statusCheckRollup"]["contexts"]
            contexts["nodes"] = nodes
            contexts["totalCount"] = 2
            outputs.append(
                classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
            )
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0]["fingerprint"], outputs[1]["fingerprint"])

    def test_check_and_review_ordering_uses_parsed_iso_instants(self) -> None:
        def check(conclusion, started, database_id):
            return {
                "__typename": "CheckRun",
                "completedAt": started,
                "conclusion": conclusion,
                "databaseId": database_id,
                "detailsUrl": "https://checks.example.test/run",
                "name": "test",
                "startedAt": started,
                "status": "COMPLETED",
            }

        offset_checks = self.load()
        contexts = offset_checks["data"]["repository"]["pullRequest"]["statusCheckRollup"]["contexts"]
        contexts["nodes"] = [
            check("FAILURE", "2026-08-28T12:00:00+02:00", 1),
            check("SUCCESS", "2026-08-28T11:00:00.250Z", 2),
        ]
        contexts["totalCount"] = 2
        self.assertEqual(
            classify_github_response(
                "example/project", 17, offset_checks, clock=lambda: NOW
            )["classification"],
            "merge-ready",
        )

        invalid_check = self.load()
        contexts = invalid_check["data"]["repository"]["pullRequest"]["statusCheckRollup"]["contexts"]
        contexts["nodes"] = [
            check("FAILURE", "2026-08-28T10:00:00Z", 1),
            check("SUCCESS", "9999-future-looking-not-an-instant", 2),
        ]
        contexts["totalCount"] = 2
        result = classify_github_response(
            "example/project", 17, invalid_check, clock=lambda: NOW
        )
        self.assertEqual(result["classification"], "product-gate")
        self.assertIn("checks", result["partial_response"]["incomplete_connections"])
        self.assertEqual(result["checks"][0]["outcome"], "failure")

        def review(state, submitted, review_id):
            return {
                "author": {"login": "reviewer"},
                "commit": {"oid": "a" * 40},
                "id": review_id,
                "state": state,
                "submittedAt": submitted,
            }

        offset_reviews = self.load()
        connection = offset_reviews["data"]["repository"]["pullRequest"]["reviews"]
        connection["nodes"] = [
            review("CHANGES_REQUESTED", "2026-08-28T12:00:00+02:00", "PRR_OLD"),
            review("APPROVED", "2026-08-28T11:00:00.250Z", "PRR_NEW"),
        ]
        connection["totalCount"] = 2
        self.assertEqual(
            classify_github_response(
                "example/project", 17, offset_reviews, clock=lambda: NOW
            )["classification"],
            "merge-ready",
        )

        invalid_review = self.load()
        connection = invalid_review["data"]["repository"]["pullRequest"]["reviews"]
        connection["nodes"] = [
            review("CHANGES_REQUESTED", "2026-08-28T10:00:00Z", "PRR_VALID"),
            review("APPROVED", "9999-future-looking-not-an-instant", "PRR_INVALID"),
        ]
        connection["totalCount"] = 2
        result = classify_github_response(
            "example/project", 17, invalid_review, clock=lambda: NOW
        )
        self.assertEqual(result["classification"], "blocking-review-feedback")
        self.assertIn("reviews", result["partial_response"]["incomplete_connections"])
        self.assertEqual(result["reviews"][0]["state"], "CHANGES_REQUESTED")

    def test_every_check_and_review_timestamp_must_parse(self) -> None:
        mutations = (
            ("checks", lambda node: node.update(startedAt="not-an-instant")),
            ("checks", lambda node: node.update(completedAt="2026-99-99T00:00:00Z")),
            ("reviews", lambda node: node.update(submittedAt="2026-08-28T12:00:00")),
        )
        for connection_name, mutate in mutations:
            with self.subTest(connection=connection_name):
                payload = self.load()
                pr = payload["data"]["repository"]["pullRequest"]
                connection = (
                    pr["statusCheckRollup"]["contexts"]
                    if connection_name == "checks"
                    else pr["reviews"]
                )
                mutate(connection["nodes"][0])
                result = classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
                self.assertEqual(result["classification"], "product-gate")
                self.assertIn(
                    connection_name,
                    result["partial_response"]["incomplete_connections"],
                )

        payload = self.load()
        contexts = payload["data"]["repository"]["pullRequest"]["statusCheckRollup"]["contexts"]
        contexts["nodes"] = [
            {
                "__typename": "StatusContext",
                "context": "test",
                "createdAt": "not-an-instant",
                "id": "SC_1",
                "state": "SUCCESS",
                "targetUrl": "https://status.example.test/context/1",
            }
        ]
        contexts["totalCount"] = 1
        result = classify_github_response(
            "example/project", 17, payload, clock=lambda: NOW
        )
        self.assertEqual(result["classification"], "product-gate")
        self.assertIn("checks", result["partial_response"]["incomplete_connections"])

    def test_every_connection_must_prove_complete_pagination(self) -> None:
        locations = (
            ("checks", lambda payload: payload["data"]["repository"]["pullRequest"]["statusCheckRollup"]["contexts"]),
            ("reviews", lambda payload: payload["data"]["repository"]["pullRequest"]["reviews"]),
            ("review_threads", lambda payload: payload["data"]["repository"]["pullRequest"]["reviewThreads"]),
            ("labels", lambda payload: payload["data"]["repository"]["pullRequest"]["labels"]),
            ("branch_protection", lambda payload: payload["data"]["repository"]["branchProtectionRules"]),
        )
        for name, locate in locations:
            for missing in ("nodes", "pageInfo"):
                with self.subTest(connection=name, missing=missing):
                    payload = self.load()
                    del locate(payload)[missing]
                    result = classify_github_response(
                        "example/project", 17, payload, clock=lambda: NOW
                    )
                    self.assertEqual(result["classification"], "product-gate")
                    self.assertIn(name, result["partial_response"]["incomplete_connections"])

            with self.subTest(connection=name, pagination="forward"):
                payload = self.load()
                locate(payload)["pageInfo"]["hasNextPage"] = True
                result = classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
                self.assertEqual(result["classification"], "product-gate")

            with self.subTest(connection=name, pagination="backward"):
                payload = self.load()
                locate(payload)["pageInfo"]["hasPreviousPage"] = True
                result = classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
                self.assertEqual(result["classification"], "product-gate")

        missing_labels = self.load()
        del missing_labels["data"]["repository"]["pullRequest"]["labels"]
        self.assertEqual(
            classify_github_response(
                "example/project", 17, missing_labels, clock=lambda: NOW
            )["classification"],
            "product-gate",
        )

    def test_connection_total_count_boundary_is_exact(self) -> None:
        for total, expected in ((100, "merge-ready"), (101, "product-gate")):
            payload = self.load()
            labels = payload["data"]["repository"]["pullRequest"]["labels"]
            labels["nodes"] = [{"name": f"label-{index}"} for index in range(100)]
            labels["totalCount"] = total
            result = classify_github_response(
                "example/project", 17, payload, clock=lambda: NOW
            )
            self.assertEqual(result["classification"], expected)

    def test_malformed_connection_cardinality_and_nodes_never_merge_ready(self) -> None:
        def connections(payload):
            repository = payload["data"]["repository"]
            pr = repository["pullRequest"]
            return {
                "checks": pr["statusCheckRollup"]["contexts"],
                "reviews": pr["reviews"],
                "review_threads": pr["reviewThreads"],
                "labels": pr["labels"],
                "branch_protection": repository["branchProtectionRules"],
            }

        def without_required_policy(payload):
            rule = payload["data"]["repository"]["branchProtectionRules"]["nodes"][0]
            rule["requiresApprovingReviews"] = False
            rule["requiredApprovingReviewCount"] = 0
            rule["requiresStatusChecks"] = False
            rule["requiredStatusCheckContexts"] = []

        for name in connections(self.load()):
            for malformed in ("too-small", "negative", "too-many", "non-object"):
                with self.subTest(connection=name, malformed=malformed):
                    payload = self.load()
                    without_required_policy(payload)
                    connection = connections(payload)[name]
                    if malformed == "too-small":
                        connection["totalCount"] = len(connection["nodes"]) - 1
                    elif malformed == "negative":
                        connection["nodes"] = []
                        connection["totalCount"] = -1
                    elif malformed == "too-many":
                        exemplar = connection["nodes"][0] if connection["nodes"] else {}
                        connection["nodes"] = [copy.deepcopy(exemplar) for _ in range(101)]
                        connection["totalCount"] = 101
                    else:
                        connection["nodes"] = ["not-an-object"]
                        connection["totalCount"] = 1
                    result = classify_github_response(
                        "example/project", 17, payload, clock=lambda: NOW
                    )
                    self.assertEqual(result["classification"], "product-gate")
                    self.assertIn(
                        name, result["partial_response"]["incomplete_connections"]
                    )

    def test_connection_nodes_require_complete_typed_schemas(self) -> None:
        def locations(payload):
            repository = payload["data"]["repository"]
            pr = repository["pullRequest"]
            return {
                "checks": pr["statusCheckRollup"]["contexts"],
                "reviews": pr["reviews"],
                "review_threads": pr["reviewThreads"],
                "labels": pr["labels"],
                "branch_protection": repository["branchProtectionRules"],
            }

        valid_nodes = {
            "checks": self.load()["data"]["repository"]["pullRequest"]["statusCheckRollup"]["contexts"]["nodes"][0],
            "reviews": self.load()["data"]["repository"]["pullRequest"]["reviews"]["nodes"][0],
            "review_threads": {
                "id": "PRRT_1",
                "isOutdated": False,
                "isResolved": True,
            },
            "labels": {"name": "safe-label"},
            "branch_protection": self.load()["data"]["repository"]["branchProtectionRules"]["nodes"][0],
        }
        required_field = {
            "checks": "status",
            "reviews": "commit",
            "review_threads": "isResolved",
            "labels": "name",
            "branch_protection": "requiredStatusCheckContexts",
        }
        wrong_type = {
            "checks": ("databaseId", True),
            "reviews": ("author", {"login": 7}),
            "review_threads": ("isOutdated", 0),
            "labels": ("name", False),
            "branch_protection": ("requiredStatusCheckContexts", ["test", 7]),
        }

        for name, exemplar in valid_nodes.items():
            malformed_nodes = [{}, copy.deepcopy(exemplar), copy.deepcopy(exemplar)]
            malformed_nodes[1].pop(required_field[name])
            field, value = wrong_type[name]
            malformed_nodes[2][field] = value
            for malformed, node in zip(("empty", "missing", "wrong-type"), malformed_nodes):
                with self.subTest(connection=name, malformed=malformed):
                    payload = self.load()
                    connection = locations(payload)[name]
                    connection["nodes"] = [node]
                    connection["totalCount"] = 1
                    result = classify_github_response(
                        "example/project", 17, payload, clock=lambda: NOW
                    )
                    self.assertEqual(result["classification"], "product-gate")
                    self.assertIn(
                        name, result["partial_response"]["incomplete_connections"]
                    )

        status_context = {
            "__typename": "StatusContext",
            "context": "test",
            "createdAt": "2026-08-28T12:00:00Z",
            "id": "SC_1",
            "state": "SUCCESS",
            "targetUrl": "https://status.example.test/context/1",
        }
        for mutation in (
            lambda node: node.pop("createdAt"),
            lambda node: node.update(id=9),
            lambda node: node.update(targetUrl={"bad": "url"}),
        ):
            payload = self.load()
            node = copy.deepcopy(status_context)
            mutation(node)
            connection = locations(payload)["checks"]
            connection["nodes"] = [node]
            connection["totalCount"] = 1
            result = classify_github_response(
                "example/project", 17, payload, clock=lambda: NOW
            )
            self.assertEqual(result["classification"], "product-gate")
            self.assertIn(
                "checks", result["partial_response"]["incomplete_connections"]
            )

    def test_required_pull_request_scalars_are_strictly_typed(self) -> None:
        invalid_values = {
            "baseRefName": ("", True, 7, [], {}),
            "headRefOid": ("", True, 7, "not-a-forty-character-hex-object-id"),
            "isDraft": (None, 0, 1, "false", {}),
            "mergeStateStatus": ("", True, 7, [], {}),
            "mergeable": ("", True, 7, [], {}),
            "state": ("", True, 7, [], {}),
        }
        for field, values in invalid_values.items():
            cases = [("missing", None), *[("invalid", value) for value in values]]
            for kind, value in cases:
                with self.subTest(field=field, kind=kind, value=value):
                    payload = self.load()
                    pr = payload["data"]["repository"]["pullRequest"]
                    if kind == "missing":
                        del pr[field]
                    else:
                        pr[field] = value
                    result = classify_github_response(
                        "example/project", 17, payload, clock=lambda: NOW
                    )
                    self.assertEqual(result["classification"], "product-gate")
                    self.assertIn(field, result["partial_response"]["missing_fields"])

        for field, value in (
            ("reviewDecision", False),
            ("reviewDecision", ""),
            ("statusCheckRollup.state", False),
            ("statusCheckRollup.state", ""),
        ):
            with self.subTest(field=field, value=value):
                payload = self.load()
                pr = payload["data"]["repository"]["pullRequest"]
                if field == "reviewDecision":
                    pr[field] = value
                else:
                    pr["statusCheckRollup"]["state"] = value
                result = classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
                self.assertEqual(result["classification"], "product-gate")
                self.assertIn(field, result["partial_response"]["missing_fields"])

    def test_required_approvals_use_latest_review_per_reviewer_on_current_head(self) -> None:
        current = "a" * 40
        old = "b" * 40

        def review(review_id, state, submitted, commit, author="reviewer"):
            return {
                "author": {"login": author},
                "commit": {"oid": commit} if commit is not None else None,
                "id": review_id,
                "state": state,
                "submittedAt": submitted,
            }

        cases = (
            ([review("old-approval", "APPROVED", "2026-08-28T10:00:00Z", old)], "product-gate"),
            ([review("new-approval", "APPROVED", "2026-08-28T11:00:00Z", current)], "merge-ready"),
            ([review("old-approved", "APPROVED", "2026-08-28T10:00:00Z", old), review("new-change", "CHANGES_REQUESTED", "2026-08-28T11:00:00Z", current)], "blocking-review-feedback"),
            ([review("old-change", "CHANGES_REQUESTED", "2026-08-28T10:00:00Z", old), review("new-approved", "APPROVED", "2026-08-28T11:00:00Z", current)], "merge-ready"),
            ([review("current-change", "CHANGES_REQUESTED", "2026-08-28T11:00:00Z", current)], "blocking-review-feedback"),
            ([review("missing-head", "APPROVED", "2026-08-28T11:00:00Z", None)], "product-gate"),
            ([review("approved", "APPROVED", "2026-08-28T10:00:00Z", current), review("dismissed", "DISMISSED", "2026-08-28T11:00:00Z", current)], "product-gate"),
        )
        for reviews, expected in cases:
            with self.subTest(expected=expected, reviews=reviews):
                outputs = []
                for ordered in (reviews, list(reversed(reviews))):
                    payload = self.load()
                    connection = payload["data"]["repository"]["pullRequest"]["reviews"]
                    connection["nodes"] = ordered
                    connection["totalCount"] = len(ordered)
                    outputs.append(
                        classify_github_response(
                            "example/project", 17, payload, clock=lambda: NOW
                        )
                    )
                self.assertEqual(outputs[0]["classification"], expected)
                self.assertEqual(outputs[0], outputs[1])
                self.assertTrue(
                    all("commit_head" in item for item in outputs[0]["reviews"])
                )

        for state in (None, "PENDING", "FUTURE_VALUE"):
            payload = self.load()
            review_node = payload["data"]["repository"]["pullRequest"]["reviews"]["nodes"][0]
            review_node["state"] = state
            self.assertEqual(
                classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )["classification"],
                "product-gate",
            )

    def test_equal_time_reviews_resolve_conservatively_without_id_ordering(self) -> None:
        current = "a" * 40
        old = "b" * 40
        submitted = "2026-08-28T11:00:00Z"

        def review(review_id, state, commit):
            return {
                "author": {"login": "reviewer"},
                "commit": {"oid": commit},
                "id": review_id,
                "state": state,
                "submittedAt": submitted,
            }

        scenarios = (
            (review("z-current", "APPROVED", current), review("a-old", "APPROVED", old), "product-gate"),
            (review("a-current", "APPROVED", current), review("z-old", "APPROVED", old), "product-gate"),
            (review("z-approved", "APPROVED", current), review("a-change", "CHANGES_REQUESTED", current), "blocking-review-feedback"),
            (review("a-approved", "APPROVED", current), review("z-change", "CHANGES_REQUESTED", current), "blocking-review-feedback"),
        )
        for first, second, expected in scenarios:
            outputs = []
            for nodes in ([first, second], [second, first]):
                payload = self.load()
                connection = payload["data"]["repository"]["pullRequest"]["reviews"]
                connection["nodes"] = nodes
                connection["totalCount"] = 2
                outputs.append(
                    classify_github_response(
                        "example/project", 17, payload, clock=lambda: NOW
                    )
                )
            self.assertEqual(outputs[0]["classification"], expected)
            self.assertEqual(outputs[0], outputs[1])

    def test_exact_review_ties_use_all_emitted_semantic_fields(self) -> None:
        submitted = "2026-08-28T11:00:00Z"

        def review(state, commit):
            return {
                "author": {"login": "reviewer"},
                "commit": {"oid": commit},
                "id": "PRR_DUPLICATE",
                "state": state,
                "submittedAt": submitted,
            }

        first = review("APPROVED", "b" * 40)
        second = review("CHANGES_REQUESTED", "c" * 40)
        outputs = []
        for nodes in ([first, second], [second, first]):
            payload = self.load()
            connection = payload["data"]["repository"]["pullRequest"]["reviews"]
            connection["nodes"] = nodes
            connection["totalCount"] = 2
            outputs.append(
                classify_github_response(
                    "example/project", 17, payload, clock=lambda: NOW
                )
            )
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0]["fingerprint"], outputs[1]["fingerprint"])

    @staticmethod
    def _merge(target: dict, source: dict) -> None:
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                PullRequestWatcherTest._merge(target[key], value)
            else:
                target[key] = value


if __name__ == "__main__":
    unittest.main()

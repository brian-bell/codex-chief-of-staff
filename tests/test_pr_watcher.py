from __future__ import annotations

import json
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codex_chief_of_staff.github_pr import (  # noqa: E402
    GitHubClient,
    ProviderError,
    READ_ONLY_QUERY,
    classify_provider_error,
)
from codex_chief_of_staff.pr_watcher import (  # noqa: E402
    RetryPolicy,
    classify_payload,
    observe_with_retry,
)
from codex_chief_of_staff.watch_pr_cli import (  # noqa: E402
    CliError,
    json_line,
    run as run_watch_cli,
)


SCENARIOS = json.loads(
    (ROOT / "tests" / "fixtures" / "pr-watcher-scenarios.json").read_text()
)
PROVIDER_ERRORS = json.loads(
    (ROOT / "tests" / "fixtures" / "pr-watcher-provider-errors.json").read_text()
)
CLI = ROOT / "skills" / "babysit-pr" / "scripts" / "watch-pr"
OBSERVED_AT = "2026-08-30T12:00:00Z"
NORMALIZED_OBSERVED_AT = "2026-08-30T12:00:00.000000Z"


def copy_payload(name: str = "merge-ready") -> dict[str, object]:
    return json.loads(json.dumps(SCENARIOS[name]["payload"]))


def pull_request(payload: dict[str, object]) -> dict[str, object]:
    return payload["data"]["repository"]["pullRequest"]


class ClassificationTest(unittest.TestCase):
    def test_fixture_driven_classifications(self) -> None:
        for expected, scenario in SCENARIOS.items():
            with self.subTest(expected=expected):
                result = classify_payload(
                    repository="brian-bell/codex-chief-of-staff",
                    pr_number=17,
                    payload=scenario["payload"],
                    expected_head_sha=scenario.get("expected_head_sha"),
                    observed_at=OBSERVED_AT,
                )
                self.assertEqual(result["classification"], expected)
                self.assertEqual(result["repository"], "brian-bell/codex-chief-of-staff")
                self.assertEqual(result["pr_number"], 17)
                self.assertIn("observed_head_sha", result)
                self.assertIn("checks", result)
                self.assertIn("reviews", result)
                self.assertIn("merge_state", result)
                self.assertEqual(result["observed_at"], NORMALIZED_OBSERVED_AT)

    def test_changed_head_invalidates_prior_sha(self) -> None:
        result = classify_payload(
            "brian-bell/codex-chief-of-staff",
            17,
            SCENARIOS["merge-ready"]["payload"],
            expected_head_sha="prior-head",
            observed_at=OBSERVED_AT,
        )

        self.assertEqual(result["classification"], "stale-head")
        self.assertEqual(result["expected_head_sha"], "prior-head")
        self.assertFalse(result["verdict_reusable"])

    def test_sha_case_difference_does_not_invalidate_current_head(self) -> None:
        payload = copy_payload()
        pr = pull_request(payload)
        observed_sha = "abcdef1234567890abcdef1234567890abcdef12"
        pr["headRefOid"] = observed_sha
        pr["commits"]["nodes"][0]["commit"]["oid"] = observed_sha
        pr["reviews"]["nodes"][0]["commit"]["oid"] = observed_sha

        result = classify_payload(
            "owner/repo",
            17,
            payload,
            expected_head_sha=observed_sha.upper(),
            observed_at=OBSERVED_AT,
        )

        self.assertEqual(result["classification"], "merge-ready")
        self.assertTrue(result["verdict_reusable"])

    def test_different_sha_remains_stale_and_cannot_reuse_verdict(self) -> None:
        payload = copy_payload()
        pr = pull_request(payload)
        observed_sha = "abcdef1234567890abcdef1234567890abcdef12"
        pr["headRefOid"] = observed_sha
        pr["commits"]["nodes"][0]["commit"]["oid"] = observed_sha
        pr["reviews"]["nodes"][0]["commit"]["oid"] = observed_sha

        result = classify_payload(
            "owner/repo",
            17,
            payload,
            expected_head_sha="ABCDEF1234567890ABCDEF1234567890ABCDEF13",
            observed_at=OBSERVED_AT,
        )

        self.assertEqual(result["classification"], "stale-head")
        self.assertFalse(result["verdict_reusable"])

    def test_partial_response_is_a_product_gate(self) -> None:
        result = classify_payload(
            "brian-bell/codex-chief-of-staff",
            17,
            PROVIDER_ERRORS["partial-response"]["payload"],
            observed_at=OBSERVED_AT,
        )

        self.assertEqual(result["classification"], "product-gate")
        self.assertEqual(result["observed_head_sha"], "head-17")
        self.assertTrue(result["provider_errors"])
        self.assertTrue(result["schema_errors"])

    def test_repeated_observations_have_the_same_semantic_fingerprint(self) -> None:
        first = classify_payload(
            "brian-bell/codex-chief-of-staff", 17,
            SCENARIOS["merge-ready"]["payload"], observed_at=OBSERVED_AT,
        )
        second = classify_payload(
            "brian-bell/codex-chief-of-staff", 17,
            SCENARIOS["merge-ready"]["payload"],
            observed_at="2026-08-30T12:01:00Z",
        )

        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["classification"], second["classification"])
        self.assertEqual(first["checks"], second["checks"])
        self.assertEqual(first["reviews"], second["reviews"])

    def test_unknown_review_decision_cannot_be_merge_ready(self) -> None:
        payload = json.loads(json.dumps(SCENARIOS["merge-ready"]["payload"]))
        payload["data"]["repository"]["pullRequest"]["reviewDecision"] = "NEW_ENUM"

        result = classify_payload(
            "brian-bell/codex-chief-of-staff", 17, payload, observed_at=OBSERVED_AT
        )

        self.assertEqual(result["classification"], "product-gate")
        self.assertIn("reviewDecision", " ".join(result["schema_errors"]))

    def test_closed_pr_cannot_be_merge_ready(self) -> None:
        payload = json.loads(json.dumps(SCENARIOS["merge-ready"]["payload"]))
        payload["data"]["repository"]["pullRequest"]["state"] = "CLOSED"

        result = classify_payload(
            "brian-bell/codex-chief-of-staff", 17, payload, observed_at=OBSERVED_AT
        )

        self.assertEqual(result["classification"], "product-gate")
        self.assertIn("pull request is closed", result["product_gate_reasons"])

    def test_truncated_connection_cannot_be_merge_ready(self) -> None:
        payload = json.loads(json.dumps(SCENARIOS["merge-ready"]["payload"]))
        contexts = (
            payload["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]
            ["commit"]["statusCheckRollup"]["contexts"]
        )
        contexts["pageInfo"] = {"hasNextPage": True}

        result = classify_payload(
            "brian-bell/codex-chief-of-staff", 17, payload, observed_at=OBSERVED_AT
        )

        self.assertEqual(result["classification"], "product-gate")
        self.assertIn("truncated", " ".join(result["schema_errors"]))

    def test_unknown_check_conclusion_cannot_be_merge_ready(self) -> None:
        payload = json.loads(json.dumps(SCENARIOS["merge-ready"]["payload"]))
        check = (
            payload["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]
            ["commit"]["statusCheckRollup"]["contexts"]["nodes"][0]
        )
        check["conclusion"] = "NEW_ENUM"

        result = classify_payload(
            "brian-bell/codex-chief-of-staff", 17, payload, observed_at=OBSERVED_AT
        )

        self.assertEqual(result["classification"], "product-gate")
        self.assertIn("check run", " ".join(result["schema_errors"]))


class StrictSchemaRegressionTest(unittest.TestCase):
    def test_missing_connection_page_info_cannot_be_merge_ready(self) -> None:
        payload = copy_payload()
        del pull_request(payload)["reviews"]["pageInfo"]
        result = classify_payload(
            "owner/repo",
            17,
            payload,
            observed_at=OBSERVED_AT,
        )

        self.assertEqual(result["classification"], "product-gate")
        self.assertIn("pageInfo", " ".join(result["schema_errors"]))

    def test_rollup_failure_with_successful_child_is_product_gated(self) -> None:
        payload = copy_payload()
        pr = pull_request(payload)
        commit = pr["commits"]["nodes"][0]["commit"]
        commit["oid"] = pr["headRefOid"]
        commit["statusCheckRollup"]["state"] = "FAILURE"

        result = classify_payload(
            "owner/repo", 17, payload, observed_at=OBSERVED_AT
        )

        self.assertEqual(result["classification"], "product-gate")
        self.assertIn("contradicts", " ".join(result["schema_errors"]))

    def test_check_rollup_must_belong_to_observed_head(self) -> None:
        payload = copy_payload()
        pr = pull_request(payload)
        pr["commits"]["nodes"][0]["commit"]["oid"] = "old-head"

        result = classify_payload(
            "owner/repo", 17, payload, observed_at=OBSERVED_AT
        )

        self.assertEqual(result["classification"], "product-gate")
        self.assertIn("observed head", " ".join(result["schema_errors"]))

    def test_invalid_scalar_and_pagination_types_are_product_gated(self) -> None:
        mutations = (
            lambda pr: pr.__setitem__("isDraft", 0),
            lambda pr: pr["reviews"].__setitem__("pageInfo", {"hasNextPage": 0}),
            lambda pr: pr["reviews"]["nodes"][0].__setitem__(
                "submittedAt", "2026-08-30T10:03:00"
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                payload = copy_payload()
                mutate(pull_request(payload))
                result = classify_payload(
                    "owner/repo", 17, payload, observed_at=OBSERVED_AT
                )
                self.assertEqual(result["classification"], "product-gate")
                self.assertTrue(result["schema_errors"])

    def test_connection_node_limit_is_enforced(self) -> None:
        payload = copy_payload()
        pr = pull_request(payload)
        reviews = pr["reviews"]["nodes"]
        reviews.extend(json.loads(json.dumps(reviews[0])) for _ in range(100))

        result = classify_payload(
            "owner/repo", 17, payload, observed_at=OBSERVED_AT
        )

        self.assertEqual(result["classification"], "product-gate")
        self.assertIn("100-node limit", " ".join(result["schema_errors"]))


class CurrentHeadReviewRegressionTest(unittest.TestCase):
    def test_pending_review_without_submission_is_ignored(self) -> None:
        payload = copy_payload()
        pull_request(payload)["reviews"]["nodes"].append(
            {
                "author": {"login": "viewer"},
                "state": "PENDING",
                "submittedAt": None,
                "commit": None,
            }
        )

        result = classify_payload("owner/repo", 17, payload, observed_at=OBSERVED_AT)

        self.assertEqual(result["classification"], "merge-ready")
        self.assertEqual(result["schema_errors"], [])
        self.assertEqual(len(result["reviews"]["items"]), 1)

    def test_old_head_approval_cannot_satisfy_merge_readiness(self) -> None:
        payload = copy_payload()
        pr = pull_request(payload)
        pr["reviews"]["nodes"][0]["commit"]["oid"] = "old-head"

        result = classify_payload(
            "owner/repo",
            17,
            payload,
            expected_head_sha="head-17",
            observed_at=OBSERVED_AT,
        )

        self.assertEqual(result["classification"], "product-gate")
        self.assertFalse(result["verdict_reusable"])
        self.assertFalse(result["reviews"]["items"][0]["applies_to_head"])

    def test_later_comment_does_not_erase_current_head_approval(self) -> None:
        payload = copy_payload()
        reviews = pull_request(payload)["reviews"]["nodes"]
        reviews.append(
            {
                "author": {"login": "reviewer"},
                "state": "COMMENTED",
                "submittedAt": "2026-08-30T10:04:00Z",
                "commit": {"oid": "head-17"},
            }
        )

        result = classify_payload("owner/repo", 17, payload, observed_at=OBSERVED_AT)

        self.assertEqual(result["classification"], "merge-ready")
        self.assertTrue(result["reviews"]["current_head_approval"])
        self.assertEqual(len(result["reviews"]["items"]), 2)

    def test_later_comment_does_not_erase_current_head_changes_request(self) -> None:
        payload = copy_payload("blocking-review-feedback")
        pr = pull_request(payload)
        pr["reviewThreads"]["nodes"] = []
        pr["reviews"]["nodes"].append(
            {
                "author": {"login": "reviewer"},
                "state": "COMMENTED",
                "submittedAt": "2026-08-30T10:04:00Z",
                "commit": {"oid": "head-17"},
            }
        )

        result = classify_payload("owner/repo", 17, payload, observed_at=OBSERVED_AT)

        self.assertEqual(result["classification"], "blocking-review-feedback")

    def test_stale_approval_does_not_poison_independent_current_approval(self) -> None:
        payload = copy_payload()
        pull_request(payload)["reviews"]["nodes"].append(
            {
                "author": {"login": "former-reviewer"},
                "state": "APPROVED",
                "submittedAt": "2026-08-30T10:04:00Z",
                "commit": {"oid": "old-head"},
            }
        )

        result = classify_payload("owner/repo", 17, payload, observed_at=OBSERVED_AT)

        self.assertEqual(result["classification"], "merge-ready")
        self.assertTrue(result["reviews"]["current_head_approval"])
        self.assertTrue(
            any(not review["applies_to_head"] for review in result["reviews"]["items"])
        )


class DeterministicNormalizationRegressionTest(unittest.TestCase):
    def test_neutral_and_skipped_checks_match_a_successful_rollup(self) -> None:
        for conclusion in ("NEUTRAL", "SKIPPED"):
            with self.subTest(conclusion=conclusion):
                payload = copy_payload()
                pr = pull_request(payload)
                check = (
                    pr["commits"]["nodes"][0]["commit"]
                    ["statusCheckRollup"]["contexts"]["nodes"][0]
                )
                check["conclusion"] = conclusion

                result = classify_payload(
                    "owner/repo", 17, payload, observed_at=OBSERVED_AT
                )

                self.assertEqual(result["classification"], "merge-ready")
                self.assertEqual(result["schema_errors"], [])

    def test_queued_rerun_without_started_at_supersedes_old_failure(self) -> None:
        payload = copy_payload()
        pr = pull_request(payload)
        rollup = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]
        old_failure = rollup["contexts"]["nodes"][0]
        old_failure.update(
            {
                "databaseId": 100,
                "conclusion": "FAILURE",
                "startedAt": "2026-08-30T09:00:00Z",
                "completedAt": "2026-08-30T09:01:00Z",
            }
        )
        queued = json.loads(json.dumps(old_failure))
        queued.update(
            {
                "databaseId": 101,
                "status": "QUEUED",
                "conclusion": None,
                "startedAt": None,
                "completedAt": None,
            }
        )
        rollup["state"] = "PENDING"
        rollup["contexts"]["nodes"].append(queued)

        result = classify_payload("owner/repo", 17, payload, observed_at=OBSERVED_AT)

        self.assertEqual(result["classification"], "checks-pending")
        self.assertEqual(result["schema_errors"], [])
        self.assertEqual(len(result["checks"]), 1)
        self.assertEqual(result["checks"][0]["status"], "QUEUED")

    def test_distinct_review_thread_ids_survive_normalization_and_reordering(self) -> None:
        payload = copy_payload()
        pr = pull_request(payload)
        thread = {
            "id": "PRRT_thread_one",
            "isResolved": True,
            "isOutdated": False,
            "comments": {
                "nodes": [
                    {
                        "author": {"login": "reviewer"},
                        "createdAt": "2026-08-30T10:05:00Z",
                    }
                ]
            },
        }
        second = json.loads(json.dumps(thread))
        second["id"] = "PRRT_thread_two"
        pr["reviewThreads"]["nodes"] = [thread, second]
        reversed_payload = json.loads(json.dumps(payload))
        pull_request(reversed_payload)["reviewThreads"]["nodes"].reverse()

        first = classify_payload("owner/repo", 17, payload, observed_at=OBSERVED_AT)
        reordered = classify_payload(
            "owner/repo", 17, reversed_payload, observed_at=OBSERVED_AT
        )

        self.assertEqual(
            [item["id"] for item in first["reviews"]["threads"]],
            ["PRRT_thread_one", "PRRT_thread_two"],
        )
        self.assertEqual(len(first["reviews"]["threads"]), 2)
        self.assertEqual(first["reviews"], reordered["reviews"])
        self.assertEqual(first["fingerprint"], reordered["fingerprint"])

    def test_reordered_check_reruns_select_the_same_newest_attempt(self) -> None:
        payload = copy_payload()
        pr = pull_request(payload)
        checks = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"]
        old_failure = json.loads(json.dumps(checks[0]))
        old_failure.update(
            {
                "conclusion": "FAILURE",
                "startedAt": "2026-08-30T09:00:00Z",
                "completedAt": "2026-08-30T09:01:00Z",
            }
        )
        checks.append(old_failure)
        reversed_payload = json.loads(json.dumps(payload))
        reversed_checks = (
            pull_request(reversed_payload)["commits"]["nodes"][0]["commit"]
            ["statusCheckRollup"]["contexts"]["nodes"]
        )
        reversed_checks.reverse()

        first = classify_payload("owner/repo", 17, payload, observed_at=OBSERVED_AT)
        second = classify_payload(
            "owner/repo", 17, reversed_payload, observed_at=OBSERVED_AT
        )

        self.assertEqual(first["classification"], "merge-ready")
        self.assertEqual(first["checks"], second["checks"])
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(len(first["checks"]), 1)

    def test_provider_error_order_and_prose_do_not_change_fingerprint(self) -> None:
        first_payload = {
            "data": None,
            "errors": [
                {"type": "FORBIDDEN", "message": "request a: forbidden"},
                {"type": "NOT_FOUND", "message": "request a: missing"},
            ],
        }
        second_payload = {
            "data": None,
            "errors": [
                {"type": "NOT_FOUND", "message": "request b: absent"},
                {"type": "FORBIDDEN", "message": "request b: denied"},
            ],
        }

        first = classify_payload(
            "owner/repo", 17, first_payload, observed_at=OBSERVED_AT
        )
        second = classify_payload(
            "owner/repo", 17, second_payload, observed_at=OBSERVED_AT
        )

        self.assertEqual(first["provider_errors"], second["provider_errors"])
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_invalid_provider_timestamps_are_product_gated(self) -> None:
        payload = copy_payload()
        pr = pull_request(payload)
        check = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"][0]
        check["completedAt"] = "yesterday"

        result = classify_payload(
            "owner/repo", 17, payload, observed_at=OBSERVED_AT
        )

        self.assertEqual(result["classification"], "product-gate")
        self.assertIn("timestamp", " ".join(result["schema_errors"]))

    def test_ambiguous_review_tie_is_canonical_and_product_gated(self) -> None:
        payload = copy_payload()
        pr = pull_request(payload)
        tied = json.loads(json.dumps(pr["reviews"]["nodes"][0]))
        tied["commit"]["oid"] = "old-head"
        pr["reviews"]["nodes"].append(tied)
        reversed_payload = json.loads(json.dumps(payload))
        pull_request(reversed_payload)["reviews"]["nodes"].reverse()

        first = classify_payload("owner/repo", 17, payload, observed_at=OBSERVED_AT)
        second = classify_payload(
            "owner/repo", 17, reversed_payload, observed_at=OBSERVED_AT
        )

        self.assertEqual(first["classification"], "product-gate")
        self.assertEqual(first["reviews"], second["reviews"])
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertIn("ambiguous", " ".join(first["schema_errors"]))

    def test_retry_telemetry_does_not_change_success_fingerprint(self) -> None:
        clean = observe_with_retry(
            lambda: copy_payload(),
            "owner/repo",
            17,
            policy=RetryPolicy(max_attempts=2, delays=(0,)),
            clock=lambda: OBSERVED_AT,
            sleep=lambda _: None,
        )
        calls = []

        def flaky_fetch() -> dict[str, object]:
            calls.append(1)
            if len(calls) == 1:
                raise ProviderError("rate-limit", "request id one", True)
            return copy_payload()

        retried = observe_with_retry(
            flaky_fetch,
            "owner/repo",
            17,
            policy=RetryPolicy(max_attempts=2, delays=(0,)),
            clock=lambda: OBSERVED_AT,
            sleep=lambda _: None,
        )

        self.assertEqual(clean["classification"], "merge-ready")
        self.assertEqual(retried["classification"], "merge-ready")
        self.assertEqual(clean["fingerprint"], retried["fingerprint"])
        self.assertEqual(retried["retry_trace"], [{"kind": "rate-limit"}])
        self.assertEqual(retried["provider_errors"], [])


class MixedProviderErrorRegressionTest(unittest.TestCase):
    def test_nontransient_error_suppresses_rate_limit_retry(self) -> None:
        calls = []
        payload = {
            "data": None,
            "errors": [
                {"type": "RATE_LIMITED", "message": "retry later"},
                {"type": "FORBIDDEN", "message": "not allowed"},
            ],
        }

        def fetch() -> dict[str, object]:
            calls.append(1)
            return payload

        result = observe_with_retry(
            fetch,
            "owner/repo",
            17,
            policy=RetryPolicy(max_attempts=3, delays=(0, 0)),
            clock=lambda: OBSERVED_AT,
            sleep=lambda _: None,
        )

        self.assertEqual(len(calls), 1)
        self.assertFalse(result["retry_exhausted"])
        self.assertEqual(result["provider_errors"][0]["kind"], "permission")


class RetryPolicyTest(unittest.TestCase):
    def test_deterministic_failure_is_observed_once(self) -> None:
        calls = []

        def fetch() -> dict[str, object]:
            calls.append(1)
            return SCENARIOS["deterministic-check-failure"]["payload"]

        result = observe_with_retry(
            fetch, "owner/repo", 17, policy=RetryPolicy(max_attempts=3),
            clock=lambda: OBSERVED_AT, sleep=lambda _: None,
        )

        self.assertEqual(result["classification"], "deterministic-check-failure")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["attempts"], 1)

    def test_transient_check_failure_uses_bounded_retries(self) -> None:
        calls = []

        def fetch() -> dict[str, object]:
            calls.append(1)
            return SCENARIOS["plausible-transient-failure"]["payload"]

        result = observe_with_retry(
            fetch, "owner/repo", 17, policy=RetryPolicy(max_attempts=3, delays=(0, 0)),
            clock=lambda: OBSERVED_AT, sleep=lambda _: None,
        )

        self.assertEqual(result["classification"], "plausible-transient-failure")
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["attempts"], 3)
        self.assertTrue(result["retry_exhausted"])

    def test_rate_limit_retries_are_bounded_and_return_evidence(self) -> None:
        calls = []
        error = PROVIDER_ERRORS["rate-limit"]["error"]

        def fetch() -> dict[str, object]:
            calls.append(1)
            raise ProviderError(**error)

        result = observe_with_retry(
            fetch, "owner/repo", 17, policy=RetryPolicy(max_attempts=2, delays=(0,)),
            clock=lambda: OBSERVED_AT, sleep=lambda _: None,
        )

        self.assertEqual(result["classification"], "product-gate")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["provider_errors"][0]["kind"], "rate-limit")
        self.assertTrue(result["retry_exhausted"])

    def test_graphql_rate_limit_response_retries_are_bounded(self) -> None:
        calls = []

        def fetch() -> dict[str, object]:
            calls.append(1)
            return PROVIDER_ERRORS["rate-limit"]["payload"]

        result = observe_with_retry(
            fetch, "owner/repo", 17, policy=RetryPolicy(max_attempts=2, delays=(0,)),
            clock=lambda: OBSERVED_AT, sleep=lambda _: None,
        )

        self.assertEqual(result["classification"], "product-gate")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["provider_errors"][0]["kind"], "rate-limit")
        self.assertTrue(result["retry_exhausted"])

    def test_missing_permission_does_not_retry(self) -> None:
        calls = []
        error = PROVIDER_ERRORS["missing-permission"]["error"]

        def fetch() -> dict[str, object]:
            calls.append(1)
            raise ProviderError(**error)

        result = observe_with_retry(
            fetch, "owner/repo", 17, policy=RetryPolicy(max_attempts=3),
            clock=lambda: OBSERVED_AT, sleep=lambda _: None,
        )

        self.assertEqual(result["classification"], "product-gate")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["provider_errors"][0]["kind"], "permission")


class GitHubClientTest(unittest.TestCase):
    def test_client_runs_only_the_fixed_graphql_read(self) -> None:
        calls = []

        def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, stdout='{"data": {}}', stderr="")

        payload = GitHubClient(runner=runner).fetch("owner/repo", 17)

        self.assertEqual(payload, {"data": {}})
        self.assertEqual(calls[0][0][0:3], ["gh", "api", "graphql"])
        self.assertIn(f"query={READ_ONLY_QUERY}", calls[0][0])
        self.assertNotIn("mutation", READ_ONLY_QUERY.lower())
        self.assertNotIn("--method", calls[0][0])
        self.assertNotIn("--input", calls[0][0])
        self.assertTrue(calls[0][1]["check"])
        self.assertIn("reviewThreads(first: 100)", READ_ONLY_QUERY)
        self.assertIn("databaseId", READ_ONLY_QUERY)
        self.assertIn("\n          id\n", READ_ONLY_QUERY)

    def test_graphql_schema_error_is_nonretryable_despite_connection_type_name(self) -> None:
        error = classify_provider_error(
            "GraphQL: Field 'bogus' doesn't exist on type 'PullRequestConnection'"
        )
        calls = []

        def fetch() -> dict[str, object]:
            calls.append(1)
            raise error

        result = observe_with_retry(
            fetch,
            "owner/repo",
            17,
            policy=RetryPolicy(max_attempts=3, delays=(0, 0)),
            clock=lambda: OBSERVED_AT,
            sleep=lambda _: None,
        )

        self.assertEqual(error.kind, "graphql")
        self.assertFalse(error.retryable)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["attempts"], 1)

    def test_structured_graphql_errors_control_retry_classification(self) -> None:
        cases = (
            (
                '{"errors":[{"type":"RATE_LIMITED","message":"later"}]}',
                "rate-limit",
                True,
            ),
            (
                '{"errors":[{"type":"RATE_LIMITED"},{"type":"FORBIDDEN"}]}',
                "permission",
                False,
            ),
            (
                '{"errors":[{"extensions":{"code":"GRAPHQL_VALIDATION_FAILED"}}]}',
                "graphql",
                False,
            ),
            ("dial tcp: connection reset by peer", "transport", True),
        )
        for evidence, kind, retryable in cases:
            with self.subTest(kind=kind, retryable=retryable):
                error = classify_provider_error(evidence)
                self.assertEqual(error.kind, kind)
                self.assertEqual(error.retryable, retryable)

    def test_client_types_nonzero_structured_schema_error_without_provider_prose(self) -> None:
        def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(
                1,
                args,
                stderr=(
                    '{"errors":[{"type":"GRAPHQL_VALIDATION_FAILED",'
                    '"message":"bad PullRequestConnection field"}]}'
                ),
            )

        with self.assertRaises(ProviderError) as raised:
            GitHubClient(runner=runner).fetch("owner/repo", 17)

        self.assertEqual(raised.exception.kind, "graphql")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(
            raised.exception.to_dict(), {"kind": "graphql", "retryable": False}
        )

    def test_client_classifies_rate_limits_and_permissions(self) -> None:
        cases = (
            ("HTTP 429: API rate limit exceeded", "rate-limit", True),
            ("GraphQL: Resource not accessible by integration", "permission", False),
        )
        for stderr, kind, retryable in cases:
            with self.subTest(kind=kind):
                def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                    raise subprocess.CalledProcessError(1, args, stderr=stderr)

                with self.assertRaises(ProviderError) as raised:
                    GitHubClient(runner=runner).fetch("owner/repo", 17)
                self.assertEqual(raised.exception.kind, kind)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertNotIn("message", raised.exception.to_dict())

    def test_client_transport_timeout_is_typed_and_bounded(self) -> None:
        calls = []

        def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(kwargs)
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])

        with self.assertRaises(ProviderError) as raised:
            GitHubClient(runner=runner, timeout=2.5).fetch("owner/repo", 17)

        self.assertEqual(calls[0]["timeout"], 2.5)
        self.assertEqual(raised.exception.kind, "transport")
        self.assertTrue(raised.exception.retryable)

    def test_client_rejects_oversized_provider_body(self) -> None:
        def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args, 0, stdout="x" * 1_048_577, stderr=""
            )

        with self.assertRaises(ProviderError) as raised:
            GitHubClient(runner=runner).fetch("owner/repo", 17)

        self.assertEqual(raised.exception.kind, "response")
        self.assertFalse(raised.exception.retryable)


class WatchPrCliTest(unittest.TestCase):
    def test_total_json_output_has_a_hard_byte_limit(self) -> None:
        with self.assertRaises(CliError):
            json_line({"value": "x" * 131_072})

    def test_fixture_cli_writes_one_compact_json_result(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            fixture = Path(tempdir) / "pr.json"
            fixture.write_text(json.dumps(SCENARIOS["merge-ready"]["payload"]))
            completed = subprocess.run(
                [sys.executable, str(CLI), "--repo", "owner/repo", "--pr", "17",
                 "--fixture", str(fixture), "--observed-at", OBSERVED_AT],
                cwd=ROOT, check=True, capture_output=True, text=True,
            )

        result = json.loads(completed.stdout)
        self.assertEqual(result["classification"], "merge-ready")
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout.count("\n"), 1)

    def test_cli_rejects_invalid_repository_before_calling_github(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--repo", "owner/repo/extra", "--pr", "17"],
            cwd=ROOT, check=False, capture_output=True, text=True,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("owner/name", completed.stderr)

    def test_cli_allows_fixed_observation_time_only_for_fixture_replay(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--repo",
                "owner/repo",
                "--pr",
                "17",
                "--observed-at",
                OBSERVED_AT,
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("requires --fixture", completed.stderr)

    def test_cli_rejects_invalid_fixed_observation_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            fixture = Path(tempdir) / "pr.json"
            fixture.write_text(json.dumps(SCENARIOS["merge-ready"]["payload"]))
            completed = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "--repo",
                    "owner/repo",
                    "--pr",
                    "17",
                    "--fixture",
                    str(fixture),
                    "--observed-at",
                    "not-a-time",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertLess(len(completed.stderr), 1024)
        self.assertEqual(json.loads(completed.stderr)["error"]["kind"], "argument")

    def test_cli_argument_errors_are_compact_typed_json(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--repo", "owner/repo", "--pr", "nope"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertLess(len(completed.stderr), 1024)
        self.assertEqual(json.loads(completed.stderr)["error"]["kind"], "argument")

    def test_cli_schema_failures_are_compact_and_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            fixture = Path(tempdir) / "partial.json"
            fixture.write_text(
                json.dumps(PROVIDER_ERRORS["partial-response"]["payload"])
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "--repo",
                    "owner/repo",
                    "--pr",
                    "17",
                    "--fixture",
                    str(fixture),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertLess(len(completed.stderr), 1024)
        self.assertIn(
            json.loads(completed.stderr)["error"]["kind"],
            {"permission", "schema"},
        )

    def test_cli_bounds_untrusted_check_names_and_total_output(self) -> None:
        payload = copy_payload()
        pr = pull_request(payload)
        check = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"][0]
        check["name"] = "x" * 200_000
        with tempfile.TemporaryDirectory() as tempdir:
            fixture = Path(tempdir) / "huge.json"
            fixture.write_text(json.dumps(payload))
            completed = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "--repo",
                    "owner/repo",
                    "--pr",
                    "17",
                    "--fixture",
                    str(fixture),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertLess(len(completed.stderr), 1024)
        self.assertEqual(json.loads(completed.stderr)["error"]["kind"], "schema")

    def test_cli_bounds_fixture_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            fixture = Path(tempdir) / "oversized.json"
            fixture.write_bytes(b"{" + b" " * 1_048_576 + b"}")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "--repo",
                    "owner/repo",
                    "--pr",
                    "17",
                    "--fixture",
                    str(fixture),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertLess(len(completed.stderr), 1024)
        self.assertEqual(json.loads(completed.stderr)["error"]["kind"], "fixture")

    def test_cli_bounds_repository_components_and_expected_head(self) -> None:
        cases = (
            ["--repo", f"{'o' * 101}/repo", "--pr", "17"],
            [
                "--repo",
                "owner/repo",
                "--pr",
                "17",
                "--expected-head",
                "not-a-sha",
            ],
        )
        for args in cases:
            with self.subTest(args=args):
                completed = subprocess.run(
                    [sys.executable, str(CLI), *args],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, "")
                self.assertLess(len(completed.stderr), 1024)
                self.assertEqual(
                    json.loads(completed.stderr)["error"]["kind"], "argument"
                )

    def test_cli_acquisition_failure_is_compact_typed_and_nonzero(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch(
            "codex_chief_of_staff.watch_pr_cli.GitHubClient.fetch",
            side_effect=ProviderError(
                "permission", "secret-bearing provider prose", False
            ),
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = run_watch_cli(
                ["--repo", "owner/repo", "--pr", "17", "--max-attempts", "1"]
            )

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertLess(len(stderr.getvalue()), 1024)
        error = json.loads(stderr.getvalue())["error"]
        self.assertEqual(error["kind"], "permission")
        self.assertNotIn("secret-bearing", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

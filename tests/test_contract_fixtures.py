from __future__ import annotations

import json
import unittest
from pathlib import Path


FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ContractFixturesTest(unittest.TestCase):
    def load(self, name: str):
        with (FIXTURES / name).open(encoding="utf-8") as fixture:
            return json.load(fixture)

    def test_contract_fixture_set_is_complete(self) -> None:
        transitions = self.load("state-transitions.json")
        authority = self.load("authority-matrix.json")
        dispatches = self.load("dispatch-briefs.json")
        report = self.load("worker-report.json")
        verdict = self.load("verdict.json")
        failures = self.load("failure-scenarios.json")

        self.assertIn("draft", transitions)
        self.assertEqual(authority["build"]["authority"], "local-write")
        self.assertEqual(
            {brief["mode"] for brief in dispatches},
            {"scout", "build", "review", "publish", "babysit", "land"},
        )
        required_brief_fields = {
            "work_id", "outcome", "mode", "authority", "project", "base",
            "scope", "context", "acceptance", "verify", "stop_conditions",
            "callback", "report",
        }
        for brief in dispatches:
            self.assertTrue(required_brief_fields.issubset(brief))
            self.assertTrue(all(brief[field] for field in required_brief_fields))
            self.assertEqual(brief["callback"]["coordinator_task_id"], "chief-task")
            self.assertEqual(brief["callback"]["work_id"], brief["work_id"])
            self.assertIn("done", brief["callback"]["notify_on"])
            self.assertIn("needs-attention", brief["callback"]["notify_on"])
        self.assertEqual(report["work_id"], "CS-FIXTURE")
        self.assertEqual(verdict["head_sha"], report["head_sha"])
        self.assertGreaterEqual(len(failures), 4)


if __name__ == "__main__":
    unittest.main()

import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from schedule_agent.agent import run
from schedule_agent.catalog import Catalog
from schedule_agent.providers import MockProvider, DeepSeekProvider
from schedule_agent.storage import DraftStore
from schedule_agent.team import scoped_provider
from test_runtime import ScriptedProvider, call

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(ROOT / "data")
        self.request = json.loads((ROOT / "examples/inputs/normal.json").read_text())
        self.store = DraftStore(":memory:")
        self.addCleanup(self.store.close)

    def execute(self, provider=None, **kwargs):
        return run(self.request, self.catalog, self.store, provider or MockProvider(), orchestration="team", **kwargs)

    def test_actual_handoff_uses_observed_review_and_separate_roles(self):
        result = self.execute()
        self.assertEqual(result["task_status"], "completed")
        handoff = next(e for e in result["trace"] if e["tool"] == "handoff_schedule")
        self.assertEqual(handoff["observation"]["handoff"]["review"]["status"], "passed")
        self.assertEqual(handoff["observation"]["handoff"]["item"]["approved_slot"], None)
        self.assertEqual([e["agent_id"] for e in result["trace"]], ["reviewer"] * 3 + ["scheduler"] * 3)
        self.assertTrue(all(e["elapsed_ms"] >= 0 for e in result["trace"]))

    def test_reviewer_cannot_query_or_save_even_if_model_requests_it(self):
        provider = ScriptedProvider([[call("save_draft", 1, slot=self.catalog.slots[1])],
                                     [call("finish", 2, status="failed", suggested_slot=None)]])
        result = self.execute(provider)
        self.assertEqual(result["task_status"], "failed")
        self.assertEqual(result["trace"][0]["observation"]["error"]["code"], "ROLE_FORBIDDEN")
        self.assertFalse(self.store.all())

    def test_handoff_cannot_fabricate_review_or_consent(self):
        for args in ({}, {"approved_slot": self.catalog.slots[1]}, {"review": {"status": "passed"}}):
            provider = ScriptedProvider([[call("handoff_schedule", 1, **args)],
                                         [call("finish", 2, status="failed", suggested_slot=None)]])
            result = self.execute(provider)
            self.assertFalse(result["trace"][0]["observation"]["ok"])
            self.assertTrue(all(e["agent_id"] == "reviewer" for e in result["trace"]))
            self.assertFalse(self.store.all())

    def test_scheduler_cannot_self_approve_or_recheck_rules(self):
        outputs = [[call("read_material", 1, source="base")], [call("check_rules", 2)],
                   [call("handoff_schedule", 3)], [call("check_rules", 4)],
                   [call("finish", 5, status="failed", suggested_slot=None)]]
        result = self.execute(ScriptedProvider(outputs))
        self.assertEqual(result["trace"][3]["agent_id"], "scheduler")
        self.assertEqual(result["trace"][3]["observation"]["error"]["code"], "ROLE_FORBIDDEN")
        self.assertFalse(self.store.all())

    def test_scheduler_can_handoff_back_and_budget_is_shared(self):
        outputs = [[call("read_material", 1, source="base")], [call("check_rules", 2)],
                   [call("handoff_schedule", 3)], [call("request_review", 4)]]
        result = self.execute(ScriptedProvider(outputs), max_steps=4)
        self.assertEqual(result["task_status"], "incomplete")
        self.assertEqual(result["trace"][-1]["observation"]["to_agent"], "reviewer")
        self.assertEqual(result["items"][0]["review"]["status"], "not_checked")
        self.assertFalse(self.store.all())

    def test_only_allowed_tools_and_role_prompt_sent_to_model(self):
        provider = DeepSeekProvider(api_key="fixture-only")
        for role, forbidden in [("reviewer", "save_draft"), ("scheduler", "check_rules")]:
            body = scoped_provider(provider, role).payload([])
            self.assertNotIn(forbidden, [t["name"] for t in body["tools"]])
            self.assertIn("你是审核 Agent" if role == "reviewer" else "你是排期 Agent", body["instructions"])
        self.assertEqual(len(provider.payload([])["tools"]), 5)

    def test_token_budget_stops_before_next_call_and_keeps_evidence(self):
        class Metered(MockProvider):
            def next(self, history):
                response = super().next(history)
                response["usage"] = {"total_tokens": 100}
                return response
        observed = []
        result = self.execute(Metered(), max_total_tokens=50, on_event=observed.append)
        self.assertEqual(result["limits"]["steps_used"], 1)
        self.assertEqual(result["items"][0]["stop_reason"], "token_budget")
        self.assertEqual(observed, result["trace"])
        self.assertFalse(self.store.all())

    def test_time_budget_and_zero_calls_do_not_claim_success(self):
        with patch("schedule_agent.agent.time.monotonic", side_effect=[0, 2, 2]):
            result = self.execute(max_seconds=1)
        self.assertEqual(result["items"][0]["stop_reason"], "time_budget")
        self.assertEqual(result["limits"]["steps_used"], 0)
        self.assertFalse(self.store.all())

    def test_full_team_matrix_with_changed_occupancy_and_repetition(self):
        from evaluate import evaluate
        with tempfile.TemporaryDirectory() as directory:
            summary = evaluate(directory, orchestration="team")
            self.assertEqual(summary["cases_passed"], 21)
            self.assertEqual(summary["cases_total"], 21)

    def test_missing_fields_can_be_observed_before_formal_review(self):
        from evaluate import behavior_checks
        self.request["items"][0]["material_id"] = "M03"
        outputs = [[call("read_material", 1, source="base")], [call("read_material", 2, source="supplement")],
                   [call("check_rules", 3)], [call("finish", 4, status="needs_input", suggested_slot=None)]]
        result = self.execute(ScriptedProvider(outputs))
        self.assertTrue(behavior_checks("missing_info", result)["supplement_observed_then_reviewed"])
        result["trace"] = [e for e in result["trace"] if not (e["tool"] == "read_material" and e["parameters"]["source"] == "supplement")]
        self.assertFalse(behavior_checks("missing_info", result)["supplement_observed_then_reviewed"])

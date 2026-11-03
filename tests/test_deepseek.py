import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from schedule_agent.agent import run
from schedule_agent.catalog import Catalog
from schedule_agent.config import load_env_file
from schedule_agent.domain import InputError
from schedule_agent.providers import DeepSeekProvider, ModelError, MockProvider
from schedule_agent.storage import DraftStore

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from evaluate import behavior_checks, evaluate


class DeepSeekTests(unittest.TestCase):
    def test_batch_is_returned_to_model_and_model_can_reselect(self):
        requests = []
        policy = MockProvider()

        class Transport:
            def open(inner, req, timeout):
                body = json.loads(req.data)
                requests.append(body)
                history = body["input"]
                if len(requests) == 1:
                    def proposal(name, number, **args):
                        return {"type": "function_call", "call_id": str(number), "name": name,
                                "arguments": json.dumps({**args, "reason": "测试并行边界"})}
                    output = [proposal("read_material", 1, source="base"), proposal("save_draft", 2, slot="2026-11-03T19:00:00+08:00")]
                else:
                    # A transport fixture, never a claim of real model validation.
                    clean_history = [h for h in history if h.get("call_id") not in {"1", "2"}]
                    output = policy.next(clean_history)["output"]
                return io.BytesIO(json.dumps({"status": "completed", "id": "fixture_" + str(len(requests)), "output": output}).encode())

        store = DraftStore(":memory:")
        self.addCleanup(store.close)
        req = json.loads((ROOT / "examples/inputs/normal.json").read_text())
        provider = DeepSeekProvider(api_key="not-a-real-key", opener=Transport())
        result = run(req, Catalog(ROOT / "data"), store, provider)
        self.assertEqual(result["task_status"], "completed")
        self.assertEqual(len(store.all()), 1)
        self.assertEqual(result["trace"][0]["execution"], "rejected_batch")
        returned = [h for h in requests[1]["input"] if h.get("type") == "function_call_output"]
        self.assertEqual({h["call_id"] for h in returned}, {"1", "2"})
        self.assertTrue(all(json.loads(h["output"])["error"]["code"] == "BATCH_REJECTED" for h in returned))
        self.assertEqual(requests[0]["reasoning"], {"effort": "none"})
        self.assertEqual(requests[0]["model"], "deepseek-flash")
        self.assertNotIn("parallel_tool_calls", requests[0])
        self.assertNotIn("not-a-real-key", json.dumps(result))

    def test_missing_deepseek_key_does_not_reuse_openai_key(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "other-service-key"}, clear=True):
            with self.assertRaises(ModelError):
                DeepSeekProvider()

    def test_env_file_is_explicit_nonexecuting_and_environment_wins(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DEEPSEEK_MODEL": "explicit-model"}, clear=True):
            file = Path(tmp) / ".env"
            file.write_text('# local only\nDEEPSEEK_API_KEY="literal-$(not-executed)"\nDEEPSEEK_MODEL=deepseek-flash\n')
            load_env_file(file)
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "literal-$(not-executed)")
            self.assertEqual(os.environ["DEEPSEEK_MODEL"], "explicit-model")

    def test_invalid_config_is_redacted_and_not_partially_loaded(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            file = Path(tmp) / ".env"
            file.write_text('DEEPSEEK_API_KEY=private-test-value\nUNKNOWN=secret-line\n')
            with self.assertRaises(InputError) as error:
                load_env_file(file)
            self.assertNotIn("secret-line", str(error.exception))
            self.assertNotIn("DEEPSEEK_API_KEY", os.environ)

    def test_no_slots_does_not_pass_when_model_only_exhausts_budget(self):
        class Loop:
            mode, model = "mock", "loop-fixture"
            def next(self, history):
                return {"output": [{"type": "function_call", "call_id": str(len(history)), "name": "read_material",
                                    "arguments": json.dumps({"source": "base", "reason": "重复读取"})}]}
        store = DraftStore(":memory:")
        self.addCleanup(store.close)
        req = json.loads((ROOT / "examples/inputs/conflict.json").read_text())
        result = run(req, Catalog(ROOT / "data"), store, Loop(), max_steps=3)
        self.assertEqual(result["task_status"], "incomplete")
        assertions = behavior_checks("no_slots", result)
        self.assertFalse(assertions["not_budget_exhaustion"])
        self.assertFalse(assertions["no_slots_observed_then_stopped"])

    def test_selected_repeat_case_runs_its_prerequisite(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = evaluate(tmp, selected_cases=["duplicate_repeat"])
            self.assertEqual([c["case"] for c in summary["cases"]], ["duplicate_first", "duplicate_repeat"])
            self.assertEqual(summary["cases_passed"], 2)


if __name__ == "__main__":
    unittest.main()

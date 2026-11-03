import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from schedule_agent.agent import run
from schedule_agent.catalog import Catalog
from schedule_agent.providers import ModelError, MockProvider, OpenAIProvider
from schedule_agent.storage import DraftStore
from schedule_agent.tools import TOOL_SCHEMAS

ROOT = Path(__file__).resolve().parents[1]
S19 = "2026-11-03T19:00:00+08:00"


def call(name, number, **args):
    return {"type": "function_call", "call_id": "call_" + str(number), "name": name,
            "arguments": json.dumps({"reason": "依据测试响应执行工具", **args}, ensure_ascii=False)}


class ScriptedProvider:
    mode, model = "mock", "adversarial-contract-fixture"

    def __init__(self, outputs):
        self.outputs = iter(outputs)

    def next(self, history):
        return {"output": next(self.outputs), "usage": {}}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(ROOT / "data")
        self.store = DraftStore(":memory:")
        self.addCleanup(self.store.close)
        self.request = json.loads((ROOT / "examples/inputs/normal.json").read_text())

    def run_with(self, provider, limit=24):
        return run(self.request, self.catalog, self.store, provider, max_steps=limit)

    def test_model_cannot_finish_completed_before_saving(self):
        provider = ScriptedProvider([[call("finish", 1, status="completed", suggested_slot=None)],
                                     [call("finish", 2, status="failed", suggested_slot=None)]])
        result = self.run_with(provider)
        self.assertFalse(result["trace"][0]["observation"]["ok"])
        self.assertEqual(result["task_status"], "failed")
        self.assertFalse(self.store.all())

    def test_malformed_tool_arguments_are_observed_by_next_turn(self):
        bad = call("read_material", 1, source="base")
        bad["arguments"] = "{bad json"
        result = self.run_with(ScriptedProvider([[bad], [call("finish", 2, status="failed", suggested_slot=None)]]))
        self.assertEqual(result["trace"][0]["observation"]["error"]["code"], "INVALID_ARGUMENTS")
        self.assertEqual(result["trace"][0]["next_action"], "finish")

    def test_parallel_tool_calls_are_not_executed(self):
        result = self.run_with(ScriptedProvider([[call("read_material", 1, source="base"), call("save_draft", 2, slot=S19)],
                                               [call("finish", 3, status="failed", suggested_slot=None)]]))
        self.assertEqual(result["task_status"], "failed")
        self.assertEqual(result["trace"][0]["observation"]["error"]["code"], "BATCH_REJECTED")
        self.assertEqual(result["items"][0]["review"]["status"], "not_checked")
        self.assertFalse(self.store.all())

    def test_plain_text_claim_does_not_become_success(self):
        result = self.run_with(ScriptedProvider([[{"type": "message", "content": "已发布且保存成功"}]]))
        self.assertEqual(result["task_status"], "failed")
        self.assertIsNone(result["items"][0]["draft"])

    def test_repeated_call_id_stops_before_second_execution(self):
        same = call("read_material", 1, source="base")
        result = self.run_with(ScriptedProvider([[same], [same]]))
        self.assertEqual(result["task_status"], "failed")
        self.assertEqual(result["limits"]["steps_used"], 2)

    def test_infinite_model_loop_is_bounded(self):
        outputs = [[call("read_material", n, source="base")] for n in range(1, 10)]
        result = self.run_with(ScriptedProvider(outputs), limit=3)
        self.assertEqual(result["limits"]["steps_used"], 3)
        self.assertEqual(result["items"][0]["stop_reason"], "step_limit")

    def test_native_responses_roundtrip_returns_tool_observations_to_provider(self):
        requests = []
        expected = [call("read_material", 1, source="base"), call("check_rules", 2),
                    call("query_slots", 3, mode="requested"), call("save_draft", 4, slot=S19),
                    call("finish", 5, status="completed", suggested_slot=S19)]

        class TransportFixture:
            def open(inner, req, timeout):
                body = json.loads(req.data)
                requests.append(body)
                index = len(requests) - 1
                output = [expected[index]]
                if index == 0:
                    output.insert(0, {"type": "reasoning", "id": "rs_fixture", "summary": [], "encrypted_content": "opaque-fixture"})
                return io.BytesIO(json.dumps({"status": "completed", "id": "response-fixture",
                                              "output": output, "usage": {"input_tokens": 1, "output_tokens": 1}}).encode())

        provider = OpenAIProvider(model="contract-fixture", api_key="test-key", opener=TransportFixture())
        result = self.run_with(provider)
        self.assertEqual(result["task_status"], "completed")
        self.assertEqual(len(requests), 5)
        self.assertEqual(requests[0]["tools"], TOOL_SCHEMAS)
        self.assertFalse(requests[0]["parallel_tool_calls"])
        self.assertFalse(requests[0]["store"])
        returned = [h for h in requests[-1]["input"] if h.get("type") == "function_call_output"]
        self.assertEqual(len(returned), 4)
        self.assertEqual(json.loads(returned[-1]["output"])["draft"]["reserved_slot"], S19)
        self.assertIn("opaque-fixture", json.dumps(requests[1]))
        self.assertNotIn("opaque-fixture", json.dumps(result))
        self.assertNotIn("test-key", json.dumps(result))

    def test_http_error_is_redacted_and_does_not_fallback_to_mock(self):
        class ErrorTransport:
            def open(self, req, timeout):
                raise urllib.error.HTTPError(req.full_url, 401, "sensitive test-key", {}, None)

        provider = OpenAIProvider(model="fixture", api_key="test-key", opener=ErrorTransport())
        result = self.run_with(provider)
        self.assertEqual(result["task_status"], "failed")
        self.assertIn("401", result["items"][0]["errors"][0]["message"])
        self.assertNotIn("test-key", json.dumps(result))
        self.assertFalse(self.store.all())

    def test_incomplete_or_invalid_provider_responses_stop(self):
        for body in [{"status": "incomplete", "output": []}, [], {"status": "completed", "output": "wrong"}]:
            class BadTransport:
                def open(self, req, timeout):
                    return io.BytesIO(json.dumps(body).encode())
            with self.subTest(body=body):
                result = self.run_with(OpenAIProvider(model="fixture", api_key="test-key", opener=BadTransport()))
                self.assertEqual(result["task_status"], "failed")

    def test_missing_key_and_insecure_remote_endpoint_are_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ModelError):
                OpenAIProvider(model="fixture")
        for url in ["http://example.com/v1", "https://name:pass@example.com/v1", "https://example.com?key=x"]:
            with self.assertRaises(ModelError):
                OpenAIProvider(model="fixture", api_key="test-key", base_url=url)


class CliTests(unittest.TestCase):
    def invoke(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        return subprocess.run([sys.executable, "-m", "schedule_agent", *args], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=15)

    def test_cli_normal_conflict_and_failure_exit_codes(self):
        cases = [("normal", [], 0), ("conflict", [], 2), ("normal", ["--fail", "save_draft:1"], 1)]
        with tempfile.TemporaryDirectory() as tmp:
            for name, extra, expected in cases:
                path = Path(tmp) / (name + str(expected) + ".json")
                proc = self.invoke("--input", "examples/inputs/" + name + ".json", "--db", ":memory:", "--output", str(path), *extra)
                self.assertEqual(proc.returncode, expected, proc.stderr)
                self.assertEqual(json.loads(proc.stdout), json.loads(path.read_text()))

    def test_cli_invalid_json_and_invalid_fault_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.json"
            path.write_text("not json")
            proc = self.invoke("--input", str(path), "--db", ":memory:")
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(json.loads(proc.stderr)["task_status"], "failed")
        proc = self.invoke("--input", "examples/inputs/normal.json", "--db", ":memory:", "--fail", "publish:1")
        self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main()

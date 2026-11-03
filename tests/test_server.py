import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from schedule_agent.server import LocalApplication, make_server
from schedule_agent.domain import InputError

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "local-test-token-never-a-model-key"


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = LocalApplication(ROOT / "data", self.tmp.name, TOKEN, demo=True)
        self.server = make_server(self.app, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.payload = {"request": json.loads((ROOT / "examples/inputs/conflict.json").read_text()), "orchestration": "team"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.app.close()
        self.tmp.cleanup()

    def http(self, method, path, data=None, auth=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        hs = {"Content-Type": "application/json"}
        if auth:
            hs["Authorization"] = "Bearer " + TOKEN
        hs.update(headers or {})
        conn.request(method, path, None if data is None else json.dumps(data), hs)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body

    def wait(self, identifier):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, raw = self.http("GET", "/api/runs/" + identifier)
            self.assertEqual(status, 200)
            result = json.loads(raw)
            if result["status"] in {"finished", "error", "interrupted"}:
                return result
            time.sleep(.01)
        self.fail("local fixture exceeded deadline")

    def submit(self, payload):
        status, raw = self.http("POST", "/api/runs", payload)
        self.assertEqual(status, 202, raw)
        return self.wait(json.loads(raw)["id"])

    def test_http_conflict_changed_occupancy_repeat_and_explicit_confirmation(self):
        before = self.submit(self.payload)
        item = before["result"]["items"][0]
        self.assertEqual(item["task_status"], "pending_confirmation")
        self.assertIsNone(item["draft"]["reserved_slot"])
        slots = self.app.config()["slots"]
        self.payload["occupied_slots"] = slots[:2]
        after = self.submit(self.payload)
        repeated = self.submit(self.payload)
        self.assertEqual(after["result"]["items"][0]["suggested_slot"], slots[2])
        self.assertTrue(repeated["result"]["items"][0]["duplicate"])
        self.assertEqual(len(self.app.drafts()), 1)
        bad, _ = self.http("POST", "/api/runs/" + after["id"] + "/confirm", {"material_id": "M01", "slot": slots[1]})
        self.assertEqual(bad, 400)
        status, raw = self.http("POST", "/api/runs/" + after["id"] + "/confirm", {"material_id": "M01", "slot": slots[2]})
        self.assertEqual(status, 202)
        confirmed = self.wait(json.loads(raw)["id"])
        self.assertEqual(confirmed["result"]["task_status"], "completed")
        rows = self.app.drafts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["draft_id"], item["draft"]["draft_id"])
        self.assertEqual(rows[0]["reserved_slot"], slots[2])

    def test_http_save_failure_and_durable_trace(self):
        self.payload["failures"] = {"save_draft": 1}
        job = self.submit(self.payload)
        self.assertEqual(job["result"]["task_status"], "failed")
        self.assertFalse(self.app.drafts())
        journal = Path(self.tmp.name, job["id"] + ".trace.jsonl")
        self.assertEqual([json.loads(s) for s in journal.read_text().splitlines()], job["events"])
        self.assertNotIn(TOKEN, journal.read_text())
        self.app.jobs.clear()
        recovered = self.app.get(job["id"])
        self.assertEqual(recovered["result"], job["result"])

    def test_interrupted_job_has_no_fabricated_completed_status(self):
        identifier = "a" * 32
        Path(self.tmp.name, identifier + ".request.json").write_text('{}')
        self.assertEqual(self.app.get(identifier)["status"], "interrupted")

    def test_auth_origin_body_and_path_boundaries(self):
        self.assertEqual(self.http("GET", "/healthz", auth=False)[0], 200)
        self.assertEqual(self.http("GET", "/api/config", auth=False)[0], 401)
        self.assertEqual(self.http("POST", "/api/runs", self.payload, headers={"Origin": "https://attacker.invalid"})[0], 401)
        self.assertEqual(self.http("GET", "/api/config", headers={"Host": "attacker.invalid"})[0], 403)
        self.assertEqual(self.http("GET", "/api/runs/../../.env")[0], 404)
        self.assertEqual(self.http("GET", "/.env")[0], 404)
        self.assertEqual(self.http("POST", "/api/runs", {"request": "x" * 40000})[0], 413)
        self.assertEqual(self.http("POST", "/api/runs", {**self.payload, "max_steps": 999})[0], 400)
        self.assertEqual(self.http("GET", "/")[0], 200)
        status, body = self.http("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertNotIn(b"API_KEY", body)

    def test_demo_overrides_not_available_without_explicit_switch(self):
        self.app.demo = False
        self.assertEqual(self.http("POST", "/api/runs", {**self.payload, "failures": {"save_draft": 1}})[0], 400)

    def test_bounded_queue_rejects_ninth_pending_task(self):
        with patch.object(self.app.pool, "submit"):
            for _ in range(8):
                self.app.submit(self.payload)
            self.assertEqual(self.http("POST", "/api/runs", self.payload)[0], 429)


class CampaignHttpTests(ServerTests):
    # Reuse the real local server fixture, but only run dedicated campaign tests here.
    test_http_conflict_changed_occupancy_repeat_and_explicit_confirmation = None
    test_http_save_failure_and_durable_trace = None
    test_interrupted_job_has_no_fabricated_completed_status = None
    test_auth_origin_body_and_path_boundaries = None
    test_demo_overrides_not_available_without_explicit_switch = None
    test_bounded_queue_rejects_ninth_pending_task = None

    def campaign_post(self,path,body):
        status,raw=self.http('POST',path,body)
        self.assertEqual(status,202,raw)
        return self.wait(json.loads(raw)['id'])

    def test_campaign_pause_resume_version_confirm_and_stale_approval(self):
        request=json.loads((ROOT/'examples/inputs/campaign.json').read_text())
        first=self.campaign_post('/api/campaigns',{'request':request,'max_steps':11})
        self.assertEqual(first['result']['task_status'],'incomplete')
        resumed=self.campaign_post('/api/campaigns/'+request['request_id']+'/resume',{'provider':'mock'})
        self.assertEqual(resumed['result']['task_status'],'pending_confirmation')
        self.assertEqual(len(resumed['result']['resume_info']['reused_reviews']),2)
        state=resumed['result']['campaign_state']
        bad=self.campaign_post('/api/campaigns/'+request['request_id']+'/confirm',{'plan_version':state['plan_version']-1,'plan_hash':state['plan_hash']})
        self.assertEqual(bad['result']['confirmation']['error']['code'],'STALE_APPROVAL')
        self.assertTrue(all(d['reserved_slot'] is None for d in bad['result']['drafts']))
        good=self.campaign_post('/api/campaigns/'+request['request_id']+'/confirm',{'plan_version':state['plan_version'],'plan_hash':state['plan_hash']})
        self.assertEqual(good['result']['task_status'],'completed')
        status,raw=self.http('GET','/api/campaigns/'+request['request_id'])
        self.assertEqual(status,200)
        self.assertEqual(json.loads(raw)['campaign_state']['status'],'completed')
        status,raw=self.http('GET','/api/campaigns')
        self.assertEqual(len(json.loads(raw)['campaigns']),1)
        self.assertEqual(self.http('GET','/campaign')[0],200)

    def test_campaign_api_rejects_forged_consent_and_resume_requirement_changes(self):
        request=json.loads((ROOT/'examples/inputs/campaign.json').read_text())
        bad={**request,'approved':True}
        self.assertEqual(self.http('POST','/api/campaigns',{'request':bad})[0],400)
        self.campaign_post('/api/campaigns',{'request':request,'max_steps':1})
        self.assertEqual(self.http('POST','/api/campaigns/'+request['request_id']+'/resume',{'request':request})[0],400)
        self.assertEqual(self.http('POST','/api/campaigns/'+request['request_id']+'/confirm',{'approved':True})[0],400)
        self.assertEqual(self.http('GET','/api/campaigns',auth=False)[0],401)

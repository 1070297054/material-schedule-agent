"""Local authenticated HTTP UI/API with a bounded single-worker task queue."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit
import uuid

from .agent import run, utc_now
from .catalog import Catalog
from .cli import write_json
from .config import load_env_file
from .domain import InputError, validate_request
from .providers import create_provider
from .storage import DraftStore

ROOT = Path(__file__).resolve().parents[2]
WEB = Path(__file__).resolve().parent / "web"


class LocalApplication:
    def __init__(self, data_dir, runtime, token, demo=False):
        self.data_dir, self.runtime = Path(data_dir), Path(runtime)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.db_path = self.runtime / "drafts.sqlite3"
        DraftStore(self.db_path).close()
        self.token, self.demo = token, demo
        self.lock = threading.Lock()
        self.jobs = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="schedule")
        self.providers = ["mock"] + [name for name in ("deepseek", "openai") if os.environ.get(name.upper() + "_API_KEY")]

    def close(self):
        self.pool.shutdown(wait=True)

    def config(self):
        catalog = Catalog(self.data_dir)
        return {"materials": list(catalog.materials.values()), "slots": catalog.slots,
                "occupied": catalog.existing, "providers": self.providers, "demo": self.demo,
                "orchestrations": ["team", "single"], "published": False}

    def validate(self, body):
        if not isinstance(body, dict) or set(body) - {"request", "provider", "orchestration", "max_steps", "occupied_slots", "failures"}:
            raise InputError("请求字段无效")
        request = validate_request(body.get("request"))
        provider, mode = body.get("provider", "mock"), body.get("orchestration", "team")
        if provider not in self.providers or mode not in {"single", "team"}:
            raise InputError("模型服务未配置或编排模式无效")
        steps = body.get("max_steps", 24)
        if type(steps) is not int or not 1 <= steps <= 48:
            raise InputError("max_steps 必须为 1 至 48")
        payload = {"request": request, "provider": provider, "orchestration": mode, "max_steps": steps}
        if "occupied_slots" in body or "failures" in body:
            if not self.demo:
                raise InputError("故障与占用覆盖仅在 --demo 演示模式开放")
            slots = body.get("occupied_slots")
            if slots is not None:
                catalog = Catalog(self.data_dir)
                if not isinstance(slots, list) or len(slots) > len(catalog.slots) or any(s not in catalog.slots for s in slots):
                    raise InputError("占用时段不在合成时段表中")
                payload["occupied_slots"] = list(dict.fromkeys(slots))
            faults = body.get("failures", {})
            if not isinstance(faults, dict) or any(k not in {"read_material", "check_rules", "query_slots", "save_draft"} or type(v) is not int or not 1 <= v <= 2 for k, v in faults.items()):
                raise InputError("演示故障参数无效")
            payload["failures"] = faults
        return payload

    def submit(self, body):
        payload = self.validate(body)
        with self.lock:
            if sum(j["status"] in {"queued", "running"} for j in self.jobs.values()) >= 8:
                raise OverflowError("本机任务队列已满，请等待当前任务完成")
            if len(self.jobs) >= 100:
                finished = [k for k, j in self.jobs.items() if j["status"] not in {"queued", "running"}]
                if finished:
                    del self.jobs[finished[0]]
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {"id": job_id, "status": "queued", "created_at": utc_now(), "events": [], "payload": payload}
            write_json(self.runtime / (job_id + ".request.json"), payload)
            self.pool.submit(self._execute, job_id, payload)
        return {"id": job_id, "status": "queued"}

    def _execute(self, job_id, payload):
        store = None
        try:
            with self.lock:
                self.jobs[job_id]["status"] = "running"
            catalog = Catalog(self.data_dir)
            if "occupied_slots" in payload:
                catalog.existing = [{"platform": "douyin", "slot": s} for s in payload["occupied_slots"]]
            provider = create_provider(payload["provider"])
            store = DraftStore(self.db_path)
            with (self.runtime / (job_id + ".trace.jsonl")).open("a", encoding="utf-8") as journal:
                def event_sink(event):
                    journal.write(json.dumps(event, ensure_ascii=False) + "\n")
                    journal.flush()
                    with self.lock:
                        self.jobs[job_id]["events"].append(deepcopy(event))
                result = run(payload["request"], catalog, store, provider, payload["max_steps"], payload.get("failures"),
                             payload["orchestration"], max_total_tokens=60000, max_seconds=180, on_event=event_sink)
            snapshot = {"id": job_id, "status": "finished", "result": result, "events": result["trace"]}
            write_json(self.runtime / (job_id + ".result.json"), snapshot)
            with self.lock:
                self.jobs[job_id].update(snapshot)
        except Exception:
            # Do not expose exception repr: it may contain upstream secrets or paths.
            with self.lock:
                self.jobs[job_id].update({"status": "error", "error": "任务执行或记录保存失败；请检查本机配置与可写目录。不能据此判断草稿未写入，请查看草稿列表。"})
        finally:
            if store:
                store.close()

    def get(self, job_id):
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise KeyError(job_id)
        with self.lock:
            job = self.jobs.get(job_id)
            if job:
                return deepcopy({k: v for k, v in job.items() if k != "payload"})
        result_file = self.runtime / (job_id + ".result.json")
        if result_file.exists():
            return json.loads(result_file.read_text(encoding="utf-8"))
        if (self.runtime / (job_id + ".request.json")).exists():
            trace = self.runtime / (job_id + ".trace.jsonl")
            events = []
            for line in trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    break
            return {"id": job_id, "status": "interrupted", "events": events,
                    "error": "服务曾中断，保留已落盘步骤；未自动续跑。请查询草稿，使用相同业务请求 ID 重试。"}
        raise KeyError(job_id)

    def confirm(self, job_id, body):
        if not isinstance(body, dict) or set(body) != {"material_id", "slot"}:
            raise InputError("确认必须提供素材 ID 与明确同意的时段")
        job = self.get(job_id)
        if job["status"] != "finished":
            raise InputError("只能确认已完成分析的待确认草稿")
        matches = [i for i in job["result"]["items"] if i["material_id"] == body["material_id"]]
        if not matches or matches[0]["task_status"] != "pending_confirmation" or matches[0]["suggested_slot"] != body["slot"]:
            raise InputError("确认时段与实际待确认建议不一致")
        payload = json.loads((self.runtime / (job_id + ".request.json")).read_text(encoding="utf-8"))
        item = next(i for i in payload["request"]["items"] if i["material_id"] == body["material_id"])
        item["approved_slot"] = body["slot"]
        payload["request"]["items"] = [item]
        return self.submit(payload)

    def drafts(self):
        store = DraftStore(self.db_path)
        try:
            return store.all()
        finally:
            store.close()


def make_server(app, host="127.0.0.1", port=8765):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ScheduleLocal/1"

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass  # No URLs, Authorization headers, or input payloads in access logs.

        def send(self, status, value, content_type="application/json; charset=utf-8"):
            raw = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, (dict, list)) else value
            self.send_response(status)
            for key, val in {"Content-Type": content_type, "Content-Length": str(len(raw)), "Cache-Control": "no-store",
                             "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                             "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items():
                self.send_header(key, val)
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def trusted(self):
            allowed = {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            return host in allowed and (origin is None or origin == "http://" + host)

        def authorized(self):
            supplied = self.headers.get("Authorization", "")
            return self.trusted() and secrets.compare_digest(supplied.encode(), ("Bearer " + app.token).encode())

        def do_GET(self):
            path = urlsplit(self.path).path
            if not self.trusted():
                return self.send(403, {"error": "仅接受本机同源请求"})
            if path == "/healthz":
                return self.send(200, {"status": "ok", "published": False})
            assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "application/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path in assets:
                name, mime = assets[path]
                return self.send(200, (WEB / name).read_bytes(), mime)
            if not self.authorized():
                return self.send(401, {"error": "请使用启动时的本机访问口令"})
            try:
                if path == "/api/config":
                    return self.send(200, app.config())
                if path == "/api/drafts":
                    return self.send(200, {"drafts": app.drafts()})
                if path.startswith("/api/runs/"):
                    return self.send(200, app.get(path[len("/api/runs/"):]))
                return self.send(404, {"error": "接口不存在"})
            except KeyError:
                return self.send(404, {"error": "任务不存在"})
            except Exception:
                return self.send(500, {"error": "本地读取失败"})

        def do_POST(self):
            if not self.authorized():
                return self.send(401, {"error": "访问口令或请求来源无效"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32768 or self.headers.get("Transfer-Encoding"):
                    return self.send(413, {"error": "请求必须为 1 至 32768 字节"})
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    return self.send(415, {"error": "仅支持 application/json"})
                body = json.loads(self.rfile.read(length))
                path = urlsplit(self.path).path
                if path == "/api/runs":
                    return self.send(202, app.submit(body))
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "confirm":
                    return self.send(202, app.confirm(parts[2], body))
                return self.send(404, {"error": "接口不存在"})
            except (ValueError, TypeError):
                return self.send(400, {"error": "输入无效，请检查请求字段、时段及演示开关"})
            except KeyError:
                return self.send(404, {"error": "任务不存在"})
            except OverflowError:
                return self.send(429, {"error": "任务队列已满，请稍后再试"})
            except Exception:
                return self.send(500, {"error": "任务提交失败，请检查本机配置"})

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description="本机素材排期工作台，不部署公网")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--env-file")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--runtime", default=str(ROOT / ".runtime" / "web"))
    parser.add_argument("--demo", action="store_true", help="开放合成占用修改与故障注入")
    args = parser.parse_args()
    load_env_file(args.env_file)
    token = os.environ.get("SCHEDULE_API_TOKEN") or secrets.token_urlsafe(32)
    if len(token) < 24 or not token.isascii():
        parser.error("SCHEDULE_API_TOKEN 须为至少 24 个 ASCII 字符")
    app = LocalApplication(args.data_dir, args.runtime, token, args.demo)
    server = make_server(app, port=args.port)
    print("仅本机访问（口令在 URL 片段中，不发送到服务器日志）：", flush=True)
    print("http://127.0.0.1:%d/#token=%s" % (server.server_port, token), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()

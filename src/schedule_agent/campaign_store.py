"""Durable campaign checkpoints, expiring leases, atomic draft-set confirmation."""
from copy import deepcopy
import json
import sqlite3
import time
import uuid

from .campaign_domain import digest, initial_state, plan_hash
from .domain import InputError
from .storage import DraftStore, error

PLAN_TTL = 1800
LEASE_SECONDS = 120


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class CampaignStore:
    def __init__(self, path):
        self.base = DraftStore(path)
        self.db = self.base.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS campaigns (
                request_id TEXT PRIMARY KEY, request_json TEXT NOT NULL,
                state_json TEXT NOT NULL, revision INTEGER NOT NULL,
                lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS campaign_events (
                execution_id TEXT NOT NULL, step INTEGER NOT NULL, request_id TEXT NOT NULL,
                event_json TEXT NOT NULL, PRIMARY KEY(execution_id, step)
            );
        ''')
        self.owner, self.request_id, self.revision = None, None, None

    def close(self):
        self.release()
        self.base.close()

    def acquire(self, request, source_version):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            rid = request["request_id"]
            row = self.db.execute("SELECT * FROM campaigns WHERE request_id=?", (rid,)).fetchone()
            if row and json.loads(row["request_json"]) != request:
                raise InputError("同一活动 ID 的硬性要求或目标已改变；请新建 request_id")
            if row and row["lease_owner"] and row["lease_until"] > time.time():
                raise InputError("此活动已有执行中的任务；请等待完成，崩溃后租约最多 120 秒失效")
            state = json.loads(row["state_json"]) if row else initial_state(request, source_version)
            owner = uuid.uuid4().hex
            if row:
                self.db.execute("UPDATE campaigns SET lease_owner=?,lease_until=? WHERE request_id=?", (owner, time.time()+LEASE_SECONDS, rid))
                revision = row["revision"]
            else:
                self.db.execute("INSERT INTO campaigns VALUES(?,?,?,?,?,?)", (rid, encoded(request), encoded(state), 0, owner, time.time()+LEASE_SECONDS))
                revision = 0
            self.db.execute("COMMIT")
            self.owner, self.request_id, self.revision = owner, rid, revision
            return state
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _update(self, state):
        changed = self.db.execute("UPDATE campaigns SET state_json=?, revision=revision+1, lease_until=? WHERE request_id=? AND revision=? AND lease_owner=? AND lease_until>?",
                                  (encoded(state), time.time()+LEASE_SECONDS, self.request_id, self.revision, self.owner, time.time())).rowcount
        if changed != 1:
            raise InputError("活动状态版本或执行租约失效，请重新恢复任务")

    def checkpoint(self, state, execution_id=None, event=None):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._update(state)
            if state["status"] not in {"pending_confirmation", "completed"}:
                self.db.execute("UPDATE campaign_drafts SET confirmation_status='stale' WHERE request_id=? AND reserved_slot IS NULL", (self.request_id,))
            if event is not None:
                self.db.execute("INSERT OR REPLACE INTO campaign_events VALUES(?,?,?,?)", (execution_id, event["step"], self.request_id, encoded(event)))
            self.db.execute("COMMIT")
            self.revision += 1
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def release(self):
        if self.owner:
            self.db.execute("UPDATE campaigns SET lease_owner=NULL,lease_until=0 WHERE request_id=? AND lease_owner=?", (self.request_id, self.owner))
            self.owner = None

    def snapshot(self, request_id):
        row = self.db.execute("SELECT request_json,state_json,revision FROM campaigns WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            raise InputError("活动不存在")
        return {"request": json.loads(row["request_json"]), "state": json.loads(row["state_json"]), "revision": row["revision"]}

    def list_campaigns(self):
        return [{"request_id": r["request_id"], **{k: json.loads(r["state_json"])[k] for k in ("status", "plan_version", "plan_hash")}} for r in self.db.execute("SELECT * FROM campaigns ORDER BY rowid DESC LIMIT 100")]

    def drafts(self, request_id=None):
        query = "SELECT * FROM campaign_drafts" + (" WHERE request_id=?" if request_id else "") + " ORDER BY draft_id"
        return [{**dict(row), "workflow": "campaign", "published": False} for row in self.db.execute(query, (request_id,) if request_id else ())]

    def reservations(self, catalog):
        rows = list(catalog.base.existing)
        rows += [dict(r) for r in self.db.execute("SELECT platform,reserved_slot AS slot FROM drafts WHERE reserved_slot IS NOT NULL")]
        rows += [dict(r) for r in self.db.execute("SELECT platform,reserved_slot AS slot FROM campaign_drafts WHERE reserved_slot IS NOT NULL AND request_id!=?", (self.request_id or "",))]
        return rows

    def verify_plan(self, request, state, catalog):
        if state["source_version"] != catalog.version:
            return error("STALE_SOURCE", "素材、规则或排期资料已变化，需恢复任务重新审核/排期")
        for task in state["tasks"].values():
            if not task["material"] or not task["review"] or task["review"]["status"] != "passed" or not task["slot"]:
                return error("PLAN_INCOMPLETE", "每个平台任务都须有实际审核与候选时段")
            current = catalog.material(task["material_id"], task["platform"], task["supplement"])
            if current != task["material"] or catalog.audit(current, task["platform"]) != task["review"]:
                return error("STALE_REVIEW", "审核依据已变更")
            conflict = catalog.conflict(request, task, task["slot"], state["tasks"], self.reservations(catalog))
            if conflict:
                return error(conflict, "方案重新校验失败：" + task["task_id"], True)
        return {"ok": True}

    def stage(self, request, state, catalog):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            catalog.reload()
            checked = self.verify_plan(request, state, catalog)
            if not checked["ok"]:
                self.db.execute("ROLLBACK")
                return checked
            updated = deepcopy(state)
            updated.update(status="pending_confirmation", plan_hash=plan_hash(request, state), plan_created_at=time.time())
            for t in state["tasks"].values():
                self.db.execute('''INSERT INTO campaign_drafts
                    (request_id,task_id,material_id,platform,suggested_slot,reserved_slot,confirmation_status,plan_version,plan_hash)
                    VALUES(?,?,?,?,?,NULL,'pending_confirmation',?,?)
                    ON CONFLICT(request_id,task_id) DO UPDATE SET suggested_slot=excluded.suggested_slot,
                    confirmation_status='pending_confirmation',plan_version=excluded.plan_version,plan_hash=excluded.plan_hash
                    WHERE campaign_drafts.reserved_slot IS NULL''',
                    (request["request_id"],t["task_id"],t["material_id"],t["platform"],t["slot"],updated["plan_version"],updated["plan_hash"]))
            self._update(updated)
            self.db.execute("COMMIT")
            self.revision += 1
            state.clear()
            state.update(updated)
            return {"ok": True, "status": "pending_confirmation", "plan_version": state["plan_version"],
                    "plan_hash": state["plan_hash"], "drafts": self.drafts(request["request_id"]), "reserved": False}
        except sqlite3.Error:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            return error("STORAGE_ERROR", "整组建议稿保存失败，事务已回滚")
        except Exception:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def confirm(self, request, state, catalog, version, expected_hash):
        """Operator-only API. No model tool exposes confirmation or approval fields."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if type(version) is not int or not isinstance(expected_hash, str) or version != state["plan_version"] or expected_hash != state["plan_hash"]:
                result = error("STALE_APPROVAL", "批准的方案版本或哈希不匹配，请查看最新方案后重新确认")
            elif state["status"] == "completed" and state["confirmed_hash"] == expected_hash:
                self.db.execute("ROLLBACK")
                return {"ok": True, "status": "completed", "duplicate": True, "drafts": self.drafts(request["request_id"])}
            elif state["status"] != "pending_confirmation" or not state["plan_created_at"] or time.time()-state["plan_created_at"] > PLAN_TTL:
                result = error("APPROVAL_EXPIRED_OR_NOT_READY", "方案未准备好或已超过 30 分钟确认期限，请恢复任务生成新版本")
            else:
                catalog.reload()
                result = self.verify_plan(request, state, catalog)
            if not result["ok"]:
                self.db.execute("ROLLBACK")
                return result
            if plan_hash(request, state) != expected_hash:
                self.db.execute("ROLLBACK")
                return error("STALE_APPROVAL", "方案内容与批准哈希不一致")
            updated = deepcopy(state)
            updated.update(status="completed", confirmed_hash=expected_hash)
            for platform in request["platforms"]:
                policy = catalog.policy["platforms"][platform]
                self.db.execute("INSERT INTO platform_limits VALUES(?,?,?) ON CONFLICT(platform) DO UPDATE SET daily_limit=excluded.daily_limit,min_gap_hours=excluded.min_gap_hours",
                                (platform,policy["daily_limit"],policy["min_gap_hours"]))
            for task in state["tasks"].values():
                changed = self.db.execute("UPDATE campaign_drafts SET reserved_slot=suggested_slot,confirmation_status='confirmed' WHERE request_id=? AND task_id=? AND plan_hash=? AND plan_version=? AND reserved_slot IS NULL",
                                          (request["request_id"],task["task_id"],expected_hash,version)).rowcount
                if changed != 1:
                    raise sqlite3.IntegrityError("draft mismatch")
            self._update(updated)
            self.db.execute("COMMIT")
            self.revision += 1
            state.clear()
            state.update(updated)
            return {"ok": True, "status": "completed", "duplicate": False, "drafts": self.drafts(request["request_id"]), "published": False}
        except sqlite3.Error:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            return error("STORAGE_ERROR", "确认保存失败，整组事务回滚，未确认任何新增占位")
        except Exception:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

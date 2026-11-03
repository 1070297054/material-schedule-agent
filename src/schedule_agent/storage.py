"""SQLite transaction is the authority for idempotency and slot reservations."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from .domain import fingerprint


def error(code, message, retryable=False):
    return {"ok": False, "error": {"code": code, "message": message, "retryable": retryable}}


class DraftStore:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS drafts (
                draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                material_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                platform TEXT NOT NULL,
                requested_slot TEXT NOT NULL,
                suggested_slot TEXT NOT NULL,
                reserved_slot TEXT,
                confirmation_status TEXT NOT NULL,
                audit_json TEXT NOT NULL,
                UNIQUE(request_id, material_id)
            );
            CREATE TABLE IF NOT EXISTS platform_limits (
                platform TEXT PRIMARY KEY, daily_limit INTEGER NOT NULL, min_gap_hours INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS campaign_drafts (
                draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL, task_id TEXT NOT NULL, material_id TEXT NOT NULL,
                platform TEXT NOT NULL, suggested_slot TEXT NOT NULL, reserved_slot TEXT,
                confirmation_status TEXT NOT NULL, plan_version INTEGER NOT NULL, plan_hash TEXT NOT NULL,
                UNIQUE(request_id, task_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS campaign_reserved_slot
                ON campaign_drafts(platform, reserved_slot) WHERE reserved_slot IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS unique_reserved_slot
                ON drafts(platform, reserved_slot) WHERE reserved_slot IS NOT NULL;
        """)

    def close(self):
        self.db.close()

    def all(self):
        return [self._public(row) for row in self.db.execute("SELECT * FROM drafts ORDER BY draft_id")]

    def _public(self, row):
        if row is None:
            return None
        value = dict(row)
        value.pop("fingerprint")
        value["audit"] = json.loads(value.pop("audit_json"))
        value["published"] = False
        return value

    def lookup(self, request_id, material_id):
        row = self.db.execute("SELECT * FROM drafts WHERE request_id=? AND material_id=?",
                              (request_id, material_id)).fetchone()
        return self._public(row)

    def capacity_conflict(self, platform, slot, request_id, material_id):
        policy = self.db.execute("SELECT * FROM platform_limits WHERE platform=?", (platform,)).fetchone()
        if not policy:
            return False
        slots = {r[0] for r in self.db.execute("SELECT reserved_slot FROM drafts WHERE platform=? AND reserved_slot IS NOT NULL AND NOT(request_id=? AND material_id=?)", (platform,request_id,material_id))}
        slots.update(r[0] for r in self.db.execute("SELECT reserved_slot FROM campaign_drafts WHERE platform=? AND reserved_slot IS NOT NULL", (platform,)))
        return (sum(s[:10]==slot[:10] for s in slots)>=policy["daily_limit"] or
                any(abs((datetime.fromisoformat(s)-datetime.fromisoformat(slot)).total_seconds())<policy["min_gap_hours"]*3600 for s in slots))

    def occupied(self, platform, slot, request_id, material_id):
        return self.capacity_conflict(platform,slot,request_id,material_id) or bool(self.db.execute("SELECT 1 FROM campaign_drafts WHERE platform=? AND reserved_slot=?", (platform, slot)).fetchone()) or self.db.execute(
            "SELECT 1 FROM drafts WHERE platform=? AND reserved_slot=? "
            "AND NOT (request_id=? AND material_id=?)",
            (platform, slot, request_id, material_id)).fetchone() is not None

    def save(self, request_id, item, material, slot, audit, existing_schedule):
        """Call after tool preconditions; recheck reservations inside the write lock."""
        fp = fingerprint(request_id, item)
        status = "confirmed" if slot == (item["approved_slot"] or item["requested_slot"]) else "pending_confirmation"
        reserved = slot if status == "confirmed" else None
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM drafts WHERE request_id=? AND material_id=?",
                                  (request_id, item["material_id"])).fetchone()
            if row and row["fingerprint"] != fp:
                self.db.execute("ROLLBACK")
                return error("IDEMPOTENCY_CONFLICT", "同一请求 ID 的原始时间要求已改变，请使用新 request_id")
            if row and row["confirmation_status"] == "confirmed" and row["suggested_slot"] != slot:
                self.db.execute("ROLLBACK")
                return error("IMMUTABLE_DRAFT", "已确认草稿不可通过重复请求改期；需另行处理原草稿")
            platform = material["platform"]
            external_conflict = any(s["platform"] == platform and s["slot"] == slot for s in existing_schedule)
            if external_conflict or self.occupied(platform, slot, request_id, item["material_id"]):
                self.db.execute("ROLLBACK")
                return error("SLOT_CONFLICT", "保存时发现时段被占用，请重新查询", True)
            if row and row["confirmation_status"] == "confirmed" and status == "pending_confirmation":
                # Replaying the original pre-confirmation input must never revoke consent.
                status, reserved = "confirmed", slot
            duplicate = bool(row and row["suggested_slot"] == slot and row["confirmation_status"] == status)
            values = (platform, item["requested_slot"], slot, reserved, status,
                      json.dumps(audit, ensure_ascii=False))
            if row:
                self.db.execute("UPDATE drafts SET platform=?, requested_slot=?, suggested_slot=?, reserved_slot=?, "
                                "confirmation_status=?, audit_json=? WHERE draft_id=?", values + (row["draft_id"],))
            else:
                self.db.execute("INSERT INTO drafts (platform, requested_slot, suggested_slot, reserved_slot, "
                                "confirmation_status, audit_json, request_id, material_id, fingerprint) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", values + (request_id, item["material_id"], fp))
            # Read our own write before committing. A successful response only follows COMMIT.
            saved = self.lookup(request_id, item["material_id"])
            self.db.execute("COMMIT")
            return {"ok": True, "draft": saved, "duplicate": duplicate}
        except sqlite3.Error:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            return error("STORAGE_ERROR", "草稿保存失败，数据库事务未确认成功；结束本次执行，由用户决定重新运行")

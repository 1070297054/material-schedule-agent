"""Bounded cross-day windows and per-platform deterministic constraints."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re

from .catalog import Catalog
from .domain import InputError, canonical_slot, review


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_campaign(raw):
    fields = {"request_id", "material_ids", "platforms", "window_start", "window_end", "preferred_slot", "timezone", "platform_order", "min_gap_hours"}
    if not isinstance(raw, dict) or set(raw) - fields:
        raise InputError("活动只接受已声明字段，不接受批准或审核结果")
    rid = raw.get("request_id")
    if not isinstance(rid, str) or not rid.strip() or len(rid) > 120:
        raise InputError("request_id 必须为 1 至 120 字符")
    materials, platforms = raw.get("material_ids"), raw.get("platforms")
    for values, maximum in [(materials, 4), (platforms, 2)]:
        if not isinstance(values, list) or not 1 <= len(values) <= maximum or any(not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", s) for s in values) or len(set(values)) != len(values):
            raise InputError("素材最多 4 项、平台最多 2 项，ID 不重复且仅用字母数字下划线横线")
    if any(p not in {"douyin", "xiaohongshu"} for p in platforms):
        raise InputError("当前支持 douyin 和 xiaohongshu")
    start, end = canonical_slot(raw.get("window_start")), canonical_slot(raw.get("window_end"))
    if end < start or (datetime.fromisoformat(end).date() - datetime.fromisoformat(start).date()).days >= 7:
        raise InputError("时间窗口必须递增，且最多覆盖 7 个自然日")
    if raw.get("timezone", "Asia/Shanghai") != "Asia/Shanghai":
        raise InputError("当前跨天版本仅支持 Asia/Shanghai，不支持其他时区或夏令时")
    preferred = raw.get("preferred_slot")
    if preferred is not None:
        preferred = canonical_slot(preferred)
        if not start <= preferred <= end:
            raise InputError("偏好时间必须位于硬性时间窗口内")
    order = raw.get("platform_order", [])
    if not isinstance(order, list) or any(not isinstance(p, str) for p in order) or (order and (len(order) != len(platforms) or set(order) != set(platforms))):
        raise InputError("平台顺序须为空或恰好包含全部目标平台")
    gap = raw.get("min_gap_hours", 0)
    if type(gap) is not int or not 0 <= gap <= 48 or (gap and not order):
        raise InputError("平台间隔为 0 至 48 小时，非零时必须指定平台顺序")
    return {"request_id": rid, "material_ids": materials, "platforms": platforms,
            "window_start": start, "window_end": end, "preferred_slot": preferred,
            "timezone": "Asia/Shanghai", "platform_order": order, "min_gap_hours": gap}


class CampaignCatalog:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.reload()

    def reload(self):
        self.base = Catalog(self.directory)
        self.policy = json.loads((self.directory / "campaign-policy.json").read_text(encoding="utf-8"))
        try:
            if self.policy["timezone"] != "Asia/Shanghai" or self.policy["max_window_days"] != 7:
                raise ValueError()
            for name in ("douyin", "xiaohongshu"):
                p = self.policy["platforms"][name]
                if not isinstance(p["hours"], list) or not p["hours"] or any(type(h) is not int or not 0 <= h <= 23 for h in p["hours"]):
                    raise ValueError()
                if type(p["daily_limit"]) is not int or not 1 <= p["daily_limit"] <= 24:
                    raise ValueError()
                if type(p["min_gap_hours"]) is not int or not 0 <= p["min_gap_hours"] <= 48:
                    raise ValueError()
                for key in ("blocked_terms", "manual_review_terms"):
                    if not isinstance(p[key], list) or any(not isinstance(s, str) for s in p[key]):
                        raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise InputError("campaign-policy.json 格式无效") from None
        self.version = digest({"materials": self.base.materials, "supplements": self.base.supplements,
                               "rules": self.base.rules, "policy": self.policy, "occupied": self.base.existing})

    def material(self, material_id, platform, supplement=False):
        value = deepcopy(self.base.materials.get(material_id))
        if value is not None:
            if supplement:
                value.update(self.base.supplements.get(material_id, {}))
            value["platform"] = platform
        return value

    def audit(self, material, platform):
        rules = deepcopy(self.base.rules)
        rules["version"] += "/" + self.policy["version"] + "/" + platform
        for key in ("blocked_terms", "manual_review_terms"):
            rules[key] += self.policy["platforms"][platform][key]
        return review(material, rules)

    def slots(self, request, platform):
        start, end = map(datetime.fromisoformat, (request["window_start"], request["window_end"]))
        current, slots = start.replace(hour=0), []
        while current.date() <= end.date():
            for hour in sorted(set(self.policy["platforms"][platform]["hours"])):
                candidate = current.replace(hour=hour)
                if start <= candidate <= end:
                    slots.append(candidate.isoformat(timespec="seconds"))
            current += timedelta(days=1)
        return slots

    def conflict(self, request, task, slot, tasks, reservations):
        if slot not in self.slots(request, task["platform"]):
            return "OUTSIDE_WINDOW_OR_PLATFORM_HOURS"
        order = request["platform_order"]
        if order and order.index(task["platform"]) > 0:
            previous = tasks[task["material_id"] + "@" + order[order.index(task["platform"]) - 1]]
            if not previous.get("slot"):
                return "DEPENDENCY_NOT_PLANNED"
            if datetime.fromisoformat(slot) < datetime.fromisoformat(previous["slot"]) + timedelta(hours=request["min_gap_hours"]):
                return "PLATFORM_ORDER_OR_GAP"
        # All other proposed tasks count towards capacity before reservation.
        occupied = {(r["platform"], r["slot"]) for r in reservations}
        occupied.update((t["platform"], t["slot"]) for t in tasks.values() if t["task_id"] != task["task_id"] and t.get("slot"))
        platform = task["platform"]
        if (platform, slot) in occupied:
            return "SLOT_CONFLICT"
        policy = self.policy["platforms"][platform]
        if sum(p == platform and s[:10] == slot[:10] for p, s in occupied) >= policy["daily_limit"]:
            return "DAILY_LIMIT"
        if any(p == platform and abs((datetime.fromisoformat(s) - datetime.fromisoformat(slot)).total_seconds()) < policy["min_gap_hours"] * 3600 for p, s in occupied):
            return "MIN_INTERVAL"
        return None


def initial_state(request, source_version):
    tasks = {}
    for material in request["material_ids"]:
        for platform in request["platform_order"] or request["platforms"]:
            task_id = material + "@" + platform
            tasks[task_id] = {"task_id": task_id, "material_id": material, "platform": platform,
                              "status": "unreviewed", "material": None, "supplement": False,
                              "review": None, "slot": None}
    return {"tasks": tasks, "source_version": source_version, "plan_version": 1,
            "plan_hash": None, "plan_created_at": None, "status": "planning", "confirmed_hash": None}


def plan_hash(request, state):
    return digest({"request": request, "source_version": state["source_version"],
                   "plan_version": state["plan_version"],
                   "tasks": [{k: t[k] for k in ("task_id", "slot", "review")} for t in sorted(state["tasks"].values(), key=lambda t: t["task_id"])]})

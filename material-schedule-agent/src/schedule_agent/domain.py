"""Input contracts and deterministic, inspectable business rules."""
import hashlib
import json
from datetime import datetime, timedelta


class InputError(ValueError):
    pass


def canonical_slot(value):
    if not isinstance(value, str):
        raise InputError("时段必须是带 +08:00 时区的 ISO 8601 字符串")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise InputError("时段格式无效") from None
    if dt.utcoffset() != timedelta(hours=8) or dt.minute or dt.second or dt.microsecond:
        raise InputError("本原型仅支持 Asia/Shanghai 整点时段，例如 2026-11-03T18:00:00+08:00")
    return dt.isoformat(timespec="seconds")


def validate_request(raw):
    if not isinstance(raw, dict) or set(raw) - {"request_id", "items"}:
        raise InputError("请求只接受 request_id 和 items")
    request_id = raw.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 120:
        raise InputError("request_id 必须为非空字符串，最长 120 字符")
    items = raw.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise InputError("items 必须包含 1 至 20 项素材")
    normalized, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) - {"material_id", "requested_slot", "approved_slot"}:
            raise InputError("素材项只接受 material_id、requested_slot、approved_slot")
        material_id = item.get("material_id")
        if not isinstance(material_id, str) or not material_id.strip() or len(material_id) > 120:
            raise InputError("material_id 必须为非空字符串，最长 120 字符")
        if material_id in seen:
            raise InputError("同一请求中 material_id 不得重复；重复执行请复用 request_id")
        seen.add(material_id)
        requested = item.get("requested_slot")
        approved = item.get("approved_slot")
        if requested is not None:
            requested = canonical_slot(requested)
        if approved is not None:
            approved = canonical_slot(approved)
            if requested is None:
                raise InputError("approved_slot 不能替代缺失的 requested_slot")
        normalized.append({"material_id": material_id, "requested_slot": requested, "approved_slot": approved})
    return {"request_id": request_id, "items": normalized}


def fingerprint(request_id, item):
    # Confirmation is an explicit update to the same draft, not a new request.
    payload = [request_id, item["material_id"], item["requested_slot"]]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def review(material, rules):
    missing = [key for key in rules["required_fields"] if not material.get(key)]
    risks = []
    content = material.get("title", "") + " " + material.get("description", "")
    for term in rules["blocked_terms"]:
        if term in content:
            risks.append({"code": "BLOCKED_CLAIM", "detail": "命中禁止声明：" + term, "severity": "block"})
    if material.get("rights") and material["rights"] not in rules["allowed_rights"]:
        risks.append({"code": "RIGHTS_UNVERIFIED", "detail": "素材使用权未获许可", "severity": "block"})
    for term in rules["manual_review_terms"]:
        if term in content:
            risks.append({"code": "MANUAL_REVIEW", "detail": "需要人工审核：" + term, "severity": "review"})
    status = "passed"
    if missing:
        status = "missing_info"
    if any(r["severity"] == "review" for r in risks):
        status = "manual_review"
    if any(r["severity"] == "block" for r in risks):
        status = "blocked"
    return {"status": status, "missing_fields": missing, "risks": risks,
            "rules_version": rules["version"], "material_version": material.get("version")}

"""Bounded model -> tool -> observation -> model loop, with evidence-derived output."""
import json
from datetime import datetime, timezone
from .domain import validate_request
from .providers import ModelError
from .storage import error
from .tools import ToolContext


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def summarize(result):
    if not result["ok"]:
        return result["error"]["code"] + ": " + result["error"]["message"]
    if "review" in result:
        return "审核=" + result["review"]["status"]
    if "available_slots" in result:
        return "可用时段=" + ", ".join(result["available_slots"])
    if "draft" in result:
        return "草稿=" + str(result["draft"]["draft_id"]) + "; " + result["draft"]["confirmation_status"]
    if "material" in result:
        return "已读取素材=" + result["material"]["id"] + "; 来源=" + result["source"]
    return "状态=" + result.get("status", "ok")


def run(request, catalog, store, provider, max_steps=24, faults=None):
    request = validate_request(request)
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or not 1 <= max_steps <= 200:
        raise ValueError("max_steps 必须为 1 至 200")
    events, reports, usage = [], [], []
    calls = 0
    started = utc_now()
    initial_faults = dict(faults or {})
    faults = dict(initial_faults)
    for item in request["items"]:
        context = ToolContext(request["request_id"], item, catalog, store, faults)
        history = [{"role": "user", "content": json.dumps({"request_id": request["request_id"], "item": item,
                    "remaining_steps": max_steps - calls}, ensure_ascii=False)}]
        previous = None
        while calls < max_steps and context.done is None:
            calls += 1
            event = {"step": calls, "material_id": item["material_id"], "decision_source": provider.mode,
                     "tool": None, "parameters": {}, "decision_basis": None, "observation": None,
                     "return_summary": None, "next_action": None}
            try:
                turn = provider.next(history)
                output = turn.get("output", [])
                if not isinstance(output, list) or any(not isinstance(o, dict) for o in output):
                    raise ModelError("模型 output 格式无效")
                function_calls = [o for o in output if o.get("type") == "function_call"]
                if len(function_calls) != 1:
                    raise ModelError("每轮必须恰好一个工具调用；纯文本或并行调用不执行")
                call = function_calls[0]
                if not isinstance(call.get("call_id"), str) or not call["call_id"] or not isinstance(call.get("name"), str):
                    raise ModelError("模型工具调用缺少有效 name/call_id")
                if any(h.get("call_id") == call["call_id"] for h in history):
                    raise ModelError("模型重复使用 call_id")
                try:
                    args = json.loads(call.get("arguments", ""))
                except (ValueError, TypeError):
                    args = None
                event["tool"] = call["name"]
                event["parameters"] = {k: v for k, v in args.items() if k != "reason"} if isinstance(args, dict) else {}
                event["decision_basis"] = args.get("reason") if isinstance(args, dict) else None
                result = context.execute(call["name"], args)
                # Keep provider output (including encrypted reasoning continuity) in memory only.
                # Trace contains only tool calls, public brief reasons and observations.
                history.extend(output)
                history.append({"type": "function_call_output", "call_id": call["call_id"],
                                "output": json.dumps(result, ensure_ascii=False)})
                usage.append(turn.get("usage", {}))
            except ModelError as exc:
                result = error("MODEL_ERROR", str(exc))
                context.errors.append({"tool": "model", **result["error"]})
                context.done, context.stop_reason = "failed", "model_error"
            event["observation"] = result
            event["return_summary"] = summarize(result)
            if previous is not None:
                previous["next_action"] = event["tool"] or "stop_model_error"
            events.append(event)
            previous = event
        if context.done is None:
            context.done, context.stop_reason = "incomplete", "step_limit"
        if previous is not None:
            previous["next_action"] = "stop:" + context.done
        reports.append(context.report())
    statuses = {r["task_status"] for r in reports}
    overall = next(iter(statuses)) if len(statuses) == 1 else "partial"
    return {"schema_version": "1.0", "request_id": request["request_id"], "task_status": overall,
            "provider": {"mode": provider.mode, "model": provider.model,
                         "is_real_model": provider.mode == "live"},
            "started_at": started, "finished_at": utc_now(), "input": request,
            "limits": {"max_steps": max_steps, "steps_used": calls, "scope": "entire_request"},
            "fault_injection": initial_faults, "items": reports, "trace": events, "usage": usage,
            "validation_boundary": "离线模拟器只验证流程和工程约束，不证明真实模型决策效果。" if provider.mode == "mock"
            else "真实模型调用记录；需结合用例断言判断效果。", "published": False}

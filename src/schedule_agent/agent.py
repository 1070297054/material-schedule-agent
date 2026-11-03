"""Bounded model -> tool -> observation -> model loop, with evidence-derived output."""
import json
import time
from datetime import datetime, timezone
from .domain import validate_request
from .providers import ModelError
from .storage import error
from .team import ROLE_TOOLS, scoped_provider, transfer
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


def run(request, catalog, store, provider, max_steps=24, faults=None, orchestration="single",
        max_total_tokens=None, max_seconds=None, on_event=None):
    request = validate_request(request)
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or not 1 <= max_steps <= 200:
        raise ValueError("max_steps 必须为 1 至 200")
    if orchestration not in {"single", "team"}:
        raise ValueError("orchestration 必须为 single 或 team")
    for value in (max_total_tokens, max_seconds):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
            raise ValueError("资源预算必须为正数")
    agents = {r: scoped_provider(provider, r) for r in ROLE_TOOLS} if orchestration == "team" else {"generalist": provider}
    clock_start, tokens_used = time.monotonic(), 0
    events, reports, usage = [], [], []
    calls = 0
    started = utc_now()
    initial_faults = dict(faults or {})
    faults = dict(initial_faults)
    for item in request["items"]:
        role = "reviewer" if orchestration == "team" else "generalist"
        context = ToolContext(request["request_id"], item, catalog, store, faults)
        history = [{"role": "user", "content": json.dumps({"request_id": request["request_id"], "item": item,
                    "remaining_steps": max_steps - calls}, ensure_ascii=False)}]
        previous = None
        while calls < max_steps and context.done is None:
            if max_total_tokens is not None and tokens_used >= max_total_tokens:
                context.done, context.stop_reason = "incomplete", "token_budget"
                break
            if max_seconds is not None and time.monotonic() - clock_start >= max_seconds:
                context.done, context.stop_reason = "incomplete", "time_budget"
                break
            calls += 1
            step_start = time.monotonic()
            target_role, handoff = role, None
            event = {"agent_id": role, "step": calls, "material_id": item["material_id"], "decision_source": provider.mode,
                     "tool": None, "parameters": {}, "decision_basis": None, "observation": None,
                     "return_summary": None, "next_action": None}
            try:
                turn = agents[role].next(history)
                event["model_ms"] = round((time.monotonic() - step_start) * 1000, 2)
                event["response_id"] = turn.get("response_id")
                usage.append(turn.get("usage", {}))
                measured = turn.get("usage", {}).get("total_tokens")
                if measured is None:
                    measured = sum(turn.get("usage", {}).get(k, 0) for k in ("input_tokens", "output_tokens"))
                tokens_used += max(0, measured) if isinstance(measured, (int, float)) else 0
                output = turn.get("output", [])
                if not isinstance(output, list) or any(not isinstance(o, dict) for o in output):
                    raise ModelError("模型 output 格式无效")
                function_calls = [o for o in output if o.get("type") == "function_call"]
                if not function_calls:
                    raise ModelError("每轮必须通过工具决策；无工具的纯文本不执行")
                seen_ids = {h.get("call_id") for h in history if h.get("call_id")}
                for call in function_calls:
                    if not isinstance(call.get("call_id"), str) or not call["call_id"] or not isinstance(call.get("name"), str):
                        raise ModelError("模型工具调用缺少有效 name/call_id")
                    if call["call_id"] in seen_ids:
                        raise ModelError("模型重复使用 call_id")
                    seen_ids.add(call["call_id"])
                if len(function_calls) > 1:
                    # Do not execute even the first proposed write. Return a matched
                    # error for every call ID, so the next decision is the model's.
                    result = error("BATCH_REJECTED", "本轮所有工具均未执行。请只选一个工具，观察结果后再决定下一步。", True)
                    context.errors.append({"tool": "model", **result["error"]})
                    event["tool"] = "retry_tool_selection"
                    event["parameters"] = {"proposed_tools": [c["name"] for c in function_calls]}
                    event["decision_basis"] = "运行器拒绝并行提议，要求模型重新选择；不是替模型决定业务动作。"
                    event["execution"] = "rejected_batch"
                else:
                    call = function_calls[0]
                    try:
                        args = json.loads(call.get("arguments", ""))
                    except (ValueError, TypeError):
                        args = None
                    event["tool"] = call["name"]
                    event["parameters"] = {k: v for k, v in args.items() if k != "reason"} if isinstance(args, dict) else {}
                    event["decision_basis"] = args.get("reason") if isinstance(args, dict) else None
                    tool_start = time.monotonic()
                    if orchestration == "team" and call["name"] not in ROLE_TOOLS[role]:
                        result = error("ROLE_FORBIDDEN", "当前 Agent 无此工具权限")
                    elif orchestration == "team" and call["name"] in {"handoff_schedule", "request_review"}:
                        result, target_role = transfer(context, role, call["name"], args)
                        if result["ok"]:
                            handoff = result["handoff"]
                    else:
                        result = context.execute(call["name"], args)
                    event["tool_ms"] = round((time.monotonic() - tool_start) * 1000, 2)
                    if not result["ok"] and (call["name"] in {"handoff_schedule", "request_review"} or result["error"]["code"] == "ROLE_FORBIDDEN"):
                        context.errors.append({"tool": call["name"], **result["error"]})
                # Keep provider output (including encrypted reasoning continuity) in memory only.
                # Trace contains only tool calls, public brief reasons and observations.
                history.extend(output)
                for call in function_calls:
                    history.append({"type": "function_call_output", "call_id": call["call_id"],
                                    "output": json.dumps(result, ensure_ascii=False)})
            except ModelError as exc:
                result = error("MODEL_ERROR", str(exc))
                context.errors.append({"tool": "model", **result["error"]})
                context.done, context.stop_reason = "failed", "model_error"
            event["elapsed_ms"] = round((time.monotonic() - step_start) * 1000, 2)
            event["observation"] = result
            event["return_summary"] = summarize(result)
            if previous is not None:
                previous["next_action"] = event["tool"] or "stop_model_error"
                if on_event:
                    on_event(dict(previous))
            events.append(event)
            previous = event
            if handoff is not None:
                role = target_role
                history = [{"role": "user", "content": json.dumps({"request_id": request["request_id"],
                    "item": item, "remaining_steps": max_steps - calls, "handoff": handoff}, ensure_ascii=False)}]
        if context.done is None:
            context.done, context.stop_reason = "incomplete", "step_limit"
        if previous is not None:
            previous["next_action"] = "stop:" + context.done
            if on_event:
                on_event(dict(previous))
        reports.append(context.report())
    statuses = {r["task_status"] for r in reports}
    overall = next(iter(statuses)) if len(statuses) == 1 else "partial"
    return {"schema_version": "1.1", "orchestration": orchestration, "request_id": request["request_id"], "task_status": overall,
            "provider": {"mode": provider.mode, "model": provider.model,
                         "service": getattr(provider, "service", "test_fixture"),
                         "is_real_model": provider.mode == "live"},
            "started_at": started, "finished_at": utc_now(), "input": request,
            "limits": {"max_steps": max_steps, "steps_used": calls, "scope": "entire_request", "max_total_tokens": max_total_tokens, "tokens_used": tokens_used,
                       "max_seconds": max_seconds, "elapsed_seconds": round(time.monotonic() - clock_start, 3)},
            "fault_injection": initial_faults, "items": reports, "trace": events, "usage": usage,
            "validation_boundary": "离线模拟器只验证流程和工程约束，不证明真实模型决策效果。" if provider.mode == "mock"
            else "真实模型调用记录；需结合用例断言判断效果。", "published": False}

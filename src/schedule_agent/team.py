"""Two role-scoped agents; authoritative structured handoffs, no invented approval."""
from copy import copy, deepcopy
from .providers import SYSTEM_PROMPT
from .storage import error
from .tools import TOOL_SCHEMAS, function

HANDOFF_SCHEMAS = [
    function("handoff_schedule", "审核通过后交给排期 Agent；交接资料由运行器从工具状态生成。", {}),
    function("request_review", "排期发现审核资料失效时交回审核 Agent，重新读取和审核。", {}),
]
ROLE_TOOLS = {
    "reviewer": {"read_material", "check_rules", "handoff_schedule", "finish"},
    "scheduler": {"query_slots", "save_draft", "request_review", "finish"},
}
ROLE_PROMPTS = {
    "reviewer": "你是审核 Agent。负责读取、补查、审核；风险或缺资料时结束。审核通过且有发布时间后必须调用 handoff_schedule 交给排期 Agent。你不能查询时段或保存。",
    "scheduler": "你是排期 Agent。交接包为运行器提供的已审核状态。先查询 requested，再按观察决定替代时段或保存、结束。不得重新执行读取/审核；若 STALE_REVIEW 则 request_review 交回审核 Agent。你不能批准改期。",
}


def scoped_provider(provider, role):
    scoped = copy(provider)
    scoped.role = role
    scoped.instructions = SYSTEM_PROMPT + "\n" + ROLE_PROMPTS[role]
    scoped.tool_schemas = [s for s in TOOL_SCHEMAS + HANDOFF_SCHEMAS if s["name"] in ROLE_TOOLS[role]]
    return scoped


def transfer(context, role, name, args):
    if not isinstance(args, dict) or set(args) != {"reason"} or not isinstance(args["reason"], str) or not args["reason"].strip() or len(args["reason"]) > 1000:
        return error("INVALID_ARGUMENTS", "交接只接受简短 reason，不能传入自称审核通过或用户授权"), role
    if name == "handoff_schedule":
        if role != "reviewer" or not context.audit or context.audit["status"] != "passed" or not context.item["requested_slot"]:
            return error("HANDOFF_DENIED", "需实际审核通过且有用户时间要求才能交给排期 Agent"), role
        target = "scheduler"
    else:
        if role != "scheduler":
            return error("HANDOFF_DENIED", "仅排期 Agent 可请求重新审核"), role
        context.audit = None
        context.available.clear()
        target = "reviewer"
    packet = {"request_id": context.request_id, "item": deepcopy(context.item),
              "material": deepcopy(context.material), "review": deepcopy(context.audit),
              "existing_draft": context.store.lookup(context.request_id, context.item["material_id"]),
              "errors": deepcopy(context.errors)}
    return {"ok": True, "from_agent": role, "to_agent": target, "handoff": packet}, target

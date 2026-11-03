#!/usr/bin/env python3
"""Generate actual outputs, not handwritten traces. Live and Mock use identical checks."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from schedule_agent.agent import run, utc_now
from schedule_agent.catalog import Catalog
from schedule_agent.cli import write_json
from schedule_agent.config import load_env_file
from schedule_agent.providers import create_provider, ModelError
from schedule_agent.storage import DraftStore

CASE_NAMES = {"normal", "missing_info", "supplement", "risk", "manual_review", "conflict",
              "changed_occupancy", "no_slots", "save_failure", "read_recovery", "duplicate_first",
              "duplicate_repeat", "confirmation_before", "confirmation_after", "confirmation_replay",
              "missing_time", "step_limit", "batch", "occupancy_repeat_before", "occupancy_repeat_after", "occupancy_repeat_again"}


def behavior_checks(name, result):
    """Check observations followed by model decisions, not just final status labels."""
    trace = result["trace"]

    def steps(tool, predicate=lambda e: True):
        return [e["step"] for e in trace if e["tool"] == tool and e["observation"]["ok"] and predicate(e)]

    def query(mode, empty):
        return steps("query_slots", lambda e: e["parameters"]["mode"] == mode and
                     (not bool(e["observation"]["available_slots"])) == empty)

    def ordered(*groups):
        previous = -1
        for group in groups:
            later = [n for n in group if n > previous]
            if not later:
                return False
            previous = min(later)
        return True

    finish = steps("finish")
    checks = {"nonempty_trace": bool(trace)}
    if result["provider"]["is_real_model"]:
        checks["real_responses_identified"] = bool(trace) and all(e.get("response_id") for e in trace)
    if result.get("orchestration") == "team" and any(e.get("agent_id") == "scheduler" for e in trace):
        checks["verified_handoff_before_scheduling"] = ordered(
            steps("handoff_schedule", lambda e: e["observation"]["handoff"]["review"]["status"] == "passed"),
            [e["step"] for e in trace if e.get("agent_id") == "scheduler"])
    if name in {"normal", "duplicate_first", "duplicate_repeat", "confirmation_after", "confirmation_replay"}:
        checks["save_observed_before_finish"] = ordered(steps("save_draft"), finish)
    if name in {"conflict", "changed_occupancy", "confirmation_before", "occupancy_repeat_before", "occupancy_repeat_after", "occupancy_repeat_again"}:
        checks["conflict_observed_then_replanned"] = ordered(query("requested", True), query("alternatives", False), steps("save_draft"), finish)
        checks["proposal_saved_not_reserved"] = bool(result["items"][0]["draft"]) and result["items"][0]["draft"]["reserved_slot"] is None
    if name == "no_slots":
        checks["no_slots_observed_then_stopped"] = ordered(query("requested", True), query("alternatives", True), finish)
        checks["not_budget_exhaustion"] = result["items"][0]["stop_reason"] == "model_finish"
    if name in {"missing_info", "supplement"}:
        # Missing fields may be observed directly in read_material before a formal check.
        missing_observed = steps("check_rules", lambda e: e["observation"]["review"]["status"] == "missing_info")
        missing_observed += steps("read_material", lambda e: e["parameters"]["source"] == "base" and
                                  any(not e["observation"]["material"].get(k) for k in ("title", "platform", "rights")))
        checks["supplement_observed_then_reviewed"] = ordered(
            sorted(missing_observed),
            steps("read_material", lambda e: e["parameters"]["source"] == "supplement"),
            steps("check_rules"), finish)
    if name in {"risk", "manual_review"}:
        checks["risk_observed_then_blocked"] = ordered(
            steps("check_rules", lambda e: e["observation"]["review"]["status"] in {"blocked", "manual_review"}), finish)
    if name in {"save_failure", "read_recovery"}:
        tool = "save_draft" if name == "save_failure" else "read_material"
        failures = [e["step"] for e in trace if e["tool"] == tool and not e["observation"]["ok"] and
                    e["observation"]["error"]["code"] == "INJECTED_FAILURE"]
        checks["injected_failure_observed_then_handled"] = ordered(failures, finish) if name == "save_failure" else ordered(failures, steps(tool), finish)
    return checks


def evaluate(output, provider_name="mock", model=None, selected_cases=None, progress=False, orchestration="single"):
    selected = set(selected_cases or CASE_NAMES)
    if selected - CASE_NAMES:
        raise ValueError("未知评估用例")
    dependencies = {"occupancy_repeat_after": {"occupancy_repeat_before"},
                    "occupancy_repeat_again": {"occupancy_repeat_before", "occupancy_repeat_after"},"duplicate_repeat": {"duplicate_first"}, "confirmation_after": {"confirmation_before"},
                    "confirmation_replay": {"confirmation_before", "confirmation_after"}}
    for name in list(selected):
        selected.update(dependencies.get(name, set()))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    provider = create_provider(provider_name, model)
    cases = []
    with tempfile.TemporaryDirectory(prefix="schedule-eval-") as temp:
        def case(name, input_name, expected, database=None, faults=None, max_steps=24, occupied=None, checks=None):
            if name not in selected:
                return
            request = json.loads((ROOT / "examples/inputs" / (input_name + ".json")).read_text(encoding="utf-8"))
            catalog = Catalog(ROOT / "data")
            if occupied is not None:
                catalog.existing = [{"platform": "douyin", "slot": s} for s in occupied]
            store = DraftStore(Path(temp) / ((database or name) + ".sqlite3"))
            try:
                result = run(request, catalog, store, provider, max_steps, faults, orchestration=orchestration)
                rows = store.all()
            finally:
                store.close()
            observations = {"expected_status": result["task_status"] == expected,
                            "never_published": not result["published"] and all(not i["published"] for i in result["items"]),
                            "bounded": result["limits"]["steps_used"] <= max_steps,
                            "trace_records_actual_calls": all(e["observation"] is not None and e["next_action"] for e in result["trace"])}
            observations.update(behavior_checks(name, result))
            if checks:
                observations.update(checks(result, rows))
            # Store exact mutated fixture and durable rows, so reviewers can reproduce the case.
            result["evaluation_fixture"] = {"case": name, "input_file": "examples/inputs/" + input_name + ".json",
                                            "occupied": catalog.existing, "slots": catalog.slots,
                                            "expected_task_status": expected, "assertions": observations,
                                            "database_group": database or name, "database_rows_after": rows}
            write_json(output / (name + ".json"), result)
            cases.append({"case": name, "file": name + ".json", "expected": expected, "actual": result["task_status"],
                          "passed": all(observations.values()), "assertions": observations})
            if progress:
                print(name + ": " + result["task_status"] + (" PASS" if cases[-1]["passed"] else " FAIL"), flush=True)

        s18, s19, s20 = Catalog(ROOT / "data").slots
        case("normal", "normal", "completed", checks=lambda r, rows: {"one_reserved_draft": len(rows) == 1 and rows[0]["reserved_slot"] == s19})
        case("missing_info", "missing_info", "needs_input", checks=lambda r, rows: {"no_draft": not rows, "missing_rights": "rights" in r["items"][0]["pending_items"]})
        case("supplement", "supplement", "completed")
        case("risk", "risk", "blocked", checks=lambda r, rows: {"no_draft": not rows, "risk_reported": bool(r["items"][0]["review"]["risks"])})
        case("manual_review", "manual_review", "blocked")
        case("conflict", "conflict", "pending_confirmation", checks=lambda r, rows: {
            "requirement_preserved": r["items"][0]["requested_slot"] == s18,
            "proposal_not_reserved": len(rows) == 1 and rows[0]["reserved_slot"] is None,
            "suggests_19": r["items"][0]["suggested_slot"] == s19})
        case("changed_occupancy", "conflict", "pending_confirmation", occupied=[s18, s19],
             checks=lambda r, rows: {"suggests_20": r["items"][0]["suggested_slot"] == s20})
        case("occupancy_repeat_before", "conflict", "pending_confirmation", database="occupancy_repeat",
             checks=lambda r, rows: {"one_row_19": len(rows) == 1 and rows[0]["suggested_slot"] == s19})
        case("occupancy_repeat_after", "conflict", "pending_confirmation", database="occupancy_repeat", occupied=[s18, s19],
             checks=lambda r, rows: {"same_row_20": len(rows) == 1 and rows[0]["draft_id"] == 1 and rows[0]["suggested_slot"] == s20,
                                   "no_approval": rows[0]["reserved_slot"] is None, "original_preserved": rows[0]["requested_slot"] == s18})
        case("occupancy_repeat_again", "conflict", "pending_confirmation", database="occupancy_repeat", occupied=[s18, s19],
             checks=lambda r, rows: {"same_row_duplicate": len(rows) == 1 and rows[0]["draft_id"] == 1 and r["items"][0]["duplicate"],
                                   "still_pending": rows[0]["reserved_slot"] is None and rows[0]["suggested_slot"] == s20})
        case("no_slots", "conflict", "incomplete", occupied=[s18, s19, s20], checks=lambda r, rows: {"no_draft": not rows})
        case("save_failure", "normal", "failed", faults={"save_draft": 1}, checks=lambda r, rows: {
            "no_draft": not rows, "save_failed": r["items"][0]["save_status"] == "failed"})
        case("read_recovery", "normal", "completed", faults={"read_material": 1})
        case("duplicate_first", "normal", "completed", database="duplicate")
        case("duplicate_repeat", "normal", "completed", database="duplicate", checks=lambda r, rows: {
            "one_row": len(rows) == 1, "duplicate_reported": r["items"][0]["duplicate"]})
        case("confirmation_before", "conflict", "pending_confirmation", database="confirmation")
        case("confirmation_after", "confirmed", "completed", database="confirmation", checks=lambda r, rows: {
            "same_row_updated": len(rows) == 1 and rows[0]["draft_id"] == 1 and rows[0]["reserved_slot"] == s19})
        case("confirmation_replay", "conflict", "completed", database="confirmation", checks=lambda r, rows: {
            "consent_not_revoked": len(rows) == 1 and rows[0]["reserved_slot"] == s19})
        case("missing_time", "missing_time", "needs_input")
        case("step_limit", "normal", "incomplete", max_steps=2, checks=lambda r, rows: {
            "explicit_limit": r["items"][0]["stop_reason"] == "step_limit", "no_draft": not rows})
        case("batch", "batch", "partial", checks=lambda r, rows: {
            "per_item_status": [i["task_status"] for i in r["items"]] == ["completed", "pending_confirmation", "blocked"]})
    summary = {"generated_at": utc_now(), "provider": provider.mode, "model": provider.model,
               "service": provider.service, "orchestration": orchestration,
               "is_real_model": provider.mode == "live", "cases_total": len(cases),
               "cases_passed": sum(c["passed"] for c in cases), "cases": cases,
               "boundary": "Mock only: validates engineering, not real model decisions." if provider.mode == "mock" else "Live model evaluation; inspect every assertion and trace."}
    write_json(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["mock", "openai", "deepseek"], default="mock")
    parser.add_argument("--model")
    parser.add_argument("--orchestration", choices=["single", "team"], default="single")
    parser.add_argument("--env-file")
    parser.add_argument("--cases", nargs="+", choices=sorted(CASE_NAMES), help="只运行指定用例；自动包含重复/确认用例的前置执行")
    parser.add_argument("--output", help="默认 examples/mock 或 examples/live，建议使用新目录保留历史")
    args = parser.parse_args()
    output = args.output or str(ROOT / "examples" / ("mock" if args.provider == "mock" else "live"))
    try:
        load_env_file(args.env_file)
        summary = evaluate(output, args.provider, args.model, args.cases, progress=True, orchestration=args.orchestration)
    except (ModelError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in summary.items() if k != "cases"}, ensure_ascii=False, indent=2))
    return 0 if summary["cases_passed"] == summary["cases_total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

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
from schedule_agent.providers import MockProvider, OpenAIProvider, ModelError
from schedule_agent.storage import DraftStore


def evaluate(output, provider_name="mock", model=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    provider = MockProvider() if provider_name == "mock" else OpenAIProvider(model=model)
    cases = []
    with tempfile.TemporaryDirectory(prefix="schedule-eval-") as temp:
        def case(name, input_name, expected, database=None, faults=None, max_steps=24, occupied=None, checks=None):
            request = json.loads((ROOT / "examples/inputs" / (input_name + ".json")).read_text(encoding="utf-8"))
            catalog = Catalog(ROOT / "data")
            if occupied is not None:
                catalog.existing = [{"platform": "douyin", "slot": s} for s in occupied]
            store = DraftStore(Path(temp) / ((database or name) + ".sqlite3"))
            try:
                result = run(request, catalog, store, provider, max_steps, faults)
                rows = store.all()
            finally:
                store.close()
            observations = {"expected_status": result["task_status"] == expected,
                            "never_published": not result["published"] and all(not i["published"] for i in result["items"]),
                            "bounded": result["limits"]["steps_used"] <= max_steps,
                            "trace_records_actual_calls": all(e["observation"] is not None and e["next_action"] for e in result["trace"])}
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
               "is_real_model": provider.mode == "live", "cases_total": len(cases),
               "cases_passed": sum(c["passed"] for c in cases), "cases": cases,
               "boundary": "Mock only: validates engineering, not real model decisions." if provider.mode == "mock" else "Live model evaluation; inspect every assertion and trace."}
    write_json(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["mock", "openai"], default="mock")
    parser.add_argument("--model")
    parser.add_argument("--output", help="默认 examples/mock 或 examples/live，建议使用新目录保留历史")
    args = parser.parse_args()
    output = args.output or str(ROOT / "examples" / ("mock" if args.provider == "mock" else "live"))
    try:
        summary = evaluate(output, args.provider, args.model)
    except (ModelError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in summary.items() if k != "cases"}, ensure_ascii=False, indent=2))
    return 0 if summary["cases_passed"] == summary["cases_total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

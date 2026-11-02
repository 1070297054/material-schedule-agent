"""CLI exit codes: 0 completed, 2 pending/blocked/incomplete, 1 runtime/config failure."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
from .agent import run
from .catalog import Catalog
from .domain import InputError
from .providers import ModelError, MockProvider, OpenAIProvider
from .storage import DraftStore


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement prevents truncated trace files on interruption.
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def parse_faults(values):
    faults = {}
    for value in values:
        try:
            name, count = value.split(":")
            count = int(count)
        except ValueError:
            raise InputError("--fail 格式为工具名:次数，例如 save_draft:1") from None
        if name not in {"read_material", "check_rules", "query_slots", "save_draft"} or not 1 <= count <= 200:
            raise InputError("--fail 工具名或次数无效")
        faults[name] = count
    return faults


def main(argv=None):
    parser = argparse.ArgumentParser(description="素材审核与发布排期 Agent（仅草稿）")
    parser.add_argument("--input", required=True, help="请求 JSON 文件")
    parser.add_argument("--provider", choices=["mock", "openai"], default="mock")
    parser.add_argument("--model", help="真实模型名称，默认读取 OPENAI_MODEL")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--db", default=".runtime/drafts.sqlite3")
    parser.add_argument("--output", help="保存完整实际输出及 Trace")
    parser.add_argument("--max-steps", type=int, default=24, help="整个请求的模型调用上限，包含 finish 和失败调用")
    parser.add_argument("--fail", action="append", default=[], help="演示故障注入，例如 save_draft:1")
    args = parser.parse_args(argv)
    store = None
    try:
        raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
        catalog = Catalog(args.data_dir)
        provider = MockProvider() if args.provider == "mock" else OpenAIProvider(model=args.model)
        faults = parse_faults(args.fail)
        store = DraftStore(args.db)
        result = run(raw, catalog, store, provider, args.max_steps, faults)
        if args.output:
            write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if any(i["task_status"] == "failed" for i in result["items"]):
            return 1
        return 0 if result["task_status"] == "completed" else 2
    except (InputError, ModelError, OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"task_status": "failed", "error": str(exc), "published": False}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()

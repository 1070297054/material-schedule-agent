"""CLI for planning, restarting from durable state, and operator confirmation."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .campaign import run_campaign, confirm_campaign
from .campaign_domain import CampaignCatalog
from .campaign_store import CampaignStore
from .cli import write_json
from .config import load_env_file
from .providers import create_provider, ModelError


def main(argv=None):
    parser=argparse.ArgumentParser(description="跨天多平台活动：规划 / 恢复 / 明确确认，仅保存草稿")
    parser.add_argument("action",choices=["plan","resume","confirm","show"])
    parser.add_argument("--input")
    parser.add_argument("--request-id")
    parser.add_argument("--plan-version",type=int)
    parser.add_argument("--plan-hash")
    parser.add_argument("--provider",choices=["mock","deepseek","openai"],default="mock")
    parser.add_argument("--env-file")
    parser.add_argument("--data-dir",default="data")
    parser.add_argument("--db",default=".runtime/campaign.sqlite3")
    parser.add_argument("--output")
    parser.add_argument("--max-steps",type=int,default=64)
    parser.add_argument("--fail-save",action="store_true")
    args=parser.parse_args(argv)
    store=None
    try:
        load_env_file(args.env_file)
        catalog=CampaignCatalog(args.data_dir)
        store=CampaignStore(args.db)
        if args.action=="plan":
            if not args.input:
                parser.error("plan 需要 --input")
            request=json.loads(Path(args.input).read_text(encoding="utf-8"))
        elif not args.request_id:
            parser.error("此操作需要 --request-id")
        if args.action in {"plan","resume"}:
            if args.action=="resume":
                request=store.snapshot(args.request_id)["request"]
            result=run_campaign(request,catalog,store,create_provider(args.provider),args.max_steps,{"save_plan":1} if args.fail_save else None)
        elif args.action=="confirm":
            if args.plan_version is None or not args.plan_hash:
                parser.error("确认必须明确提供 --plan-version 与 --plan-hash，不能自动选择最新方案")
            result=confirm_campaign(args.request_id,args.plan_version,args.plan_hash,catalog,store)
        else:
            result=store.snapshot(args.request_id)
            result["drafts"]=store.drafts(args.request_id)
        if args.output:
            write_json(args.output,result)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 1 if result.get("task_status")=="failed" else 0 if result.get("task_status") in {None,"completed"} else 2
    except (OSError,ValueError,ModelError,sqlite3.Error) as exc:
        print(json.dumps({"task_status":"failed","error":str(exc),"published":False},ensure_ascii=False),file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()


if __name__=="__main__":
    raise SystemExit(main())

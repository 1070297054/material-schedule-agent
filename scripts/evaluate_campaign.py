#!/usr/bin/env python3
"""Business-invariant evaluation of cross-day planning and durable recovery."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from schedule_agent.agent import utc_now
from schedule_agent.campaign import run_campaign,confirm_campaign
from schedule_agent.campaign_domain import CampaignCatalog
from schedule_agent.campaign_store import CampaignStore
from schedule_agent.cli import write_json
from schedule_agent.config import load_env_file
from schedule_agent.providers import create_provider


def evaluate_campaign(output,provider_name='mock',progress=False):
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    provider=create_provider(provider_name)
    base=json.loads((ROOT/'examples/inputs/campaign.json').read_text())
    cases=[]
    previous={}
    with tempfile.TemporaryDirectory(prefix='campaign-eval-') as tmp:
        def case(name,expected,group=None,request=None,limit=64,faults=None,mutation=None,confirm_from=None):
            group=group or name
            data=Path(tmp)/group/'data'
            if not data.exists():
                shutil.copytree(ROOT/'data',data)
            if mutation:
                file,fn=mutation
                path=data/file
                source=json.loads(path.read_text())
                fn(source)
                path.write_text(json.dumps(source,ensure_ascii=False))
            req=deepcopy(request or base)
            store=CampaignStore(data.parent/'drafts.sqlite3')
            catalog=CampaignCatalog(data)
            try:
                if confirm_from:
                    old=previous[confirm_from]['campaign_state']
                    result=confirm_campaign(req['request_id'],old['plan_version'],old['plan_hash'],catalog,store)
                    result['input']={'request_id':req['request_id'],'expected_plan_version':old['plan_version'],'expected_plan_hash':old['plan_hash']}
                    result['confirmation_source']='explicit_operator_action_simulated_by_evaluation; no model call'
                else:
                    result=run_campaign(req,catalog,store,provider,max_steps=limit,faults=faults)
                rows=store.drafts()
            finally:
                store.close()
            checks={'expected_status':result['task_status']==expected,'never_published':result['published'] is False,
                    'no_duplicate_platform_draft':len({(r['request_id'],r['task_id']) for r in rows})==len(rows)}
            if not confirm_from:
                checks['within_budget']=result['limits']['steps_used']<=limit
                checks['actual_trace']=bool(result['trace']) and all(e.get('return_summary') and e.get('next_action') for e in result['trace'])
                if provider.mode=='live':
                    checks['real_response_ids']=all(e.get('response_id') for e in result['trace'])
                checks['hard_window_preserved']=all(not t['slot'] or req['window_start']<=t['slot']<=req['window_end'] for t in result['items'])
            if name=='cross_day':
                checks.update(three_roles={e['agent_id'] for e in result['trace']}=={'planner','reviewer','scheduler'},
                              cross_day_slots=[t['slot'] for t in result['items']]==['2026-11-04T18:00:00+08:00','2026-11-04T20:00:00+08:00'])
            if expected=='pending_confirmation':
                checks['all_pending_not_reserved']=len(rows)==2 and all(r['reserved_slot'] is None and r['confirmation_status']=='pending_confirmation' for r in rows)
            if name=='repeat_pending':
                checks['same_draft_ids']=[r['draft_id'] for r in rows]==[r['draft_id'] for r in previous['cross_day']['drafts']]
            if name in {'confirm_exact','replay_confirmed'}:
                checks['confirmed_exact_plan']=len(rows)==2 and all(r['confirmation_status']=='confirmed' and r['reserved_slot']==r['suggested_slot'] for r in rows)
            if name in {'missing_info','platform_risk','hard_deadline','save_failure','checkpoint_limit'}:
                checks['no_saved_drafts']=not rows
            if name=='hard_deadline':
                checks['observed_infeasible_then_stopped']=result['stop_reason']=='model_finish' and any(e['tool']=='query_candidates' and e['observation'].get('search')=='window' and not e['observation'].get('available_slots') for e in result['trace'])
            if name=='save_failure':
                checks['observed_write_failure']=any(e['tool']=='save_plan' and e['observation'].get('error',{}).get('code')=='INJECTED_FAILURE' for e in result['trace'])
            if name in {'resume_after_failure','resume_checkpoint'}:
                before=previous['save_failure' if name=='resume_after_failure' else 'checkpoint_limit']
                passed={t['task_id'] for t in before['items'] if t['review'] and t['review']['status']=='passed'}
                checks['reuse_persisted_reviews']=bool(passed) and passed.issubset(set(result['resume_info']['reused_reviews'])) and not any(e['tool']=='check_platform_rules' and e['task_id'] in passed for e in result['trace'])
            if name=='checkpoint_limit':
                checks['explicit_limit']=result['stop_reason']=='step_limit'
            if name=='stale_approval':
                checks['old_approval_rejected']=result['confirmation'].get('error',{}).get('code')=='STALE_SOURCE' and all(r['reserved_slot'] is None for r in rows)
            if name=='replan_changed_occupancy':
                checks['new_day_same_rows']=result['items'][0]['slot']=='2026-11-05T18:00:00+08:00' and [r['draft_id'] for r in rows]==[r['draft_id'] for r in previous['before_occupancy_change']['drafts']]
                checks['new_plan_identity']=result['campaign_state']['plan_hash']!=previous['before_occupancy_change']['campaign_state']['plan_hash']
            result['evaluation_fixture']={'case':name,'expected':expected,'assertions':checks,'database_group':group,'database_rows_after':rows,
                'faults':faults or {},'source_snapshot':{p.name:json.loads(p.read_text()) for p in data.glob('*.json')}}
            write_json(output/(name+'.json'),result)
            previous[name]=result
            cases.append({'case':name,'file':name+'.json','expected':expected,'actual':result['task_status'],'passed':all(checks.values()),'assertions':checks})
            if progress:
                print(name+': '+result['task_status']+(' PASS' if all(checks.values()) else ' FAIL'),flush=True)
        case('cross_day','pending_confirmation',group='normal')
        case('repeat_pending','pending_confirmation',group='normal')
        case('confirm_exact','completed',group='normal',confirm_from='cross_day')
        case('replay_confirmed','completed',group='normal')
        case('missing_info','needs_input',request={**base,'material_ids':['M03']})
        case('platform_risk','blocked',mutation=('campaign-policy.json',lambda d:d['platforms']['xiaohongshu']['blocked_terms'].append('字幕')))
        case('hard_deadline','incomplete',request={**base,'window_end':'2026-11-03T20:00:00+08:00'})
        case('save_failure','failed',group='failure',faults={'save_plan':1})
        case('resume_after_failure','pending_confirmation',group='failure')
        case('checkpoint_limit','incomplete',group='checkpoint',limit=11)
        case('resume_checkpoint','pending_confirmation',group='checkpoint')
        extended={**base,'window_end':'2026-11-05T20:00:00+08:00'}
        case('before_occupancy_change','pending_confirmation',group='changed',request=extended)
        case('stale_approval','failed',group='changed',request=extended,confirm_from='before_occupancy_change',
             mutation=('schedule.json',lambda d:d['occupied'].append({'platform':'douyin','slot':'2026-11-04T18:00:00+08:00'})))
        case('replan_changed_occupancy','pending_confirmation',group='changed',request=extended)
    summary={'generated_at':utc_now(),'service':provider.service,'model':'deterministic-campaign-simulator' if provider_name=='mock' else provider.model,'is_real_model':provider.mode=='live',
             'cases_total':len(cases),'cases_passed':sum(c['passed'] for c in cases),'cases':cases,
             'boundary':'Operator confirmation cases explicitly simulate user action; model cases use the selected provider. Mock is not model evidence.'}
    write_json(output/'summary.json',summary)
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--provider',choices=['mock','deepseek','openai'],default='mock')
    p.add_argument('--env-file')
    p.add_argument('--output',default='.runtime/campaign-evaluation')
    args=p.parse_args()
    load_env_file(args.env_file)
    summary=evaluate_campaign(args.output,args.provider,True)
    print(json.dumps({k:v for k,v in summary.items() if k!='cases'},ensure_ascii=False,indent=2))
    return 0 if summary['cases_passed']==summary['cases_total'] else 1


if __name__=='__main__':
    raise SystemExit(main())

"""Real queue/worker calls after an explicitly synthetic USER batch failure."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from studio_runtime.workflow import Workflow
from studio_runtime.service import StudioService
from studio_runtime import user_collaboration as user
from studio_runtime.contracts import atomic_json

base=ROOT/'.validation'/('user-recovery-'+uuid.uuid4().hex[:8])
service=StudioService(base,max_workers=6)
service.create('Synthetic acceptance: preserve scoped observations and resume USER after an isolated failed batch.',run_id='run',web_search='disabled')
w=Workflow(base/'run')
with w._tx() as (c,s):
    for j in s['jobs'].values():j['state']='superseded'
    for r in s['inquiries'].values():r['state']='closed'
    s['topics']['ROOT'].update(state='quiescent',dirty=False)
    s['state']='quiescent'
    user.record_event(s,'FIXTURE.OLD','observation',source='fixture',content='Synthetic earlier observation; preserve as quarantined evidence.')
    w._user_review(c,s)
    broken=next(j for j in s['jobs'].values() if j['state']=='pending' and j['spec']['role']=='user_review')
    broken.update(state='failed',failure_kind='contract',error='Synthetic injected exhausted contract failure, not a real provider failure')
    broken_id=broken['spec']['job_id']
    s['diagnostics'].append(dict(kind='contract_repair_exhausted',job_id=broken_id,error=broken['error']))
    w._user_review(c,s)
    assert s['jobs'][broken_id]['state']=='quarantined'

rounds=[]
for index,text in enumerate(['Synthetic person prefers short concrete examples when learning an unfamiliar domain.',
                              'Synthetic new observation: in familiar mathematics they prefer equations; do not generalize the earlier preference to every task.']):
    with w._tx() as (_,s):
        user.record_event(s,f'FIXTURE.NEW.{index}','observation',source='fixture',content=text,
            allowed_uses=['profile'],audience=['USER'])
    while True:
        snapshot=service.tick('run')
        assert not snapshot['error'],snapshot['error']
        active=snapshot['queue']['active']+snapshot['queue']['queued']
        pending=snapshot['workflow']['pending_semantic_work']
        if not active and not pending:
            failed=[j for j in snapshot['jobs'] if j['semantic_state']=='failed']
            assert not failed,failed
            break
        time.sleep(1)
    with w._read_tx() as (_,s):
        assert f'FIXTURE.NEW.{index}' in s['user_collaboration']['processed_event_ids']
        assert s['jobs'][broken_id]['state']=='quarantined'
        count=len(s['jobs'])
        assert not s['topics']['ROOT']['dirty']
    for _ in range(30):
        view=service.tick('run')
        assert view['workflow']['pending_semantic_work']==0
        assert len(view['workflow']['jobs'])==count
    rounds.append({'event':f'FIXTURE.NEW.{index}','stable_ticks':30,'jobs':count,'state':view['workflow']['state']})
    print('STABLE',rounds[-1],flush=True)
with w._read_tx() as (c,s):
    real=[]
    for j in s['jobs'].values():
        if j['state']!='applied' or j['spec']['role']!='user_review':continue
        r=json.loads(c.execute('SELECT body FROM results WHERE result_id=?',(j['result_id'],)).fetchone()[0])
        assert r['receipt']['model']=='gpt-6-astra' and r['receipt']['reasoning_effort']=='xhigh'
        real.append({'job':j['spec']['job_id'],'result':j['result_id'],'receipt':r['receipt']})
    assert len(real)>=2
    assert all(a['basis']=='fixture' and a['audience']==['USER'] for a in s['user_collaboration']['assets'].values())
atomic_json(base/'acceptance.json',{'rounds':rounds,'actual_user_calls':real,'broken_fixture_job':broken_id,
    'boundary':'Failure and user observations are synthetic. Recovery, queue/worker dispatch, Astra/xhigh outputs, import, scope and waiting are real. No human benefit is claimed.'})
service.close()
print('VERIFIED',base,flush=True)

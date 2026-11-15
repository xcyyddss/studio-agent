"""Actual model request -> exact historical artifact -> review -> uptake."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from studio_runtime.workflow import Workflow
from studio_runtime.service import StudioService
from studio_runtime.contracts import atomic_json

base=ROOT/'.validation'/('bound-locator-'+uuid.uuid4().hex[:8])
service=StudioService(base)
service.create('合成验收：用一次有明确清单的 bounded_review 核对 artifacts/FIXTURE.OLD/plan.txt 的实际正文：'
    '是否保留源文件只读、仅缓存17天、JSON UTF-8导出且包含原路径和哈希。然后承接核对结果，保存报告并交付。'
    '只要求这次资料核对，不需要实现软件或提出新的产品方向。原件在本次授权目录中。'
    '为验证精确正文接口，请把该文件目录条目的 /content 子指针绑定给 cognition.process；目录虽不内嵌正文，软件会加载该版本的实际正文。',
    run_id='run',web_search='disabled')
w=Workflow(base/'run')
with w._tx() as (c,s):
    for j in s['jobs'].values():j['state']='superseded'
    for r in s['inquiries'].values():r['state']='closed'
    for jid in ('FIXTURE.OLD','FIXTURE.PRIOR'):
        s['jobs'][jid]={'state':'applied','created_at':'setup','spec':{'job_id':jid,'role':'parent','metadata':{'topic_id':'ROOT'}}}
    c.execute('INSERT INTO artifacts VALUES(?,?,?)',('artifacts/FIXTURE.OLD/plan.txt',
        'Synthetic plan: source files remain read-only. Only generated report caches expire after 17 days; original files are never deleted. Export is JSON in UTF-8, retaining original source path and version hash.', 'FIXTURE.OLD'))
    s['topics']['ROOT'].update(dirty=True,state='active')
while True:
    view=service.tick('run')
    assert not view['error'],view['error']
    if not view['queue']['active'] and not view['queue']['queued'] and not view['workflow']['pending_semantic_work']:
        assert view['workflow']['state'] in {'delivered','quiescent'},(view['workflow']['state'],view['workflow']['state_reason'])
        break
    time.sleep(1)
with w._read_tx() as (c,s):
    plans=[deepcopy(p) for p in s.get('cognitive_operations',{}).values() if p['operation']=='bounded_review']
    assert plans and all(p['state']=='consumed' and p['consumption'] for p in plans)
    assert any(m['source'].get('resolved_artifact',{}).get('complete') for p in plans for m in p['sources']+p['constraints'])
    assert any(m['source']['pointer'].endswith('/content') and m['source'].get('permission_pointer')==m['source']['pointer'][:-8]
        for p in plans for m in p['sources']+p['constraints'])
    results=[]
    for j in s['jobs'].values():
        if not j.get('result_id'):continue
        r=json.loads(c.execute('SELECT body FROM results WHERE result_id=?',(j['result_id'],)).fetchone()[0])
        assert r['receipt']['model']=='gpt-6-astra' and r['receipt']['reasoning_effort']=='xhigh'
        assert j['state']=='applied',(j['spec']['job_id'],j['state'])
        results.append({'job':j['spec']['job_id'],'role':j['spec']['role'],'result':j['result_id']})
    count=len(s['jobs']);deliveries=deepcopy(s['deliveries'])
for _ in range(30):
    view=service.tick('run');assert view['workflow']['pending_semantic_work']==0 and len(view['workflow']['jobs'])==count
atomic_json(base/'acceptance.json',{'results':results,'plans':plans,'deliveries':deliveries,'stable_ticks':30,
    'boundary':'Synthetic original; actual service queue, model request, exact-body adapter, independent review, parent uptake and delivery.'})
service.close();print('VERIFIED',base,flush=True)

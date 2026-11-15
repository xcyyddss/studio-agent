"""Fresh-model verification of same-call file retrieval and actual uptake."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import uuid

PROJECT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT))
from studio_runtime.contracts import atomic_json
from studio_runtime.provider import CodexProvider
from studio_runtime.service import StudioService
from studio_runtime.workflow import Workflow

base=PROJECT/'.validation'/('real-generated-'+uuid.uuid4().hex[:8])
service=StudioService(base);service.create('Synthetic generated-file retrieval',run_id='fixture',web_search='disabled')
w=Workflow(base/'fixture');provider=CodexProvider({'web_search':'disabled'})
with w._tx() as (c,s):
    s['paused']=True
    for job in s['jobs'].values():job['state']='superseded'
    for inquiry in s['inquiries'].values():inquiry['state']='closed'
    s['topics']['ROOT']['dirty']=False
    w.parliament._put(c,'A','topic',parent='ROOT',actor='A.AUTHOR')
    w.parliament._put(c,'A.v1','revision',topic='A',parent='ROOT',questions=[
        '合成工具验收。首次调用请生成probe.txt，正文为alpha=1，并用context.retrieve的path=@self/probe.txt读取同次生成的文件。'
        '使用status=wait，不产生阶段贡献。另有真实登记的H-next交接尚未回答。'
        '取得文件工具结果后，确认实际读取的正文、路径及完整范围，并用input_uses明确承接；继续等待H-next，不重复生成文件或工具。'],
        I=[],candidates=[],previous=None,continuity='fresh',unresolved_previous='',relation='complement')
    c.execute('INSERT INTO heads VALUES(?,?)',('A','A.v1'))
    s['topics']['A']=dict(id='A',parent='ROOT',revision='A.v1',state='active',summary='',new_returns=[],consumed_returns=[],dirty=False)
    s['question_handoffs']=[dict(id='H-next',question_id='Q-next',recipient='A',coordinator='ROOT',source_job='fixture',state='awaiting_answer')]
    w._author(c,s,'A')
w.resume();calls=[]
for _ in range(5):
    service._execute_tools(base/'fixture',w)
    jobs=w.advance()
    if not jobs:break
    job=jobs[0];aid='real-'+uuid.uuid4().hex
    print('CALL',job.job_id,job.role,flush=True)
    result=provider.execute(job,aid,base/'fixture'/'attempts'/aid);result['result_id']=aid
    imported=w.apply(result,defer_progress=True)
    calls.append(dict(job=job.job_id,role=job.role,outcome=result['outcome'],imported=imported))
    atomic_json(base/'calls.json',calls)
    assert imported['status']=='applied',result
else:raise AssertionError('Generated-file test did not settle')
with w._tx() as (_,s):snapshot=deepcopy(s)
reads=[i for i in snapshot['user_inputs'] if i.get('kind')=='tool_result']
assert len(reads)==1 and reads[0]['value']['outcome']=='succeeded',reads
assert reads[0]['value']['payload']['complete']
assert 'alpha=1' in reads[0]['value']['payload']['value']['content']
assert snapshot['input_receipts'][reads[0]['id']]['state']=='processed'
count=len(snapshot['jobs']);w=Workflow(base/'fixture')
for _ in range(40):assert w.advance()==[]
assert len(w.status()['jobs'])==count
atomic_json(base/'acceptance.json',dict(calls=calls,tool_result=reads[0],receipt=snapshot['input_receipts'][reads[0]['id']],
    polls_after_restart=40,jobs_before=count,jobs_after=len(w.status()['jobs']),state=w.status()['state']))
print('VERIFIED',base,flush=True);service.close()

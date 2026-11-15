"""Synthetic archival scale check, without any model invocation."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import time
import uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from studio_runtime.workflow import Workflow
from studio_runtime.state_store import StateStore
from studio_runtime.contracts import atomic_json

base=ROOT/'.validation'/('storage-'+uuid.uuid4().hex[:8])
w=Workflow.create(base/'run','Synthetic state I/O benchmark; no model calls')
with w._tx() as (_,s):
    for j in s['jobs'].values():j['state']='superseded'
    for r in s['inquiries'].values():r['state']='closed'
    template=deepcopy(next(iter(s['jobs'].values())))
    for n in range(1000):
        j=deepcopy(template);jid='SCALE.'+str(n)
        j['spec'].update(job_id=jid,prompt=str(n)+'p'*64000)
        j['state']='applied';s['jobs'][jid]=j
        s['context_snapshots'][str(n)]={'body':str(n)+'s'*100000}
    s['topics']['ROOT'].update(state='waiting',dirty=False)
    s['questions']['SCALE.Q']=dict(id='SCALE.Q',topic_id='ROOT',state='open',blocking=True,text='Synthetic event gate',source_job='fixture')
    s['state']='waiting'
with closing(sqlite3.connect(w.db)) as c:
    before=c.execute("SELECT body FROM workflow WHERE key='state'").fetchone()[0]
    archived=c.execute('SELECT sum(length(body)) FROM state_bodies').fetchone()[0]
assert len(before)<3000000 and archived>160000000
reads=[];advances=[]
with patch.object(StateStore,'fetch',side_effect=AssertionError('idle/status loaded archived body')):
    for _ in range(20):
        t=time.perf_counter();data=Workflow(w.path).status(compact=True);json.dumps(data,ensure_ascii=False);reads.append(time.perf_counter()-t)
        t=time.perf_counter();assert Workflow(w.path).advance()==[];advances.append(time.perf_counter()-t)
with closing(sqlite3.connect(w.db)) as c:
    after=c.execute("SELECT body FROM workflow WHERE key='state'").fetchone()[0]
assert json.loads(before)['jobs'].keys()==json.loads(after)['jobs'].keys()
atomic_json(base/'acceptance.json',{'jobs':1002,'archive_chars':archived,'mutable_state_chars':len(after),
    'status_seconds':{'median':statistics.median(reads),'max':max(reads)},
    'idle_advance_seconds':{'median':statistics.median(advances),'max':max(advances)},
    'iterations':20,'archive_body_reads':0,'boundary':'Synthetic inactive history and real SQLite/read/advance paths; not a ten-hour production endurance measurement.'})
print((base/'acceptance.json').read_text(encoding='utf-8'),flush=True)
print('VERIFIED',base,flush=True)

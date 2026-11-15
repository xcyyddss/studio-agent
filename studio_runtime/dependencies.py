"""Explicit upward requests, scoped deliveries, and event-driven wakeups.

A request can be registered in the same transaction as a partial return. Its
owner is the immediate parent, never an inferred human or an arbitrary peer.
Only the owner's authored report and public artifacts can travel back down.
"""
from copy import deepcopy
import json
import hashlib
from .contracts import digest


class Dependencies:
    def _dependency_context(self, state, topic):
        return {"outgoing": [deepcopy(r) for r in state.get('internal_requests', [])
                             if r['requester'] == topic and r['state'] in {'open', 'deferred'}],
                "incoming": [deepcopy(r) for r in state.get('internal_requests', [])
                             if r['owner'] == topic and r['state'] == 'open'],
                "boundary": "Explicit internal duties. A deferred response must name an existing wake dependency; delivery is not uptake."}

    def _internal_actions(self, con, state, spec, output):
        topic = spec['metadata']['topic_id']
        task = json.loads(spec['prompt'])['task']
        requests = state.setdefault('internal_requests', [])
        for proposal in output.get('internal_requests', []):
            owner = state['topics'][topic].get('parent')
            if not owner or not proposal['key'].strip() or not proposal['need'].strip():
                raise ValueError('internal request needs a parent, stable key and actual need; ROOT uses a user/tool dependency')
            previous = next((r for r in requests if r['requester'] == topic and r['key'] == proposal['key']
                             and r['state'] in {'open', 'deferred'}), None)
            if previous:
                if previous['need'] != proposal['need'] or previous['kind'] != proposal['kind']:
                    raise ValueError('active internal request key cannot silently change its need')
                continue
            requests.append({'id': self._id(state, 'DEPENDENCY'), 'requester': topic, 'owner': owner,
                'revision': spec['metadata']['revision'], 'source_job': spec['job_id'],
                'state': 'open', **proposal})
            state['topics'][owner]['dirty'] = True

        visible = {r['id'] for r in task.get('dependencies', {}).get('incoming', [])}
        responses = output.get('internal_responses', [])
        if {r['request_id'] for r in responses} != visible or len(responses) != len(visible):
            raise ValueError('each visible incoming internal request needs exactly one response')
        for response in responses:
            request = next(r for r in requests if r['id'] == response['request_id'])
            if request['owner'] != topic or not response['report'].strip():
                raise ValueError('internal response requires its owner and an explicit report')
            if response['outcome'] == 'defer':
                response = deepcopy(response)
                for index, ref in enumerate(response['wait_for']):
                    for prefix, rows in [('@self/question/', list(state['questions'].values())),
                                         ('@self/tool/', state['tool_requests'])]:
                        if ref.startswith(prefix):
                            own = [r for r in rows if r['source_job'] == spec['job_id']]
                            try: response['wait_for'][index] = own[int(ref[len(prefix):])]['id']
                            except (ValueError, IndexError): raise ValueError('unknown self wait dependency')
                allowed = {q['id'] for q in state['questions'].values() if q['topic_id'] == topic and q['state'] in {'open','proposed','linked'}}
                allowed |= {t['id'] for t in state['tool_requests'] if t['topic_id'] == topic and t['state'] == 'requested'}
                allowed |= {r['id'] for r in requests if r['requester'] == topic and r['state'] in {'open','deferred'}}
                if not response['wait_for'] or not set(response['wait_for']) <= allowed or response['artifacts']:
                    raise ValueError('deferred internal response must name actual owner question/tool/upward dependencies and no claimed delivery')
                request.update(state='deferred', response=deepcopy(response), response_job=spec['job_id'])
                continue
            if response['wait_for']:
                raise ValueError('resolved internal response cannot retain an unregistered wait')
            delivered = []
            for path in response['artifacts']:
                if path.startswith('@self/'): path = 'artifacts/' + spec['job_id'] + '/' + path[6:]
                row = con.execute('SELECT content,source_job FROM artifacts WHERE path=?', (path,)).fetchone()
                if not row or state['jobs'][row[1]]['spec']['metadata']['topic_id'] != topic:
                    raise ValueError('internal delivery accepts only the owner authored public artifact version')
                delivered.append({'path':path, 'sha256':hashlib.sha256(row[0].encode('utf-8')).hexdigest(),
                    'hash_representation':'UTF-8 file bytes', 'content':row[0], 'source_job':row[1]})
            record = self._interaction_message(state, request['requester'], json.dumps({
                'request_id':request['id'], 'outcome':response['outcome'], 'report':response['report'],
                'artifacts':delivered, 'boundary':'Owner authored response and exact public artifact bodies; not independent verification.'}, ensure_ascii=False), spec['job_id'], preserve_inflight=True)
            record.update(kind='internal_response', internal_request_id=request['id'], artifacts=delivered)
            request.update(state='delivered', response=deepcopy(response), response_job=spec['job_id'], input_id=record['id'])

    def _dependency_progress(self, state):
        # A deferred owner is revisited once when its named dependency changes,
        # not on every tick and not merely because the child is still waiting.
        requests = state.get('internal_requests', [])
        pending = {q['id'] for q in state['questions'].values() if q['state'] in {'open','proposed','linked'}}
        pending |= {t['id'] for t in state['tool_requests'] if t['state'] == 'requested'}
        pending |= {r['id'] for r in requests if r['state'] in {'open','deferred'}}
        for request in requests:
            if request['state'] == 'deferred' and not set(request['response']['wait_for']) <= pending:
                request['state'] = 'open'
                state['topics'][request['owner']]['dirty'] = True
            if request['state'] == 'delivered' and state.get('input_receipts', {}).get(request['input_id'], {}).get('state') == 'processed':
                request['state'] = 'consumed'

    def _internal_waits(self, state, spec):
        topic = spec['metadata']['topic_id']
        return [{'kind':'internal','id':r['id'],'state':r['state']} for r in state.get('internal_requests', [])
                if r['state'] in {'open','deferred'} and r['requester'] in
                (self._scope(state, topic) if spec['role'] == 'parent' else {topic})]

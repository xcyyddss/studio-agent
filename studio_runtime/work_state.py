"""Materialized working knowledge and explicit obligation/phase transitions.

Updates are attributable role judgments, not semantic novelty certificates.
No deletion or similarity threshold erases originals. Changes to a knowledge
item name its preceding version; an empty update leaves prior items intact.
"""
from copy import deepcopy
import json
import hashlib
from .contracts import digest


class WorkState:
    def _working_context(self, state, topic, role):
        records = state.get('working_knowledge', {}).get(topic, {})
        active = [deepcopy(v) for v in records.values() if v['role'] == role and v['status'] != 'retired']
        return {'items':active, 'phase':deepcopy(state['topics'][topic].get('phase', {})),
                'last_delta':deepcopy(state['topics'][topic].get('last_delta', {}).get(role, {})),
                'boundary':'Persisted author judgments with versioned sources; an empty delta does not erase knowledge. Original history remains retrievable.'}

    def _record_work(self, state, spec, output):
        topic, role = spec['metadata']['topic_id'], spec['role']
        task = json.loads(spec['prompt'])['task']
        items = state.setdefault('working_knowledge', {}).setdefault(topic, {})
        visible = {v['id']:v for v in task.get('working_state', {}).get('items', [])}
        changed = []
        seen = set()
        for update in output.get('work_updates', []):
            old_id = update['item_id']
            if old_id and (old_id not in visible or old_id in seen or items[old_id]['version'] != visible[old_id]['version']):
                raise ValueError('knowledge update requires one visible current item version')
            if not update['statement'].strip() or not update['reason'].strip():
                raise ValueError('knowledge update requires explicit meaning and reason')
            if not old_id and update['status'] == 'retired':
                raise ValueError('cannot retire an item that never existed')
            seen.add(old_id) if old_id else None
            item_id = old_id or self._id(state, 'KNOWLEDGE')
            previous = deepcopy(items.get(item_id))
            version = digest([item_id, spec['job_id'], update])
            if previous and all(previous[k] == update[k] for k in ('statement','kind','condition','status')):
                continue
            record = {**update, 'id':item_id, 'version':version, 'role':role, 'source_job':spec['job_id'],
                      'previous_version':previous['version'] if previous else None}
            state.setdefault('knowledge_versions', {})[version] = deepcopy(record)
            items[item_id] = record
            changed.append({'id':item_id,'version':version,'status':update['status']})
        visible_obligations = {o['id'] for o in task.get('unresolved', []) if o.get('state') == 'conditional'}
        resolved = set()
        for decision in output.get('obligation_decisions', []):
            oid = decision['id']
            if oid not in visible_obligations or oid in resolved or not decision['reason'].strip():
                raise ValueError('obligation transition needs one visible conditional obligation and reason')
            original = next(o for o in state['obligations'] if o['id'] == oid and o['topic_id'] == topic)
            original.update(state=decision['state'], resolution=deepcopy(decision), resolution_job=spec['job_id'])
            resolved.add(oid)
        phase = output.get('phase', {})
        if phase.get('name'):
            if not phase['deliverable'].strip() or not phase['acceptance'].strip():
                raise ValueError('phase needs a concrete deliverable and acceptance condition')
            previous = state['topics'][topic].get('phase')
            if previous != phase:
                state['topics'][topic].setdefault('phase_history', []).append({'source_job':spec['job_id'], 'phase':deepcopy(phase)})
                state['topics'][topic]['phase'] = deepcopy(phase)
        state['topics'][topic].setdefault('last_delta', {})[role] = {
            'source_job':spec['job_id'], 'declared':deepcopy(output.get('progress', {})),
            'knowledge_changes':changed, 'obligation_changes':sorted(resolved),
            'input_ids':[u['input_id'] for u in output.get('input_uses', [])],
            'artifacts':[f['path'] for f in output.get('files', [])],
            'boundary':'These are recorded changes, not proof of novelty, correctness or user benefit.'}


def working_history(records):
    """Lossless semantic fields; execution bodies stay in their own ledgers.

No arbitrary last-N cut. Code, tool arguments and input receipts are referenced,
not re-embedded in every later report. All prior ans/con/summary/contribution,
conditions and decision reasons remain until explicitly reviewed elsewhere.
"""
    projected = []
    fields = {'ans','con','summary','reason','contributions','unresolved','dispositions',
              'contribution_decisions','selection_responses','escalation_responses',
              'internal_responses','progress','work_updates','phase','obligation_decisions'}
    for record in records:
        original = record['output']
        body = {k:deepcopy(v) for k,v in original.items() if k in fields and v}
        # Delivered artifact bodies already live in the exact-version artifact
        # archive. Preserve locators and hashes, not duplicate content strings.
        if original.get('files'):
            body['artifacts'] = [{'path':'artifacts/'+record['job_id']+'/'+f['path'],
                                  'sha256':hashlib.sha256(f['content'].encode('utf-8')).hexdigest(),
                                  'hash_representation':'UTF-8 file bytes'}
                                 for f in original['files']]
        omitted = sorted(set(original)-set(body))
        projected.append({**{k:deepcopy(v) for k,v in record.items() if k != 'output'}, 'output':body,
            'original':{'job_id':record['job_id'],'sha256':digest(original),'retrieve':'history.read',
                        'separate_transport_fields':omitted}})
    return projected

"""UI transport projections; never used as cognitive input or durable state."""


def headers(rows, fields):
    return [{k:r[k] for k in fields if k in r} for r in rows]


def input_views(rows):
    result = []
    for item in rows:
        view = {k:item[k] for k in ('id','kind','topic_id','source_job','request_id','question_id','created_at','source') if k in item}
        if item.get('kind') == 'tool_result':
            value = item.get('value', {})
            if value.get('receipt', {}).get('adapter') == 'codex_native_web':
                payload = value.get('payload') or {}
                view['value'] = {'outcome':value.get('outcome'),'error':value.get('error'),
                    'receipt':{'adapter':'codex_native_web'},
                    'payload':{k:payload[k] for k in ('summary','limitations','sources') if k in payload}}
        result.append(view)
    return result


def memory_view(data):
    return {k:len(v) if isinstance(v,(dict,list)) else v for k,v in data.items()}

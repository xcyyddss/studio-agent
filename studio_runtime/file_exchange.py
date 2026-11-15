"""Immutable attachments, explicit recipients, bounded reads and authored uptake.

Original bytes live in SQLite, not a writable shared document. A read receipt
records text ranges and native images delivered to a successful role call,
not human-like comprehension.
"""
import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import PurePosixPath
import re
import uuid
import zipfile
import subprocess
from xml.etree import ElementTree

from .contracts import canonical, digest
from .media import IMAGE_SUFFIXES, prepare_media, render_page, image_from_storage

MAX_FILE = 4_000_000
MAX_TEXT = 2_000_000


def sha(data):
    return hashlib.sha256(data).hexdigest()


def extract(name, raw):
    """Deterministic text extraction. Never execute or render uploaded content."""
    suffix = PurePosixPath(name).suffix.lower()
    try:
        if suffix == '.docx':
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = archive.infolist()
                if len(entries) > 2000 or sum(i.file_size for i in entries) > 32_000_000:
                    raise ValueError('DOCX 解压规模超过限制')
                entry = archive.getinfo('word/document.xml')
                if entry.file_size > 8_000_000:
                    raise ValueError('DOCX 正文超过限制')
                xml = archive.read(entry)
                if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
                    raise ValueError('DOCX 包含不支持的 XML 声明')
                tree = ElementTree.fromstring(xml)
                ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
                text = '\n'.join(''.join(n.text or '' for n in p.iter(ns+'t')) for p in tree.iter(ns+'p'))
            method = 'docx_main_body_text'
            limitation = '仅提取主文档段落及表格中的文字；不含图片、批注、页眉页脚、修订语义或版式。'
        elif suffix in {'.pdf', '.doc', '.xlsx', '.pptx', '.zip', '.png', '.jpg', '.jpeg', '.gif', '.webp'}:
            raise ValueError('此格式尚无正文解析器；请另附 UTF-8 文本或 DOCX 正文版本')
        else:
            encoding = 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
            text = raw.decode(encoding)
            if any(ord(c) < 32 and c not in '\n\r\t\f' for c in text):
                raise ValueError('文件含二进制控制字符，不能当作文本读取')
            method, limitation = encoding, '按文本读取；不执行代码、公式、网页脚本或文件内指令。'
        if len(text) > MAX_TEXT:
            raise ValueError('提取正文超过 200 万字符；请提供较小文件')
        return text, {'state': 'ready', 'method': method, 'chars': len(text),
                      'sha256': sha(text.encode()), 'limitations': limitation}
    except (ValueError, UnicodeError, KeyError, OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        return None, {'state': 'unsupported', 'method': None, 'chars': None, 'sha256': None,
                      'limitations': str(exc)}


def prepare(files):
    if files is None:
        return []
    if not isinstance(files, list) or len(files) > 4:
        raise ValueError('一次最多上传 4 个文件')
    result = []
    for item in files:
        if not isinstance(item, dict) or not {'name', 'data_base64'} <= set(item) <= {'name', 'data_base64', 'replaces'}:
            raise ValueError('附件须包含 name、data_base64，可选 replaces')
        name, data = item['name'], item['data_base64']
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or any(ord(c) < 32 or c in '/\\' for c in name):
            raise ValueError('附件名称须为不含目录的文件名，最多 200 字符')
        if not isinstance(data, str) or len(data) > (MAX_FILE + 2)//3*4:
            raise ValueError('单个附件不得超过 4 MB')
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError as exc:
            raise ValueError('附件编码无效') from exc
        if len(raw) > MAX_FILE:
            raise ValueError('单个附件不得超过 4 MB')
        replaces = item.get('replaces', '')
        if not isinstance(replaces, str) or len(replaces) > 600:
            raise ValueError('原版本标识无效')
        pages = []
        if PurePosixPath(name).suffix.lower() in IMAGE_SUFFIXES | {'.pdf'}:
            text, extraction, pages = prepare_media(name, raw)
        else:
            text, extraction = extract(name, raw)
        result.append({'name': name, 'raw': raw, 'text': text, 'sha256': sha(raw),
                       'bytes': len(raw), 'replaces': replaces, 'extraction': extraction, 'pages': pages})
    replacements = [item['replaces'] for item in result if item['replaces']]
    if len(set(replacements)) != len(replacements):
        raise ValueError('同一次提交不能用多个文件替换同一版本')
    return result


def public(record):
    value = {k: deepcopy(record[k]) for k in ('id', 'name', 'topic_id', 'origin', 'bytes', 'sha256',
            'source_job', 'input_id', 'question_id', 'replaces', 'version', 'extraction', 'state') if k in record}
    value.get('extraction', {}).pop('pages', None)
    return value


def complete_read(record):
    pages = record['extraction'].get('pages')
    if pages:
        for p in pages:
            reads = [r for r in record.get('reads', []) if r.get('page') == p['page'] and r.get('state') == 'ready']
            if not any(r.get('image_delivered') and r.get('image_sha256') == p.get('image_sha256') for r in reads):
                return False
            if not _covered(reads, p['chars']):
                return False
        return True
    return record['extraction']['state'] == 'ready' and _covered(record.get('reads', []), record['extraction']['chars'])


def _covered(reads, chars):
    end = 0
    ranges = sorted((r['start'], r['end']) for r in reads if r.get('state') == 'ready')
    for start, stop in ranges:
        if start > end:
            return False
        end = max(end, stop)
    return bool(ranges) and end >= chars


def validate_storage(con, state):
    """Also used when validating portable checkpoints."""
    for record in state.get('file_exchange', {}).values():
        if record['origin'] == 'model':
            row = con.execute('SELECT content FROM artifacts WHERE path=?', (record['path'],)).fetchone()
            raw = row[0].encode() if row else None
        else:
            row = con.execute('SELECT raw,text FROM attachment_blobs WHERE id=?', (record['id'],)).fetchone()
            raw = row[0] if row else None
            if row and row[1] is not None and sha(row[1].encode()) != record['extraction']['sha256']:
                raise ValueError('附件提取正文校验失败')
        if raw is None or len(raw) != record['bytes'] or sha(raw) != record['sha256']:
            raise ValueError('附件原件缺失或校验失败')
        for p in record['extraction'].get('pages', []):
            row = con.execute('SELECT text,image FROM attachment_pages WHERE file_id=? AND page=?', (record['id'], p['page'])).fetchone()
            if not row or sha(row[0].encode()) != p['text_sha256'] or len(row[0]) != p['chars']:
                raise ValueError('附件分页正文校验失败')
            if (row[1] is not None or p.get('image_sha256')) and (row[1] is None or sha(row[1]) != p.get('image_sha256')):
                raise ValueError('附件页图校验失败')


class FileExchange:
    def _model_file(self, con, state, path, topic):
        row = con.execute('SELECT content,source_job FROM artifacts WHERE path=?', (path,)).fetchone()
        if not row:
            raise ValueError('附件必须指向已保存的真实产物: ' + path)
        owner = state['jobs'][row[1]]['spec']['metadata']['topic_id']
        if owner not in self._scope(state, topic) or owner == 'USER':
            raise ValueError('产物不在此议题的责任范围')
        fid = 'FILE.' + sha(path.encode())[:24]
        records = state.setdefault('file_exchange', {})
        if fid not in records:
            raw = row[0].encode()
            records[fid] = {'id': fid, 'name': PurePosixPath(path).name, 'path': path,
                'origin': 'model', 'topic_id': owner, 'source_job': row[1], 'bytes': len(raw),
                'sha256': sha(raw), 'version': 1, 'replaces': '', 'state': 'published',
                'extraction': {'state': 'ready', 'method': 'authored_text', 'chars': len(row[0]),
                    'sha256': sha(raw), 'limitations': '模型生成的文本，不代表已经执行或验证。'}}
        return records[fid]

    def _resolve_file(self, con, state, reference, topic, *, job=None):
        if reference.startswith('@self/') and job:
            reference = 'artifacts/' + job + '/' + reference[6:]
        if reference.startswith('artifacts/'):
            return self._model_file(con, state, reference, topic)
        record = state.get('file_exchange', {}).get(reference)
        if not record:
            raise ValueError('未知附件标识；请用真实 FILE ID 或产物完整路径')
        allowed = record['topic_id'] == topic or (record['origin'] == 'model' and record['topic_id'] in self._scope(state, topic) and record['topic_id'] != 'USER')
        if not allowed:
            raise ValueError('附件不属于该接收议题')
        return record

    def _receive_files(self, con, state, files, record):
        if not files:
            return
        if record['topic_id'] == 'USER':
            raise ValueError('附件请交给任务议题；USER 目前只接收显式协作文字')
        con.execute('CREATE TABLE IF NOT EXISTS attachment_blobs(id TEXT PRIMARY KEY,raw BLOB NOT NULL,text TEXT)')
        con.execute('CREATE TABLE IF NOT EXISTS attachment_pages(file_id TEXT,page INTEGER,text TEXT NOT NULL,image BLOB,PRIMARY KEY(file_id,page))')
        records = state.setdefault('file_exchange', {})
        bound = []
        for item in files:
            prior = self._resolve_file(con, state, item['replaces'], record['topic_id']) if item['replaces'] else None
            fid = 'FILE.' + uuid.uuid4().hex
            entry = {'id': fid, 'name': item['name'], 'origin': record['source'], 'topic_id': record['topic_id'],
                'input_id': record['id'], 'question_id': record['question_id'], 'sha256': item['sha256'],
                'bytes': item['bytes'], 'extraction': item['extraction'], 'created_at': record['created_at'],
                'version': prior['version'] + 1 if prior else 1, 'replaces': prior['id'] if prior else '',
                'state': 'received', 'reads': [], 'deliveries': [], 'uses': []}
            con.execute('INSERT INTO attachment_blobs VALUES(?,?,?)', (fid, item['raw'], item['text']))
            con.executemany('INSERT INTO attachment_pages VALUES(?,?,?,?)',
                [(fid, p['page'], p['text'], p.get('image')) for p in item.get('pages', [])])
            records[fid] = entry
            if prior and prior['origin'] != 'model' and prior['state'] not in {'handled', 'superseded'}:
                prior.update(state='superseded', superseded_by=fid)
            bound.append(public(entry))
        record['attachments'] = bound

    def _invalidate_file_waiters(self, state, record):
        """New input invalidates sealed decisions, never rewrites their prompts.

        Preserve in-flight originals through the existing late-result path.
        Otherwise a terminal return would retry the same old input forever,
        unable to read a file that arrived after it was sealed.
        """
        scope, topic = set(), record['topic_id']
        while topic:
            scope.add(topic)
            topic = state['topics'][topic]['parent']
        affected = []
        for job in state['jobs'].values():
            spec = job['spec']
            tid = spec['metadata']['topic_id']
            if job['state'] == 'pending' and tid in scope and spec['role'] in {'author', 'parent'}:
                job['state'] = 'superseded'
                affected.append(spec['job_id'])
                state['topics'][tid]['author_refresh_required' if spec['role'] == 'author' else 'dirty'] = True
        if affected:
            state['diagnostics'].append({'kind': 'attachment_arrival', 'input_id': record['id'],
                'superseded_jobs': affected,
                'reason': 'New file requires fresh responsible input; sealed prompts and late outputs remain unchanged.'})

    def _bind_question_files(self, con, state, spec, references):
        if len(references) > 8 or len(set(references)) != len(references):
            raise ValueError('提问最多绑定 8 个不重复附件')
        return [public(self._resolve_file(con, state, ref, spec['metadata']['topic_id'], job=spec['job_id'])) for ref in references]

    def _file_context(self, state, task):
        entries = [f for f in state.get('file_exchange', {}).values() if f['topic_id'] == task['topic']]
        if entries:
            task['attachments'] = {'recent': [public(f) for f in entries[-6:]], 'total': len(entries),
                'pending': sum(f['origin'] != 'model' and f['state'] not in {'handled', 'superseded'} for f in entries),
                'boundary': '目录不是正文。attachment.catalog 可分页；attachment.read 读取指定附件正文。原件只发给指定议题，画像不会自动接收正文。'}
        task['tool_catalog'].extend([
            {'kind': 'attachment.catalog', 'description': '本议题附件目录；arguments={cursor?:0}，每页6个。目录不是阅读证据。'},
            {'kind': 'attachment.read', 'description': '读取本议题附件；arguments={file_id,page?:1,start?:0,max_chars?:1..8000}。PDF必须指定物理页码(从1开始)，每次返回该页文字段及整页图；图片page=1返回原生视觉附件。一次最多请求4张图/页，后续再读；文字start/end为当前页字符位置。等待真实返回，未读页不得声称已读。'}])

    def attachment_tool(self, request):
        with self._tx() as (con, state):
            if request not in state['tool_requests']:
                raise ValueError('附件操作须对应真实工具请求')
            args, topic = request['arguments'], request['topic_id']
            if request['kind'] == 'attachment.catalog':
                if set(args) - {'cursor'} or type(args.get('cursor', 0)) is not int or args.get('cursor', 0) < 0:
                    raise ValueError('目录游标无效')
                entries = [public(f) for f in state.get('file_exchange', {}).values() if f['topic_id'] == topic]
                start = args.get('cursor', 0)
                payload = {'entries': entries[start:start+6], 'total': len(entries),
                           'next_cursor': start+6 if start+6 < len(entries) else None}
            else:
                if not {'file_id'} <= set(args) <= {'file_id', 'page', 'start', 'max_chars'}:
                    raise ValueError('attachment.read 要求 file_id，可选 page、start、max_chars')
                f = self._resolve_file(con, state, args['file_id'], topic)
                start, count = args.get('start', 0), args.get('max_chars', 4000)
                if type(start) is not int or start < 0 or type(count) is not int or not 1 <= count <= 8000:
                    raise ValueError('附件读取范围无效')
                if f['extraction']['state'] != 'ready':
                    payload = {'file_id': f['id'], 'sha256': f['sha256'], 'state': 'unsupported',
                               'limitations': f['extraction']['limitations'], 'content': None}
                elif f['extraction'].get('pages'):
                    page = args.get('page', 1 if f['extraction']['kind'] == 'image' else None)
                    if type(page) is not int or not 1 <= page <= f['extraction']['page_count']:
                        raise ValueError('请指定有效 PDF 物理页码 page，从 1 开始')
                    p = f['extraction']['pages'][page-1]
                    row = con.execute('SELECT text,image FROM attachment_pages WHERE file_id=? AND page=?', (f['id'], page)).fetchone()
                    if not row or sha(row[0].encode()) != p['text_sha256'] or start > len(row[0]):
                        raise ValueError('分页正文或范围校验失败')
                    if row[1] is None:
                        raw = con.execute('SELECT raw FROM attachment_blobs WHERE id=?', (f['id'],)).fetchone()[0]
                        if sha(raw) != f['sha256']:
                            raise ValueError('PDF 原件校验失败')
                        try:
                            png, info = render_page(raw, page)
                        except (ValueError, OSError, subprocess.SubprocessError) as exc:
                            return {'outcome': 'succeeded', 'payload': {'file_id': f['id'], 'sha256': f['sha256'],
                                'version': f['version'], 'page': page, 'state': 'unreadable', 'content': None,
                                'limitations': '该页渲染失败，尚未读取页图；可请求可读版本，或明确因不可读而不采用：' + str(exc)},
                                'receipt': {'adapter': 'file_exchange', 'request_id': request['id'], 'topic_id': topic}}
                        p.update(info, image_sha256=sha(png))
                        con.execute('UPDATE attachment_pages SET image=? WHERE file_id=? AND page=?', (png, f['id'], page))
                    visual = {'file_id': f['id'], 'file_sha256': f['sha256'], 'version': f['version'], 'page': page,
                              'image_sha256': p['image_sha256'], 'width': p['width'], 'height': p['height'], 'scope': 'whole_page' if f['extraction']['kind'] == 'pdf' else 'whole_image'}
                    image_from_storage(con, state, visual, topic)
                    payload = {'file_id': f['id'], 'sha256': f['sha256'], 'version': f['version'], 'page': page,
                        'page_count': f['extraction']['page_count'], 'text_sha256': p['text_sha256'], 'state': 'ready',
                        'start': start, 'end': min(start+count, len(row[0])), 'total_chars': len(row[0]),
                        'content': row[0][start:start+count], 'visual': visual, 'limitations': f['extraction']['limitations']}
                else:
                    if 'page' in args:
                        raise ValueError('文本附件没有物理页码，请使用 start/max_chars')
                    text = (con.execute('SELECT content FROM artifacts WHERE path=?', (f['path'],)).fetchone()[0]
                            if f['origin'] == 'model' else con.execute('SELECT text FROM attachment_blobs WHERE id=?', (f['id'],)).fetchone()[0])
                    if sha(text.encode()) != f['extraction']['sha256'] or start > len(text):
                        raise ValueError('附件正文或范围校验失败')
                    payload = {'file_id': f['id'], 'sha256': f['sha256'], 'text_sha256': f['extraction']['sha256'],
                        'state': 'ready', 'start': start, 'end': min(start+count, len(text)), 'total_chars': len(text),
                        'content': text[start:start+count], 'limitations': f['extraction']['limitations']}
            return {'outcome': 'succeeded', 'payload': payload,
                    'receipt': {'adapter': 'file_exchange', 'request_id': request['id'], 'topic_id': topic}}

    def _observe_file_use(self, state, spec, output, result=None):
        task = json.loads(spec['prompt'])['task']
        records = state.get('file_exchange', {})
        visible = {f['id'] for f in task.get('attachments', {}).get('recent', [])}
        for inp in task.get('user_inputs', []):
            for f in inp.get('attachments', []):
                visible.add(f['id'])
                if f['id'] in records:
                    records[f['id']].setdefault('deliveries', []).append({'job_id': spec['job_id'], 'input_id': inp['id']})
            value = inp.get('value', {})
            if not isinstance(value, dict) or value.get('receipt', {}).get('adapter') != 'file_exchange' or value.get('outcome') != 'succeeded':
                continue
            p = value['payload']
            visible.update(f['id'] for f in p.get('entries', []))
            if p.get('file_id') not in records:
                continue
            f = records[p['file_id']]
            visible.add(f['id'])
            if f['topic_id'] != spec['metadata']['topic_id'] or f['origin'] == 'model':
                continue
            visual = p.get('visual')
            receipt = (result or {}).get('receipt', {})
            delivered = bool(visual and visual in spec['metadata'].get('native_images', []) and
                visual in receipt.get('native_images', []) and receipt.get('native_images_dispatched') is True)
            f['reads'].append({k: deepcopy(p[k]) for k in ('state', 'start', 'end', 'text_sha256', 'page') if k in p} |
                {'job_id': spec['job_id'], 'input_id': inp['id'], 'file_sha256': f['sha256'], 'version': f['version'],
                 **({'image_delivered': delivered, 'image_sha256': visual['image_sha256'], 'visual_scope': visual['scope']} if visual else {})})
            if f['state'] not in {'handled', 'superseded'}:
                f['state'] = 'read' if complete_read(f) else 'partly_read' if p['state'] == 'ready' else 'unreadable'
        used = set()
        for use in output.get('attachment_uses', []):
            fid = use['file_id']
            if fid not in visible or fid in used or fid not in records:
                raise ValueError('附件处置必须引用本次可见且唯一的 FILE ID')
            used.add(fid)
            f = records[fid]
            if f['topic_id'] != spec['metadata']['topic_id'] or f['origin'] == 'model':
                raise ValueError('附件处置仅负责本议题收到的用户文件')
            if not use['reason'].strip() or not use['effect'].strip():
                raise ValueError('附件承接须说明理由和后续影响')
            if use['decision'] == 'adopt' and not complete_read(f):
                raise ValueError('采用前须让该版本全部文字范围和所有页图进入成功调用；目录和工具完成不等于读取')
            unreadable = any(r.get('state') == 'unreadable' for r in f['reads'])
            if use['decision'] == 'reject' and f['extraction']['state'] == 'ready' and not complete_read(f) and not unreadable:
                raise ValueError('可读附件须先完整读取再作不采用判断')
            if use['decision'] == 'reject' and f['extraction']['state'] != 'ready' and not f['reads']:
                raise ValueError('须先取得实际不可读结果，再说明不采用或请求转换')
            f['uses'].append({'job_id': spec['job_id'], **deepcopy(use), 'full_text_delivered': complete_read(f),
                             'full_content_delivered': complete_read(f), 'version': f['version'], 'sha256': f['sha256']})
            if f['state'] != 'superseded':
                f['state'] = 'handled' if use['decision'] in {'adopt', 'reject'} else 'deferred'
        if output.get('action') in {'deliver', 'quiesce'} or output.get('status') == 'complete':
            pending = [f['id'] for f in records.values() if f['topic_id'] in self._scope(state, spec['metadata']['topic_id'])
                       and f['origin'] != 'model' and f['state'] not in {'handled', 'superseded'}]
            if pending:
                raise ValueError('尚有用户附件未读取并承接，不能结束: ' + ', '.join(pending[:6]))

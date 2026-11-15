from .state_store import read_state, write_state
"""Bounded local extraction and immutable visual inputs for fresh CLI calls.

No uploaded paths or commands are evaluated. Original files remain in SQLite;
temporary decoder inputs are removed and only selected PNGs enter a role call.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import warnings

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff'}
MAX_PAGES = 200
MAX_PIXELS = 40_000_000
MAX_EDGE = 2400
MAX_IMAGES = 4


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def poppler():
    folder = os.environ.get('STUDIO_POPPLER_BIN')
    path = str(Path(folder) / ('pdftoppm.exe' if os.name == 'nt' else 'pdftoppm')) if folder else shutil.which('pdftoppm')
    if not path or not Path(path).is_file():
        raise ValueError('PDF 页图需要 Poppler 的 pdftoppm；请加入 PATH 或设置 STUDIO_POPPLER_BIN')
    return path


def normalize_image(raw):
    from PIL import Image, ImageOps
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        try:
            source = Image.open(io.BytesIO(raw))
        except Image.DecompressionBombError as exc:
            raise ValueError('图像像素规模超过限制') from exc
        with source:
            if source.width * source.height > MAX_PIXELS:
                raise ValueError('图像超过 4000 万像素')
            frames = getattr(source, 'n_frames', 1)
            if frames != 1:
                raise ValueError('多帧图像请拆成单帧文件或 PDF；不静默忽略后续帧')
            oriented = ImageOps.exif_transpose(source).convert('RGBA')
            im = Image.new('RGBA', oriented.size, 'white')
            im.alpha_composite(oriented)
            im = im.convert('RGB')
            original = list(im.size)
            im.thumbnail((MAX_EDGE, MAX_EDGE))
            out = io.BytesIO()
            im.save(out, format='PNG')
            return out.getvalue(), {'width': im.width, 'height': im.height,
                'original_dimensions': original, 'transform': 'EXIF orientation; alpha on white; RGB PNG; longest edge <=2400; entire image, no crop'}


def _pdf_text(path, output):
    from pypdf import PdfReader
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError('加密 PDF 请先另存为未加密版本')
    if not 1 <= len(reader.pages) <= MAX_PAGES:
        raise ValueError('PDF 须为 1–200 页；较大文件请分册')
    pages, total = [], 0
    for page in reader.pages:
        text = page.extract_text() or ''
        total += len(text)
        if total > 2_000_000:
            raise ValueError('PDF 提取文字超过 200 万字符；请分册')
        pages.append(text)
    Path(output).write_text(json.dumps(pages, ensure_ascii=False), encoding='utf-8')


def prepare_media(name, raw):
    suffix = Path(name).suffix.lower()
    try:
        if suffix in IMAGE_SUFFIXES:
            png, info = normalize_image(raw)
            pages = [{'page': 1, 'text': '', 'text_sha256': sha(b''), 'chars': 0,
                      'image': png, 'image_sha256': sha(png), **info}]
            method, limitation = 'image_png', '原图经方向校正并转为 RGB PNG，最长边 2400；整图送入原生视觉附件，不做 OCR。细小文字或缩放损失须说明，必要时请求更清晰的局部文件。'
        else:
            poppler()  # Missing renderer must not masquerade as full PDF support.
            with tempfile.TemporaryDirectory(prefix='studio-pdf-') as folder:
                source, output = Path(folder) / 'input.pdf', Path(folder) / 'text.json'
                source.write_bytes(raw)
                proc = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), str(source), str(output)],
                    capture_output=True, timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if proc.returncode:
                    raise ValueError('PDF 文字提取失败: ' + proc.stderr.decode('utf-8', errors='replace')[-1200:])
                texts = json.loads(output.read_text(encoding='utf-8'))
            pages = [{'page': n, 'text': text, 'text_sha256': sha(text.encode()), 'chars': len(text)} for n, text in enumerate(texts, 1)]
            method, limitation = 'pdf_text_and_pages', '按 PDF 物理页码（从 1 开始）提取文字，并按请求渲染整页 PNG（最长边 2400）。文字提取可能漏字或失序，空文字页仍需看页图；无自动 OCR、无附件/音视频/交互内容解析。细节不清时不得猜测。'
        # Page boundaries live in the page table, not synthetic text characters.
        text = ''.join(p['text'] for p in pages)
        metadata = [{k: v for k, v in p.items() if k not in {'text', 'image'}} for p in pages]
        return text, {'state': 'ready', 'method': method, 'kind': 'pdf' if suffix == '.pdf' else 'image',
            'chars': len(text), 'sha256': sha(text.encode()), 'page_count': len(pages),
            'pages': metadata, 'limitations': limitation}, pages
    except (ValueError, OSError, ImportError, RuntimeError, subprocess.SubprocessError, Warning) as exc:
        return None, {'state': 'unsupported', 'method': None, 'chars': None, 'sha256': None,
                      'limitations': str(exc)}, []


def render_page(raw, page):
    with tempfile.TemporaryDirectory(prefix='studio-page-') as folder:
        source, output = Path(folder) / 'input.pdf', Path(folder) / 'page'
        source.write_bytes(raw)
        proc = subprocess.run([poppler(), '-f', str(page), '-l', str(page), '-singlefile', '-scale-to', str(MAX_EDGE),
                               '-png', str(source), str(output)], capture_output=True, timeout=60,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if proc.returncode or not output.with_suffix('.png').exists():
            raise ValueError('PDF 页图渲染失败: ' + proc.stderr.decode('utf-8', errors='replace')[-1000:])
        return normalize_image(output.with_suffix('.png').read_bytes())


def visual_inputs(task):
    """Only explicit successful attachment.read results supply images, not refs."""
    found = {}
    for item in task.get('user_inputs', []):
        value = item.get('value', {})
        if not isinstance(value, dict) or value.get('outcome') != 'succeeded' or value.get('receipt', {}).get('adapter') != 'file_exchange':
            continue
        p = value.get('payload', {})
        if p.get('visual'):
            v = p['visual']
            found[(v['file_id'], v['page'])] = v
    return list(found.values())


def image_from_storage(con, state, item, topic=None):
    f = state.get('file_exchange', {}).get(item.get('file_id'))
    if not f or f['origin'] == 'model' or (topic is not None and f['topic_id'] != topic):
        raise ValueError('视觉附件不属于当前议题')
    if f['sha256'] != item.get('file_sha256') or f['version'] != item.get('version'):
        raise ValueError('视觉附件版本不匹配')
    page = next((p for p in f['extraction'].get('pages', []) if p['page'] == item.get('page')), None)
    row = con.execute('SELECT image FROM attachment_pages WHERE file_id=? AND page=?', (f['id'], item['page'])).fetchone()
    if not page or not row or row[0] is None or sha(row[0]) != item.get('image_sha256') or page.get('image_sha256') != item['image_sha256']:
        raise ValueError('视觉附件页图校验失败')
    return row[0]


def dispatch_images(job, directory):
    items = job.metadata.get('native_images', [])
    try:
        prompt_items = json.loads(job.prompt).get('task', {}).get('native_images', [])
    except (ValueError, AttributeError):
        prompt_items = []
    if items != prompt_items or (items and (job.role not in {'author', 'parent'} or len(items) > MAX_IMAGES)):
        raise ValueError('视觉附件与封存上下文不符')
    if not items:
        return [], []
    # Executor's attempt directory is run/attempts/<attempt_id>; use only this
    # run's committed records, never model-provided filesystem locations.
    db = directory.parent.parent / 'workflow.db'
    con = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
    try:
        state = read_state(con)
        if state['run_id'] != job.run_id or state['jobs'].get(job.job_id, {}).get('spec') != job.to_dict():
            raise ValueError('视觉调用不匹配本运行的封存任务')
        paths = []
        for index, item in enumerate(items, 1):
            raw = image_from_storage(con, state, item, job.metadata['topic_id'])
            path = directory / ('image-%04d.png' % index)
            path.write_bytes(raw)
            paths.append(path)
        return paths, items
    finally:
        con.close()


if __name__ == '__main__':
    _pdf_text(sys.argv[1], sys.argv[2])

"""Recover only native image-tool outputs belonging to this conversation.

Images are decoded locally, never fetched from model-supplied remote URLs.
The private source manifest is separate from browser-visible attachments.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import datetime

MAX_BYTES = 16_000_000
DECODER = '''import io,json,sys,warnings
from PIL import Image
warnings.simplefilter("error",Image.DecompressionBombWarning)
Image.MAX_IMAGE_PIXELS=32000000
d=sys.stdin.buffer.read(16000001)
if len(d)>16000000: raise ValueError("Image too large")
with Image.open(io.BytesIO(d)) as im:
 if im.format not in ("PNG","JPEG","WEBP"): raise ValueError("Unsupported image format")
 if getattr(im,"n_frames",1)!=1: raise ValueError("Animated image not supported")
 w,h=im.size
 if not 0<w<=8192 or not 0<h<=8192 or w*h>32000000: raise ValueError("Image dimensions too large")
 fmt=im.format
 im.verify()
with Image.open(io.BytesIO(d)) as im: im.load()
print(json.dumps({"format":fmt,"width":w,"height":h}))
'''


def generation_summary(job, requested_count):
    """Public bounded diagnosis from hook records; no raw error or source paths."""
    def records(name):
        path=no_symlinks(Path(job)/name)
        if not path.exists():return []
        if not path.is_file() or path.stat().st_size>MAX_BYTES:raise ValueError('Invalid image hook log')
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    audit=records('image-tool-audit.jsonl')
    results=records('image-tool-results.jsonl')
    outcomes={x.get('image_name'):x for x in results}
    slots={};models=set();reset=None;quota=False
    for row in audit:
        if row.get('tool')!='generate_image' or row.get('decision')!='allow':continue
        slot=row.get('slot')
        if type(slot) is not int or not 1<=slot<=8:continue
        current=slots.setdefault(slot,{'slot':slot,'attempts':0,'retries':0,'status':'running'})
        current['attempts']+=1
        current['retries']+=int(row.get('attempt')==2)
        result=outcomes.get(row.get('image_name'))
        if result is None:continue
        error=result.get('error')
        current['status']='failed' if error else 'generated'
        if not isinstance(error,str) or 'body:' not in error:continue
        try:body=json.loads(error.partition('body:')[2]).get('error',{})
        except (ValueError,TypeError,AttributeError):continue
        if body.get('code')!=429 or body.get('status')!='RESOURCE_EXHAUSTED':continue
        quota=True;current['status']='quota_exhausted'
        for detail in body.get('details',[]):
            meta=detail.get('metadata',{}) if isinstance(detail,dict) else {}
            model=meta.get('model')
            if isinstance(model,str) and re.fullmatch(r'[a-z0-9._-]{1,100}',model):models.add(model)
            stamp=meta.get('quotaResetTimeStamp')
            if isinstance(stamp,str):
                try:
                    datetime.datetime.fromisoformat(stamp.replace('Z','+00:00'))
                    reset=stamp
                except ValueError:pass
    if type(requested_count) is int and 1<=requested_count<=8:
        for slot in range(1,requested_count+1):slots.setdefault(slot,{'slot':slot,'attempts':0,'retries':0,'status':'not_started'})
    unresolved_quota=any(x['status']=='quota_exhausted' for x in slots.values())
    return {'requested_count':requested_count,'successful_tool_count':sum(x['status']=='generated' for x in slots.values()),
            'observed_attempts':sum(x['attempts'] for x in slots.values()),'retries_used':sum(x['retries'] for x in slots.values()),
            'quota_encountered':quota,'quota_exhausted':unresolved_quota,'quota_reset_at':reset if unresolved_quota else None,'provider_reported_models':sorted(models),
            'model_evidence':'provider quota error metadata; successful image model not independently reported' if models else 'not reported',
            'per_image_status':[slots[x] for x in sorted(slots)]}


def no_symlinks(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Image path must be absolute without traversal')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Image paths cannot contain symlinks')
    return path


def describe(data):
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError('Image exceeds size limit or is empty')
    python = Path(os.environ.get('CODEX_IMAGE_PYTHON') or
                  Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3')
    if not python.is_file():
        raise ValueError('Local image decoder unavailable; configure CODEX_IMAGE_PYTHON with Pillow')
    try:
        result = subprocess.run([str(python), '-c', DECODER], input=data,
                                capture_output=True, timeout=15, check=True)
        meta = json.loads(result.stdout)
    except (subprocess.SubprocessError, ValueError) as error:
        raise ValueError('Image failed local decoding') from error
    meta.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                mime_type={'PNG':'image/png', 'JPEG':'image/jpeg', 'WEBP':'image/webp'}[meta['format']])
    return meta


def validate_saved(job, entry):
    if not isinstance(entry, dict) or not re.fullmatch(r'[1-9][0-9]*', str(entry.get('id', ''))):
        raise ValueError('Invalid image attachment ID')
    name = entry.get('path')
    if not isinstance(name, str) or not re.fullmatch(r'outputs/[1-9][0-9]*\.(png|jpg|webp)', name):
        raise ValueError('Invalid image attachment path')
    if Path(name).stem != str(entry['id']):
        raise ValueError('Image ID and filename disagree')
    path = no_symlinks(Path(job) / name)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('Image attachment missing or oversized')
    meta = describe(path.read_bytes())
    for key in ('bytes', 'sha256', 'width', 'height', 'mime_type', 'format'):
        if meta[key] != entry.get(key):
            raise ValueError('Image attachment integrity mismatch')
    if Path(name).suffix != {'PNG':'.png', 'JPEG':'.jpg', 'WEBP':'.webp'}[meta['format']]:
        raise ValueError('Image filename does not match decoded format')
    return dict(id=str(entry['id']), path=name, **meta)


def recover(job, conversation_id, started_at):
    """Only recover from a tool-authorized, matching fresh native conversation."""
    if not isinstance(conversation_id, str) or not re.fullmatch(r'[0-9a-f-]{36}', conversation_id):
        raise ValueError('Image conversation identity unverified')
    native = no_symlinks(Path.home() / '.gemini/antigravity-cli/brain' / conversation_id)
    audit_path = Path(job) / 'image-tool-audit.jsonl'
    if not audit_path.is_file():
        raise ValueError('Native image permission hook was not observed')
    audit = [json.loads(x) for x in audit_path.read_text().splitlines()]
    allowed = [x for x in audit if x.get('tool') == 'generate_image' and x.get('decision') == 'allow']
    if not allowed or any(x.get('conversation_id') != conversation_id for x in allowed):
        raise ValueError('Image tool invocation authorization unverified')
    if any(Path(x.get('artifact_directory', '')) != native for x in allowed):
        raise ValueError('Native image artifact root does not match conversation')
    results_path=Path(job)/'image-tool-results.jsonl'
    if not results_path.exists():
        raise ValueError('Native image completion hook was not observed')
    results={x['image_name']:x for x in (json.loads(x) for x in results_path.read_text().splitlines())}
    successful = {}
    for call in allowed:
        result=results.get(call['image_name'])
        if result is not None and result.get('conversation_id')==conversation_id and not result.get('error'):
            successful[call['slot']]=call
    if not successful:
        raise ValueError('No successfully completed native image attempts')
    paths = []
    slots = []
    # ImageName identifies the native generated basename. Do not recover other
    # screenshots, selected references, or old images in the conversation.
    for call in (successful[slot] for slot in sorted(successful)):
        stem = call['image_name']
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',stem):
            raise ValueError('Invalid authorized image basename')
        matching = []
        for p in native.glob(stem + '*'):
            if p.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
                continue
            no_symlinks(p)
            if not p.is_file() or p.stat().st_mtime < started_at - 2:
                continue
            matching.append(p)
        if len(matching)!=1 or matching[0] in paths:
            raise ValueError('Expected one fresh image per native call')
        paths.append(matching[0])
        slots.append(call['slot'])
    output = no_symlinks(Path(job) / 'outputs')
    output.mkdir(exist_ok=True, mode=0o700)
    entries, sources = [], []
    decoded = []
    for source in paths:
        if source.stat().st_size > MAX_BYTES:
            raise ValueError('Generated image exceeds size limit')
        data = source.read_bytes()
        meta = describe(data)
        decoded.append((data,meta))
    for index,source,(data,meta) in zip(slots,paths,decoded):
        target = output / (str(index) + {'PNG':'.png', 'JPEG':'.jpg', 'WEBP':'.webp'}[meta['format']])
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        entry = dict(id=str(index), path=str(target.relative_to(job)), **meta)
        entries.append(entry)
        sources.append({**entry, 'source_path':str(source)})
    from bridge import atomic_json
    atomic_json(Path(job) / 'generated-images.json', entries)
    atomic_json(Path(job) / 'generated-image-sources.json', sources)
    return entries

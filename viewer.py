"""Independent loopback observer. Never imports or starts the execution service."""
import argparse
import fcntl
import errno
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import secrets
import time
from urllib.parse import unquote, urlsplit, parse_qs
from urllib.request import Request

from bridge import ROOT, atomic_json, validate_job_id
from progress import snapshot

VERSION = '1.0.0'
_CHILDREN = []
READ_LIMIT = 16 * 1024 * 1024
ARTIFACTS = ('submission.json', 'prompt.txt', 'inputs.json', 'requested-profile.json',
             'report.json', 'result.md', 'acceptance.json')
PROFILE_KEYS = ('model', 'effort', 'timeout_seconds', 'model_execution')
SUBMISSION_KEYS = ('prompt', 'title', 'kind', 'acceptance', 'dispatch_mode',
                   'output_contract', 'files', 'thread_id', 'task_id','image_count','image_retry_limit', 'timeout_seconds', 'request_id', 'task_complexity', 'task_purpose', 'selection_reason', 'workspace')
SUMMARY_KEYS = ('job_id', 'status', 'title', 'kind', 'model', 'effort', 'review',
                'input_count', 'created_at', 'updated_at','execution_mode','delivery_count','workspace','model_execution')
INPUT_KEYS = ('name', 'sha256', 'bytes', 'kind')
IMAGE_KEYS = ('id', 'format', 'mime_type', 'width', 'height', 'bytes', 'sha256')
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"


def viewer_fingerprint(root=ROOT):
    digest=hashlib.sha256()
    for name in ('viewer.py','progress.py'):
        path=Path(root)/name
        digest.update(name.encode())
        if path.is_file():digest.update(path.read_bytes())
    return digest.hexdigest()


def source_fingerprint(root=ROOT):
    digest = hashlib.sha256()
    for name in ('viewer.py', 'progress.py', 'web/index.html', 'web/app.js', 'web/style.css'):
        path = root / name
        digest.update(name.encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def safe_path(base, relative, *, directory=False):
    """Reject symlinks in every component, including the configured base."""
    base = Path(base).absolute()
    relative = Path(relative)
    if relative.is_absolute() or not relative.parts or any(p in ('.', '..') for p in relative.parts):
        raise ValueError('Unsafe path')
    current = Path(base.anchor)
    for part in base.parts[1:] + relative.parts:
        current = current / part
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError('Unsafe path')
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        raise ValueError('Unsafe path')
    return current


def read_bytes(base, relative, *, limit=None):
    if limit is None:limit=READ_LIMIT
    path = safe_path(base, relative)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('Unsafe path')
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError('File exceeds observer read limit')
    return data


def pick(value, keys):
    return {k: value[k] for k in keys if k in value} if isinstance(value, dict) else None


class Observer:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.download_tickets={}
        self.download_lock=threading.Lock()

    def delivery_key(self):
        runtime=self.root/'.runtime';runtime.mkdir(exist_ok=True,mode=0o700)
        with (runtime/'delivery-ticket.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            path=runtime/'delivery-signing.key'
            if not path.exists():
                fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'wb') as output:output.write(secrets.token_bytes(32))
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise ValueError('Unsafe delivery signing key')
            key=path.read_bytes()
            if len(key)!=32:raise ValueError('Invalid delivery signing key')
            return key

    def delivery_entry(self,job_id,delivery_id):
        if not isinstance(delivery_id,str) or not delivery_id.isascii() or not delivery_id.isdigit():
            raise ValueError('Invalid delivery id')
        job=self.job(job_id)
        manifest=json.loads(read_bytes(job,'deliverables.json'))
        if not isinstance(manifest,list):raise ValueError('Invalid delivery manifest')
        entries=[v for v in manifest if isinstance(v,dict) and v.get('id')==delivery_id]
        if len(entries)!=1:raise ValueError('Unknown delivery')
        entry=entries[0]
        relative=Path(entry['path'])
        if relative.is_absolute() or not relative.parts or relative.parts[0]!='deliverables':
            raise ValueError('Delivery must be a frozen task file')
        path=safe_path(job,relative)
        if path.stat().st_nlink!=1 or path.stat().st_size!=entry['bytes']:
            raise ValueError('Delivery changed')
        return entry

    def delivery_ticket(self,job_id,entry):
        expiry=int(time.time())+86400
        payload=f"{job_id}:{entry['id']}:{expiry}:{entry['sha256']}".encode()
        return str(expiry)+'.'+hmac.new(self.delivery_key(),payload,hashlib.sha256).hexdigest()

    def delivery_ticket_valid(self,job_id,delivery_id,ticket):
        try:
            expiry,signature=ticket.split('.',1)
            if not expiry.isdigit() or not time.time()<int(expiry)<=time.time()+86401:return False
            entry=self.delivery_entry(job_id,delivery_id)
            payload=f"{job_id}:{delivery_id}:{expiry}:{entry['sha256']}".encode()
            return hmac.compare_digest(signature,hmac.new(self.delivery_key(),payload,hashlib.sha256).hexdigest())
        except (OSError,ValueError,TypeError,KeyError):return False

    def deliveries(self,job,job_id,warnings):
        if not (job/'deliverables.json').exists():return []
        manifest=self.read(job,'deliverables.json',warnings)
        if not isinstance(manifest,list):return []
        result=[]
        for row in manifest:
            try:
                entry=self.delivery_entry(job_id,row['id'])
                result.append(dict(pick(entry,('id','name','bytes','sha256')),
                    attachment_url=f"/api/jobs/{job_id}/deliveries/{entry['id']}?ticket={self.delivery_ticket(job_id,entry)}"))
            except (OSError,ValueError,TypeError,KeyError):warnings.append('delivery: missing, unsafe or changed')
        return result

    def delivery(self,job_id,delivery_id):
        entry=self.delivery_entry(job_id,delivery_id)
        data=read_bytes(self.job(job_id),entry['path'],limit=128*1024*1024)
        if hashlib.sha256(data).hexdigest()!=entry['sha256']:raise ValueError('Delivery hash changed')
        suffix=Path(entry['name']).suffix.lower()
        text={'.txt','.md','.json','.csv','.html','.svg','.py','.swift','.js','.ts','.tsx','.css','.patch'}
        mime=({'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp'}.get(suffix)
              or ('text/plain; charset=utf-8' if suffix in text else 'application/octet-stream'))
        extension=suffix if suffix[1:].isalnum() else '.bin'
        return data,mime,'delivery-'+delivery_id+extension

    def image_download_ticket(self,job_id,image_id):
        self.image(job_id,image_id)  # Verify a fixed image, never arbitrary paths.
        with self.download_lock:
            now=time.monotonic()
            self.download_tickets={k:v for k,v in self.download_tickets.items() if v[2]>now}
            if len(self.download_tickets)>=100:raise ValueError('Too many pending downloads')
            token=secrets.token_urlsafe(32)
            self.download_tickets[token]=(job_id,image_id,now+60)
        return {'url':'/downloads/'+token,'expires_in_seconds':60}

    def image_download(self,token):
        with self.download_lock:
            record=self.download_tickets.pop(token,None)
        if not record or record[2]<time.monotonic():raise ValueError('Download link expired')
        return self.image(record[0],record[1])

    def job(self, job_id):
        validate_job_id(job_id)
        return safe_path(self.root, Path('runs') / job_id, directory=True)

    def read(self, job, name, warnings, *, text=False):
        try:
            data = read_bytes(job, name).decode('utf-8')
            return data if text else json.loads(data)
        except (OSError, ValueError, UnicodeError):
            warnings.append(name + ': missing, unsafe, invalid, or too large')
            return None

    def inputs(self, manifest, job_id):
        if not isinstance(manifest, list):
            return None
        return [dict(pick(item, INPUT_KEYS) or {}, attachment_url=f'/api/jobs/{job_id}/inputs/{index}')
                for index, item in enumerate(manifest, 1)]

    def generated_images(self, job, job_id, warnings):
        manifest_path = job / 'generated-images.json'
        if not manifest_path.exists() and not manifest_path.is_symlink():
            return []
        manifest = self.read(job, 'generated-images.json', warnings)
        if not isinstance(manifest, list):
            warnings.append('generated images: invalid manifest')
            return []
        from image_artifacts import validate_saved
        images, seen = [], set()
        for entry in manifest:
            try:
                item = validate_saved(job, entry)
                image_id = item['id']
                if not isinstance(image_id, str) or not image_id.isascii() or not image_id.isdigit() or image_id in seen:
                    raise ValueError('Invalid image id')
                if sum(isinstance(other, dict) and other.get('id') == image_id for other in manifest) != 1:
                    raise ValueError('Duplicate image id')
                seen.add(image_id)
                images.append(dict(pick(item, IMAGE_KEYS),
                                   attachment_url=f'/api/jobs/{job_id}/images/{image_id}'))
            except (OSError, ValueError, KeyError, TypeError):
                warnings.append('generated image: missing, unsafe, or invalid')
        return images

    def public_report(self, report, images):
        if not isinstance(report, dict):
            return report
        value = dict(report)
        if 'generated_images' in value or images:
            value['generated_images'] = images
        if value.get('image_generation') or images:
            original = value.get('response','')
            value['response'] = self.public_image_text(original)
            value['response_paths_redacted'] = value['response'] != original
        return value

    @staticmethod
    def public_image_text(text):
        import re
        return re.sub(r'(?:file://)?/(?:[^\s<>\[\]()\"\']+/)*\.gemini/antigravity-cli/brain/[0-9a-f-]{36}/[^\s<>\[\]()\"\']+',
                      '（图片已回收，见图片预览）',str(text))

    def progress(self, job_id, job, state, warnings):
        if not isinstance(state, dict):
            return None
        try:
            for name in ('events.jsonl', 'stderr.log', 'requested-profile.json','live-progress.json'):
                if (job / name).exists() or (job / name).is_symlink():
                    safe_path(job, name)
            return snapshot(job_id, state, root=self.root)
        except (OSError, ValueError, KeyError, TypeError):
            warnings.append('progress: local evidence unavailable')
            return None

    def detail(self, job_id):
        job = self.job(job_id)
        warnings = []
        state = self.read(job, 'state.json', warnings)
        submission = pick(self.read(job, 'submission.json', warnings), SUBMISSION_KEYS)
        profile = pick(self.read(job, 'requested-profile.json', warnings), PROFILE_KEYS)
        inputs = self.inputs(self.read(job, 'inputs.json', warnings), job_id)
        images = self.generated_images(job, job_id, warnings)
        deliveries=self.deliveries(job,job_id,warnings)
        workspace=None
        if (job/'workspace.json').exists():
            value=self.read(job,'workspace.json',warnings)
            workspace=pick(value,('mode','read_roots','write_roots','output_directory','working_directory','commands','worktree','enforcement'))
        report = self.public_report(self.read(job, 'report.json', warnings), images)
        image_diagnostics = None
        if isinstance(state,dict) and state.get('kind')=='image':
            try:
                from image_artifacts import generation_summary
                image_diagnostics=generation_summary(job,state.get('image_count'))
            except (ValueError,OSError,TypeError,KeyError):
                warnings.append('image diagnosis: local evidence unavailable')
        acceptance = self.read(job, 'acceptance.json', warnings)
        prompt = self.read(job, 'prompt.txt', warnings, text=True)
        artifacts = []
        for name in ARTIFACTS:
            try:
                path = safe_path(job, name)
                artifacts.append({'name': name, 'bytes': path.stat().st_size,
                                  'url': f'/api/jobs/{job_id}/artifacts/{name}'})
            except (OSError, ValueError):
                pass
        return {'state': state, 'progress': self.progress(job_id, job, state, warnings),
                'submission': submission, 'profile': profile, 'inputs': inputs,
                'report': report, 'generated_images': images, 'image_diagnostics':image_diagnostics, 'acceptance': acceptance, 'prompt': prompt,
                'workspace':workspace,'deliverables':deliveries,
                'artifacts': artifacts, 'warnings': warnings}

    def list(self):
        records, warnings = [], []
        try:
            runs = safe_path(self.root, 'runs', directory=True)
        except (OSError, ValueError):
            return {'jobs': [], 'warnings': ['runs: unavailable']}
        for candidate in runs.iterdir():
            try:
                job = self.job(candidate.name)
                state = self.read(job, 'state.json', warnings)
                if not isinstance(state, dict) or not isinstance(state.get('status'), str):
                    continue
                row = pick(state, SUMMARY_KEYS)
                row['job_id'] = candidate.name
                records.append(row)
            except (OSError, ValueError):
                warnings.append('A run entry was skipped')
        def updated(row):
            value = row.get('created_at', 0)
            return value if type(value) in (int, float) else 0
        records.sort(key=updated, reverse=True)
        records = records[:50]
        for row in records:
            if row.get('status') in ('queued', 'running', 'cancelling', 'failed_or_unverified', 'timeout'):
                job = self.job(row['job_id'])
                state = self.read(job, 'state.json', warnings)
                row['progress'] = self.progress(row['job_id'], job, state, warnings)
        return {'jobs': records, 'warnings': warnings}

    def attachment(self, job_id, index):
        job = self.job(job_id)
        manifest = json.loads(read_bytes(job, 'inputs.json'))
        if not isinstance(manifest, list) or not 1 <= index <= len(manifest):
            raise ValueError('Unknown attachment')
        item = manifest[index - 1]
        staged = Path(item['staged_path'])
        # Only the manifest's already staged copy; never the submission source.
        inputs = safe_path(job, 'inputs', directory=True)
        if not staged.is_absolute():
            staged = job / staged
        relative = staged.relative_to(inputs)
        data = read_bytes(inputs, relative)
        suffix = staged.suffix.lower()
        mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.gif': 'image/gif', '.webp': 'image/webp'}.get(suffix, 'text/plain; charset=utf-8')
        return data, mime

    def artifact(self, job_id, name):
        if name not in ARTIFACTS:
            raise ValueError('Unknown artifact')
        job = self.job(job_id)
        data = read_bytes(job, name)
        if name == 'result.md':
            state = self.read(job,'state.json',[])
            if isinstance(state,dict) and state.get('kind') == 'image':
                data = self.public_image_text(data.decode('utf-8')).encode()
        if name in ('submission.json', 'requested-profile.json', 'inputs.json', 'report.json'):
            value = json.loads(data)
            if name == 'submission.json': value = pick(value, SUBMISSION_KEYS)
            elif name == 'requested-profile.json': value = pick(value, PROFILE_KEYS)
            elif name == 'report.json': value = self.public_report(value, self.generated_images(job, job_id, []))
            else: value = self.inputs(value, job_id)
            data = json.dumps(value, ensure_ascii=False, indent=2).encode()
        mime = 'application/json; charset=utf-8' if name.endswith('.json') else 'text/plain; charset=utf-8'
        return data, mime

    def image(self, job_id, image_id):
        from image_artifacts import validate_saved
        job = self.job(job_id)
        manifest = json.loads(read_bytes(job, 'generated-images.json'))
        if not isinstance(manifest, list):
            raise ValueError('Unknown image')
        entries = [entry for entry in manifest if isinstance(entry, dict) and entry.get('id') == image_id]
        if len(entries) != 1:
            raise ValueError('Unknown image')
        item = validate_saved(job, entries[0])
        data = read_bytes(job, item['path'])
        if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('Image changed')
        extension = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp'}[item['mime_type']]
        return data, item['mime_type'], f'image-{image_id}.{extension}'


def make_server(root=ROOT, port=0):
    observer = Observer(root)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, data, code=200, mime='application/json; charset=utf-8', attachment=False):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            for name, value in (('Content-Type', mime), ('Content-Length', str(len(data))),
                                ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
                                ('Referrer-Policy', 'no-referrer'), ('Content-Security-Policy', CSP)):
                self.send_header(name, value)
            if attachment:
                if isinstance(attachment,str) and attachment.startswith('download:'):
                    disposition=f'attachment; filename="{attachment[9:]}"'
                else:
                    disposition = 'attachment' if attachment is True else f'inline; filename="{attachment}"'
                self.send_header('Content-Disposition', disposition)
            self.end_headers()
            self.wfile.write(data)

        def authorized(self, api=False):
            host = '127.0.0.1:' + str(self.server.server_port)
            origins = self.headers.get_all('Origin', [])
            return (self.headers.get_all('Host', []) == [host]
                    and (not origins or origins == ['http://' + host])
                    and (not api or self.headers.get_all('X-Bridge-Viewer', []) == ['1']))

        def do_GET(self):
            split=urlsplit(self.path)
            path = unquote(split.path)
            parts=path.split('/')
            ticket=parse_qs(split.query).get('ticket',[])
            signed_delivery=(len(parts)==6 and parts[1:3]==['api','jobs'] and parts[4]=='deliveries'
                             and len(ticket)==1 and observer.delivery_ticket_valid(parts[3],parts[5],ticket[0]))
            if not self.authorized(path.startswith('/api/') and not signed_delivery):
                return self.send({'error': 'Forbidden'}, 403)
            try:
                if path == '/favicon.ico':
                    return self.send(b'', code=204)
                if path == '/health':
                    return self.send({'observer': True, 'version': VERSION})
                if path == '/api/jobs':
                    return self.send(observer.list())
                parts = path.split('/')
                if len(parts)==3 and parts[1]=='downloads':
                    data,mime,filename=observer.image_download(parts[2])
                    return self.send(data,mime=mime,attachment='download:'+filename)
                if len(parts) >= 4 and parts[1:3] == ['api', 'jobs']:
                    job_id = validate_job_id(parts[3])
                    if len(parts) == 4:
                        return self.send(observer.detail(job_id))
                    if len(parts) == 6 and parts[4] == 'inputs' and parts[5].isdigit():
                        data, mime = observer.attachment(job_id, int(parts[5]))
                        return self.send(data, mime=mime)
                    if len(parts)==6 and parts[4]=='deliveries':
                        data,mime,filename=observer.delivery(job_id,parts[5])
                        return self.send(data,mime=mime,attachment=('download:'+filename) if mime=='application/octet-stream' else filename)
                    if len(parts) == 6 and parts[4] == 'images' and parts[5].isascii() and parts[5].isdigit():
                        data, mime, filename = observer.image(job_id, parts[5])
                        return self.send(data, mime=mime, attachment=filename)
                    if len(parts)==7 and parts[4]=='images' and parts[5].isascii() and parts[5].isdigit() and parts[6]=='download':
                        return self.send(observer.image_download_ticket(job_id,parts[5]))
                    if len(parts) == 6 and parts[4] == 'artifacts':
                        data, mime = observer.artifact(job_id, parts[5])
                        return self.send(data, mime=mime, attachment=True)
                    raise ValueError('Unknown route')
                static = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/index.html': ('index.html', 'text/html; charset=utf-8'),
                          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/style.css': ('style.css', 'text/css; charset=utf-8')}
                if path in static:
                    name, mime = static[path]
                    return self.send(read_bytes(Path(root) / 'web', name), mime=mime)
                return self.send({'error': 'Not found'}, 404)
            except (OSError, ValueError, KeyError, TypeError, UnicodeError):
                return self.send({'error': 'Not found or unavailable'}, 404)

        def reject_write(self):
            self.close_connection = True
            self.send({'error': 'Read-only observer'}, 405 if self.authorized() else 403)
        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = do_HEAD = reject_write
    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except OSError as error:
        if not port or error.errno != errno.EADDRINUSE:
            raise
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.port_changed = bool(port and server.server_port != port)
    server.daemon_threads = True
    return server


def discovery(root=ROOT):
    try:
        value = json.loads(read_bytes(Path(root) / '.runtime', 'viewer.json'))
        port, pid = value['port'], value['pid']
        if type(port) is not int or not 1 <= port <= 65535 or type(pid) is not int or pid <= 0:
            return None
        url = f'http://127.0.0.1:{port}/'
        if value.get('url') != url or value.get('viewer_fingerprint') != viewer_fingerprint(Path(root)):
            return None
        os.kill(pid, 0)
        # Ignore proxy environment for the loopback probe.
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(Request(url + 'health'), timeout=1) as response:
            health = json.loads(response.read(4096))
        if health != {'observer': True, 'version': VERSION}:
            return None
        value['source_fingerprint'] = source_fingerprint(Path(root))
        return {k: value[k] for k in ('url', 'pid', 'port', 'version', 'source_fingerprint', 'port_changed')}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def ensure_running(root=ROOT):
    root = Path(root)
    found = discovery(root)
    if found:
        return found
    runtime = root / '.runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    with (runtime / 'viewer-start.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        found = discovery(root)
        if found:
            return found
        # Only restart a process whose command identifies this observer and root.
        preferred_port = 0
        try:
            previous = json.loads(read_bytes(runtime, 'viewer.json'))
            old_port = previous.get('port')
            if type(old_port) is int and 1 <= old_port <= 65535:
                preferred_port = old_port
            os.kill(previous['pid'], 0)
        except (OSError, ValueError, KeyError, TypeError):
            previous = None
        if previous:
            import urllib.request
            old_port = previous.get('port')
            if type(old_port) is not int or not 1 <= old_port <= 65535:
                raise RuntimeError('Existing observer identity cannot be verified')
            old_url = f'http://127.0.0.1:{old_port}/'
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(old_url + 'health', timeout=1) as response:
                    old_health = json.loads(response.read(4096))
            except (OSError, ValueError):
                raise RuntimeError('Existing observer identity cannot be verified') from None
            if old_health != {'observer': True, 'version': previous.get('version')}:
                raise RuntimeError('Existing observer identity cannot be verified')
            expected = str(ROOT / 'viewer.py') + ' --serve --port ' + str(previous.get('requested_port', 0)) + ' --root ' + str(root)
            command = subprocess.run(['/bin/ps', '-p', str(previous['pid']), '-o', 'command='],
                                     capture_output=True, text=True, timeout=2, check=False).stdout.strip()
            legacy_expected = str(ROOT / 'viewer.py') + ' --serve --root ' + str(root)
            if not (command.endswith(' ' + expected) or ('requested_port' not in previous and command.endswith(' ' + legacy_expected))):
                raise RuntimeError('Existing observer identity cannot be verified; explicit restart required')
            os.kill(previous['pid'], signal.SIGTERM)
            # Wait for the observer lock to be released before starting its replacement.
            with (runtime / 'viewer.lock').open('a') as observer_lock:
                for _ in range(100):
                    try:
                        fcntl.flock(observer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(0.05)
                else:
                    raise RuntimeError('Existing observer did not stop')
                fcntl.flock(observer_lock, fcntl.LOCK_UN)
        with (runtime / 'viewer.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, str(ROOT / 'viewer.py'), '--serve', '--port', str(preferred_port), '--root', str(root)],
                                       cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True)
            _CHILDREN[:] = [child for child in _CHILDREN if child.poll() is None]
            _CHILDREN.append(process)
        for _ in range(100):
            found = discovery(root)
            if found:
                return found
            if process.poll() is not None:
                break
            time.sleep(0.05)
        raise RuntimeError('Read-only observer did not become ready')


def job_url(job_id, root=ROOT):
    validate_job_id(job_id)
    return ensure_running(root)['url'] + '?job=' + job_id



def attach_links(value, root=ROOT):
    """Decorate existing results without allowing observer failures to affect execution."""
    if not isinstance(value, dict):
        return value
    state = value.get('state')
    has_job = 'job_id' in value or (isinstance(state, dict) and 'job_id' in state)
    job_id = value['job_id'] if 'job_id' in value else state.get('job_id') if isinstance(state, dict) else None
    rows = value.get('jobs')
    if not has_job and not isinstance(rows, list):
        return value
    result = dict(value)
    try:
        if has_job:
            validate_job_id(job_id)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get('job_id') is not None:
                    validate_job_id(row['job_id'])
        base_url = ensure_running(root)['url']
        result['viewer_url'] = base_url
        if job_id is not None:
            result['task_view_url'] = base_url + '?job=' + job_id
        if isinstance(rows, list):
            result['jobs'] = [dict(row, task_view_url=base_url + '?job=' + row['job_id'])
                              if isinstance(row, dict) and row.get('job_id') is not None else row
                              for row in rows]
    except Exception:
        # Fixed message: exceptions may contain local paths or discovery details.
        result['viewer_error'] = 'Read-only observer unavailable'
    return result


def serve(root=ROOT, port=0):
    root = Path(root)
    runtime = root / '.runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    with (runtime / 'viewer.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        server = make_server(root, port)
        info = {'url': f'http://127.0.0.1:{server.server_port}/', 'pid': os.getpid(),
                'port': server.server_port, 'version': VERSION, 'source_fingerprint': source_fingerprint(root),
                'viewer_fingerprint': viewer_fingerprint(root), 'port_changed': server.port_changed, 'requested_port': port}
        atomic_json(runtime / 'viewer.json', info)
        def stop(signum, frame):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            server.server_close()
            # Keep the private discovery record so a later start can reuse its port.
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--job-id')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    if args.serve:
        if not 0 <= args.port <= 65535:
            parser.error('Invalid observer port')
        serve(args.root, args.port)
    elif args.job_id:
        print(job_url(args.job_id, args.root))
    else:
        print(json.dumps(ensure_running(args.root)))


if __name__ == '__main__':
    main()

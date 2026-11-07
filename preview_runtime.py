"""Trusted static-preview controller. Model HTML executes only in Chromium."""
from __future__ import annotations
import base64
import fcntl
import hashlib
import http.server
import json
import mimetypes
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid

from preview_spec import parse_request, record_preflight_failure, validate_actions
from workspace_guard import _atomic_record, _digest, _path, _SandboxProcesses

EXTENSIONS = {'.html', '.htm', '.css', '.js', '.mjs', '.json', '.png', '.jpg', '.jpeg',
              '.gif', '.webp', '.svg', '.ico', '.avif', '.woff', '.woff2', '.ttf', '.otf', '.txt', '.map'}
VIEWPORTS = {'desktop': {'width': 1280, 'height': 900}, 'mobile': {'width': 390, 'height': 844}}
MAX_CAPTURE_HEIGHT=12000
MAX_CAPTURE_WIDTH=4096
MAX_CAPTURE_PIXELS=24000000
ACTION_TIMEOUT_MS=2000


def _open_regular(path):
    parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent); parent = next_fd
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)


def freeze(policy, root, target, check=None):
    """Copy bounded ordinary static files using no-follow descriptors; never serve source."""
    manifest = []
    total = 0
    visited = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        if check: check()
        visited += 1
        if visited > 2000: raise ValueError('Too many snapshot directories')
        for name in list(dirs):
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError('Snapshot symlink is forbidden')
            try:
                _path(policy, str(path / '.preview-scope-check'), write=True)
            except ValueError:
                dirs.remove(name)
        for name in sorted(files):
            if check: check()
            source = Path(directory) / name
            if source.is_symlink():
                raise ValueError('Snapshot symlink is forbidden')
            if source.is_file() and source.stat().st_nlink != 1:
                raise ValueError('Snapshot hard link is forbidden')
            try:
                _path(policy, str(source), write=True)
            except ValueError:
                continue  # Protected and credential files are never copied.
            if source.suffix.lower() not in EXTENSIONS:
                continue
            fd = _open_regular(source)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise ValueError('Snapshot requires ordinary singly linked files')
                if len(manifest) >= 1000 or total + before.st_size > 64 * 1024 * 1024:
                    raise ValueError('Preview snapshot exceeds limits')
                with os.fdopen(fd, 'rb', closefd=False) as stream:
                    data = stream.read(64 * 1024 * 1024 - total + 1)
                after = os.fstat(fd)
                if len(data) != before.st_size or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError('Snapshot source changed during read')
            finally:
                os.close(fd)
            total += len(data)
            relative = source.relative_to(root)
            output = target / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
            manifest.append({'path': relative.as_posix(), 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    return manifest


def _network_profile(port, output, marker_rules):
    return ('(version 1)\n(allow default)\n'
            '(deny network*)\n(allow network-outbound (remote tcp \"localhost:' + str(port) + '\"))\n'
            '(deny network-bind)\n(deny file-write* (subpath ' + json.dumps(str(output.parent), ensure_ascii=False) + '))\n' + marker_rules)


def perform_actions(page, actions, events, initial_url, on_result=None):
    """Fixed CSS-only interactions; model input never becomes JavaScript or URLs."""
    endpoint=urllib.parse.urlsplit(initial_url)
    allowed_prefix=endpoint.scheme+'://'+endpoint.netloc+'/'
    results=[]
    for index,item in enumerate(actions):
        kind=item['action']
        record={'index':index,'action':kind,'selector':item['selector'],'status':'failed'}
        prior=(events['blocked_navigations'],events['popups'])
        try:
            locator=page.locator('css='+item['selector'])
            if kind=='click':locator.click(timeout=ACTION_TIMEOUT_MS)
            elif kind=='fill':locator.fill(item['value'],timeout=ACTION_TIMEOUT_MS)
            elif kind=='select':locator.select_option(value=item['value'],timeout=ACTION_TIMEOUT_MS)
            elif kind=='check':locator.set_checked(item['checked'],timeout=ACTION_TIMEOUT_MS)
            elif kind=='press':locator.press(item['key'],timeout=ACTION_TIMEOUT_MS)
            elif kind=='wait_for':locator.wait_for(state=item['state'],timeout=ACTION_TIMEOUT_MS)
            page.wait_for_timeout(100)
            if prior!=(events['blocked_navigations'],events['popups']):raise ValueError('External navigation or popup was blocked')
            if not page.url.startswith(allowed_prefix):raise ValueError('Navigation left the preview endpoint')
            record['status']='passed'
            if kind!='wait_for' or item['state'] not in ('hidden','detached'):
                try:
                    if locator.count()==1:
                        record['observed']=locator.evaluate("e => ({text:(e.innerText||e.textContent||'').slice(0,256),checked:typeof e.checked==='boolean'?e.checked:null,selected:e.tagName==='SELECT'?e.value:null})",timeout=250)
                    else:record['observation_unavailable']=True
                except Exception:record['observation_unavailable']=True
        except Exception as error:
            record['status']='failed'
            record['error']=type(error).__name__+': '+str(error).split('Call log:')[0][:512]
        if not page.url.startswith(allowed_prefix):
            try:
                page.goto(initial_url,wait_until='load',timeout=15000)
                record['page_restored_after_block']=True
            except Exception as error:
                record['status']='failed'
                record['restore_error']=type(error).__name__+': '+str(error)[:256]
        results.append(record)
        if on_result:on_result(list(results))
    return results


def capture_page(page,png):
    """Whole document with explicit pixel/length bounds and measured coverage."""
    metrics=page.evaluate("""() => ({scrollWidth:Math.max(document.documentElement.scrollWidth,document.body?.scrollWidth||0),scrollHeight:Math.max(document.documentElement.scrollHeight,document.body?.scrollHeight||0),innerWidth,innerHeight,horizontalOverflow:document.documentElement.scrollWidth>innerWidth,verticalOverflow:document.documentElement.scrollHeight>innerHeight,title:document.title.slice(0,1024),textPreview:(document.body?.innerText||'').slice(0,4096)})""")
    width=min(MAX_CAPTURE_WIDTH,max(metrics['innerWidth'],metrics['scrollWidth']))
    height=min(MAX_CAPTURE_HEIGHT,max(metrics['innerHeight'],metrics['scrollHeight']),MAX_CAPTURE_PIXELS//width)
    truncated=width<metrics['scrollWidth'] or height<metrics['scrollHeight']
    page.evaluate('() => scrollTo(0,0)')
    options={'path':str(png),'full_page':True,'timeout':10000,'animations':'disabled'}
    # Always bound the capture rectangle, including pages that resize while
    # screenshot animations settle. A changing page cannot bypass the pixel cap.
    options['clip']={'x':0,'y':0,'width':width,'height':height}
    page.screenshot(**options)
    after=page.evaluate('() => ({width:Math.max(document.documentElement.scrollWidth,document.body?.scrollWidth||0),height:Math.max(document.documentElement.scrollHeight,document.body?.scrollHeight||0)})')
    metrics['scrollWidth']=max(metrics['scrollWidth'],after['width'])
    metrics['scrollHeight']=max(metrics['scrollHeight'],after['height'])
    metrics['horizontalOverflow']=metrics['scrollWidth']>metrics['innerWidth']
    metrics['verticalOverflow']=metrics['scrollHeight']>metrics['innerHeight']
    data=png.read_bytes()
    # PNG IHDR supplies actual exported pixels, rather than assuming viewport coverage.
    actual_width=int.from_bytes(data[16:20],'big');actual_height=int.from_bytes(data[20:24],'big')
    truncated=truncated or actual_width<metrics['scrollWidth'] or actual_height<metrics['scrollHeight']
    coverage={'mode':'bounded_document' if truncated else 'full_page','full_page':not truncated and actual_width>=metrics['scrollWidth'] and actual_height>=metrics['scrollHeight'],
              'truncated':truncated,'document_width':metrics['scrollWidth'],'document_height':metrics['scrollHeight'],
              'captured_width':actual_width,'captured_height':actual_height,'origin':{'x':0,'y':0},
              'limits':{'max_width':MAX_CAPTURE_WIDTH,'max_height':MAX_CAPTURE_HEIGHT,'max_pixels':MAX_CAPTURE_PIXELS}}
    if truncated:coverage['notice']='页面超过预览上限，截图仅覆盖标明区域，不能作为全页验收。'
    return metrics,coverage,data


def _worker(spec_path):
    from playwright.sync_api import sync_playwright
    spec = json.loads(Path(spec_path).read_text())
    snapshot, output = Path(spec['snapshot']), Path(spec['output'])
    actions=validate_actions({'actions':spec.get('actions',[])})
    token = uuid.uuid4().hex
    events = {'blocked_requests': [], 'page_errors': [], 'console_errors': [], 'popups': 0, 'downloads': 0,
              'diagnostic_counts': {'blocked_requests': 0, 'page_errors': 0, 'console_errors': 0}, 'blocked_navigations':0}
    def sample(kind, item):
        events['diagnostic_counts'][kind] += 1
        if len(events[kind]) < 100:
            events[kind].append(item)
    def console(message):
        if message.type == 'error':
            text = message.text[:1024]
            sample('console_errors', text)
            if 'Content Security Policy' in text or 'violates' in text:
                sample('blocked_requests', {'reason': 'csp', 'message': text})
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_HEAD(self):
            self.do_GET(head=True)
        def do_GET(self, head=False):
            if self.headers.get('Upgrade') or 'upgrade' in self.headers.get('Connection', '').lower():
                sample('blocked_requests', {'reason': 'upgrade', 'path': self.path[:1024]})
                self.send_error(403); return
            if self.headers.get('Host') != self.server.expected_host:
                self.send_error(403); return
            parsed = urllib.parse.urlsplit(self.path)
            raw = urllib.parse.unquote(parsed.path)
            prefix = '/' + token + '/'
            if not raw.startswith(prefix) or '\\' in raw or '\x00' in raw:
                self.send_error(403); return
            relative = raw[len(prefix):]
            if any(part in {'.', '..', ''} for part in relative.split('/')):
                self.send_error(403); return
            path = snapshot / relative
            if not path.is_file() or path.is_symlink():
                self.send_error(404); return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', mimetypes.guess_type(path)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            if not head:
                self.wfile.write(data)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    server.expected_host = '127.0.0.1:' + str(port)
    origin = 'http://' + server.expected_host
    url = origin + '/' + token + '/' + urllib.parse.quote(spec['entry'], safe='/')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    progress = {'url': url, 'port': port, 'worker_pid': os.getpid()}
    _atomic_record(output / 'progress.json', progress)
    profile = Path(spec['temp']) / 'chromium.sb'
    # Kernel egress rule complements Playwright request interception (including WebRTC).
    profile.write_text(_network_profile(port, output, spec['marker_rules']))
    browser = context = None
    try:
        with sync_playwright() as playwright:
            wrapper = Path(spec['temp']) / 'chromium-wrapper'
            executable = Path(playwright.chromium.executable_path)
            installation = next(p for p in executable.parents if re.fullmatch(r'chromium-[0-9]+', p.name))
            shell_root = installation.parent / installation.name.replace('chromium-', 'chromium_headless_shell-')
            shells = list(shell_root.glob('*/chrome-headless-shell'))
            if len(shells) != 1: raise ValueError('Matching Chromium headless shell unavailable')
            wrapper.write_text('#!/bin/sh\nexec /usr/bin/sandbox-exec -f ' + shlex.quote(str(profile)) + ' ' + shlex.quote(str(shells[0])) + ' "$@"\n')
            wrapper.chmod(0o700)
            browser = playwright.chromium.launch(executable_path=str(wrapper), headless=True,
                args=['--disable-background-networking', '--disable-component-update', '--disable-sync',
                      '--force-webrtc-ip-handling-policy=disable_non_proxied_udp', '--disable-features=WebRtcHideLocalIpsWithMdns'])
            def route(request_route):
                request = request_route.request
                parsed = urllib.parse.urlsplit(request.url)
                allowed = (parsed.scheme == 'http' and parsed.netloc == server.expected_host
                           and request.method in {'GET', 'HEAD'}
                           and request.resource_type not in {'websocket', 'eventsource'})
                if allowed:
                    rewritten = request.url if parsed.path.startswith('/' + token + '/') else origin + '/' + token + parsed.path + (('?' + parsed.query) if parsed.query else '')
                    request_route.continue_(url=rewritten)
                else:
                    if request.is_navigation_request():events['blocked_navigations']+=1
                    sample('blocked_requests', {'url': request.url[:1024], 'method': request.method})
                    request_route.abort('blockedbyclient')
            pages = {}
            for label, viewport in VIEWPORTS.items():
                context=browser.new_context(service_workers='block',accept_downloads=False,permissions=[])
                context.route('**/*',route)
                page = context.new_page()
                page.set_viewport_size(viewport)
                page.on('console', console)
                page.on('pageerror', lambda error: sample('page_errors', str(error)[:1024]))
                def popup(p):
                    events['popups'] += 1; p.close()
                page.on('popup', popup)
                page.on('download', lambda download: (events.__setitem__('downloads', events['downloads'] + 1), download.cancel()))
                page.on('dialog', lambda dialog: dialog.dismiss())
                print('Rendering ' + label + ': navigate', flush=True)
                page.goto(url, wait_until='load', timeout=15000)
                print('Rendering ' + label + ': settled', flush=True)
                page.wait_for_timeout(500)
                if not page.url.startswith(origin + '/' + token + '/'):
                    raise ValueError('Preview navigated away from entry')
                print('Rendering '+label+': actions '+str(len(actions)),flush=True)
                def action_progress(rows):
                    progress.setdefault('viewport_actions',{})[label]=rows
                    progress['phase']='actions';progress['viewport']=label
                    _atomic_record(output/'progress.json',progress)
                action_progress([])
                action_results=perform_actions(page,actions,events,url,action_progress)
                png=output/(label+'.png')
                print('Rendering '+label+': full-page screenshot',flush=True)
                metrics,coverage,data=capture_page(page,png)
                pages[label]={'viewport':viewport,'path':str(png),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),
                              'dom':metrics,'actual_url':page.url,'actions':action_results,
                              'actions_passed':all(a['status']=='passed' for a in action_results),'coverage':coverage}
                page.close()
                context.close();context=None
            print('Closed isolated viewport contexts', flush=True)
            print('Closing browser', flush=True)
            browser.close(); browser = None
            result = {**progress, 'viewports': pages, **events}
            _atomic_record(output / 'render.json', result)
    finally:
        if context:
            context.close()
        if browser:
            browser.close()
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    return 0


def _cancelled(policy):
    job = Path(policy['job_dir'])
    if any((job / name).exists() for name in ('cancel.requested', 'cancel.flag')):
        return 143
    try:
        state = json.loads((job / 'state.json').read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return 143
    if not isinstance(state, dict):
        return 143
    if state.get('status') == 'timeout':
        return 124
    if state.get('cancel_requested') or state.get('status') in {'cancelled', 'cancelling', 'interrupted', 'failed_or_unverified', 'bridge_error'}:
        return 143


def run_preview(policy, argv, cwd, record_id, args_sha256=None):
    spec = parse_request(policy, argv, cwd)
    if not isinstance(record_id, str) or not re.fullmatch('[0-9a-f]{64}', record_id):
        raise ValueError('Preview requires a stable record ID')
    job = Path(policy['job_dir']).resolve()
    guard = json.loads((job / 'workspace-guard-state.json').read_text())
    if guard.get('command_requests', {}).get(record_id) != {'argv': argv, 'cwd': cwd, 'args_sha256': args_sha256}:
        raise ValueError('Preview does not match an approved native request')
    raw_directory = Path(policy['command_results_dir'])
    directory = raw_directory.resolve()
    if directory != job / 'command-results' or raw_directory.is_symlink():
        raise ValueError('Unexpected preview results directory')
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / (record_id + '.json')
    fingerprint = _digest({'argv': argv, 'cwd': str(Path(cwd).resolve()), 'args_sha256': args_sha256})
    with (directory / (record_id + '.lock')).open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            previous = json.loads(path.read_text())
            if previous.get('request_sha256') != fingerprint:
                raise ValueError('Preview record ID conflicts with request')
            previous_code = previous.get('runner_exit_code')
            return previous_code if type(previous_code) is int else 126
        output = directory / record_id
        output.mkdir(mode=0o700)
        record = {'record_id': record_id, 'status': 'running', 'started_at': time.time(),
                  'request_sha256': fingerprint, 'args_sha256': args_sha256, 'executable': 'bridge-preview',
                  'argc': len(argv), 'cwd': str(Path(cwd).resolve()), 'stdout_path': str(directory / (record_id + '.stdout.log')),
                  'stderr_path': str(directory / (record_id + '.stderr.log')), 'logs_complete': False}
        _atomic_record(path, record)
        code, status, process, members = 126, 'failed', None, None
        old = {}
        temp = None
        cancelled_signal = []
        observed_browser_pids = set()
        def cancel(signum, frame):
            cancelled_signal.append(signum)
        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                old[sig] = signal.signal(sig, cancel)
            deadline = time.monotonic() + min(90, max(1, int(policy.get('command_timeout_seconds', 90))))
            temporary_root = job / 'preview-runtime'
            if temporary_root.is_symlink(): raise ValueError('Preview control directory symlink')
            temporary_root.mkdir(mode=0o700, exist_ok=True)
            temp = Path(tempfile.mkdtemp(prefix='preview-', dir=temporary_root))
            snapshot = temp / 'snapshot'; snapshot.mkdir()
            def check():
                if cancelled_signal or _cancelled(policy): raise InterruptedError('Preview cancelled')
                if time.monotonic() >= deadline: raise TimeoutError('Preview timed out')
            sources = freeze(policy, Path(spec['root']), snapshot, check)
            if not (snapshot / Path(spec['entry']).name).is_file():
                raise ValueError('Entry was not included in static snapshot')
            control = temp / 'control'; control.mkdir()
            members = _SandboxProcesses(control); members.pids()
            worker_spec = temp / 'worker.json'
            worker_spec.write_text(json.dumps({'snapshot': str(snapshot), 'output': str(output), 'temp': str(temp),
                'entry': Path(spec['entry']).name, 'marker_rules': members.rules(), 'actions':spec.get('actions',[])}))
            python = str(Path(__file__).parent / '.venv-preview/bin/python')
            with open(record['stdout_path'], 'wb') as stdout, open(record['stderr_path'], 'wb') as stderr:
                if _cancelled(policy) or cancelled_signal:
                    code, status = 143, 'cancelled'
                else:
                    process = subprocess.Popen([python, str(Path(__file__).resolve()), '--worker', str(worker_spec)],
                        stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True,
                        env={**{k: v for k, v in os.environ.items() if k in {'PATH', 'HOME', 'PLAYWRIGHT_BROWSERS_PATH', 'LANG'}}, 'TMPDIR': str(temp)})
                    while process.poll() is None:
                        observed_browser_pids.update(members.pids())
                        stop = _cancelled(policy)
                        if cancelled_signal or stop or time.monotonic() >= deadline:
                            code = stop or (143 if cancelled_signal else 124)
                            status = 'timeout' if code == 124 else 'cancelled'
                            break
                        time.sleep(0.05)
                    else:
                        code = process.returncode
                        status = 'exited' if code == 0 else 'failed'
            record['source_files'] = sources
        except Exception as error:
            if isinstance(error, TimeoutError): code, status = 124, 'timeout'
            if isinstance(error, InterruptedError): code, status = 143, 'cancelled'
            Path(record['stderr_path']).write_text(type(error).__name__ + ': ' + str(error) + '\n')
        finally:
            if process and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
            remaining = []
            if members:
                members.terminate(); remaining = members.pids()
            cleanup = {'worker_exited': process is None or process.poll() is not None, 'browser_pids_remaining': remaining, 'browser_pids_observed': sorted(observed_browser_pids)}
            if temp:
                shutil.rmtree(temp)
            cleanup['temporary_directory_removed'] = temp is None or not temp.exists()
            progress_path = output / 'progress.json'
            if progress_path.exists():
                import socket
                progress = json.loads(progress_path.read_text())
                with socket.socket() as probe:
                    probe.settimeout(0.2)
                    cleanup['listener_closed'] = probe.connect_ex(('127.0.0.1', progress['port'])) != 0
            else:
                cleanup['listener_closed'] = True
            if cancelled_signal and code == 0:
                code, status = 143, 'cancelled'
            complete = all(cleanup.get(k) for k in ('worker_exited', 'temporary_directory_removed', 'listener_closed')) and not remaining
            if not complete or (code == 0 and not observed_browser_pids):
                code, status = 126, 'failed'
            render = output / 'render.json'
            cleanup.update(browser_closed=not remaining, server_closed=cleanup['listener_closed'])
            rendered = json.loads(render.read_text()) if render.exists() else None
            screenshots = []
            if rendered and complete and code == 0:
                screenshots = [dict(item, name=label + '.png') for label, item in rendered['viewports'].items()]
            entry_sha = next((item['sha256'] for item in record.get('source_files', []) if item['path'] == Path(spec['entry']).name), None)
            manifest = {'action_progress':json.loads(progress_path.read_text()).get('viewport_actions',{}) if progress_path.exists() else {}, 'actions_source':spec.get('actions_source'), 'entry': spec['entry'], 'entry_sha256': entry_sha, 'record_id': record_id, 'args_sha256': args_sha256, 'screenshots': screenshots, 'root': spec['root'], 'source_files': record.get('source_files', []),
                        'cleanup': cleanup, 'render': json.loads(render.read_text()) if render.exists() else None}
            _atomic_record(output / 'manifest.json', manifest)
            record.update(status=status, runner_exit_code=code, exit_code=code, finished_at=time.time(),
                          logs_complete=complete, manifest_path=str(output / 'manifest.json'), cleanup=cleanup)
            if screenshots:
                record['preview'] = {'manifest_path': str(output / 'manifest.json')}
            Path(record['stdout_path']).touch(exist_ok=True)
            with open(record['stdout_path'], 'a') as log:
                for label in VIEWPORTS:
                    png = output / (label + '.png')
                    if png.exists():
                        message = 'Preview screenshot (' + label + '): ' + str(png)
                        log.write(message + '\n'); print(message, flush=True)
            Path(record['stderr_path']).touch(exist_ok=True)
            _atomic_record(path, record)
            for sig, handler in old.items():
                signal.signal(sig, handler)
        return code


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--worker':
        return _worker(sys.argv[2])
    if len(sys.argv) != 3:
        raise ValueError('Expected policy and base64url request')
    policy = json.loads(Path(sys.argv[1]).read_text())
    request = json.loads(base64.b64decode(sys.argv[2], altchars=b'-_', validate=True))
    try:
        return run_preview(policy, request['argv'], request['cwd'], request['record_id'], request.get('args_sha256'))
    except Exception as error:
        record_preflight_failure(policy, request, error)
        print('Preview denied: ' + type(error).__name__, file=sys.stderr)
        return 126


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('Preview denied: ' + str(error), file=sys.stderr)
        raise SystemExit(126)

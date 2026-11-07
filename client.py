"""Shared daemon connection used by MCP and local management clients."""
import json
import re
from pathlib import Path
import subprocess
import sys
import time
import uuid
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from bridge import ROOT, VERSION, SOURCE_FINGERPRINT

RUNTIME = ROOT / '.runtime'

def watch(job_id, seconds=30):
    """Bounded local polling without Codex/Gemini model turns or resubmission."""
    from bridge import validate_job_id
    from progress import enrich
    validate_job_id(job_id)
    if type(seconds) is not int or not 0 <= seconds <= 60:
        raise ValueError('Watch duration must be between 0 and 60 seconds')
    started = time.monotonic()
    while True:
        budget = min(5, max(.1, seconds-(time.monotonic()-started))) if seconds else 5
        value = request('/jobs/' + job_id, start=False, timeout_seconds=budget)
        if value['state']['status'] not in ('queued', 'running', 'cancelling') or time.monotonic()-started >= seconds:
            result = enrich(value)
            result['waited_seconds'] = round(time.monotonic()-started,1)
            return result
        time.sleep(min(1, max(0, seconds-(time.monotonic()-started))))

def request(path, payload=None, *, start=True, timeout_seconds=55):
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 55:
        raise ValueError('Invalid local request timeout')
    if path == '/jobs' and payload is not None:
        payload = dict(payload)
        payload.setdefault('request_id', str(uuid.uuid4()))
    # One deadline covers health, HTTP response, startup and all retries.
    # Restarting a failed read must never reset another full 55-second window.
    network_deadline = time.monotonic() + timeout_seconds
    def timeout_error():
        recovery = (' Recover the original request_id=' + payload['request_id'] + '; do not create another request.'
                    if path == '/jobs' and isinstance(payload,dict) else '')
        return TimeoutError('Local Bridge query timed out; this does not cancel generation.' + recovery)
    def call():
        if time.monotonic() >= network_deadline:
            raise timeout_error()
        info = json.loads((RUNTIME / 'service.json').read_text())
        base = 'http://127.0.0.1:' + str(info['port'])
        compatibility = None
        if path != '/health':
            health_request = Request(base + '/health', headers={'Authorization': 'Bearer ' + info['token']})
            with urlopen(health_request, timeout=min(5, max(.01, network_deadline-time.monotonic()))) as health_response:
                health = json.load(health_response)
            if health.get('version') != VERSION or health.get('source_fingerprint') != SOURCE_FINGERPRINT:
                inspect_only = (payload is None and (path == '/jobs' or path.startswith('/jobs/'))
                                or path == '/task-mode' and isinstance(payload, dict) and payload.get('action') == 'status')
                stop_only = payload == {} and bool(re.fullmatch(r'/jobs/[A-Za-z0-9-]+/cancel', path))
                if not (inspect_only or stop_only):
                    raise RuntimeError('Bridge daemon version mismatch; restart the local service before submitting. '
                                       'Expected ' + VERSION + ', observed ' + str(health.get('version')))
                compatibility = {'mode':('cancel_only_during_service_update' if stop_only else 'read_only_during_service_update'),
                                 'expected_version':VERSION, 'observed_version':health.get('version'),
                                 'new_submissions_require_current_service':True}
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        req = Request(base + path, data=data,
                      headers={'Authorization': 'Bearer ' + info['token'], 'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=max(.01, network_deadline-time.monotonic())) as response:
                value = json.load(response)
                if compatibility and isinstance(value, dict):
                    value['service_compatibility'] = compatibility
                return value
        except HTTPError as error:
            raise ValueError(json.load(error).get('error', str(error))) from error
    try:
        return call()
    except (OSError, json.JSONDecodeError) as error:
        if not start:
            raise
        if time.monotonic() >= network_deadline:
            raise timeout_error() from error
        RUNTIME.mkdir(exist_ok=True, mode=0o700)
        with (RUNTIME / 'service.log').open('a') as output:
            subprocess.Popen([sys.executable, str(ROOT / 'service.py')], cwd=ROOT,
                             stdout=output, stderr=output, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        for attempt in range(60):
            remaining=network_deadline-time.monotonic()
            if remaining<=0:
                raise timeout_error()
            time.sleep(min(0.1,remaining))
            try:
                return call()
            except (OSError, json.JSONDecodeError):
                pass
        raise RuntimeError('Bridge service did not start')

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['list', 'status', 'watch', 'cancel', 'models', 'submit', 'review', 'task-mode', 'health', 'view', 'doctor', 'plan', 'summary', 'retry', 'steer', 'recover'])
    parser.add_argument('--wait-seconds', type=int, default=30)
    parser.add_argument('--job-id')
    parser.add_argument('--thread-id')
    parser.add_argument('--request-file', type=Path)
    args = parser.parse_args()
    if args.action == 'view':
        from viewer import ensure_running, job_url
        print(job_url(args.job_id) if args.job_id else ensure_running()['url'])
        raise SystemExit(0)
    from bridge import validate_job_id
    if args.action in ('status', 'watch', 'cancel', 'review', 'summary', 'retry', 'steer', 'recover'):
        validate_job_id(args.job_id)
    routes = {'list': '/jobs', 'models': '/models', 'status': '/jobs/' + str(args.job_id),
              'cancel': '/jobs/' + str(args.job_id) + '/cancel', 'review': '/jobs/' + str(args.job_id) + '/review',
              'submit': '/jobs', 'task-mode': '/task-mode', 'health': '/health', 'doctor':'/doctor', 'plan':'/plan', 'summary':'/jobs/'+str(args.job_id)+'/summary', 'retry':'/jobs/'+str(args.job_id)+'/retry', 'steer':'/jobs/'+str(args.job_id)+'/steer','recover':'/jobs/'+str(args.job_id)+'/recover'}
    payload = json.loads(args.request_file.read_text()) if args.request_file else ({} if args.action == 'cancel' else None)
    if args.action == 'retry':
        if not args.thread_id or args.request_file:
            parser.error('retry requires --thread-id and does not accept --request-file')
        payload={'thread_id':args.thread_id}
    if args.action in ('submit', 'review', 'task-mode', 'plan', 'steer', 'recover') and payload is None:
        parser.error('--request-file required')
    from cli_updates import attach_notice
    from progress import enrich
    from viewer import attach_links
    value = watch(args.job_id, args.wait_seconds) if args.action == 'watch' else enrich(request(routes[args.action], payload))
    print(json.dumps(attach_notice(attach_links(value)), ensure_ascii=False, indent=2))

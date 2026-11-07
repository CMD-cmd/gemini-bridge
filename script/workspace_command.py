"""Native command broker shim; local sandbox execution is a daemon-only mode."""
import base64
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from workspace_guard import literal_command, run_sandboxed

ROOT = Path(__file__).resolve().parent.parent


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Command broker redirects are forbidden')


def _cancelled(job):
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
    if state.get('status') in {'cancelled', 'cancelling', 'interrupted', 'failed_or_unverified', 'bridge_error'} or state.get('cancel_requested'):
        return 143
    return None


def _flush_log(path, offset, fd):
    if not path.exists():
        return offset
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink > 1:
        raise ValueError('Unexpected command log path')
    with path.open('rb') as log:
        log.seek(offset)
        while True:
            chunk = log.read(65536)
            if not chunk:
                return log.tell()
            done = 0
            while done < len(chunk):
                done += os.write(fd, chunk[done:])


def broker_command(policy, request):
    """Submit to a task-scoped loopback broker; never execute locally on failure."""
    url = os.environ.get('GEMINI_BRIDGE_COMMAND_URL', '')
    token = os.environ.get('GEMINI_BRIDGE_COMMAND_TOKEN', '')
    parsed = urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or not re.fullmatch(r'/jobs/[^/]+/commands', parsed.path)
            or not parsed.port or not 1 <= parsed.port <= 65535):
        raise ValueError('A loopback task command broker URL is required')
    if not token or '\r' in token or '\n' in token:
        raise ValueError('Missing task command broker capability')
    record_id = request.get('record_id')
    if not isinstance(record_id, str) or not re.fullmatch(r'[0-9a-f]{64}', record_id):
        raise ValueError('Missing stable command record ID')
    job = Path(policy['job_dir']).resolve()
    directory = Path(policy['command_results_dir']).resolve()
    if directory != job / 'command-results':
        raise ValueError('Unexpected task result directory')
    record_path = directory / (record_id + '.json')
    stop = _cancelled(job)
    if stop is not None:
        return stop
    payload = {key: request[key] for key in ('argv', 'cwd', 'record_id')}
    if request.get('args_sha256'):
        payload['args_sha256'] = request['args_sha256']
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    http_request = Request(url, data=json.dumps(payload).encode(), method='POST',
                           headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with opener.open(http_request, timeout=10) as response:
        receipt = json.loads(response.read(65537))
    if not isinstance(receipt, dict) or receipt.get('record_path') != str(record_path):
        raise ValueError('Command broker returned an unexpected record path')
    print('Workspace command result: ' + str(record_path), flush=True)
    paths = [directory / (record_id + suffix) for suffix in ('.stdout.log', '.stderr.log')]
    offsets = [0, 0]
    deadline = time.monotonic() + max(1, int(policy['command_timeout_seconds'])) + 10
    while time.monotonic() < deadline:
        for i, path in enumerate(paths):
            offsets[i] = _flush_log(path, offsets[i], i + 1)
        try:
            if record_path.is_symlink():
                raise ValueError('Unexpected command record symlink')
            record = json.loads(record_path.read_text())
        except FileNotFoundError:
            record = None
        if record is not None:
            if not isinstance(record, dict) or record.get('record_id') != record_id:
                raise ValueError('Unexpected command record identity')
            if record.get('status') in {'exited', 'timeout', 'cancelled', 'failed', 'interrupted'}:
                for i, path in enumerate(paths):
                    offsets[i] = _flush_log(path, offsets[i], i + 1)
                code = record.get('runner_exit_code')
                if type(code) is not int:
                    raise ValueError('Command record has no verified exit code')
                return code
        stop = _cancelled(job)
        if stop is not None:
            return stop
        time.sleep(0.25)
    raise TimeoutError('Command broker result deadline exceeded')


def main():
    try:
        policy = json.loads(Path(sys.argv[1]).read_text())
        request = json.loads(base64.b64decode(sys.argv[2], altchars=b'-_', validate=True))
        argv = request['argv']
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise ValueError('Invalid literal argv')
        if len(sys.argv) == 4 and sys.argv[3] == '--execute-local':
            if argv[0] == 'bridge-preview':
                try:
                    literal_command(policy, {'CommandLine': __import__('shlex').join(argv), 'Cwd': request['cwd']})
                    python = str(ROOT / '.venv-preview/bin/python')
                    env = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8',
                           'PYTHONDONTWRITEBYTECODE': '1'}
                    os.execve(python, [python, str(ROOT / 'preview_runtime.py'), sys.argv[1], sys.argv[2]], env)
                except Exception as error:
                    from preview_spec import record_preflight_failure
                    record_preflight_failure(policy, request, error)
                    raise
                raise RuntimeError('Preview exec unexpectedly returned')
            return run_sandboxed(policy, argv, request['cwd'], request.get('record_id'), request.get('args_sha256'))
        if len(sys.argv) != 3:
            raise ValueError('Unknown command runner mode')
        return broker_command(policy, request)
    except Exception as error:
        print('Workspace command denied: ' + type(error).__name__, file=sys.stderr)
        return 126


if __name__ == '__main__':
    raise SystemExit(main())

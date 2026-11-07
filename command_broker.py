"""Per-job command capabilities; trusted runners live outside the CLI sandbox."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time

from workspace_guard import literal_command


class CommandBroker:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()
        self.channels = {}

    def open(self, job_id, base_url):
        with self.lock:
            if job_id in self.channels:
                raise ValueError('Command capability is already open')
            token = secrets.token_urlsafe(32)
            self.channels[job_id] = {'token': token, 'closed': False, 'requests': {}}
            return {'GEMINI_BRIDGE_COMMAND_URL': base_url + '/jobs/' + job_id + '/commands',
                    'GEMINI_BRIDGE_COMMAND_TOKEN': token}

    def submit(self, job_id, token, request):
        with self.lock:
            channel = self.channels.get(job_id)
            if (not channel or channel['closed'] or not isinstance(token, str)
                    or not hmac.compare_digest(token, channel['token'])):
                raise ValueError('Command capability is unavailable')
            if (not isinstance(request, dict) or set(request) - {'argv', 'cwd', 'record_id', 'args_sha256'}
                    or not isinstance(request.get('argv'), list) or not request['argv']
                    or any(not isinstance(s, str) for s in request['argv'])
                    or len(json.dumps(request)) > 131072):
                raise ValueError('Invalid command request')
            record_id = request.get('record_id')
            if not isinstance(record_id, str) or not re.fullmatch(r'[0-9a-f]{64}', record_id):
                raise ValueError('Invalid command record identity')
            job = self.root / 'runs' / job_id
            policy_file = job / 'workspace-policy.json'
            policy = json.loads(policy_file.read_text())
            if Path(policy['job_dir']).resolve() != job:
                raise ValueError('Command policy belongs to another task')
            guard = json.loads((job / 'workspace-guard-state.json').read_text())
            claim = guard.get('command_requests', {}).get(record_id)
            selected = {key: request.get(key) for key in ('argv', 'cwd', 'args_sha256')}
            if claim != selected:
                raise ValueError('Command does not match an approved native tool request')
            argv, cwd = literal_command(policy, {'CommandLine': shlex.join(request['argv']), 'Cwd': request.get('cwd')})
            if argv != request['argv'] or cwd != request['cwd']:
                raise ValueError('Command arguments changed after approval')
            digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
            previous = channel['requests'].get(record_id)
            if previous:
                if previous['digest'] != digest:
                    raise ValueError('Command record identity was reused')
                return {'record_path': str(job / 'command-results' / (record_id + '.json'))}
            if len(channel['requests']) >= 256:
                raise ValueError('Too many commands in one task')
            if sum(entry['process'].poll() is None for entry in channel['requests'].values()) >= 4:
                raise ValueError('Wait for running task commands before starting another')
            encoded = base64.urlsafe_b64encode(json.dumps(request).encode()).decode()
            # No model-controlled script runs in this controller process. The runner
            # clears its environment before launching the approved sandboxed argv.
            process = subprocess.Popen([str(Path(sys.executable).resolve()),
                        str(self.root / 'script/workspace_command.py'), str(policy_file), encoded, '--execute-local'],
                        cwd=job, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(Path.home()),
                             'PYTHONDONTWRITEBYTECODE': '1', 'LANG': 'en_US.UTF-8'}, start_new_session=True)
            channel['requests'][record_id] = {'digest': digest, 'process': process}
            return {'record_path': str(job / 'command-results' / (record_id + '.json'))}

    def finish(self, job_id):
        """Revoke first, then drain runners before any delivery snapshot is taken."""
        with self.lock:
            channel = self.channels.get(job_id)
            if not channel:
                return []
            channel['closed'] = True
            processes = [entry['process'] for entry in channel['requests'].values()]
        for process in processes:
            if process.poll() is None:
                process.terminate()  # The trusted runner reaps sandbox-tagged descendants.
        deadline = time.monotonic() + 10
        for process in processes:
            try:
                process.wait(timeout=max(.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                # Do not freeze deliverables while an unverified writer may survive.
                raise ValueError('Command runner did not stop; delivery snapshot withheld') from error
        job = self.root / 'runs' / job_id
        records = []
        for record_id in channel['requests']:
            path = job / 'command-results' / (record_id + '.json')
            if not path.exists():
                raise ValueError('Command runner ended without a result record')
            record = json.loads(path.read_text())
            if record.get('status') == 'running' or record.get('logs_complete') is False:
                raise ValueError('Command result or output collection is incomplete')
            records.append(record)
        return records

    def retire(self, job_id):
        self.finish(job_id)
        with self.lock:
            self.channels.pop(job_id, None)

"""Restart only this bridge's idle daemon; never replay jobs or kill a busy worker."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from client import request


def restart():
    path = ROOT / '.runtime' / 'service.json'
    if path.exists():
        info = json.loads(path.read_text())
        pid = info['pid']
        try:
            command = subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='], text=True).strip()
        except subprocess.CalledProcessError:
            command = ''
        if command:
            if not command.endswith(' ' + str(ROOT / 'service.py')):
                raise RuntimeError('Discovery PID is not this bridge daemon; refusing to stop it')
            req = Request('http://127.0.0.1:' + str(info['port']) + '/jobs',
                          headers={'Authorization': 'Bearer ' + info['token']})
            with urlopen(req, timeout=5) as response:
                jobs = json.load(response)['jobs']
            if any(job['status'] in ('queued', 'running', 'cancelling') for job in jobs):
                raise RuntimeError('Bridge has active jobs; wait or explicitly cancel them first')
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not path.exists():
                    break
                time.sleep(.1)
            else:
                raise RuntimeError('Daemon did not stop; refusing to force-kill it')
    # The client validates the expected version and fingerprint before this read.
    request('/jobs')
    return request('/health')


if __name__ == '__main__':
    print(json.dumps(restart()))

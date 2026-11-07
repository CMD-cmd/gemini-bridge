"""Zero-model CLI inspection. Local transport probes do not verify provider IO."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

SCHEMA_VERSION = 1
_TIMEOUT = 5


def _run(argv, *, cwd=None, input=b''):
    # No inherited API keys or configuration pointers are needed for help.
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR') if k in os.environ}
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, input=input,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=_TIMEOUT, check=False)
        return {'ok': result.returncode == 0, 'returncode': result.returncode,
                'stdout': result.stdout.decode('utf-8', 'replace')[:65536],
                'stderr': result.stderr.decode('utf-8', 'replace')[:65536]}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'ok': False, 'error': str(error)}


def _local_probes():
    payload = '{"event":"user","message":{"content":"local probe 零模型"}}\n'
    result = _run([sys.executable, '-I', '-c',
                   'import json,sys; rows=[json.loads(x) for x in sys.stdin if x.strip()]; '
                   'print(json.dumps(rows,ensure_ascii=False))'], input=payload.encode())
    try:
        stdin_ok = result['ok'] and json.loads(result['stdout']) == [json.loads(payload)]
    except (KeyError, ValueError):
        stdin_ok = False
    stdin = {'ok': stdin_ok, 'scope': 'local Python stdin and NDJSON only', **result}
    stdin['ok'] = stdin_ok
    pty_result = _run([sys.executable, '-I', '-c',
                      'import os,pty; m,s=pty.openpty(); '
                      'assert os.isatty(s); os.write(s,b"local-pty-probe\\n"); '
                      'assert b"local-pty-probe" in os.read(m,1024); '
                      'os.close(m); os.close(s); print("local-pty-ok")'])
    pty_result['scope'] = 'local OS PTY only; provider interactive mode untested'
    return {'local_pty': pty_result, 'local_stdin_ndjson': stdin}


def probe(root, refresh=False, persist=True):
    """Inspect bin/agy using only --help/--version; persist=False never writes."""
    root = Path(root).resolve()
    binary = root / 'bin' / 'agy'
    identity = {'path': str(binary), 'realpath': str(binary.resolve())}
    try:
        digest = hashlib.sha256()
        with binary.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        identity.update(sha256=digest.hexdigest(), fingerprint=digest.hexdigest())
    except OSError as error:
        return {'schema_version': SCHEMA_VERSION, 'binary': identity,
                'declared': {'flags': [], 'capabilities': {}},
                'observed': {'binary': {'ok': False, 'error': str(error)}},
                'cache': {'hit': False}, 'model_calls': 0}
    cache_path = root / '.runtime' / 'capabilities' / (identity['fingerprint'] + '.json')
    if not refresh:
        try:
            cached = json.loads(cache_path.read_text())
            if isinstance(cached, dict) and cached.get('schema_version') == SCHEMA_VERSION and cached.get('binary') == identity:
                cached['cache'] = {'hit': True, 'path': str(cache_path)}
                return cached
        except (OSError, ValueError, TypeError):
            pass
    help_result = _run([str(binary), '--help'], cwd=root)
    version = _run([str(binary), '--version'], cwd=root)
    flags = sorted(set(re.findall(r'(?m)^\s+(--[a-z][a-z0-9-]*|-[a-z])(?=\s|,|$)',
                                 help_result.get('stdout', '') + '\n' + help_result.get('stderr', '')))) if help_result['ok'] else []
    capabilities = {name: flag in flags for name, flag in {
        'print': '--print', 'conversation': '--conversation', 'continue_latest': '--continue',
        'input_format': '--input-format', 'output_format': '--output-format',
        'model': '--model', 'effort': '--effort', 'sandbox': '--sandbox',
        'agent': '--agent', 'print_timeout': '--print-timeout',
        'disable_slash_commands': '--disable-slash-commands',
    }.items()}
    result = {'schema_version': SCHEMA_VERSION, 'checked_at': time.time(), 'binary': identity,
              'declared': {'source': '--help; declarations are not execution tests',
                           'flags': flags, 'capabilities': capabilities},
              'observed': {'binary': {'ok': os.access(binary, os.X_OK)}, 'help': help_result,
                           'version': version, **_local_probes()},
              'cache': {'hit': False}, 'model_calls': 0}
    if persist:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(mode='w', dir=cache_path.parent, delete=False) as target:
                temp_path = Path(target.name)
                json.dump(result, target, ensure_ascii=False, indent=2)
            os.replace(temp_path, cache_path)
            result['cache']['path'] = str(cache_path)
        except OSError as error:
            result['cache']['error'] = str(error)
    return result


def doctor(root, refresh=False, persist=True):
    return probe(root, refresh=refresh, persist=persist)

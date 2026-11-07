"""Inspect disconnected ACP jobs or load existing context, without prompting."""
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

from acp_client import ACPClient, ACPError

_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,199}')
_SID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
_LIMIT = 80000


def _safe(path, directory=False):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise ValueError('Recovery paths must not contain symbolic links')
    info = path.lstat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('Untrusted recovery control object')
    if directory:
        if not stat.S_ISDIR(info.st_mode): raise ValueError('Expected recovery directory')
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Expected local recovery record')
    return path


def _read(path, optional=False):
    path = Path(path)
    if optional and not path.exists() and not path.is_symlink(): return {}
    _safe(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
        raw = source.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024: raise ValueError('Recovery metadata too large')
    value = json.loads(raw)
    if not isinstance(value, dict): raise ValueError('Invalid recovery metadata')
    return value


def _atomic(path, value):
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temp, path)


def _local_context(job, report, sid):
    response = report.get('response')
    if isinstance(response, str) and response:
        return response[-_LIMIT:], 'report.response'
    events = job / 'events.jsonl'
    if not events.exists(): return '', 'local_public_events'
    _safe(events)
    chunks = ''
    with events.open('rb') as source:
        source.seek(0, os.SEEK_END)
        start = max(0, source.tell() - 2 * 1024 * 1024)
        source.seek(start)
        if start: source.readline()
        for line in source:
            try: event = json.loads(line)
            except (ValueError, UnicodeError): continue
            step = event.get('step_update') or {}
            if step.get('step_type') == 'agent_response' and step.get('conversation_id') == sid:
                text = step.get('text_delta')
                if isinstance(text, str): chunks = (chunks + text)[-_LIMIT:]
    return chunks, 'local_public_events'


def _runtime(metadata):
    pid = metadata.get('pid')
    if (type(pid) is not int or pid <= 0 or not metadata.get('started_at')
            or not isinstance(metadata.get('command_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', metadata['command_sha256'])
            or not isinstance(metadata.get('session_cwd'), str)):
        return 'local_runtime_unknown'
    try: os.kill(pid, 0)
    except ProcessLookupError: return 'local_process_exited'
    except (PermissionError, OSError): return 'local_runtime_unknown'
    return 'local_process_present'


def _active_session_job(root, job_id, sid):
    """Inspect trusted local provenance, including queued transitive descendants.

    An unreadable candidate cannot establish that loading this session is safe;
    fail closed without starting a provider process or changing any job record.
    """
    rows = {}
    try:
        runs = _safe(root / 'runs', True)
        candidates = list(runs.iterdir())
    except (OSError, ValueError):
        return None, 'session_job_metadata_unreadable'
    for candidate in candidates:
        if candidate.name == job_id or not _ID.fullmatch(candidate.name): continue
        try:
            _safe(candidate, True)
            row = {name: _read(candidate / (name + '.json'), True)
                   for name in ('submission', 'requested-profile', 'report', 'acp-session', 'state')}
        except (OSError, ValueError):
            return None, 'session_job_metadata_unreadable'
        rows[candidate.name] = row
    related = {job_id}
    changed = True
    while changed:
        changed = False
        for ident, row in rows.items():
            continuation = row['requested-profile'].get('continuation') or {}
            if not isinstance(continuation, dict):
                return None, 'session_job_metadata_unreadable'
            same_session = sid in (row['report'].get('provider_session_id'),
                                  row['acp-session'].get('sessionId'),
                                  continuation.get('provider_session_id'))
            parents = (row['submission'].get('continue_job_id'), continuation.get('parent_job_id'),
                       continuation.get('root_job_id'))
            if ident not in related and (same_session or any(parent in related for parent in parents if isinstance(parent, str))):
                related.add(ident); changed = True
    for ident in sorted(related - {job_id}):
        state = rows[ident]['state']
        if not state.get('status'):
            return ident, 'related_job_state_unreadable'
        if state['status'] in ('queued', 'running', 'cancelling'):
            return ident, None
    return None, None


def inspect(root, job_id, thread_id, probe=False):
    if not isinstance(job_id, str) or not _ID.fullmatch(job_id): raise ValueError('Invalid job ID')
    if type(probe) is not bool: raise ValueError('probe must be boolean')
    root = _safe(root, True); job = _safe(root / 'runs' / job_id, True)
    submission = _read(job / 'submission.json')
    if not isinstance(thread_id, str) or not thread_id or submission.get('thread_id') != thread_id:
        raise ValueError('Recovery belongs to another thread')
    state = _read(job / 'state.json')
    report = _read(job / 'report.json', True)
    session = _read(job / 'acp-session.json', True)
    init = _read(job / 'acp-initialize.json', True)
    active = state.get('status') in ('queued', 'running', 'cancelling')
    requested = _read(job / 'requested-profile.json', True)
    if report.get('transport') != 'acp' and not (session and init.get('protocolVersion') == 1) and not (active and requested.get('transport') == 'acp'):
        raise ValueError('Recovery only supports existing ACP jobs')
    sid = report.get('provider_session_id') or session.get('sessionId')
    if not (active and sid is None) and (not isinstance(sid, str) or not _SID.fullmatch(sid)):
        raise ValueError('No verified ACP session identity')
    if session.get('sessionId') and session['sessionId'] != sid:
        raise ValueError('ACP session provenance conflicts')
    text, method = _local_context(job, report, sid)
    result = {'job_id': job_id, 'provider_session_id': sid, 'checked_at': time.time(),
              'provider_execution_state': 'unknown', 'prompt_replayed': False,
              'available_context': bool(text), 'recovered_text': text,
              'sources': [{'method': method, 'observed_at': time.time()}],
              'status': 'local_context_only', 'probe_performed': False}
    if active:
        result.update(status='continue_observing', local_runtime='job_active')
        return result
    process = _read(job / 'acp-process.json', True)
    runtime = _runtime(process)
    result['local_runtime'] = runtime
    if not probe: return result
    active_job, scan_error = _active_session_job(root, job_id, sid)
    if scan_error:
        result.update(status='probe_blocked', reason=scan_error)
        if active_job: result['active_job_id'] = active_job
        return result
    if active_job:
        result.update(status='continue_observing', active_job_id=active_job, reason='provider_session_active_in_another_job')
        return result
    if runtime != 'local_process_exited':
        result.update(status='probe_blocked', reason=runtime)
        return result
    cwd = report.get('session_cwd') or session.get('session_cwd') or session.get('cwd')
    if not isinstance(cwd, str) or cwd != process['session_cwd'] or not Path(cwd).is_absolute():
        result.update(status='probe_blocked', reason='original_session_cwd_unverified')
        return result
    _safe(cwd, True)
    attempts = job / 'recovery-attempts'
    if attempts.exists() or attempts.is_symlink(): _safe(attempts, True)
    else: attempts.mkdir(mode=0o700)
    attempt = attempts / str(uuid.uuid4()); attempt.mkdir(mode=0o700)
    result['receipt_path'] = str(attempt / 'result.json')
    _probe(root, attempt, sid, cwd, result)
    _atomic(attempt / 'result.json', result)
    return result


def _probe(root, attempt, sid, cwd, result):
    from bridge import cli_environment, ensure_account_route, VERSION
    name = 'bridge-context-recovery'
    directory = attempt / '.agents/agents' / name
    directory.mkdir(parents=True, mode=0o700)
    agent = directory / 'agent.md'
    agent.write_text('---\nname: ' + name + '\ndescription: Read existing session history only.\n'
                     'mainAgent: true\nsubagent: false\ntools: []\ninheritMcp: false\n'
                     'inheritCustomizations: false\ncommandExecutionPolicy: off\n---\n'
                     'This connection loads history only. Do not generate or invoke tools.\n')
    agent.chmod(0o600)
    deadline = time.monotonic() + 42
    def remaining(limit):
        value = min(limit, deadline - time.monotonic())
        if value <= 0: raise TimeoutError('Context observation deadline reached')
        return value
    chunks = ''; chunks_lock = threading.Lock(); client = None
    def notification(method, params):
        nonlocal chunks
        if method != 'session/update' or not isinstance(params, dict) or params.get('sessionId') != sid: return
        update = params.get('update', {})
        content = update.get('content', {})
        if update.get('sessionUpdate') == 'agent_message_chunk' and content.get('type') == 'text':
            text = content.get('text')
            if isinstance(text, str):
                with chunks_lock: chunks = (chunks + text)[-_LIMIT:]
    def deny(method, params):
        if method == 'session/request_permission': return {'outcome': {'outcome': 'cancelled'}}
        raise ACPError('History recovery grants no client tools', code=-32601)
    try:
        ensure_account_route()
        env = cli_environment()
        # Never inherit a prior task's fresh-scope capability or MCP token.
        env = {k: v for k, v in env.items() if not k.startswith('GEMINI_BRIDGE_')}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        client = ACPClient([str(root / 'bin/agy'), 'acp', '--process-per-session', '--agent', name, '--sandbox'],
                           cwd=attempt, env=env, on_notification=notification, on_request=deny,
                           stderr_path=attempt / 'stderr.log')
        client.start(); result['probe_performed'] = True
        initialized = client.request('initialize', {'protocolVersion': 1, 'clientCapabilities': {},
                                     'clientInfo': {'name': 'gemini-bridge-recovery', 'version': VERSION}}, timeout=remaining(20))
        if initialized.get('protocolVersion') != 1 or initialized.get('agentCapabilities', {}).get('loadSession') is not True:
            result.update(status='probe_blocked', reason='session_load_not_advertised')
            return
        loaded = client.request('session/load', {'sessionId': sid, 'cwd': cwd, 'mcpServers': []}, timeout=remaining(20))
        if isinstance(loaded, dict) and loaded.get('sessionId', sid) != sid:
            raise ValueError('Loaded context identity mismatch')
        client.drain_notifications(timeout=remaining(2))
        result.update(status='context_loaded', reason='load_does_not_establish_previous_execution_state')
        client.request('session/close', {'sessionId': sid}, timeout=remaining(2))
    except Exception:
        # Public context preceding EOF is still useful; exception payloads are not receipts.
        if client is not None:
            try: client.drain_notifications(timeout=2)
            except Exception: pass
        result.update(status='probe_failed', reason='context_transport_or_protocol_failed')
    finally:
        if client is not None:
            try: client.close()
            except Exception: pass
        # stderr is transport diagnostics, not approved recovered public context.
        (attempt / 'stderr.log').unlink(missing_ok=True)
        with chunks_lock:
            if chunks:
                result['recovered_text'] = chunks
                result['available_context'] = True
                result['sources'].append({'method': 'ACP session/load agent_message_chunk', 'observed_at': time.time()})
        result['provider_execution_state'] = 'unknown'
        result['prompt_replayed'] = False

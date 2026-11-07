"""Host-owned stable ACP cwd with per-turn, fail-closed workspace leases."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import sys
import tempfile
import threading
import time

_ROOT = Path(__file__).resolve().parent
_held = {}
_mutex = threading.RLock()
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,199}')
_SESSION = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')


def _safe(path, directory=False):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
            # System-owned ancestors are normal; task/control objects are not.
            if stat.S_ISLNK(info.st_mode) or (part == path or info.st_uid not in (0, os.getuid())):
                raise ValueError('Untrusted workspace control path')
    info = path.lstat()
    if (directory and not stat.S_ISDIR(info.st_mode)) or (not directory and
            (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
        raise ValueError('Expected local workspace control object')
    if info.st_mode & 0o022:
        raise ValueError('Workspace control object is writable by another user')
    return path


def _read(path):
    path = _safe(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        raw = source.read()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _open_readonly(path):
    """Open a host-pinned control file without statting sandbox-denied ancestors."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o022):
            raise ValueError('Untrusted pinned workspace control file')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_control(path):
    with os.fdopen(_open_readonly(path), 'rb') as source:
        raw = source.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('Workspace control file exceeds size limit')
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _atomic(path, value):
    path = Path(path)
    if path.exists() or path.is_symlink(): _safe(path)
    fd, name = tempfile.mkstemp(prefix='.lease-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


@contextmanager
def _state_lock(lease, *, readonly=False):
    lock = Path(lease).parent / '.lease-state.lock'
    if readonly:
        fd = _open_readonly(lock)
    else:
        if lock.exists() or lock.is_symlink(): _safe(lock)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if readonly else fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _fingerprint(job, reader=_read):
    values = {}
    for name in ('submission.json', 'requested-profile.json', 'workspace-policy.json'):
        values[name] = reader(job / name)[1]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _job(root, job):
    root = _safe(root, True)
    job = _safe(job, True)
    if job.parent != root / 'runs' or not _ID.fullmatch(job.name):
        raise ValueError('Expected a new local Bridge job')
    return root, job


def activate(root, job, record, profile):
    root, job = _job(root, job)
    continuation = profile.get('continuation') or {}
    root_id = continuation.get('root_job_id', job.name)
    if not isinstance(root_id, str) or not _ID.fullmatch(root_id):
        raise ValueError('Invalid provider session root')
    base = root / 'provider-sessions'
    if base.exists() or base.is_symlink(): _safe(base, True)
    else: base.mkdir(mode=0o700)
    cwd = base / root_id
    if continuation:
        if continuation.get('session_cwd') != str(cwd):
            raise ValueError('Legacy workspace cwd requires a new root task')
        if not cwd.exists(): raise ValueError('Continuation stable cwd is missing')
    elif not cwd.exists(): cwd.mkdir(mode=0o700)
    _safe(cwd, True)
    lease = cwd / 'lease.json'
    lockpath = cwd / '.session.lock'
    if lockpath.exists() or lockpath.is_symlink(): _safe(lockpath)
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error: raise ValueError('Provider session is already active') from error
        with _state_lock(lease):
            old = _read(lease)[0] if lease.exists() else None
            rearm=bool(old and old.get('current_job')==job.name and old.get('no_prompt_started') is True
                       and old.get('job_fingerprint')==_fingerprint(job))
            if old:
                if old.get('active') is not False or old.get('remote_terminal') is not True:
                    raise ValueError('Previous provider lease is active or remotely unknown')
                if old.get('current_job') == job.name and not rearm:
                    raise ValueError('Historical jobs cannot be reactivated')
                previous = root / 'runs' / old['current_job']
                state = _read(previous / 'state.json')[0]
                if not rearm and state.get('status') not in ('completed_unreviewed', 'failed_or_unverified', 'cancelled', 'timeout', 'bridge_error', 'interrupted'):
                    raise ValueError('Previous job is not terminal')
                if state.get('remote_state') in ('unknown', 'running') or state.get('remote_status') in ('unknown', 'running'):
                    raise ValueError('Previous remote result is unknown')
            elif continuation:
                raise ValueError('Continuation lease is missing')
            submission = _read(job / 'submission.json')[0]
            thread = submission.get('thread_id')
            if not isinstance(thread, str) or not thread or submission.get('dispatch_mode') != 'auto':
                raise ValueError('Fresh automatic thread scope is required')
            if old and old['thread_id'] != thread:
                raise ValueError('Provider workspace belongs to another thread')
            policy, policy_hash = _read(job / 'workspace-policy.json')
            if policy.get('job_dir') != str(job) or policy.get('model') != profile.get('model'):
                raise ValueError('Workspace policy identity mismatch')
            if old and not rearm and continuation.get('provider_session_id') != old.get('expected_provider_session_id'):
                raise ValueError('Continuation provider identity mismatch')
            if set(record.get('read_roots', [])) != set(policy.get('read_roots', [])) or set(record.get('write_roots', [])) != set(policy.get('write_roots', [])):
                raise ValueError('CLI directories do not match authorized policy')
            source = _safe(job / '.agents', True)
            for entry in source.rglob('*'): _safe(entry, entry.is_dir())
            target = cwd / '.agents'
            if target.exists() or target.is_symlink():
                _safe(target, True)
                for entry in target.rglob('*'): _safe(entry, entry.is_dir())
                shutil.rmtree(target)
            shutil.copytree(source, target)
            hook = shlex.join([str(Path(sys.executable).resolve()), str(root / 'script/workspace_session_guard.py'), str(lease)])
            hooks = {'bridge-workspace': {
                'PreToolUse': [{'matcher': '*', 'hooks': [{'type': 'command', 'command': hook, 'timeout': 10}]}],
                'PostToolUse': [{'matcher': '*', 'hooks': [{'type': 'command', 'command': hook + ' post', 'timeout': 10}]}]}}
            (target / 'hooks.json').write_text(json.dumps(hooks))
            for entry in target.rglob('*'):
                entry.chmod(0o700 if entry.is_dir() else 0o600)
            target.chmod(0o700)
            value = {'schema_version': 1, 'active': True, 'root': str(root), 'root_job_id': root_id,
                     'session_cwd': str(cwd), 'current_job': job.name, 'job_dir': str(job),
                     'policy_path': str(job / 'workspace-policy.json'), 'policy_sha256': policy_hash,
                     'job_fingerprint': _fingerprint(job), 'thread_id': thread,
                     'expected_provider_session_id': continuation.get('provider_session_id'),
                     'owner_pid': os.getpid(), 'activated_at': time.time()}
            _atomic(lease, value)
        with _mutex: _held[str(lease)] = fd
        fd = None
        # --add-dir affects CLI workspace discovery, never policy read/write roots.
        dirs = sorted(set([str(job), *record.get('read_roots', []), *record.get('write_roots', [])]))
        return {'session_cwd': str(cwd), 'cli_extra_dirs': dirs, 'lease_file': str(lease)}
    finally:
        if fd is not None: os.close(fd)


def bind_session(lease_file, job, session_id):
    if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
        raise ValueError('Invalid provider session identity')
    with _state_lock(lease_file):
        lease = _read(lease_file)[0]
        _owned(lease_file, lease, job)
        old = lease.get('expected_provider_session_id')
        if old and old != session_id: raise ValueError('Provider session identity changed')
        lease['expected_provider_session_id'] = session_id
        _atomic(lease_file, lease)


def _owned(path, lease, job):
    with _mutex: owned = str(path) in _held
    if not owned or not lease.get('active') or lease.get('job_dir') != str(Path(job).absolute()):
        raise ValueError('Lease is not owned by this active job')


def retire(lease_file, job, *, remote_terminal=False, no_prompt_started=False):
    remote_terminal=bool(remote_terminal or no_prompt_started)
    with _state_lock(lease_file):
        lease = _read(lease_file)[0]
        _owned(lease_file, lease, job)
        lease['active'] = not remote_terminal
        lease['remote_terminal'] = bool(remote_terminal)
        lease['no_prompt_started'] = bool(no_prompt_started)
        lease['retired_at'] = time.time()
        _atomic(lease_file, lease)
    with _mutex: fd = _held.pop(str(lease_file))
    os.close(fd)


def handle(lease_file, payload, mode='pre', event=None):
    """Read host-pinned controls without creating/writing session controls.

    The trusted launch path supplies the lease. Host activation checks every
    ancestor before publishing it; the model sandbox must keep that control tree
    read-only. The hook directly opens fixed current-job files because runs'
    parent metadata and sibling jobs are intentionally sandbox-denied.
    """
    lease_file = Path(lease_file)
    root = _ROOT
    if (not lease_file.is_absolute() or lease_file.name != 'lease.json'
            or lease_file.parent.parent != root / 'provider-sessions'
            or not _ID.fullmatch(lease_file.parent.name)):
        raise ValueError('Lease is outside the trusted Bridge installation')
    with _state_lock(lease_file, readonly=True):
        lease = _read_control(lease_file)[0]
        if lease.get('root') != str(root) or lease.get('root_job_id') != lease_file.parent.name:
            raise ValueError('Lease installation or root identity mismatch')
        job_id = lease.get('current_job')
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            raise ValueError('Invalid current job identity')
        job = root / 'runs' / job_id
        if lease.get('job_dir') != str(job) or lease.get('session_cwd') != str(lease_file.parent) or not lease.get('active'):
            raise ValueError('Inactive or misplaced provider lease')
        fd = _open_readonly(lease_file.parent / '.session.lock')
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise ValueError('Provider lease has no live owner')
        finally: os.close(fd)
        if not lease.get('expected_provider_session_id') or payload.get('conversationId') != lease['expected_provider_session_id']:
            raise ValueError('Provider conversation does not match active lease')
        if lease.get('policy_path') != str(job / 'workspace-policy.json'):
            raise ValueError('Lease policy path mismatch')
        policy, digest = _read_control(lease['policy_path'])
        if digest != lease['policy_sha256'] or _fingerprint(job, _read_control) != lease['job_fingerprint']:
            raise ValueError('Current job policy or provenance changed')
        submission = _read_control(job / 'submission.json')[0]
        if submission.get('thread_id') != lease['thread_id']:
            raise ValueError('Current thread provenance changed')
        if policy.get('job_dir') != str(job): raise ValueError('Policy job mismatch')
        state = _read_control(job / 'state.json')[0]
        if state.get('status') != 'running' or state.get('cancel_requested'):
            raise ValueError('Current job is inactive')
        from workspace_guard import gate, post, lifecycle
        if event: return lifecycle(policy, payload, event)
        return post(policy, payload) if mode == 'post' else gate(policy, payload)

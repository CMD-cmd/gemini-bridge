"""Fail-closed native workspace hooks; OS sandboxing is a separate required layer."""
from __future__ import annotations
import base64
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time
import threading
import uuid

READ_TOOLS = {'view_file': 'AbsolutePath', 'read_file': 'Path', 'list_dir': 'DirectoryPath',
              'find_by_name': 'SearchDirectory', 'grep_search': 'SearchPath'}
WRITE_TOOLS = {'write_to_file': 'TargetFile', 'replace_file_content': 'TargetFile',
               'multi_replace_file_content': 'TargetFile'}
allowed_tools = tuple((*READ_TOOLS, *WRITE_TOOLS, 'run_command', 'command_status'))
ALLOWED_TOOLS = allowed_tools
BLOCKED = {'.agents', '.codex', '.git', '.ssh', '.aws', '.azure', '.gnupg',
           '.gemini', '.config', '.npmrc', '.netrc', '.pypirc', 'credentials',
           'credentials.json', 'secrets.json', 'id_rsa', 'id_ed25519'}


def _within(path, root):
    return path == root or root in path.parents


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _path(policy, value, write=False):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('An absolute scoped path is required')
    raw = Path(value)
    resolved = raw.resolve()
    if any(part.lower() in BLOCKED or part.lower().startswith('.env') or
           part.lower().endswith(('.pem', '.key', '.p12', '.pfx')) for part in (*raw.parts, *resolved.parts)):
        raise ValueError('Credential or control path is protected')
    if write and resolved.exists() and resolved.stat().st_nlink > 1:
        raise ValueError('Writing hard-linked files is forbidden')
    protected = [Path(p).resolve() for p in policy['protected_paths']]
    protected += [Path(policy[k]).resolve() for k in
                  ('audit_path', 'events_path', 'command_profile')]
    job = Path(policy['job_dir']).resolve()
    protected += [job / n for n in ('workspace-policy.json', 'workspace-guard-state.json',
                                   'workspace-guard-state.tmp', 'workspace-guard.lock', 'state.json',
                                   'cancel.requested', 'cancel.flag', 'command-control', 'preview-runtime',
                                   'project-tools-audit.jsonl','native-child-stop.requested')]
    module = Path(__file__).resolve()
    root = module.parent.resolve()
    if _within(resolved, root) and not _within(resolved, job):
        relative = resolved.relative_to(root)
        archived = not write and any(_within(resolved, Path(p).resolve())
                                     for p in policy.get('archive_read_roots', []))
        if relative.parts and (relative.parts[0] == '.runtime'
                or (relative.parts[0] == 'runs' and not archived)
                or (write and (relative.parts[0] in {'script', 'bin'}
                               or resolved.suffix.lower() in {'.py', '.json'}))):
            raise ValueError('Bridge runtime control path is protected')
    if write:
        protected += [Path(policy['command_runner']).resolve(), module, module.parent / 'script/workspace_tool_guard.py',
                      module.parent / 'script/workspace_command.py']
        if policy.get('command_results_dir'):
            protected.append(Path(policy['command_results_dir']).resolve())
        protected.append(job/'project-tools-results')
    if any(_within(resolved, p) for p in protected):
        raise ValueError('Task control path is protected')
    roots = policy['write_roots'] if write else policy['read_roots'] + policy['write_roots'] + policy.get('archive_read_roots', [])
    if not write and policy.get('command_results_dir'):
        roots = roots + [policy['command_results_dir']]
    if not write and policy.get('project_tools'):roots=roots+[str(job/'project-tools-results')]
    if not any(_within(resolved, Path(root).resolve()) for root in roots):
        raise ValueError('Path is outside authorized roots')
    return str(resolved)


def _scan_tree(policy, value):
    """Recheck every descendant immediately before recursive native searches.

    Hard links have no canonical original path, so refuse all multiply linked
    regular files. This is conservative and does not replace kernel path rules.
    """
    pending = [Path(_path(policy, value)).resolve()]
    seen = set()
    while pending:
        current = pending.pop()
        _path(policy, str(current))
        stat = current.stat()
        if current.is_file() and stat.st_nlink > 1:
            raise ValueError('Recursive tools cannot inspect hard-linked files')
        if not current.is_dir():
            continue
        identity = (stat.st_dev, stat.st_ino)
        if identity in seen:
            continue
        seen.add(identity)
        with os.scandir(current) as children:
            for child in children:
                path = Path(child.path)
                if child.is_symlink():
                    # A symlink may not hide an escape/control target behind a safe name.
                    _path(policy, str(path))
                    pending.append(path.resolve(strict=True))
                    continue
                stat = child.stat(follow_symlinks=False)
                if child.is_file(follow_symlinks=False) and stat.st_nlink > 1:
                    raise ValueError('Recursive tools cannot inspect hard-linked files')
                try:
                    _path(policy, str(path))
                except ValueError:
                    # Ordinary sensitive descendants are kernel-denied by the outer
                    # sandbox. Skip them here without making the whole root unusable.
                    continue
                pending.append(path.resolve(strict=True))



def _identity(policy, payload, state, active=True):
    job = Path(policy['job_dir']).resolve()
    if active and (policy.get('fresh_scope_required') or policy.get('project_tools') or policy.get('native_subagents')):
        current=json.loads((job/'state.json').read_text())
        if current.get('status')!='running' or current.get('cancel_requested'):
            raise ValueError('Task no longer authorizes native actions')
        from project_scope import verify
        verify(policy)
    conversation = payload.get('conversationId')
    if not isinstance(conversation, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', conversation):
        raise ValueError('Missing conversation identity')
    if state.get('conversation_id') and conversation!=state['conversation_id'] and policy.get('native_subagents'):
        from native_children import resolve_child
        return resolve_child(policy,payload,state)
    if payload.get('modelName') != policy['model']:
        raise ValueError('Unexpected model')
    workspaces = payload.get('workspacePaths')
    if not isinstance(workspaces, list) or str(job) not in workspaces:
        raise ValueError('Unexpected workspace')
    native = Path.home() / '.gemini/antigravity-cli/brain' / conversation
    if payload.get('artifactDirectoryPath') != str(native):
        raise ValueError('Unexpected artifact directory')
    if state.get('conversation_id', conversation) != conversation:
        raise ValueError('Conversation is already bound to another task')
    state['conversation_id'] = conversation


def _project_dispatch(policy,args):
    variants=(('ServerName','ToolName','Arguments'),('server_name','tool_name','arguments'),
              ('server_name','tool_name','tool_arguments'))
    for server_key,tool_key,args_key in variants:
        if set(args)=={server_key,tool_key,args_key}:
            server,tool,parameters=args[server_key],args[tool_key],args[args_key]
            if isinstance(parameters,str):parameters=json.loads(parameters)
            from project_tools import list_tools
            if server!=policy.get('project_server_name','bridge-project') or tool not in {t['name'] for t in list_tools(policy)}:
                raise ValueError('MCP server or tool is outside this task')
            from project_tools import validate_tool_arguments
            parameters=validate_tool_arguments(policy,tool,parameters)
            if 'path' in parameters:_path(policy,parameters['path'])
            return {'server':server,'tool':tool}
    raise ValueError('Unverified MCP dispatcher schema; keys='+','.join(sorted(args)))


def literal_command(policy, args):
    if args.get('RunPersistent', False) is not False:
        raise ValueError('Persistent commands are forbidden')
    command = args.get('CommandLine')
    if not isinstance(command, str) or not command.strip() or '\n' in command or '\r' in command:
        raise ValueError('One literal command is required')
    # Parse operators separately: quoted operator characters remain literal argv.
    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>()')
    lexer.whitespace_split = True
    lexer.commenters = ''
    list(lexer)  # Validate balanced quoting.
    argv = shlex.split(command, posix=True)
    # shlex removes quote provenance, so inspect a second token stream retaining quotes.
    check = shlex.shlex(command, posix=False, punctuation_chars=';&|<>()')
    check.whitespace_split = True
    check.commenters = ''
    if any(t and all(c in ';&|<>()' for c in t) for t in check):
        raise ValueError('Shell operators are forbidden')
    if not argv or any('\x00' in item for item in argv):
        raise ValueError('Invalid command argv')
    # Expansions are rejected even in quotes; literal shell-free execution never needs them.
    if any(c in command for c in ('`', '$')):
        raise ValueError('Shell expansion syntax is forbidden')
    cwd = _path(policy, args['Cwd'] if 'Cwd' in args else policy.get('working_directory'))
    if not Path(cwd).is_dir():
        raise ValueError('Command cwd must be an existing directory')
    if argv[0] == 'bridge-preview':
        from preview_spec import parse_request
        parse_request(policy, argv, cwd, read_actions=False)
        return argv, cwd
    prefixes = policy['command_prefixes']
    if not any(isinstance(p, list) and p and all(isinstance(s, str) for s in p)
               and argv[:len(p)] == p for p in prefixes):
        raise ValueError('Command is outside approved prefixes')
    return argv, cwd


def observed_command_ids(policy, conversation):
    """Only accept explicit official PostToolUse records belonging to this conversation.

    Streaming events are intentionally not guessed: the integrator must validate its
    native result adapter before adding additional event layouts here.
    """
    return set()  # Until native events schema is verified, post() is the sole adapter.


def _decision(policy, payload, state):
    child_record = _identity(policy, payload, state)
    if payload.get('decision') == 'deny' or payload.get('permissionDecision') == 'deny':
        raise ValueError('Native explicit deny is authoritative')
    call = payload.get('toolCall', {})
    name, args = call.get('name'), call.get('args', {})
    if not isinstance(args, dict):
        raise ValueError('Tool args must be an object')
    child=payload.get('conversationId')!=state['conversation_id']
    if child:
        permitted = set(READ_TOOLS) | {'send_message'}
        if child_record and child_record.get('role') == 'builder':
            permitted |= set(WRITE_TOOLS)
        if name not in permitted:
            raise ValueError('Native child cannot use this tool or delegate')
    if name=='invoke_subagent':
        from native_children import approve_spawn
        return approve_spawn(policy,payload,state)
    if name in ('send_message','manage_subagents'):
        from native_children import authorize_message_control
        return authorize_message_control(policy,payload,state)
    if name=='wait_5_seconds' and policy.get('native_subagents') and not child:
        if args:raise ValueError('Unexpected wait arguments')
        return {'decision':'auto_approve','reason':'Bounded wait for task children'}
    if name=='call_mcp_tool':
        _project_dispatch(policy,args)
        return {'decision':'auto_approve','reason':'Registered read-only project tool; broker rechecks scope'}
    if name in READ_TOOLS:
        _path(policy, args.get(READ_TOOLS[name]))
        if name in {'list_dir', 'find_by_name', 'grep_search'}:
            _scan_tree(policy, args.get(READ_TOOLS[name]))
    elif name in WRITE_TOOLS:
        if child:
            write_root = child_record.get('write_root')
            if not write_root:
                raise ValueError('Child has no isolated output directory')
            # Narrow only write authority. All original protected-path, symlink,
            # hard-link and scope checks remain active in _path.
            child_policy = dict(policy, write_roots=[write_root])
            target = _path(child_policy, args.get(WRITE_TOOLS[name]), write=True)
            if not _within(Path(target), Path(write_root)):
                raise ValueError('Child write escapes its assigned output directory')
        else:
            _path(policy, args.get(WRITE_TOOLS[name]), write=True)
    elif name == 'run_command':
        argv, cwd = literal_command(policy, args)
        args_hash = _digest(args)
        record_id = _digest({'conversation': payload['conversationId'], 'stepIdx': payload.get('stepIdx'), 'args_sha256': args_hash})
        request = {'argv': argv, 'cwd': cwd, 'args_sha256': args_hash}
        if policy.get('command_results_dir'):
            request['record_id'] = record_id
            approved = {'argv': argv, 'cwd': cwd, 'args_sha256': args_hash}
            requests = state.setdefault('command_requests', {})
            if record_id in requests and requests[record_id] != approved:
                raise ValueError('Command record ID conflicts with an approved request')
            requests[record_id] = approved
        encoded = base64.urlsafe_b64encode(json.dumps(request).encode()).decode()
        runner = shlex.join([str(Path(policy['python']).resolve()),
                             str(Path(policy['command_runner']).resolve()),
                             str(Path(policy['job_dir']) / 'workspace-policy.json'), encoded])
        state.setdefault('commands', {})[str(payload.get('stepIdx'))] = _digest(args)
        return {'decision': 'auto_approve', 'overwrite': {'CommandLine': runner,
                'Cwd': policy['job_dir'], 'RunPersistent': False, 'WaitMsBeforeAsync': 10000},
                'reason': ('Scoped command; view_file result: ' + str(Path(policy['command_results_dir']) / (record_id + '.json')))
                          if policy.get('command_results_dir') else 'Scoped command through sandbox runner'}
    elif name == 'command_status':
        cid = args.get('CommandId')
        if not isinstance(cid, str) or cid not in state.get('command_ids', []):
            raise ValueError('Native CommandId attribution is unavailable; use view_file on task command result JSON in ' + str(policy.get('command_results_dir', Path(policy['job_dir']) / 'command-results')))
    else:
        raise ValueError('Tool is not authorized: ' + str(name))
    return {'decision': 'auto_approve', 'reason': 'Scoped workspace permission'}


def _record(policy, payload, result, kind):
    call = payload.get('toolCall', {})
    args = call.get('args', {})
    record = {'kind': kind, 'at': time.time(), 'stepIdx': payload.get('stepIdx'),
              'conversationId': payload.get('conversationId'), 'tool': call.get('name'),
              'args_sha256': _digest(args), 'decision': result.get('decision'),
              'reason': result.get('reason')}
    record['modelName']=payload.get('modelName')
    if call.get('name') in {'call_mcp_tool','invoke_subagent','manage_subagents','send_message'} and isinstance(args,dict):
        record['argument_keys']=sorted(args)
        record['argument_types']={k:type(v).__name__ for k,v in args.items()}
    field = READ_TOOLS.get(call.get('name')) or WRITE_TOOLS.get(call.get('name'))
    if field and isinstance(args, dict):
        record['path'] = args.get(field)
    if call.get('name') in {'view_file', 'read_file'} and result.get('decision') == 'auto_approve':
        path = Path(_path(policy, args.get(READ_TOOLS[call['name']]))).resolve()
        if path.is_file():
            digest = hashlib.sha256()
            size = 0
            with path.open('rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
                    size += len(chunk)
            record['file_sha256'] = digest.hexdigest()
            record['file_size'] = size
    if call.get('name') == 'run_command' and isinstance(args, dict):
        try:
            argv = shlex.split(args.get('CommandLine', ''))
            record['command'] = {'executable': Path(argv[0]).name, 'argc': len(argv)} if argv else {}
        except ValueError:
            record['command'] = {'invalid': True}
    return record


def _transaction(policy, payload, kind):
    job = Path(policy['job_dir']).resolve()
    with (job / 'workspace-guard.lock').open('a+') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = job / 'workspace-guard-state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        try:
            if kind.startswith('lifecycle:'):
                event=kind.split(':',1)[1]
                if not state.get('conversation_id') or payload.get('conversationId')==state.get('conversation_id'):
                    _identity(policy,payload,state,active=False)
                else:
                    from native_children import observe_lifecycle
                    observe_lifecycle(policy,payload,state,event)
                result={'decision':'observed','reason':'Native lifecycle '+event}
            elif kind == 'pre':
                result = _decision(policy, payload, state)
            else:
                _identity(policy, payload, state)
                # Official post payload result shape must be verified live before use.
                # No CommandId is accepted from an unverified result structure.
                result = {'decision': 'post', 'reason': 'Completion observed; command ID adapter pending native verification'}
        except (ValueError, TypeError, KeyError, OSError) as error:
            result = {'decision': 'deny', 'reason': str(error)}
            if (kind=='pre' and policy.get('native_subagents') and state.get('conversation_id')
                    and payload.get('conversationId')!=state['conversation_id']
                    and any(s in str(error) for s in ('model','identity','workspace','artifact','root-to-child'))):
                (job/'native-child-stop.requested').write_text(str(error))
        record = _record(policy, payload, result, kind)
        key = _digest({k: v for k, v in record.items() if k != 'at'})
        seen = state.setdefault('audit_keys', [])
        if key not in seen:
            with Path(policy['audit_path']).open('a') as stream:
                os.chmod(stream.name, 0o600)
                stream.write(json.dumps(record) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            seen.append(key)
        temp = state_path.with_suffix('.tmp')
        temp.write_text(json.dumps(state))
        temp.chmod(0o600)
        temp.replace(state_path)
        return result


def gate(policy, payload):
    """Return official PreToolUse decision JSON; all errors fail closed."""
    try:
        return _transaction(policy, payload, 'pre')
    except Exception as error:
        return {'decision': 'deny', 'reason': 'Workspace guard failure: ' + type(error).__name__}


def lifecycle(policy,payload,event):
    try:_transaction(policy,payload,'lifecycle:'+event)
    except Exception:pass
    return {}


def post(policy, payload):
    try:
        result = _transaction(policy, payload, 'post')
        return {} if result['decision'] == 'post' else result
    except Exception as error:
        return {'decision': 'deny', 'reason': 'Workspace post guard failure: ' + type(error).__name__}


class _SandboxProcesses:
    """Identify a command sandbox by a random allow/deny pair, never by env/PID ancestry.

    sandbox_check is a macOS SPI. Failure to load it is fail-closed before spawn.
    A process that deliberately applies a stricter nested sandbox can hide the
    allow probe; this is not a general proof against hostile sandbox reconfiguration.
    """
    def __init__(self, cache):
        self.sandbox = ctypes.CDLL('/usr/lib/libsandbox.dylib', use_errno=True)
        self.proc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        self.check = self.sandbox.sandbox_check
        self.check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        self.check.restype = ctypes.c_int
        self.proc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.proc.proc_listallpids.restype = ctypes.c_int
        self.filter = 1 | ctypes.c_int.in_dll(self.sandbox, 'SANDBOX_CHECK_NO_REPORT').value
        token = 'probe-' + uuid.uuid4().hex
        self.directory = cache.resolve()
        self.allow = str(self.directory / (token + '.allow')).encode()
        self.deny = str(self.directory / (token + '.deny')).encode()
        # sandbox_check(PATH) reports a nonexistent path as denied regardless of
        # policy; materialize both task-private probes before applying the profile.
        for path in (self.allow, self.deny):
            Path(path.decode()).touch(mode=0o600, exist_ok=False)

    def rules(self):
        allowed = json.dumps(self.allow.decode(), ensure_ascii=False)
        denied = json.dumps(self.deny.decode(), ensure_ascii=False)
        directory = json.dumps(str(self.directory), ensure_ascii=False)
        # SBPL deny takes precedence over allow. Exclude only the allow probe
        # from the broad write deny, then deny its namespace/metadata mutations.
        # Its data permission is the identity bit; modifying bytes cannot hide it.
        return ('\n(allow file-write-data (literal ' + allowed + '))'
                '\n(deny file-write-data (literal ' + denied + '))'
                '\n(deny file-write* (require-all (subpath ' + directory + ')'
                ' (require-not (literal ' + allowed + '))))'
                '\n(deny file-write-unlink file-write-create file-write-mode '
                'file-write-owner file-write-flags file-write-xattr file-write-setugid '
                '(literal ' + allowed + '))\n')

    def matches(self, pid):
        return (pid != os.getpid()
                and self.check(pid, b'file-write-data', self.filter, ctypes.c_char_p(self.allow)) == 0
                and self.check(pid, b'file-write-data', self.filter, ctypes.c_char_p(self.deny)) == 1)

    def pids(self):
        count = self.proc.proc_listallpids(None, 0)
        if count <= 0:
            raise RuntimeError('Cannot enumerate command sandbox processes')
        buffer = (ctypes.c_int * (count * 2 + 64))()
        actual = self.proc.proc_listallpids(buffer, ctypes.sizeof(buffer))
        if actual <= 0:
            raise RuntimeError('Cannot enumerate command sandbox processes')
        return [pid for pid in buffer[:actual] if pid > 0 and self.matches(pid)]

    def terminate(self):
        # Freeze tagged members before killing them, then rescan for racing forks.
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            pids = self.pids()
            if not pids:
                return
            for sig in (signal.SIGSTOP, signal.SIGKILL):
                for pid in pids:
                    if self.matches(pid):  # Recheck after enumeration to avoid PID reuse.
                        try:
                            os.kill(pid, sig)
                        except ProcessLookupError:
                            pass
            time.sleep(0.02)


def _run_sandboxed(policy, argv, cwd, logs=None, outcome=None):
    """Run literal argv, clear inherited credentials, stop the whole process group."""
    checked, cwd = literal_command(policy, {'CommandLine': shlex.join(argv), 'Cwd': cwd})
    if checked[0] == 'bridge-preview':
        raise ValueError('Built-in preview requires the dedicated preview runner')
    mapping = policy.get('executable_map', {})
    if not isinstance(mapping, dict):
        raise ValueError('Invalid executable map')
    if checked[0] in mapping:
        executable = mapping[checked[0]]
        if not isinstance(executable, str) or not Path(executable).is_absolute():
            raise ValueError('Mapped executable must be absolute')
        launch = Path(executable)
        resolved = launch.resolve(strict=True)
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ValueError('Mapped executable is not executable')
        identities = policy.get('executable_identities')
        if identities is not None:
            identity = identities.get(checked[0]) if isinstance(identities, dict) else None
            stat = resolved.stat()
            if not isinstance(identity, dict) or (identity.get('resolved_path'), identity.get('dev'), identity.get('ino')) != (str(resolved), stat.st_dev, stat.st_ino):
                raise ValueError('Mapped executable identity has changed')
        # Preserve launch spelling: venv Python uses the symlink directory to find
        # pyvenv.cfg even though its real executable is the shared interpreter.
        checked = [str(launch), *checked[1:]]
    job = Path(policy['job_dir']).resolve()
    tmp = job / 'command-tmp'
    cache = job / 'command-cache'
    tmp.mkdir(exist_ok=True, mode=0o700)
    cache.mkdir(exist_ok=True, mode=0o700)
    control = job / 'command-control'
    control.mkdir(exist_ok=True, mode=0o700)
    command_path = policy.get('command_path', '/usr/bin:/bin:/usr/sbin:/sbin')
    if isinstance(command_path, list) and all(isinstance(p, str) for p in command_path):
        command_path = ':'.join(command_path)
    if not isinstance(command_path, str) or not command_path or any(not p or not Path(p).is_absolute() for p in command_path.split(':')):
        raise ValueError('Command PATH requires absolute directories')
    env = {'PATH': command_path, 'HOME': str(tmp),
           'TMPDIR': str(tmp), 'XDG_CACHE_HOME': str(cache), 'LANG': 'en_US.UTF-8',
           'PYTHONDONTWRITEBYTECODE': '1'}
    def cancellation_status():
        if any((job / name).exists() for name in ('cancel.requested', 'cancel.flag')):
            return 143
        state_file = job / 'state.json'
        try:
            state = json.loads(state_file.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return 143  # Unreadable cancellation state cannot authorize continued work.
        if not isinstance(state, dict):
            return 143
        status = state.get('status')
        if status == 'timeout':
            return 124
        if status in {'cancelled', 'cancelling', 'interrupted', 'failed_or_unverified', 'bridge_error'} or state.get('cancel_requested'):
            return 143
        return None
    cancelled = cancellation_status()
    if cancelled is not None:
        if outcome is not None:
            outcome.update(status='timeout' if cancelled == 124 else 'cancelled', exit_code=None)
        return cancelled
    members = _SandboxProcesses(control)
    # Verify enumeration works before starting work; marker profile is task-private.
    members.pids()
    profile = tmp / ('command-' + uuid.uuid4().hex + '.sb')
    record_protection = '\n(deny file-write* (subpath ' + json.dumps(str(job / 'command-results'), ensure_ascii=False) + '))\n'
    profile.write_text(Path(policy['command_profile']).read_text() + members.rules() + record_protection)
    profile.chmod(0o600)
    process = subprocess.Popen(['/usr/bin/sandbox-exec', '-f', str(profile), *checked],
                               cwd=cwd, env=env, start_new_session=True,
                               stdout=subprocess.PIPE if logs else None, stderr=subprocess.PIPE if logs else None)
    threads = []
    if logs:
        for source, fd, target in ((process.stdout, 1, logs[0]), (process.stderr, 2, logs[1])):
            thread = threading.Thread(target=_tee, args=(source, fd, target), daemon=True)
            thread.start()
            threads.append(thread)
    def terminate(*_):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=0.5)
            except (ProcessLookupError, subprocess.TimeoutExpired, PermissionError):
                pass
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1)
        members.terminate()
    def cancel(signum, _frame):
        if outcome is not None:
            outcome['status'] = 'cancelled'
        terminate()
        raise SystemExit(128 + signum)
    old = {s: signal.signal(s, cancel) for s in (signal.SIGTERM, signal.SIGINT)}
    deadline = time.monotonic() + max(1, int(policy['command_timeout_seconds']))
    try:
        while True:
            cancelled = cancellation_status()
            remaining = deadline - time.monotonic()
            if cancelled is not None or remaining <= 0:
                if outcome is not None:
                    outcome['status'] = 'timeout' if cancelled == 124 or cancelled is None else 'cancelled'
                terminate()
                process.wait()
                return cancelled if cancelled is not None else 124
            try:
                return process.wait(timeout=min(0.25, remaining))
            except subprocess.TimeoutExpired:
                pass
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
        terminate()  # Also clean descendants after a successful parent exit.
        if outcome is not None:
            outcome.setdefault('status', 'exited')
            outcome['exit_code'] = process.poll()
        for thread in threads:
            thread.join(timeout=2)
        if outcome is not None:
            outcome['logs_complete'] = not any(thread.is_alive() for thread in threads)
        profile.unlink(missing_ok=True)
        for probe in (members.allow, members.deny):
            Path(probe.decode()).unlink(missing_ok=True)


def _tee(source, fd, target):
    limit = 4 * 1024 * 1024
    total = 0
    truncated = False
    with target.open('wb') as log:
        os.chmod(target, 0o600)
        while True:
            data = source.read1(65536)
            if not data:
                break
            try:
                offset = 0
                while offset < len(data):
                    offset += os.write(fd, data[offset:])
            except OSError:
                pass  # A disconnected native terminal must not discard the durable log.
            keep = data[:max(0, limit - total)]
            log.write(keep)
            log.flush()
            total += len(keep)
            if len(keep) < len(data):
                truncated = True
        if truncated:
            log.write(b'\n[bridge log truncated after 4194304 bytes]\n')
    source.close()


def _atomic_record(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w') as stream:
        os.chmod(temp, 0o600)
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def run_sandboxed(policy, argv, cwd, record_id=None, args_sha256=None):
    """Optional durable command receipt; record files remain outside the inner sandbox."""
    if record_id is None:
        return _run_sandboxed(policy, argv, cwd)
    if not isinstance(record_id, str) or not re.fullmatch(r'[0-9a-f]{64}', record_id):
        raise ValueError('Invalid command record ID')
    directory = Path(policy['command_results_dir']).resolve()
    job = Path(policy['job_dir']).resolve()
    if directory != job / 'command-results':
        raise ValueError('Command results must use the protected task directory')
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / (record_id + '.json')
    print('Workspace command result: ' + str(path), flush=True)
    fingerprint = _digest({'argv': argv, 'cwd': str(Path(cwd).resolve()), 'args_sha256': args_sha256})
    with (directory / (record_id + '.lock')).open('a+') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            previous = json.loads(path.read_text())
            if previous.get('request_sha256') != fingerprint:
                raise ValueError('Command record ID was reused for a different request')
            # A stale running record after a killed runner must never cause re-execution.
            return previous.get('runner_exit_code') if isinstance(previous.get('runner_exit_code'), int) else 126
        record = {'record_id': record_id, 'status': 'running', 'started_at': time.time(),
                  'args_sha256': args_sha256, 'request_sha256': fingerprint,
                  'executable': Path(argv[0]).name, 'argc': len(argv), 'cwd': str(Path(cwd).resolve()),
                  'stdout_path': str(directory / (record_id + '.stdout.log')),
                  'stderr_path': str(directory / (record_id + '.stderr.log'))}
        _atomic_record(path, record)
        code, status = 126, 'failed'
        outcome = {}
        try:
            code = _run_sandboxed(policy, argv, cwd, logs=(Path(record['stdout_path']), Path(record['stderr_path'])), outcome=outcome)
            status = outcome.get('status', 'exited')
            return code
        except SystemExit as error:
            code = error.code if isinstance(error.code, int) else 143
            status = 'cancelled'
            raise
        finally:
            record.update(status=status, exit_code=outcome.get('exit_code'), runner_exit_code=code, logs_complete=outcome.get('logs_complete', True), finished_at=time.time())
            _atomic_record(path, record)

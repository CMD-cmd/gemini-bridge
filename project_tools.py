"""Bounded, read-only project tools. Importing this module has no side effects."""
from __future__ import annotations
import copy
import csv
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import statistics
import time
import uuid

import project_crossref
import project_results
import project_research
import project_documents
import workspace_guard
from workspace import check_scope
from task_mode import scope_current

ROOT = Path(__file__).resolve().parent
MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 100000
MAX_COLUMNS = 200
MAX_RESULT_BYTES = 768 * 1024
ROOT_META_KEY = 'antigravity.google/root_conversation_id'
_TOOLS = [
    {'name': 'file_manifest', 'description': 'Return bytes and SHA-256 for one authorized regular file.',
     'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string', 'pattern': '^/'}},
                     'required': ['path'], 'additionalProperties': False}},
    {'name': 'table_profile', 'description': 'Profile a UTF-8 CSV/TSV table: exact data rows, missing values and finite numeric summaries; no scientific inference.',
     'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string', 'pattern': '^/'},
                     'delimiter': {'type': 'string', 'enum': [',', '\t', ';']}},
                     'required': ['path'], 'additionalProperties': False}},
]

_TOOLS += project_research.TOOL_DEFINITIONS + project_documents.TOOL_DEFINITIONS + project_crossref.TOOL_DEFINITIONS

def validate_tool_arguments(policy,name,args):
    if name=='result_read':return project_results.validate_args(args)
    if name=='literature_search':return project_crossref.validate_args(args,policy.get('network_hosts',[]))
    if name in {t['name'] for t in project_research.TOOL_DEFINITIONS}:
        return project_research.validate_args(name,args,policy.get('network_hosts',[]))
    if name=='document_extract':return project_documents.validate_args(args)
    if not isinstance(args,dict) or set(args)-({'path','delimiter'} if name=='table_profile' else {'path'}) or 'path' not in args:
        raise ValueError('Invalid project tool arguments')
    if name not in ('file_manifest','table_profile'):raise ValueError('Unknown project tool')
    if args.get('delimiter',',') not in (',','\t',';'):raise ValueError('Invalid delimiter')
    return dict(args)

def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def list_tools(policy):
    enabled = policy.get('project_tools', [])
    if not isinstance(enabled, list) or any(x not in {t['name'] for t in _TOOLS} for x in enabled):
        return []
    selected = [copy.deepcopy(tool) for tool in _TOOLS if tool['name'] in enabled]
    if selected:
        selected.append(copy.deepcopy(project_results.TOOL_DEFINITION))
    return selected


def _open(path, flags=os.O_RDONLY, mode=0o600):
    """Walk from / using directory handles; never follow a component symlink."""
    if not isinstance(path, str) or not Path(path).is_absolute() or '\x00' in path or '..' in Path(path).parts:
        raise ValueError('An absolute path without parent traversal is required')
    parts = Path(path).parts[1:]
    if not parts:
        raise ValueError('A regular file is required')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        result = os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=fd)
        info = os.fstat(result)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(result)
            raise ValueError('Only regular, singly linked files are allowed')
        return result
    finally:
        os.close(fd)


def _read(path, limit=MAX_BYTES):
    fd = _open(str(path))
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        if before.st_size > limit:
            raise ValueError('File exceeds byte limit')
        data = source.read(limit + 1)
        after = os.fstat(source.fileno())
        if len(data) > limit:
            raise ValueError('File exceeds byte limit')
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink):
            raise ValueError('Source changed while reading')
        return data


def _json(path):
    value = json.loads(_read(path, 2 * 1024 * 1024))
    if not isinstance(value, dict):
        raise ValueError('Invalid authorization state')
    return value


def authorize_scope(policy):
    """Recheck live job and workspace authorization without native root binding."""
    job = Path(policy['job_dir'])
    state = _json(job / 'state.json')
    if state.get('status') != 'running' or state.get('dispatch_mode') != 'auto' or state.get('cancel_requested'):
        raise ValueError('Task is not accepting project tools')
    if any(os.path.lexists(job / name) for name in ('cancel.requested', 'cancel.flag')):
        raise ValueError('Task cancellation was requested')
    delegation = state.get('delegation')
    if not isinstance(delegation, dict):
        raise ValueError('Missing job delegation')
    taskid, thread = delegation.get('task_id'), delegation.get('thread_id')
    if not isinstance(taskid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', taskid) or not isinstance(thread, str) or not thread:
        raise ValueError('Invalid delegation identity')
    scope = _json(ROOT / '.runtime' / 'task-modes' / (taskid + '.json'))
    if not scope_current(scope) or scope.get('task_id') != taskid or scope.get('thread_id') != thread:
        raise ValueError('Task scope is closed or belongs to another thread')
    identities = scope.get('workspace_identities')
    granted = scope.get('workspace')
    if not isinstance(granted, dict) or not isinstance(identities, dict) or not identities:
        raise ValueError('Workspace identity authorization is missing')
    if set(granted.get('read_roots', []) + granted.get('write_roots', [])) != set(identities):
        raise ValueError('Workspace identities are incomplete')
    check_scope({}, granted, identities)
    saved = _json(job / 'requested-profile.json').get('workspace')
    if not isinstance(saved, dict):
        raise ValueError('Job workspace delegation is missing')
    check_scope(saved, granted, identities)
    return state


def _authorize(policy, rootid):
    state = authorize_scope(policy)
    bound = _json(Path(policy['job_dir']) / 'workspace-guard-state.json').get('conversation_id')
    if not isinstance(rootid, str) or not rootid or not isinstance(bound, str) or rootid != bound:
        raise ValueError('Missing or mismatched root conversation identity')
    return state


def _source(policy, path):
    checked = workspace_guard._path(policy, path)
    roots = policy.get('read_roots', []) + policy.get('write_roots', [])
    if not any(workspace_guard._within(Path(checked), Path(root).resolve()) for root in roots):
        raise ValueError('Path is outside project tool roots')
    # command-results is an explicit native guard read exception, never tool input.
    job = Path(policy['job_dir']).resolve()
    if workspace_guard._within(Path(checked), job) and not any(workspace_guard._within(Path(checked), job / name) for name in ('outputs', 'artifacts', 'deliverables', 'worktree')):
        raise ValueError('Task control files are not project tool inputs')
    if Path(path) != Path(checked):
        raise ValueError('Source path must be canonical and contain no links')
    return checked, _read(path)


def _profile(data, delimiter):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError('Table must be UTF-8') from None
    if '\x00' in text:
        raise ValueError('NUL bytes are not valid table text')
    reader = csv.reader(io.StringIO(text, newline=''), delimiter=delimiter, strict=True)
    previous_limit = csv.field_size_limit(MAX_BYTES)
    try:
        header = next(reader)
        if not header or len(header) > MAX_COLUMNS:
            raise ValueError('Table requires 1 to 200 columns')
        values = [[] for _ in header]
        missing = [0] * len(header)
        numeric = [0] * len(header)
        rows = 0
        for row in reader:
            rows += 1
            if rows > MAX_ROWS:
                raise ValueError('Table exceeds 100000 data rows')
            if len(row) != len(header):
                raise ValueError('Malformed CSV: inconsistent column count')
            for i, cell in enumerate(row):
                cell = cell.strip()
                if not cell:
                    missing[i] += 1
                    continue
                try:
                    number = float(cell)
                except ValueError:
                    continue
                numeric[i] += 1
                if math.isfinite(number):
                    values[i].append(number)
    except StopIteration:
        raise ValueError('Table is empty') from None
    except csv.Error:
        raise ValueError('Malformed CSV') from None
    finally:
        csv.field_size_limit(previous_limit)
    columns = []
    for i, name in enumerate(header):
        finite = values[i]
        count = len(finite)
        mean = statistics.mean(finite) if count else None
        sd = statistics.stdev(finite) if count >= 2 else None
        if any(x is not None and not math.isfinite(x) for x in (mean, sd)):
            raise ValueError('Numeric summary exceeds finite range')
        columns.append({'index': i, 'name': name, 'missing': missing[i], 'numeric': {
            'count': numeric[i], 'finite_count': count, 'nonfinite_count': numeric[i] - count,
            'min': min(finite) if count else None, 'max': max(finite) if count else None,
            'mean': mean, 'std_sample': sd}})
    return {'rows': rows, 'columns': len(header), 'column_profiles': columns,
            'row_definition': 'Data records excluding the header; quoted multiline fields count as one record.',
            'numeric_definition': 'Float-parsable nonempty cells; summaries use finite values only. std_sample is null when finite_count < 2.',
            'missing_definition': 'Empty or whitespace-only cells.'}


def _result_record(policy, result):
    # Pin every parent directory before creating the private result directory.
    job = Path(policy['job_dir'])
    if not job.is_absolute() or '..' in job.parts:
        raise ValueError('Invalid job directory')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in job.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        try:
            os.mkdir('project-tools-results', mode=0o700, dir_fd=fd)
        except FileExistsError:
            pass
        directory = os.open('project-tools-results', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            name = str(uuid.uuid4()) + '.json'
            data = json.dumps(result, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
            output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(output, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
            return {'result_path': 'project-tools-results/' + name,
                    'result_bytes': len(data), 'result_sha256': hashlib.sha256(data).hexdigest()}
        finally:
            os.close(directory)
    finally:
        os.close(fd)


def _audit(policy, name, args, rootid, result):
    receipt = _result_record(policy, result)
    row = {'at': time.time(), 'tool': name, 'args_sha256': _hash(args),
           'root_conversation_id': rootid if isinstance(rootid, str) else None,
           'source_sha256': result.get('source_sha256'), **receipt,
           'summary': {'ok': result['ok'], 'rows': result.get('rows'), 'columns': result.get('columns'), 'bytes': result.get('bytes')}}
    path = str(Path(policy['job_dir']) / 'project-tools-audit.jsonl')
    fd = _open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    with os.fdopen(fd, 'a', encoding='utf-8') as log:
        fcntl.flock(log, fcntl.LOCK_EX)
        log.write(json.dumps(row, allow_nan=False) + '\n')
        log.flush()
        os.fsync(log.fileno())
    return receipt


def handle_call(policy, name, args, root_conversation_id):
    """Return a JSON result; every success and refusal requires a durable audit."""
    sourcehash = None
    try:
        _authorize(policy, root_conversation_id)
        if name not in {t['name'] for t in list_tools(policy)}:
            raise ValueError('Project tool is not authorized')
        selected=validate_tool_arguments(policy,name,args)
        recheck=lambda:_authorize(policy,root_conversation_id)
        if name=='result_read':
            result=project_results.read(policy,selected,root_conversation_id,_read)
            sourcehash=result.get('source_sha256')
        elif name=='literature_search':
            result={'ok':True,'tool':name,**project_crossref.search(selected,policy.get('network_hosts',[]),recheck,time.monotonic()+60,project_research._fetch)}
            sourcehash=_hash([item['sha256'] for item in result.get('sources',[])]);result['source_sha256']=sourcehash
        elif name in {'pubmed_search','public_page'}:
            result={'ok':True,'tool':name,**project_research.execute(name,selected,policy.get('network_hosts',[]),recheck)}
            sourcehash=result.get('source_sha256') or _hash([item['sha256'] for item in result.get('sources',[])])
            result['source_sha256']=sourcehash
        else:
            path,data=_source(policy,selected['path']);sourcehash=hashlib.sha256(data).hexdigest()
            result={'ok':True,'tool':name,'path':path,'bytes':len(data),'source_sha256':sourcehash}
            if name=='file_manifest':result['sha256']=sourcehash
            elif name=='document_extract':result.update(project_documents.extract(data,path,selected,recheck))
            else:
                delimiter=selected.get('delimiter','\t' if path.lower().endswith('.tsv') else ',')
                result.update(_profile(data,delimiter));result['delimiter']=delimiter
        # A source read or profile may outlive the authorization that admitted it.
        _authorize(policy, root_conversation_id)
        encoded = json.dumps(result, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(encoded) > MAX_RESULT_BYTES:
            raise ValueError('Project tool result exceeds 768 KiB serialized JSON limit')
    except (ValueError, TypeError, KeyError, OSError, OverflowError) as error:
        # OS exceptions can expose unrelated path names. Return only their class.
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        result = {'ok': False, 'error': reason, 'source_sha256': sourcehash}
    try:
        receipt = _audit(policy, name, args, root_conversation_id, result)
    except (ValueError, TypeError, KeyError, OSError):
        return {'ok': False, 'error': 'Project tool audit could not be persisted'}
    try:
        return project_results.bounded(result, receipt)
    except (ValueError, TypeError, KeyError, OSError):
        return {'ok': False, 'error': 'Project result could not be represented inline'}

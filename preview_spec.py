"""Authorization for the fixed, task-local static HTML preview capability."""
from pathlib import Path
import os
import stat
import json
import hashlib


def parse_request(policy, argv, cwd, *, read_actions=True):
    if policy.get('preview') is not True:
        raise ValueError('Preview capability is disabled')
    if (not isinstance(argv, list) or len(argv) not in (2,4) or argv[0] != 'bridge-preview'
            or any(not isinstance(x,str) or '\x00' in x for x in argv)
            or (len(argv)==4 and argv[2]!='--actions')):
        raise ValueError('Expected bridge-preview /absolute/index.html [--actions /absolute/actions.json]')
    from workspace_guard import _path
    entry = Path(argv[1])
    if not entry.is_absolute() or '..' in entry.parts or entry.suffix.lower() not in {'.html', '.htm'}:
        raise ValueError('Preview requires an absolute HTML file')
    # Resolve cannot erase symlink provenance before it is inspected.
    if any(p.is_symlink() for p in (entry, *entry.parents)):
        raise ValueError('Preview symlinks are forbidden')
    checked = Path(_path(policy, str(entry), write=True))
    info = checked.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Preview entry must be an ordinary, singly linked file')
    checked_cwd = Path(_path(policy, cwd))
    if not checked_cwd.is_dir():
        raise ValueError('Preview cwd must be a scoped directory')
    result={'entry': str(checked), 'root': str(checked.parent)}
    if len(argv)==4:
        if read_actions:result.update(load_actions(policy,argv[3]))
        else:validate_actions_path(policy,argv[3])
    return result


MAX_ACTION_BYTES=65536
ACTION_FIELDS={'click':{'action','selector'},'fill':{'action','selector','value'},
               'select':{'action','selector','value'},'check':{'action','selector','checked'},
               'press':{'action','selector','key'},'wait_for':{'action','selector','state'}}
PRESS_KEYS={'Enter','Space','Tab','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End'}


def validate_actions(value):
    if not isinstance(value,dict) or set(value)!={'actions'} or not isinstance(value['actions'],list) or len(value['actions'])>20:
        raise ValueError('Expected an actions list with at most 20 actions')
    for item in value['actions']:
        if not isinstance(item,dict) or not isinstance(item.get('action'),str) or item.get('action') not in ACTION_FIELDS or set(item)!=ACTION_FIELDS[item['action']]:
            raise ValueError('Unsupported preview action schema')
        selector=item.get('selector')
        if not isinstance(selector,str) or not 1<=len(selector)<=256 or any(c in selector for c in ('\x00','\n','\r','>>')):
            raise ValueError('Expected a bounded CSS selector')
        kind=item['action']
        if kind in ('fill','select') and (not isinstance(item['value'],str) or len(item['value'])>2048 or '\x00' in item['value']):
            raise ValueError('Expected bounded text or option value')
        if kind=='check' and type(item['checked']) is not bool:raise ValueError('Expected a checked boolean')
        if kind=='press' and (not isinstance(item['key'],str) or item['key'] not in PRESS_KEYS):raise ValueError('Unsupported preview key')
        if kind=='wait_for' and item['state'] not in ('visible','hidden','attached','detached'):raise ValueError('Unsupported wait state')
    return value['actions']


def validate_actions_path(policy,source):
    from workspace_guard import _path
    path=Path(source)
    if not path.is_absolute() or '..' in path.parts or path.suffix.lower()!='.json':raise ValueError('Actions require an absolute JSON file')
    # Actions may be in any authorized read root; no write permission is added.
    if any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('Actions symlinks are forbidden')
    checked=Path(_path(policy,str(path)))
    if checked!=path:raise ValueError('Actions path changed during authorization')
    return checked


def load_actions(policy,source):
    checked=validate_actions_path(policy,source)
    parent=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in checked.parts[1:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            os.close(parent);parent=child
        fd=os.open(checked.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
    finally:os.close(parent)
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>MAX_ACTION_BYTES:raise ValueError('Actions require an ordinary singly linked file <=64KiB')
        raw=stream.read(MAX_ACTION_BYTES+1);after=os.fstat(stream.fileno())
    if len(raw)>MAX_ACTION_BYTES or (before.st_dev,before.st_ino,before.st_nlink,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_nlink,after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise ValueError('Actions changed during read')
    actions=validate_actions(json.loads(raw))
    return {'actions':actions,'actions_source':{'path':str(checked),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'count':len(actions)}}


def record_preflight_failure(policy, request, error):
    """Record a trusted, claimed preview failure even when the HTML no longer exists."""
    import fcntl
    import json
    import re
    import time
    from workspace_guard import _atomic_record, _digest
    try:
        record_id = request.get('record_id')
        if not isinstance(record_id, str) or not re.fullmatch(r'[0-9a-f]{64}', record_id):
            return False
        job = Path(policy['job_dir']).resolve()
        raw_directory = Path(policy['command_results_dir'])
        directory = raw_directory.resolve()
        if directory != job / 'command-results' or raw_directory.is_symlink():
            return False
        guard = json.loads((job / 'workspace-guard-state.json').read_text())
        selected = {key: request.get(key) for key in ('argv', 'cwd', 'args_sha256')}
        if guard.get('command_requests', {}).get(record_id) != selected:
            return False
        if not isinstance(selected['argv'], list) or not selected['argv'] or selected['argv'][0] != 'bridge-preview':
            return False
        if not isinstance(selected['cwd'], str) or not Path(selected['cwd']).is_absolute():
            return False
        directory.mkdir(mode=0o700, exist_ok=True)
        lock_path = directory / (record_id + '.lock')
        if lock_path.is_symlink() or (lock_path.exists() and lock_path.stat().st_nlink != 1):
            return False
        with lock_path.open('a+') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = directory / (record_id + '.json')
            if path.is_symlink():
                return False
            if path.exists():
                return not path.is_symlink() and path.is_file() and path.stat().st_nlink == 1
            stdout = directory / (record_id + '.stdout.log')
            stderr = directory / (record_id + '.stderr.log')
            for log in (stdout, stderr):
                if log.is_symlink() or (log.exists() and (not log.is_file() or log.stat().st_nlink != 1)):
                    return False
            stdout.write_text('')
            stderr.write_text(type(error).__name__ + ': ' + str(error)[:512] + '\n')
            stdout.chmod(0o600); stderr.chmod(0o600)
            now = time.time()
            _atomic_record(path, {'record_id': record_id, 'status': 'failed', 'runner_exit_code': 126,
                'exit_code': None, 'logs_complete': True, 'preview_started': False,
                'started_at': now, 'finished_at': now, 'args_sha256': selected['args_sha256'],
                'request_sha256': _digest({'argv': selected['argv'], 'cwd': str(Path(selected['cwd']).resolve()),
                                          'args_sha256': selected['args_sha256']}),
                'stdout_path': str(stdout), 'stderr_path': str(stderr), 'cwd': selected['cwd'],
                'executable': 'bridge-preview', 'argc': len(selected['argv'])})
            return True
    except (OSError, ValueError, KeyError, TypeError):
        return False

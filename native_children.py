"""Bounded native-child approvals and evidence; caller holds workspace state lock.

No API calls are made here. A requested kill is never evidence of remote termination.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
import uuid

MAX_EVENT_BYTES = 8 * 1024 * 1024
EDGE_WAIT_SECONDS = 2.0
EDGE_POLL_SECONDS = 0.05


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _config(policy):
    from native_child_profiles import validate_roles
    cfg = dict(policy.get('native_subagents', {}))
    if cfg.get('enabled') is not True:
        raise ValueError('Native children are disabled')
    if (type(cfg.get('max_children')) is not int or not 1 <= cfg['max_children'] <= 4
            or cfg.get('model') != policy.get('model') or not cfg.get('model')
            or cfg.get('effort') != policy.get('effort') or not cfg.get('effort')):
        raise ValueError('Native children require the exact parent model and effort')
    cfg['capacity_mode'] = cfg.get('capacity_mode', 'lifetime')
    if cfg['capacity_mode'] not in ('lifetime', 'concurrent'):
        raise ValueError('Invalid native child capacity_mode')
    cfg['roles'] = validate_roles(cfg.get('roles'))
    names = cfg.get('type_names')
    if names is None and cfg['roles'] == ['reader']:
        names = {'reader': cfg.get('type_name')}
    if (not isinstance(names, dict) or set(names) != set(cfg['roles'])
            or any(not isinstance(v, str) or not v.strip() for v in names.values())
            or len(set(names.values())) != len(names)):
        raise ValueError('Each configured native role needs a unique exact TypeName')
    cfg['type_names'] = names
    return cfg


def _role(cfg, type_name):
    for role, name in cfg['type_names'].items():
        if name == type_name:
            return role
    raise ValueError('Only the configured native child types are allowed')


def _slot_root(policy, step, index):
    """Create an exclusive, nonsymlink output slot under the job artifact tree."""
    job = Path(policy['job_dir']).resolve(strict=True)
    current = job
    for part in ('artifacts', 'delegates', 'step-' + str(step) + '-slot-' + str(index)):
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            # Only ancestor directories may preexist; a new slot must be exclusive.
            if part.startswith('step-'):
                raise ValueError('Native output slot already exists')
        if current.is_symlink() or not current.is_dir() or current.resolve() != current:
            raise ValueError('Native output slot path is unsafe')
    return str(current)


def _root(state):
    root = state.get('conversation_id')
    if not _uuid(root):
        raise ValueError('Root conversation must already be bound')
    return root


def _identity(policy, payload, conversation, cfg):
    if not _uuid(conversation) or payload.get('conversationId') != conversation:
        raise ValueError('Unexpected conversation identity')
    if payload.get('modelName') != cfg['model']:
        raise ValueError('Unexpected child/parent model')
    paths = payload.get('workspacePaths')
    if not isinstance(paths, list) or str(Path(policy['job_dir']).resolve()) not in paths:
        raise ValueError('Unexpected child/parent workspace')
    artifact = Path.home() / '.gemini/antigravity-cli/brain' / conversation
    if payload.get('artifactDirectoryPath') != str(artifact):
        raise ValueError('Unexpected child/parent artifact directory')


def _state(state):
    return state.setdefault('native_subagents', {'approvals': {}, 'children': {}})


def _call(payload, name):
    call = payload.get('toolCall', {})
    if call.get('name') != name or not isinstance(call.get('args'), dict):
        raise ValueError('Unexpected native child tool schema')
    return call['args']


def _spawn_specs(args, cfg):
    """Validate verified flat and declared Subagents calls without changing shape.

    Verified native spawn calls used flat fields;
    their original conversation JSON hashes match the guard audit. Model is
    accepted only as the observed literal 'inherit', never as an override.
    """
    if 'Subagents' in args:
        if set(args) != {'Subagents'}:
            raise ValueError('Unexpected native invocation fields')
        specs = args['Subagents']
    else:
        specs = [args]
    if not isinstance(specs, list) or not specs:
        raise ValueError('Only explicit Subagents are accepted')
    normalized = []
    required = {'Prompt', 'Role', 'TypeName'}
    for spec in specs:
        if (not isinstance(spec, dict) or not required <= set(spec)
                or set(spec) - required - {'Workspace', 'Model'}
                or any(not isinstance(spec[k], str) or not spec[k].strip() for k in required)
                or spec['TypeName'] not in cfg['type_names'].values()):
            raise ValueError('Only the configured native child type is allowed')
        if 'Workspace' in spec and spec['Workspace'] != '':
            raise ValueError('Native child workspace must remain inherited')
        if 'Model' in spec and spec['Model'] != 'inherit':
            raise ValueError('Native child model must remain inherited')
        normalized.append({k: spec[k] for k in ('Prompt', 'Role', 'TypeName')})
    if len({s['Role'] for s in normalized}) != len(normalized):
        raise ValueError('Native slot Role labels must be unique within an invocation')
    return normalized


def _released(ns):
    """Only a proven child with its own actual Stop hook releases capacity."""
    slots = {slot.get('child_id') for approval in ns.get('approvals', {}).values()
             for slot in approval['specs'] if slot.get('child_id')}
    return sum(child_id in slots and child.get('stop_observed') is True
               and any(event.get('event') == 'Stop' for event in child.get('lifecycle', []))
               for child_id, child in ns.get('children', {}).items())


def approve_spawn(policy, payload, state):
    """Reserve bounded child capacity, idempotently for one root invoke step."""
    cfg = _config(policy)
    root = _root(state)
    _identity(policy, payload, root, cfg)
    args = _call(payload, 'invoke_subagent')
    specs = _spawn_specs(args, cfg)
    step = payload.get('stepIdx')
    if type(step) is not int or step < 0:
        raise ValueError('Native invocation needs a nonnegative stepIdx')
    ns = _state(state)
    _reconcile_lifecycle(policy,state,cfg)
    if ns.get('identity_failures'):
        raise ValueError('Native child identity failure prevents further delegation')
    key, digest = str(step), _digest(args)
    old = ns['approvals'].get(key)
    if old is not None:
        if old['args_sha256'] != digest or old['conversation_id'] != root:
            raise ValueError('Invocation step conflicts with prior approval')
    else:
        labels = {slot['requested_role'] for approval in ns['approvals'].values() for slot in approval['specs']}
        if labels.intersection(spec['Role'] for spec in specs):
            raise ValueError('Native slot Role labels must remain unique for this task')
        reserved = sum(a['reserved'] for a in ns['approvals'].values())
        occupied = reserved - (_released(ns) if cfg['capacity_mode'] == 'concurrent' else 0)
        if occupied + len(specs) > cfg['max_children']:
            raise ValueError('Native child quota exceeded')
        slots = []
        for index, spec in enumerate(specs):
            role = _role(cfg, spec['TypeName'])
            slots.append({'type_name': spec['TypeName'], 'role': role,
                          'requested_role': spec['Role'], 'slot': index,
                          'write_root': _slot_root(policy, step, index) if role == 'builder' else None})
        ns['approvals'][key] = {'conversation_id': root, 'stepIdx': step,
            'args_sha256': digest, 'reserved': len(specs), 'specs': slots}
    approval = ns['approvals'][key]
    rewritten = []
    for spec, slot in zip(specs, approval['specs']):
        suffix = ('\nYour parent conversation ID is ' + root +
                  '. Send findings only to this parent. Do not delegate, use MCP, or run commands.')
        if slot['role'] == 'builder':
            suffix += (' Your only writable output directory is ' + slot['write_root'] +
                       '. Deliver files or patches here; the parent integrates and runs commands. '
                       'Do not edit the shared worktree, other delegates, or control files.')
        else:
            suffix += ' You are read-only. Read relevant scoped sources before reporting evidence.'
        rewritten.append(dict(spec, Prompt=spec['Prompt'] + suffix))
    overwrite = {'Subagents': rewritten} if 'Subagents' in args else rewritten[0]
    return {'decision': 'auto_approve', 'overwrite': overwrite,
            'reason': 'Bounded native child reservation'}


def _events(policy):
    path = Path(policy.get('events_path', Path(policy['job_dir']) / 'events.jsonl'))
    try:
        with path.open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            offset = max(0, size - MAX_EVENT_BYTES)
            previous = b'\n'
            if offset:
                stream.seek(offset - 1)
                previous = stream.read(1)
            else:
                stream.seek(0)
            raw = stream.read(MAX_EVENT_BYTES)
            if offset and previous != b'\n':
                # The bounded window begins in an older record. Never parse or
                # treat that partial prefix as evidence of an unknown old edge.
                raw = raw.split(b'\n', 1)[1] if b'\n' in raw else b''
    except FileNotFoundError:
        return []
    records = []
    lines = raw.splitlines(keepends=True)
    for i, line in enumerate(lines):
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            # An append in progress can leave only the final record incomplete.
            if i == len(lines) - 1 and not line.endswith(b'\n'):
                continue
            raise ValueError('Malformed native events')
        if isinstance(record, dict) and record.get('event') == 'step_update':
            step = record.get('step_update')
            if isinstance(step, dict):
                records.append(step)
    return records


def _edges(policy, state, cfg):
    ns, root = _state(state), _root(state)
    candidates = {}
    for event in _events(policy):
        step = event.get('step_index')
        if event.get('conversation_id') != root or type(step) is not int:
            continue
        approval = ns['approvals'].get(str(step))
        if not approval or approval['conversation_id'] != root:
            continue
        info = event.get('subagent_info', {})
        edges = info.get('subagents', []) if isinstance(info, dict) else []
        if not isinstance(edges, list):
            continue
        for edge in edges:
            if (not isinstance(edge, dict) or not _uuid(edge.get('conversation_id'))
                    or edge['conversation_id'] == root):
                continue
            # Role is free-form model text. Authority comes only from exact TypeName.
            matches = [s for s in approval['specs'] if s['type_name'] == edge.get('type_name')
                       and s['requested_role'] == edge.get('role')]
            if not matches:
                continue
            child_id = edge['conversation_id']
            record = {'conversation_id': child_id, 'parent_conversation_id': root,
                'stepIdx': step, 'type_name': edge['type_name'], 'role': _role(cfg, edge['type_name']),
                'requested_role': edge.get('role'),
                'log_uri': edge.get('log_uri'), 'workspace_uris': edge.get('workspace_uris')}
            if child_id in candidates and candidates[child_id] != record:
                raise ValueError('Conflicting native child edges')
            candidates[child_id] = record
    for child_id, child in ns['children'].items():
        prior = {k: child.get(k) for k in ('conversation_id', 'parent_conversation_id',
                 'stepIdx', 'type_name', 'role', 'requested_role', 'log_uri', 'workspace_uris')}
        if child_id not in candidates:
            candidates[child_id] = prior
        elif any(candidates[child_id][k] != prior[k] for k in
                 ('parent_conversation_id', 'stepIdx', 'type_name', 'role')):
            raise ValueError('Native child identity changed')
    assignments = []
    for key, approval in ns['approvals'].items():
        group = [c for c in candidates.values() if c['stepIdx'] == int(key)]
        if len(group) > approval['reserved']:
            raise ValueError('Native event exceeds approved child reservation')
        used = set()
        for child in group:
            slots = [s for s in approval['specs'] if s.get('child_id') == child['conversation_id']]
            if not slots:
                slots = [s for s in approval['specs'] if not s.get('child_id')
                         and s['slot'] not in used and s['type_name'] == child['type_name']
                         and s['requested_role'] == child['requested_role']]
            if not slots:
                raise ValueError('Native child role multiplicity exceeds approval')
            slot = slots[0]
            used.add(slot['slot'])
            child.update(slot=slot['slot'], write_root=slot['write_root'])
            assignments.append((slot, child['conversation_id']))
    # Commit bindings only once the entire observed event set has passed checks.
    for slot, child_id in assignments:
        slot['child_id'] = child_id
    return candidates  # Previously bound UUID edges remain evidence after tail rotation.


def _model_evidence(cfg,payload):
    return {'modelName':payload['modelName'],'effort':cfg['effort'],
            'basis':'exact model slug including suffix and inherited parent selection',
            'independent_backend_trace':False}


def _bind_edge(ns,edge,evidence):
    child_id=edge['conversation_id'];old=ns['children'].get(child_id)
    if old and any(old.get(k)!=edge[k] for k in ('parent_conversation_id','stepIdx','type_name','role')):
        raise ValueError('Native child identity changed')
    if old is None:
        old=dict(edge);ns['children'][child_id]=old
    old['model_evidence']=dict(evidence)
    return old


def _record_lifecycle(child,item):
    item=dict(item)
    if item.get('event')=='PreInvocation':
        if child.get('resume_requested') or child.get('stop_observed'):
            child['turn_index']=child.get('turn_index',0)+1
        child['resume_requested']=False
        child['stop_observed']=False
    if item.get('event')=='Stop' and not child.get('resume_requested'):
        child['stop_observed']=True
    if child.get('turn_index'):item['turn_index']=child['turn_index']
    events=child.setdefault('lifecycle',[])
    if item not in events:events.append(item)


def _reconcile_lifecycle(policy,state,cfg):
    ns=_state(state);pending=ns.get('unbound_observations',[])
    denied=ns.get('unbound_identity_failures',[])
    if not (pending or denied) or not _uuid(state.get('conversation_id')):return
    # Old retained observations lack the identity receipt needed for new binding.
    # Reading historical summaries must not reinterpret their legacy edge schema.
    trusted=any((o.get('model_evidence') or {}).get('modelName')==cfg['model']
                and (o.get('model_evidence') or {}).get('effort')==cfg['effort'] for o in pending)
    if not trusted and not denied:return
    edges=_edges(policy,state,cfg);remaining=[]
    for observation in pending:
        evidence=observation.get('model_evidence') or {}
        edge=edges.get(observation.get('conversation_id'))
        if edge is None or evidence.get('modelName')!=cfg['model'] or evidence.get('effort')!=cfg['effort']:
            remaining.append(observation);continue
        child=_bind_edge(ns,edge,evidence)
        _record_lifecycle(child,{k:v for k,v in observation.items() if k not in ('conversation_id','model_evidence')})
    ns['unbound_observations']=remaining
    remaining=[]
    for observation in denied:
        if observation.get('conversation_id') in edges:
            record_identity_failure(policy,state,observation)
        else:remaining.append(observation)
    ns['unbound_identity_failures']=remaining


def record_identity_failure(policy,state,observation):
    failures=_state(state).setdefault('identity_failures',[])
    if observation not in failures:failures.append(dict(observation))
    (Path(policy['job_dir'])/'native-child-stop.requested').write_text(observation['reason'])


def resolve_child(policy, payload, state):
    """Bind only a child proven by an approved root invocation's native edge."""
    cfg = _config(policy)
    root = _root(state)
    child_id = payload.get('conversationId')
    if child_id == root:
        raise ValueError('Root is not a child')
    _identity(policy, payload, child_id, cfg)
    ns = _state(state)
    deadline = time.monotonic() + EDGE_WAIT_SECONDS
    while True:
        edge = _edges(policy, state, cfg).get(child_id)
        if edge is not None:
            old=_bind_edge(ns,edge,_model_evidence(cfg,payload))
            _reconcile_lifecycle(policy,state,cfg)
            return old
        if time.monotonic() >= deadline:
            raise ValueError('No approved root-to-child native edge')
        time.sleep(min(EDGE_POLL_SECONDS, max(0, deadline - time.monotonic())))


def authorize_message_control(policy, payload, state):
    cfg, root = _config(policy), _root(state)
    ns = _state(state)
    _reconcile_lifecycle(policy,state,cfg)
    sender = payload.get('conversationId')
    if sender != root and sender not in ns['children']:
        raise ValueError('Unidentified child cannot control messages')
    _identity(policy, payload, sender, cfg)
    name = payload.get('toolCall', {}).get('name')
    if name == 'send_message':
        args = _call(payload, name)
        if (set(args) != {'Recipient', 'Message'} or not isinstance(args['Message'], str)
                or not args['Message'].strip()):
            raise ValueError('Invalid native message')
        recipient = args['Recipient']
        if not isinstance(recipient, str):
            raise ValueError('Native message recipient must be one conversation ID')
        if not ((sender == root and recipient in ns['children']) or (sender in ns['children'] and recipient == root)):
            raise ValueError('Native messages must stay on a proven parent-child edge')
        if sender==root and ns['children'][recipient].get('stop_observed'):
            if cfg['capacity_mode']=='concurrent':
                occupied=sum(a['reserved'] for a in ns['approvals'].values())-_released(ns)
                if occupied>=cfg['max_children']:
                    raise ValueError('Native child quota exceeded when resuming an idle child')
            # Messaging an idle child starts another turn. Reserve before dispatch;
            # a duplicate old Stop cannot release it before a new PreInvocation.
            ns['children'][recipient].update(resume_requested=True,stop_observed=False)
        return {'decision': 'auto_approve', 'reason': 'Proven task parent-child message'}
    if name != 'manage_subagents' or sender != root:
        raise ValueError('Only root may manage native children')
    args = _call(payload, name)
    if set(args) - {'Action', 'ConversationIds'}:
        raise ValueError('Invalid native management arguments')
    action, ids = args.get('Action'), args.get('ConversationIds')
    if action == 'list':
        if ids is not None and ids != []:
            raise ValueError('List cannot target conversations')
        return {'decision': 'auto_approve', 'reason': 'Root native child listing'}
    if action == 'kill_all':
        if ids is not None and ids != []:
            raise ValueError('kill_all cannot provide conversation IDs')
        overwrite = {'Action': 'kill', 'ConversationIds': sorted(ns['children'])} if ns['children'] else {'Action': 'list'}
    elif action == 'kill':
        if (not isinstance(ids, list) or not ids or any(not isinstance(c, str) or c not in ns['children'] for c in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('Kill may only target identified task children')
        overwrite = {'Action': 'kill', 'ConversationIds': ids}
    else:
        raise ValueError('Unsupported native child action')
    ns.setdefault('control_requests', []).append({'stepIdx': payload.get('stepIdx'), 'args': overwrite})
    return {'decision': 'auto_approve', 'overwrite': overwrite,
            'reason': 'Scoped native control request; remote stop remains unconfirmed'}


def observe_lifecycle(policy, payload, state, event):
    """Record actual hook observations, never turn a kill request into a Stop."""
    if event not in {'SessionStart', 'PreInvocation', 'Stop'}:
        raise ValueError('Unsupported native child lifecycle event')
    cfg = _config(policy)
    root = _root(state)
    child_id = payload.get('conversationId')
    if child_id == root:
        return None
    _identity(policy, payload, child_id, cfg)
    ns = _state(state)
    item = {'event': event, 'stepIdx': payload.get('stepIdx')}
    if event == 'Stop':
        for key in ('terminationReason', 'error', 'fullyIdle'):
            if key in payload:
                item[key] = payload[key]
    child = ns['children'].get(child_id)
    if child is None:
        # Hooks may precede the parent's stdout edge. Observation grants nothing.
        ns.setdefault('unbound_observations', []).append({'conversation_id': child_id,
            'model_evidence':_model_evidence(cfg,payload),**item})
        _reconcile_lifecycle(policy,state,cfg)
        return ns['children'].get(child_id)
    _record_lifecycle(child,item)
    return child


def summarize(policy, state):
    cfg = _config(policy)
    _reconcile_lifecycle(policy,state,cfg)
    ns = state.get('native_subagents', {})
    children = ns.get('children', {})
    requested = sum(a['reserved'] for a in ns.get('approvals', {}).values())
    stopped = sum(c.get('stop_observed') is True for c in children.values())
    return {'requested': requested, 'identified': len(children), 'stop_observed': stopped,
            'identity_failures':ns.get('identity_failures',[]),
            'capacity_mode': cfg['capacity_mode'],
            'capacity_used': requested - (_released(ns) if cfg['capacity_mode'] == 'concurrent' else 0),
            'unknown': max(0, requested - len(children)),
            'active_or_stop_unobserved': sorted(k for k, c in children.items() if not c.get('stop_observed')),
            'model': cfg['model'], 'effort': cfg['effort'],
            'model_evidence': {k: c.get('model_evidence') for k, c in children.items()},
            'children': [dict(c, status='stop_observed' if c.get('stop_observed') else 'active_or_stop_unobserved',
                              stop_evidence=[e for e in c.get('lifecycle', []) if e.get('event') == 'Stop'])
                         for _, c in sorted(children.items())],
            'remote_termination_proven': False}

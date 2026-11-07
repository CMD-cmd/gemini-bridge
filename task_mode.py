"""Per-task delegation authorization and frozen model policy checks.

This enforces declared scope and runtime ceilings. User intent and whether
prompt text is appropriate to share remain the coordinating agent's duty.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
from input_limits import load_limits
from bridge import ROOT, atomic_json, load_profile, ensure_account_route

PURPOSES = ('general', 'tone_polish', 'important_communication', 'sensitive_communication',
            'key_copy', 'technical_second_opinion', 'technical_deep_review')
KINDS = ('copy', 'english', 'extract', 'code-map', 'visual', 'prototype', 'image')
HOME = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
ROUTING = Path(os.environ.get('GEMINI_BRIDGE_ROUTING_DIR') or ROOT / 'routing')


class RuntimePending(ValueError):
    """Evidence is being flushed, not a permanent model/policy rejection."""


def runtime_decision(thread_id, profile, *, executing=False, task_purpose=None, kind=None, dispatch_mode=None):
    spec = importlib.util.spec_from_file_location('external_routing_resolver', ROUTING / 'resolve.py')
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)
    raw = (ROUTING / 'policy.json').read_bytes()
    policy = json.loads(raw)
    try:
        model, effort, evidence = resolver.read_parent(HOME, thread_id, require_active=not executing)
    except resolver.RuntimeNotReady as error:
        raise RuntimePending(str(error)) from error
    context = {k: v for k, v in {'task_purpose': task_purpose, 'kind': kind, 'dispatch_mode': dispatch_mode}.items() if v is not None}
    decision = resolver.decide_external(policy, model, effort, profile['model'], profile['effort'], **context)
    if decision.get('enabled') and decision.get('required_billing_route'):
        ensure_account_route()
    decision.update(evidence=evidence, policy_sha256=hashlib.sha256(raw).hexdigest())
    return decision


def scope_decision(thread_id, *, executing=False):
    profile = load_profile(ROOT/'profile.json')
    default = runtime_decision(thread_id, profile, executing=executing)
    if default['enabled']:
        return default
    policy = json.loads((ROUTING/'policy.json').read_text())
    for record in policy.get('external_models', {}).values():
        if record.get('automatic_eligible', True) is True:
            decision = runtime_decision(thread_id, {'model':record['model'],'effort':record['reasoning_effort']}, executing=executing)
            if decision['enabled']:
                decision['scope_eligibility_note']='Default profile rejected; an eligible mapped candidate exists. Submission selects from live catalog.'
                decision['default_profile_decision']=default
                return decision
    return default


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f-]{36}', value):
        raise ValueError('Expected task/thread UUID')
    return value


def high_tier_quote_valid(quote):
    # The coordinator records direct human intent; this checks the quoted form.
    if not isinstance(quote, str) or not quote.strip():
        return False
    if re.search(r'(?<![\w-])\$gemini-bridge(?![\w-])', quote):
        return True
    if re.search(r'不(?:要|能|可|允许|同意|准)|禁止|勿|怎么|如何|是否|能否|可不可以|[？?]|do not|don.t', quote, re.I):
        return False
    model = r'(?:gemini\s*)?4\s*argon'
    return bool(re.search(r'(?:可以|允许|同意|请|使用|调用|交给|用|allow|use).{0,30}' + model,
                          quote, re.I) or
                re.search(model + r'.{0,20}(?:可以使用|可以调用|可以用|允许使用|允许调用|随便用|可用)',
                          quote, re.I))


def chat_opt_in_quote_valid(quote):
    """Check a coordinator-recorded direct permission, not arbitrary mentions.

    The coordinator must select the actual user instruction from this chat.
    This deliberately does not treat the account's standing Ultra grant as
    chat consent or claim to infer intent from the whole transcript.
    """
    if not isinstance(quote, str) or not quote.strip():
        return False
    target = r'(?:\$?gemini(?:-bridge)?|antigravity|(?:gemini\s*)?4\s*argon)'
    if re.search(r'(?:不(?:要|能|可|允许|同意|准)|禁止|勿|停用|关闭|禁用|不再|do not|don.t|disable).{0,16}' + target, quote, re.I):
        return False
    # A policy condition or a request for instructions is not an opt-in.
    if re.search(r'只有|除非|如果|(?:only\s+when|\bif\b)|怎么|如何', quote, re.I):
        return False
    invoked = bool(re.search(r'(?<![\w-])(?:\$|@)gemini-bridge(?![\w-])', quote, re.I) or
                   re.search(r'\[[^\]\n]+\]\(plugin://gemini-bridge-local@gemini-bridge-local\)', quote, re.I))
    if re.search(r'是否|能否|可不可以', quote, re.I):
        # A tagged plugin plus a direct request to test an actual reply is an
        # invocation, even when the result is phrased as "whether it succeeds".
        # Keep ordinary capability/configuration questions and read-only checks off.
        return bool(invoked and
                    re.search(r'^(?:请(?:你)?|帮我|麻烦(?:你)?|现在|再|那|先|\s)*(?:看一下|检查|验证|测试|试一下|试试)', quote) and
                    re.search(r'(?:调用|生成|回复).{0,12}(?:成功|正常|返回|可用)|(?:正常|成功).{0,8}(?:生成|回复|调用)', quote) and
                    not re.search(r'不要|不必|不需|不用|不允许|暂不|只读|先别|禁止|勿|文档|教程|原理|do not|don.t|read.only', quote, re.I))
    if invoked:
        return True
    if re.search(r'[？?]', quote):
        return False
    verb = r'(?:可以(?:使用|调用|用)|允许(?:使用|调用|用)?|同意(?:使用|调用|用)?|请(?:使用|调用|用)|启用|交给|交由|让|调用|用|allow|use|enable)'
    return bool(re.search(verb + r'.{0,24}' + target, quote, re.I) or
                re.search(target + r'.{0,16}(?:可以用|可以使用|可以调用|随便用|可用|is allowed|may be used)', quote, re.I))


def chat_scope_authorized(state):
    proof = state.get('chat_authorization')
    if isinstance(proof, dict):
        return (proof.get('thread_id') == state.get('thread_id')
                and proof.get('basis') == 'coordinator_recorded_direct_user_permission'
                and chat_opt_in_quote_valid(proof.get('quote')))
    # Existing explicit task permissions remain valid. An inherited global
    # Ultra quote must never become permission for a different chat.
    if chat_opt_in_quote_valid(state.get('authorization')):
        return True
    return (state.get('high_tier_authorization_basis') != 'standing_ultra_authorization'
            and chat_opt_in_quote_valid(state.get('high_tier_authorization')))


def access_settings():
    path = ROOT / 'model-routing.json'
    value = json.loads(path.read_text()).get('access', {}) if path.exists() else {}
    if not isinstance(value, dict):
        raise ValueError('Invalid Bridge access configuration')
    standing = value.get('standing_high_tier', False)
    opt_in = value.get('require_chat_opt_in', False)
    maximum = value.get('default_max_jobs', 3)
    ttl = value.get('scope_ttl_seconds', 86400)
    if (type(standing) is not bool or type(opt_in) is not bool or type(maximum) is not int or maximum < 0
            or ttl is not None and (type(ttl) is not int or ttl < 1)):
        raise ValueError('Invalid Bridge access limits')
    if standing and (value.get('basis') != 'user_authorized_ultra'
                     or not isinstance(value.get('authorization'), str) or not value['authorization'].strip()):
        raise ValueError('Standing high-tier access requires recorded user authorization')
    return {**value, 'standing_high_tier':standing, 'require_chat_opt_in':opt_in, 'default_max_jobs':maximum,
            'scope_ttl_seconds':ttl}


def scope_current(state):
    expiry = state.get('expires_at', 0)
    return bool(state.get('enabled') and (expiry is None or expiry > time.time()))


def high_scope_authorized(state):
    access = access_settings()
    if access['require_chat_opt_in']:
        return bool(chat_scope_authorized(state)
                    and (access['standing_high_tier'] or high_tier_quote_valid(state.get('high_tier_authorization'))))
    if high_tier_quote_valid(state.get('high_tier_authorization')):
        return True
    return bool(access['standing_high_tier']
                and state.get('high_tier_authorization_basis') == 'standing_ultra_authorization'
                and state.get('high_tier_authorization') == access.get('authorization'))


def model_usage(profile):
    """Use current policy at both submission and execution; absent tiers are high."""
    policy = json.loads((ROUTING / 'policy.json').read_text())
    key = str(profile.get('model')) + '/' + str(profile.get('effort'))
    record = policy.get('external_models', {}).get(key, {})
    tier = record.get('usage_tier', 'high')
    if tier not in ('low', 'high'):
        tier = 'high'
    return tier, record.get('specialist_only') is True


class TaskModes:
    def __init__(self, root=None):
        self.root = root or ROOT / '.runtime' / 'task-modes'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()

    def read(self, task_id):
        return json.loads((self.root / (identifier(task_id) + '.json')).read_text())

    def write(self, state):
        atomic_json(self.root / (state['task_id'] + '.json'), state)

    def control(self, args):
        with self.lock:
            allowed = {'action', 'thread_id', 'task_id', 'scope', 'authorization',
                       'allowed_kinds', 'allowed_files', 'allowed_purposes', 'allowed_model_tiers',
                       'high_tier_authorization', 'material_class', 'max_jobs', 'workspace'}
            if set(args) - allowed:
                raise ValueError('Unknown task-mode fields')
            action = args.get('action', 'status')
            thread_id = identifier(args.get('thread_id'))
            if action == 'enable':
                if args.get('task_id'):
                    raise ValueError('Enable creates a new scoped task; cannot reset an existing task budget')
                scope, authorization = args.get('scope'), args.get('authorization')
                if not isinstance(scope, str) or not 1 <= len(scope.strip()) <= 500:
                    raise ValueError('A concrete task scope is required')
                if not isinstance(authorization, str) or not 1 <= len(authorization.strip()) <= 500:
                    raise ValueError('Record the direct user authorization; do not infer global consent')
                access = access_settings()
                chat_quote = next((q for q in (authorization, args.get('high_tier_authorization'))
                                   if chat_opt_in_quote_valid(q)), None)
                if access['require_chat_opt_in'] and chat_quote is None:
                    raise ValueError('Gemini is off in this chat: record explicit user permission to use Gemini or a direct $gemini-bridge invocation; an Ultra subscription alone is not chat consent')
                # Proves the exact supplied chat is active; does not infer identity from defaults.
                decision = scope_decision(thread_id)
                kinds = args.get('allowed_kinds', [k for k in KINDS if k != 'image'])
                if not isinstance(kinds, list) or not kinds or len(kinds) > len(KINDS) or any(k not in KINDS for k in kinds):
                    raise ValueError('Invalid allowed task kinds')
                purposes = args.get('allowed_purposes', list(PURPOSES))
                if (not isinstance(purposes, list) or not purposes or len(purposes) > len(PURPOSES)
                        or any(p not in PURPOSES for p in purposes)):
                    raise ValueError('Invalid allowed task purposes')
                tiers = args.get('allowed_model_tiers', ['low','high'] if access['standing_high_tier'] else ['low'])
                if (not isinstance(tiers, list) or not tiers or len(tiers) > 2
                        or any(t not in ('low', 'high') for t in tiers)):
                    raise ValueError('Invalid allowed model tiers')
                high_quote = args.get('high_tier_authorization')
                high_basis = 'coordinator_recorded_quote; user intent remains coordinator responsibility'
                if 'high' in tiers and access['require_chat_opt_in'] and access['standing_high_tier']:
                    high_quote, high_basis = chat_quote, 'chat_opt_in_with_ultra_tier_access'
                elif 'high' in tiers and not high_tier_quote_valid(high_quote):
                    if access['standing_high_tier']:
                        high_quote, high_basis = access['authorization'], 'standing_ultra_authorization'
                    else:
                        raise ValueError('High tier requires standing Ultra authorization or explicit $gemini-bridge invocation')
                if high_quote is not None and not isinstance(high_quote, str):
                    raise ValueError('High tier authorization must be original quoted text')
                files = args.get('allowed_files', [])
                if not isinstance(files, list) or len(files) > load_limits(ROOT)['MAX_FILES'] or any(
                    not isinstance(p, str) or not Path(p).is_absolute() or Path(p).is_symlink() for p in files):
                    raise ValueError('File scope exceeds configured count or contains invalid paths')
                maximum = args.get('max_jobs', 0 if 'high' in tiers else access['default_max_jobs'])
                if type(maximum) is not int or not 0 <= maximum <= 10 or maximum == 0 and 'high' not in tiers and access['default_max_jobs'] != 0:
                    raise ValueError('max_jobs must be 1..10, or 0 for an explicitly authorized high-tier scope')
                material = args.get('material_class')
                if material not in ('public_or_synthetic', 'user_approved_non_sensitive'):
                    raise ValueError('Material sharing classification required')
                state = {'task_id': str(uuid.uuid4()), 'thread_id': thread_id, 'enabled': True,
                         'scope': scope, 'authorization': authorization, 'allowed_kinds': sorted(set(kinds)),
                         'allowed_files': [str(Path(p).resolve()) for p in files],
                         'allowed_purposes': sorted(set(purposes)),
                         'allowed_model_tiers': sorted(set(tiers)),
                         'high_tier_authorization': high_quote,
                         'high_tier_authorization_basis': high_basis,
                         'material_class': material, 'max_jobs': maximum, 'job_ids': [],
                         'capability_routing_authorized': access['standing_high_tier'],
                         'created_at': time.time(), 'expires_at': (time.time() + access['scope_ttl_seconds'] if access['scope_ttl_seconds'] is not None else None),
                         'initial_decision': decision}
                if 'workspace' in args:
                    from workspace import normalize_spec, directory_identities
                    state['workspace'] = normalize_spec(args['workspace'])
                    state['workspace_identities'] = directory_identities(state['workspace'])
                if access['require_chat_opt_in']:
                    state['chat_authorization'] = {'thread_id':thread_id, 'quote':chat_quote,
                        'basis':'coordinator_recorded_direct_user_permission'}
                self.write(state)
                return {'state': state, 'effective_enabled': decision['enabled'], 'decision': decision}
            task_id = args.get('task_id')
            if task_id is None:
                if action != 'status':
                    raise ValueError('Provide task_id to close a scoped task')
                candidates = [json.loads(p.read_text()) for p in self.root.glob('*.json')]
                candidates = [s for s in candidates if s.get('thread_id') == thread_id
                              and scope_current(s)
                              and (not access_settings()['require_chat_opt_in'] or chat_scope_authorized(s))]
                if len(candidates) == 1:
                    task_id = candidates[0]['task_id']
                else:
                    return {'enabled': False, 'effective_enabled': False,
                            'reason': ('Multiple active scopes; choose an existing task_id.' if candidates else
                                       'Default off; no active authorized scope for this chat.'),
                            'task_modes': [{'task_id':s['task_id'], 'scope':s['scope'], 'max_jobs':s['max_jobs'],
                                            'submitted_jobs':len(s['job_ids'])} for s in candidates]}
            state = self.read(task_id)
            if state['thread_id'] != thread_id:
                raise ValueError('Task mode belongs to a different chat')
            if action in ('disable', 'complete'):
                state.update(enabled=False, closed_at=time.time(), closed_reason=action)
                self.write(state)
                return {'state': state, 'effective_enabled': False}
            if action != 'status':
                raise ValueError('Unknown task-mode action')
            # Inspection must not require a running chat or mutate its permission.
            try:
                decision = scope_decision(thread_id, executing=True)
            except ValueError as error:
                decision = {'enabled': False, 'reason': str(error)}
            opted_in = not access_settings()['require_chat_opt_in'] or chat_scope_authorized(state)
            permission = scope_current(state) and opted_in
            unlimited = state['max_jobs'] == 0
            remaining = None if unlimited else max(0, state['max_jobs'] - len(state['job_ids']))
            active = decision.get('evidence', {}).get('turn_state') == 'active'
            effective = bool(permission and (unlimited or remaining) and decision['enabled'] and active)
            reason = ('Ready for new submissions.' if effective else
                      'Gemini is off in this chat; explicit user opt-in is required.' if not opted_in else
                      'Task permission is closed or expired.' if not permission else
                      'Submission budget used; existing jobs remain readable.' if not unlimited and not remaining else
                      decision.get('reason', 'Runtime identity unavailable.') if not decision['enabled'] else
                      'No active submitting turn; already reserved jobs may continue.')
            return {'state': state, 'decision': decision, 'effective_enabled': effective,
                    'permission_enabled': permission, 'remaining_jobs': remaining, 'reason': reason}

    def authorize(self, args, profile, *, reserve_job=None, executing=False, executing_job=None, scope_only=False):
        with self.lock:
            purpose = args.get('task_purpose', 'general')
            if purpose not in PURPOSES:
                raise ValueError('Invalid task purpose')
            if args.get('dispatch_mode', 'manual') != 'auto':
                if 'workspace' in args:
                    raise ValueError('Native workspaces require a controlled dispatch_mode=auto scope')
                if purpose != 'general':
                    raise ValueError('Specialist purposes require dispatch_mode=auto')
                if args.get('dispatch_mode', 'manual') != 'manual' or 'task_id' in args or 'thread_id' in args:
                    raise ValueError('Task IDs require dispatch_mode=auto')
                if access_settings()['require_chat_opt_in']:
                    raise ValueError('Gemini requires explicit chat opt-in and a controlled dispatch_mode=auto scope; unscoped manual generation is disabled')
                tier, specialist = model_usage(profile)
                if tier == 'high' or specialist:
                    raise ValueError('High tier requires a controlled dispatch_mode=auto scope')
                return None
            state = self.read(args.get('task_id'))
            if state['thread_id'] != identifier(args.get('thread_id')):
                raise ValueError('Task mode belongs to a different chat')
            if not scope_current(state):
                raise ValueError('Task delegation is disabled or expired')
            if access_settings()['require_chat_opt_in'] and not chat_scope_authorized(state):
                raise ValueError('Gemini is off in this chat; inherited Ultra authorization is not explicit chat consent')
            if executing and executing_job not in state['job_ids']:
                raise ValueError('Execution requires a previously reserved job')
            if not executing and state['max_jobs'] != 0 and len(state['job_ids']) >= state['max_jobs']:
                raise ValueError('Task delegation budget exhausted; do not automatically retry')
            if purpose not in state.get('allowed_purposes', ['general']):
                raise ValueError('Task purpose is outside authorized scope')
            if args.get('kind', 'copy') not in state['allowed_kinds']:
                raise ValueError('Task kind is outside authorized scope')
            if any(str(Path(p).resolve()) not in state['allowed_files'] for p in args.get('files', [])):
                raise ValueError('File is outside authorized task scope')
            workspace = None
            if 'workspace' in args:
                from workspace import check_scope
                workspace = check_scope(args['workspace'], state.get('workspace'), state.get('workspace_identities'))
            if scope_only:
                return {'task_id':state['task_id'], 'scope_checked':True, 'allowed_files':list(state['allowed_files']),
                        'material_class':state.get('material_class'),
                        'workspace':workspace,
                        'allowed_model_tiers':list(state.get('allowed_model_tiers', ['low'])),
                        'capability_routing_authorized':state.get('capability_routing_authorized',False)}
            tier, specialist = model_usage(profile)
            if tier not in state.get('allowed_model_tiers', ['low']):
                raise ValueError('Model tier is outside authorized scope')
            if specialist and purpose == 'general' and not state.get('capability_routing_authorized',False):
                raise ValueError('Scope predates Ultra general-specialist authorization; retain its original model scope')
            if specialist and tier != 'high':
                raise ValueError('Specialist model must retain high usage tier')
            if (tier == 'high' or specialist) and not high_scope_authorized(state):
                raise ValueError('High tier requires quoted user permission to use 4 Argon or explicit $gemini-bridge invocation')
            # Work already accepted by an active submitting turn remains valid
            # when that turn ends while another chat occupies the queue. Read
            # the latest actual context/model and policy again, never defaults.
            context = ({'task_purpose': purpose, 'kind': args.get('kind', 'copy'), 'dispatch_mode': 'auto'}
                       if purpose != 'general' or specialist else {})
            decision = runtime_decision(state['thread_id'], profile, executing=executing, **context)
            if not decision['enabled']:
                raise ValueError(decision['reason'] + ' ' + json.dumps(decision, ensure_ascii=False))
            if args.get('kind', 'copy') not in decision['allowed_kinds']:
                raise ValueError('Model is not qualified for this task kind')
            # Image submissions and run_job verify effective /settings once
            # each. Do not repeat that read-only CLI startup at both scope
            # reservations; execution still fails closed before any model call.
            if reserve_job:
                state['job_ids'].append(reserve_job)
                self.write(state)
            return {'task_id': state['task_id'], 'thread_id': state['thread_id'],
                    'scope': state['scope'], 'material_class': state['material_class'], 'decision': decision,
                    **({'workspace':workspace} if workspace is not None else {})}

"""Read-only routing adviser. Does not spawn agents or modify Codex settings."""
import datetime as dt
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time


def decide(policy, model, effort):
    out = {'policy_version': policy['version'], 'parent': {'model': model, 'effort': effort}, 'enabled': False, 'roles': {}}
    bans = policy['disabled_parent_models']
    pairs = policy.get('disabled_parent_combinations', [])
    if any(not isinstance(items, list) or any(not isinstance(x, str) or not x for x in items)
           for items in (bans, pairs)):
        raise ValueError('禁派遣名单格式无效')
    key = str(model) + '/' + str(effort)
    if model in bans or key in pairs:
        out['reason'] = '当前主模型或精确组合命中本版策略禁派遣名单。'
        return out
    entry = policy['parents'].get(key)
    if entry is None:
        out['reason'] = '模型与推理档位组合未分类；不派遣。'
        return out
    def valid(value):
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    if policy.get('cost_basis') != 'AA Intelligence Index USD per task':
        raise ValueError('策略成本口径缺失或不一致')
    version = policy.get('index_version')
    if not isinstance(version, str) or not version or entry.get('index_version') != version:
        raise ValueError('主模型 AA 版本缺失或不一致')
    if not all(valid(entry.get(k)) for k in ('score', 'cost_usd_per_task')):
        out['reason'] = '主模型能力或每任务成本缺失；不派遣。'
        return out
    eligible = []
    for number, anchor in policy['tiers'].items():
        if anchor.get('index_version') != version:
            raise ValueError('候选档 AA 版本缺失或不一致')
        if not all(valid(anchor.get(k)) for k in ('score', 'cost_usd_per_task')):
            raise ValueError('候选档能力或每任务成本无效')
        if anchor['score'] <= entry['score'] and anchor['cost_usd_per_task'] <= entry['cost_usd_per_task']:
            eligible.append(int(number))
    out['eligible_tiers'] = sorted(eligible)
    cap = max(eligible, default=0)
    if type(cap) is not int or cap not in range(5):
        raise ValueError('主模型上限格式无效')
    out['ceiling'] = cap
    if not cap:
        out['reason'] = '没有同时满足能力和每任务成本上限的候选档；不派遣。'
        return out
    out['enabled'] = True
    out['reason'] = '允许在上限内按任务需要派遣；不要求派遣。'
    for role, base in policy['role_base_tiers'].items():
        if type(base) is not int or base not in range(1, 5):
            raise ValueError('角色原定档格式无效')
        tier = max((n for n in eligible if n <= base), default=0)
        if tier:
            anchor = policy['tiers'][str(tier)]
            out['roles'][role] = {k: anchor[k] for k in ('model', 'reasoning_effort')}
            out['roles'][role]['tier'] = tier
    return out


class RuntimeNotReady(ValueError):
    """A bounded retry can resolve index rotation or a partially flushed turn."""


class InactiveTurnError(ValueError):
    def __init__(self, turn_id, state):
        self.turn_id = turn_id
        self.turn_state = state
        super().__init__('运行上下文不能证明属于当前活动回合：' + state)


def current_rollout_path(home, thread_id):
    """Resolve rotated/edit-message logs through the local index, not defaults."""
    databases = [p for p in home.glob('state_*.sqlite') if re.fullmatch(r'state_\d+\.sqlite', p.name)]
    if databases:
        database = max(databases, key=lambda p: int(p.stem.split('_')[1]))
        try:
            with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.1)) as connection:
                row = connection.execute('SELECT rollout_path FROM threads WHERE id=?', (thread_id,)).fetchone()
        except sqlite3.OperationalError as error:
            if 'locked' in str(error).lower() or 'busy' in str(error).lower():
                raise RuntimeNotReady('本地聊天运行索引暂不可读；不使用旧日志猜测当前身份') from error
            raise ValueError('本地聊天运行索引不可读；不使用旧日志猜测当前身份') from error
        except sqlite3.Error as error:
            raise ValueError('本地聊天运行索引不可读；不使用旧日志猜测当前身份') from error
        if row:
            path = Path(row[0]) if isinstance(row[0], str) else Path('')
            if (not path.is_absolute() or path.is_symlink()
                    or not path.resolve().is_relative_to((home / 'sessions').resolve())
                    or not re.search('-' + re.escape(thread_id) + r'(?:_[0-9a-fA-F-]{36})?\.jsonl$', path.name)):
                raise ValueError('本地聊天索引指向无效运行日志；不回退旧日志')
            if not path.is_file():
                raise RuntimeNotReady('本地聊天索引运行日志尚未就绪；不回退旧日志')
            return path, 'local_thread_index'
        raise RuntimeNotReady('本地聊天索引尚未定位当前任务；不回退旧日志')
    paths = set((home / 'sessions').glob('*/*/*/*-' + thread_id + '.jsonl'))
    paths.update((home / 'sessions').glob('*/*/*/*-' + thread_id + '_*.jsonl'))
    if len(paths) != 1:
        if databases:
            raise RuntimeNotReady('当前任务运行日志缺失或不唯一，且本地索引尚未定位')
        raise ValueError('当前任务运行日志缺失或不唯一，且无本地索引可定位')
    return paths.pop(), 'unique_thread_filename'


def _read_parent_once(home, thread_id, now, require_active):
    path, selection = current_rollout_path(home, thread_id)
    meta = None
    context = None
    turn_id = None
    turn_state = 'unknown'
    try:
        handle = path.open(encoding='utf-8')
    except FileNotFoundError as error:
        raise RuntimeNotReady('当前运行日志轮换中') from error
    with handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                if not line.endswith('\n'):
                    raise RuntimeNotReady('运行日志末行尚未写入完整；重新读取')
                continue  # historical malformed records cannot prove identity
            if not isinstance(row, dict):
                continue
            payload = row.get('payload', {})
            if not isinstance(payload, dict):
                raise ValueError('运行日志记录格式无效')
            if row.get('type') == 'session_meta':
                if meta and meta.get('id') != payload.get('id'):
                    raise ValueError('日志身份记录冲突')
                meta = payload
            elif row.get('type') == 'event_msg':
                event_type = payload.get('type')
                if event_type == 'task_started':
                    turn_id = payload.get('turn_id')
                    turn_state = 'active' if turn_id else 'unknown'
                elif event_type in ('task_complete', 'task_aborted'):
                    event_turn = payload.get('turn_id')
                    if (event_turn == turn_id or event_turn is None) and turn_state != 'aborted':
                        turn_state = ('aborted' if event_type == 'task_aborted'
                                      else 'completed' if event_turn else 'unknown')
            elif row.get('type') == 'turn_context':
                context = row
    # Check the authoritative pointer again before accepting any snapshot.
    if current_rollout_path(home, thread_id) != (path, selection):
        raise RuntimeNotReady('本地聊天索引轮换中；重新读取当前运行日志')
    if not meta or meta.get('id') != thread_id:
        raise ValueError('日志身份与当前任务不符')
    source = meta.get('source', {})
    if isinstance(source, dict) and 'subagent' in source:
        raise ValueError('子 Agent 默认不得向下派遣')
    if turn_state in ('aborted', 'unknown'):
        raise InactiveTurnError(turn_id, turn_state)
    if not context or context['payload'].get('turn_id') != turn_id:
        if turn_state == 'active':
            raise RuntimeNotReady('当前活动回合 turn_context 尚未就绪')
        raise ValueError('运行上下文不能证明属于最新回合')
    try:
        stamp = dt.datetime.fromisoformat(context['timestamp'].replace('Z', '+00:00'))
        age = ((now or dt.datetime.now(dt.timezone.utc)) - stamp).total_seconds()
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('运行上下文时间格式无效') from error
    if age < -120 or age > 86400:
        raise ValueError('运行上下文时间过旧或异常；不猜测当前档位')
    payload = context['payload']
    model = payload.get('model')
    effort = payload.get('effort')
    cm = payload.get('collaboration_mode', {})
    settings = cm.get('settings', {}) if isinstance(cm, dict) else {}
    if not isinstance(settings, dict):
        raise ValueError('运行上下文 settings 格式无效')
    if settings.get('model') not in (None, model):
        raise ValueError('运行上下文中的模型字段冲突')
    if settings.get('reasoning_effort') not in (None, effort):
        raise ValueError('运行上下文中的推理字段冲突')
    if not isinstance(model, str) or not model or not isinstance(effort, str) or not effort:
        raise ValueError('当前模型或推理档位缺失')
    if require_active and turn_state != 'active':
        raise InactiveTurnError(turn_id, turn_state)
    return model, effort, {'thread_id': thread_id, 'turn_id': turn_id, 'turn_state': turn_state,
                           'log': str(path), 'timestamp': context['timestamp'],
                           'source': 'turn_context', 'log_selection': selection}


def read_parent(home, thread_id, now=None, *, require_active=True):
    """Read runtime identity; completed snapshots are only for reserved execution.

    Never permits aborted turns, mismatched contexts, or stale model settings.
    Retries only known transient readiness failures, without changing any files.
    """
    if not thread_id or not re.fullmatch(r'[0-9a-fA-F-]{36}', thread_id):
        raise ValueError('没有可靠的 CODEX_THREAD_ID')
    for attempt in range(3):
        try:
            return _read_parent_once(home, thread_id, now, require_active)
        except RuntimeNotReady:
            if attempt == 2:
                raise
            time.sleep(0.05)


SPECIALIST_PURPOSES = {
    'general': {'efforts':['low','medium','high','xhigh','fixed'], 'kinds':['copy','english','extract','code-map','visual','prototype','image']},
    'tone_polish': {'efforts': ['low', 'medium', 'high'], 'kinds': ['copy', 'english']},
    'important_communication': {'efforts': ['low', 'medium', 'high'], 'kinds': ['copy', 'english']},
    'sensitive_communication': {'efforts': ['low', 'medium', 'high'], 'kinds': ['copy', 'english']},
    'key_copy': {'efforts': ['low', 'medium', 'high'], 'kinds': ['copy', 'english']},
    'technical_second_opinion': {'efforts': ['low', 'medium', 'high'], 'kinds': ['code-map', 'prototype']},
    'technical_deep_review': {'efforts': ['low', 'medium', 'high'], 'kinds': ['code-map']},
}


def decide_external(policy, model, effort, child_model, child_effort, *,
                    task_purpose=None, kind=None, dispatch_mode=None):
    """Independent frozen external mapping; never changes the seven role tiers."""
    parent = decide(policy, model, effort)
    out = {'enabled': False, 'parent': parent['parent'],
           'candidate': {'model': child_model, 'effort': child_effort},
           'policy_version': policy['version'], 'cost_basis': policy.get('cost_basis')}
    if not parent['enabled']:
        out['reason'] = parent['reason']
        return out
    candidate = policy.get('external_models', {}).get(child_model + '/' + child_effort)
    if candidate is None:
        out['reason'] = '外部模型精确组合未分类；不派遣。'
        return out
    # Specialist permission never changes native role ceilings or generic routing.
    if candidate.get('specialist_only') is True:
        exception = policy.get('external_specialist_exception', {})
        purpose = SPECIALIST_PURPOSES.get(task_purpose) if isinstance(task_purpose, str) else None
        configured = exception.get('allowed_purposes', {}) if isinstance(exception, dict) else {}
        rule = configured.get(task_purpose) if isinstance(configured, dict) and isinstance(task_purpose, str) else None
        key = child_model + '/' + child_effort
        # Policy controls permitted efforts; old singular rules stay exact.
        configured_efforts = (rule.get('efforts',
                              [rule['effort']] if isinstance(rule.get('effort'), str) else None)
                              if isinstance(rule, dict) else None)
        valid = (isinstance(exception, dict) and exception.get('enabled') is True
                 and exception.get('basis') == 'user_authorized_bounded_specialist'
                 and exception.get('api_fallback') == 'forbidden'
                 and isinstance(exception.get('authorization'), str) and bool(exception['authorization'].strip())
                 and isinstance(exception.get('allowed_combinations'), list)
                 and key in exception['allowed_combinations']
                 and candidate.get('specialist_only') is True
                 and candidate.get('usage_tier', 'high') == exception.get('allowed_usage_tiers', {}).get(key, 'high')
                 and (child_effort != 'fixed' or (candidate.get('model_execution', {}).get('mode') == 'orchestrated' and candidate.get('model_execution', {}).get('effort_mode') == 'fixed'))
                 and candidate.get('model') == child_model and candidate.get('reasoning_effort') == child_effort
                 and dispatch_mode == 'auto' and purpose is not None and isinstance(rule, dict)
                 and isinstance(configured_efforts, list) and isinstance(rule.get('kinds'), list)
                 and child_effort in purpose['efforts'] and child_effort in configured_efforts
                 and kind in purpose['kinds'] and kind in rule['kinds']
                 and kind in candidate.get('allowed_kinds', []))
        if not valid:
            out['reason'] = '专用外部模型需要有效任务用途、精确档位及用户授权的自动分发例外；不派遣。'
            return out
        primary = policy['parents'][model + '/' + effort]
        def metric(key):
            value = candidate.get(key)
            return type(value) in (int, float) and math.isfinite(value) and value >= 0
        same_index = candidate.get('index_version') == policy['index_version']
        score_known = same_index and metric('score')
        cost_known = same_index and metric('cost_usd_per_task')
        out.update(enabled=True, specialist_exception=True, task_purpose=task_purpose,
                   index_version=candidate.get('index_version'),
                   benchmark_metrics_verified=bool(score_known and cost_known),
                   score_within_ceiling=candidate['score'] <= primary['score'] if score_known else None,
                   cost_within_ceiling=candidate['cost_usd_per_task'] <= primary['cost_usd_per_task'] if cost_known else None,
                   parent_score=primary['score'], child_score=candidate.get('score'),
                   parent_cost_usd_per_task=primary['cost_usd_per_task'],
                   child_cost_usd_per_task=candidate.get('cost_usd_per_task'),
                   allowed_kinds=[k for k in purpose['kinds'] if k in rule['kinds'] and k in candidate.get('allowed_kinds', [])],
                   required_billing_route='existing_account_authentication_only',
                   cost_gate_basis='user_authorized_bounded_specialist',
                   actual_subscription_deduction='unknown',
                   reason='按用户明确授权的限定用途专用模型例外自动分发；仅限现有账户认证，禁止 API 回退。')
        return out
    primary = policy['parents'][model + '/' + effort]
    if candidate.get('index_version') != policy['index_version']:
        out['reason'] = '外部模型 AA 版本缺失或不一致；不派遣。'
        return out
    exception = policy.get('external_subscription_exception', {})
    subscription_allowed = (exception.get('enabled') is True
                            and exception.get('basis') == 'existing_subscription_quota'
                            and exception.get('api_fallback') == 'forbidden'
                            and child_model + '/' + child_effort in exception.get('allowed_combinations', []))
    def metric_valid(key):
        return type(candidate.get(key)) in (int, float) and math.isfinite(candidate[key]) and candidate[key] >= 0
    # A specifically approved membership route replaces the API proxy cost
    # gate, not the score gate. Missing API cost remains null, never zero.
    if not metric_valid('score') or (not metric_valid('cost_usd_per_task') and not (subscription_allowed and candidate.get('cost_usd_per_task') is None)):
        out['reason'] = '外部模型能力或所需成本证据缺失；不派遣。'
        return out
    if candidate.get('automatic_eligible', True) is not True:
        out['reason'] = candidate.get('automatic_reason', '外部模型自动分发资格尚未核验。')
        return out
    out.update(index_version=policy['index_version'],
               score_within_ceiling=candidate['score'] <= primary['score'],
               cost_within_ceiling=metric_valid('cost_usd_per_task') and candidate['cost_usd_per_task'] <= primary['cost_usd_per_task'],
               parent_score=primary['score'], child_score=candidate['score'],
               parent_cost_usd_per_task=primary['cost_usd_per_task'],
               child_cost_usd_per_task=candidate['cost_usd_per_task'],
               allowed_kinds=candidate.get('allowed_kinds', []))
    out['subscription_cost_exception'] = subscription_allowed
    out['cost_gate_basis'] = 'existing_subscription_quota_user_approved_exception' if subscription_allowed else policy['cost_basis']
    out['required_billing_route'] = 'existing_account_authentication_only' if subscription_allowed else None
    out['actual_subscription_deduction'] = 'not_independently_measured'
    out['enabled'] = out['score_within_ceiling'] and (out['cost_within_ceiling'] or subscription_allowed)
    out['reason'] = ('外部组合满足能力和每任务成本双上限。' if out['enabled']
                     else '外部组合超出能力或每任务成本上限；由主 Agent 完成。')
    if out['enabled'] and subscription_allowed and not out['cost_within_ceiling']:
        out['reason'] = '能力上限通过；按用户批准的现有会员路线成本例外派遣，禁止 API 回退。'
    return out


def main():
    try:
        home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        policy_bytes = (Path(__file__).resolve().parent / 'policy.json').read_bytes()
        policy = json.loads(policy_bytes)
        model, effort, evidence = read_parent(home, os.environ.get('CODEX_THREAD_ID'))
        out = decide(policy, model, effort)
        out['external_models'] = {
            key: decide_external(policy, model, effort, *key.rsplit('/', 1))
            for key in policy.get('external_models', {})}
        out['policy_sha256'] = hashlib.sha256(policy_bytes).hexdigest()
        out['evidence'] = evidence
    except Exception as exc:
        out = {'enabled': False, 'roles': {}, 'reason': '无法可靠判断当前档位：' + str(exc)}
    print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()

"""Read-only progress derived from existing CLI streams, not a provider heartbeat."""
import json
import math
import os
from pathlib import Path
import re
import time
import stat as stat_module
from bridge import ROOT, validate_job_id

LIMIT = 262144
ACTIVE = ('queued', 'running', 'cancelling')
PRINT_TIMEOUT = re.compile(r'\bprint timeout after\b', re.IGNORECASE)


def live_wait(job):
    """Read only the bounded, content-free runtime waiting snapshot."""
    try:
        with (job/'live-progress.json').open('rb') as source:
            raw=source.read(32769)
        if len(raw)>32768:return {}
        value=json.loads(raw)
        if not isinstance(value,dict) or value.get('mode')!='activity':return {}
        out={'mode':'activity'}
        for key in ('soft_timeout_seconds','hard_timeout_seconds','effective_deadline_seconds','hard_timeout_grace_seconds','meaningful_events'):
            n=value.get(key)
            limit=10000000 if key=='meaningful_events' else 1830
            if type(n) in (int,float) and math.isfinite(n) and 0<=n<=limit:out[key]=n
        at=value.get('last_meaningful_activity_at')
        if type(at) in (int,float) and math.isfinite(at) and at>0:out['last_meaningful_activity_at']=at
        if value.get('observed_state') in ('initializing','awaiting_response','thinking','receiving_response','tool_running','finalizing'):out['observed_state']=value['observed_state']
        if value.get('deadline_reason') in ('quiet_grace','recent_activity','hard_limit'):out['deadline_reason']=value['deadline_reason']
        return out
    except (OSError,ValueError,TypeError):return {}


def process_exists(pid):
    if type(pid) is not int or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None


def tail(path, limit=LIMIT):
    if not path.exists():
        return b'', None, False
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        stat = os.fstat(handle.fileno())
        if not stat_module.S_ISREG(stat.st_mode):return b'', None, False
        offset = max(0, stat.st_size - limit)
        handle.seek(offset)
        if offset:
            handle.readline(limit)  # skip only the partial leading record
        data = handle.read(limit)
    return data, stat, bool(offset)


def snapshot(job_id, state=None, *, root=None, now=None, probe=None):
    root = root or ROOT
    now = now if now is not None else time.time()
    job = root / 'runs' / validate_job_id(job_id)
    state = state if state is not None else json.loads((job/'state.json').read_text())
    reported = state['status']
    raw, stat, truncated = tail(job/'events.jsonl')
    events = []
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b'\n'):continue
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
        except (ValueError, UnicodeDecodeError):
            # A live writer's partial trailing line is not a failure.
            continue
    stderr, err_stat, _ = tail(job/'stderr.log', 16384)
    timed_out = bool(PRINT_TIMEOUT.search(stderr.decode('utf-8', 'replace')))
    init = next((e.get('init') for e in events if e.get('event') == 'init'), None)
    if init is None and truncated:
        with (job/'events.jsonl').open('rb') as handle:
            try:
                first = json.loads(handle.readline(65536))
                if isinstance(first, dict) and first.get('event') == 'init':init = first.get('init')
            except (ValueError, UnicodeDecodeError):pass
    init = init if isinstance(init, dict) else {}
    terminal = next((e.get('result') for e in reversed(events) if e.get('event') == 'result'), None)
    terminal = terminal if isinstance(terminal, dict) else {}
    step = next((e.get('step_update') for e in reversed(events) if e.get('event') == 'step_update'), {})
    step = step if isinstance(step, dict) else {}
    completed = set()
    text_started = False
    for event in events:
        s = event.get('step_update', {})
        if not isinstance(s, dict):continue
        if s.get('step_type') == 'tool' and s.get('state') == 'DONE':
            completed.add((s.get('conversation_id'), s.get('step_index'), s.get('tool_name')))
        if s.get('step_type') == 'agent_response' and s.get('text_delta'):
            text_started = True
    profile = {}
    try:profile = json.loads((job/'requested-profile.json').read_text())
    except (OSError, ValueError):pass
    timeout = profile.get('timeout_seconds', 180)
    if type(timeout) is not int or not 10 <= timeout <= 1800:timeout = None
    waiting_policy = profile.get('waiting_policy', {})
    waiting_policy = waiting_policy if isinstance(waiting_policy, dict) else {}
    quiet_notice = waiting_policy.get('quiet_notice_seconds', 90)
    if type(quiet_notice) is not int or not 10 <= quiet_notice <= 1800:quiet_notice = 90
    started = state.get('execution_started_at')
    start_evidence = 'state.execution_started_at (CLI process started)'
    if type(started) not in (int, float) or not math.isfinite(started) or started <= 0:
        # New jobs create their event file before preflight completes. Only
        # historical profiles lack the explicit process-start contract.
        started = getattr(stat, 'st_birthtime', None) if stat and 'waiting_policy' not in profile else None
        start_evidence = 'events.jsonl birth time (legacy fallback)' if started is not None else None
    if type(started) not in (int, float) or not math.isfinite(started) or started <= 0:
        started, start_evidence = None, None
    # A queued job has no generation process yet, even if an events file exists.
    if reported == 'queued':started, start_evidence = None, None
    since_start = max(0, now-started) if started else None
    last_activity = stat.st_mtime if stat and stat.st_size else None
    quiet = max(0, now-last_activity) if last_activity is not None else None
    live=live_wait(job)
    activity_mode=waiting_policy.get('mode')=='activity'
    if activity_mode:
        last_activity=live.get('last_meaningful_activity_at')
        quiet=max(0,now-last_activity) if last_activity is not None else since_start
    effective_deadline=live.get('effective_deadline_seconds',timeout)
    soft_exceeded=bool(activity_mode and timeout is not None and since_start is not None and since_start>=timeout)
    exists = (probe or process_exists)(state.get('pid')) if reported in ('running', 'cancelling') else None
    stage, label = reported, reported
    if reported == 'queued':stage, label = 'queued', '排队中'
    elif reported == 'cancelling':stage, label = 'cancelling', '正在取消本地 CLI'
    elif reported == 'running':
        if terminal:stage, label = 'finalizing', 'CLI已返回，桥接正在整理结果'
        elif exists is False:stage, label = 'process_missing', '本地CLI进程已不存在，桥接尚未记录终态'
        elif not init:stage, label = 'initializing', '启动或初始化CLI'
        elif step.get('step_type') == 'tool' and step.get('state') == 'ACTIVE':
            stage, label = 'tool_running', 'CLI正在调用工具'
            if step.get('tool_name') == 'ask_permission':stage, label = 'waiting_permission', 'CLI已请求权限'
        elif live.get('observed_state') == 'thinking':stage, label = 'thinking', 'Gemini 正在思考，等待回复'
        elif text_started:stage, label = 'receiving_response', '已收到流式正文，等待完整结果'
        else:stage, label = 'awaiting_response', 'CLI已启动，等待后续输出'
    elif reported == 'completed_unreviewed':stage, label = reported, '已返回结果；内容验收单独记录'
    elif reported == 'timeout':stage, label = 'timeout', '本地等待超时'
    elif reported == 'failed_or_unverified':stage, label = reported, '执行失败或结果未核验'
    elif reported == 'bridge_error':stage, label = reported, '桥接执行出错'
    elif reported in ('cancelled', 'interrupted'):stage, label = reported, '本地执行已取消或中断'
    if terminal.get('status') == 'WAITING':stage, label = 'waiting_input', 'CLI以等待输入状态退出'
    if terminal.get('status') == 'RUNNING':stage, label = 'incomplete_run', 'CLI回传时运行尚未结束'
    if timed_out:stage, label = 'timeout', 'CLI等待超时，仅返回部分结果'
    if terminal.get('status') in ('ERROR', 'INVALID'):stage, label = 'failed', 'CLI已报告错误'
    silent = reported == 'running' and quiet is not None and quiet >= quiet_notice and not terminal
    attention = stage in ('process_missing','waiting_input','waiting_permission','incomplete_run','timeout','failed','failed_or_unverified','bridge_error','interrupted')
    ended = reported not in ACTIVE
    end_time = last_activity if ended and last_activity is not None else state.get('updated_at', now) if ended else now
    submitted = state.get('created_at')
    from activity_view import project, ACTIONS
    activity = project(job, events, stat, truncated, state, init, tail)
    if live.get('last_meaningful_activity_at'):
        activity['last_effective_activity_at']=live['last_meaningful_activity_at']
    return {'activity':activity, 'stage':stage, 'label':label, 'reported_status':reported,
            'elapsed_since_submission_seconds':round(max(0,end_time-submitted),1) if type(submitted) in (int,float) else None,
            'execution_elapsed_seconds':round(since_start,1) if since_start is not None and not ended else None,
            'execution_start_evidence':start_evidence,
            'timeout_seconds':timeout,
            'waiting_mode':'activity' if activity_mode else 'fixed',
            'soft_timeout_seconds':timeout,
            'hard_timeout_seconds':live.get('hard_timeout_seconds',waiting_policy.get('hard_timeout_seconds',timeout)),
            'effective_deadline_seconds':effective_deadline,
            'soft_budget_exceeded':soft_exceeded,
            'wait_reason':live.get('deadline_reason'),
            'observed_generation_state':live.get('observed_state'),
            'meaningful_events':live.get('meaningful_events'),
            'remaining_wait_seconds':round(max(0,effective_deadline-since_start),1) if effective_deadline is not None and since_start is not None and reported == 'running' else None,
            'local_process_exists':exists, 'observed_model':init.get('model'),
            'conversation_id':step.get('conversation_id') or terminal.get('conversation_id') or init.get('conversation_id'),
            'last_stream_activity_at':last_activity,
            'stream_quiet_seconds':round(quiet,1) if quiet is not None and not ended and reported != 'queued' else None,
            'quiet_notice_after_seconds':quiet_notice,
            'no_recent_stream_activity':silent, 'response_started':text_started,
            'last_step_type':step.get('step_type'), 'last_step_state':step.get('state'),
            'current_tool':(step.get('tool_name') if step.get('tool_name') in ACTIONS else 'tool') if step.get('step_type') == 'tool' and step.get('state') == 'ACTIVE' else None,
            'completed_tools_observed':len(completed), 'event_history_truncated':truncated,
            'cli_print_timeout':timed_out, 'requires_attention':attention,
            'native_status':terminal.get('status'),
            'result_partial_or_nonterminal':timed_out or terminal.get('status') in ('RUNNING','WAITING'),
            'result_available':(job/'report.json').exists(), 'review':state.get('review'),
            'provider_status':'not_exposed_by_cli',
            'evidence':'local CLI events, stderr timeout marker, and process existence; execution start: '
                       + (start_evidence or 'unavailable') + '; no remote heartbeat'}


def enrich(value, *, root=None):
    """Keep original states/reports intact, including historical acceptance."""
    root = root or ROOT
    if not isinstance(value, dict):return value
    result = dict(value)
    read_current_state = False
    if isinstance(value.get('state'), dict) and value['state'].get('job_id'):
        state = value['state']
    elif value.get('job_id') and value.get('status'):
        state = value
        # Reports and list rows omit execution PID/acceptance metadata.
        read_current_state = 'bridge_version' in state or 'request_id' not in state
    else:state = None
    if state:
        try:result['progress'] = snapshot(state['job_id'], None if read_current_state else state, root=root)
        except (OSError, ValueError, KeyError, TypeError):result['progress'] = {'available':False,'reason':'local_progress_unavailable'}
    if isinstance(value.get('jobs'), list):
        result['jobs'] = [enrich(j, root=root) if j.get('status') in ACTIVE else j for j in value['jobs']]
    return result

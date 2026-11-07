"""Bounded, allowlisted read-only UI projection. Never dispatches provider calls."""
import hashlib
import json
import math
import os
import stat as stat_module
import re

MAX_JSON = 2 * 1024 * 1024
ACTIONS = {
    'view_file':'读取文件', 'read_file':'读取文件', 'list_dir':'查看目录',
    'grep_search':'搜索材料', 'find_by_name':'搜索材料', 'search':'搜索材料',
    'run_command':'运行命令', 'command_status':'等待命令',
    'pubmed_search':'检索文献','literature_search':'检索文献','result_read':'继续阅读资料','public_page':'阅读公开资料','document_extract':'提取文档',
    'call_mcp_tool':'调用任务工具', 'file_manifest':'核对文件', 'table_profile':'分析表格',
    'render_preview':'生成预览', 'preview':'生成预览', 'preview_render':'生成预览', 'render_web_preview':'生成预览',
    'invoke_subagent':'启动子任务', 'wait_for_subagents':'等待子任务',
    'manage_subagents':'管理子任务', 'send_message':'协调子任务',
    'write_to_file':'写入交付文件', 'replace_file_content':'修改交付文件',
    'multi_replace_file_content':'修改交付文件', 'ask_permission':'等待权限确认',
}


def safe_text(value, limit=8192):
    if not isinstance(value, str):return ''
    value = re.sub(r'(?i)\b(?:bearer\s+|sk-|AIza)[A-Za-z0-9_./+\-=]{8,}', '[凭据已隐藏]', value)
    value = re.sub(r'(?i)\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret|authorization|token|credential)\b\s*[:=]\s*["\']?[^\s,;"\']+', '[凭据已隐藏]', value)
    return ''.join(c for c in value if c in '\n\t' or ord(c)>=32)[-limit:]


def action(name):
    # Tool titles and arguments are untrusted and may contain commands/secrets.
    return ACTIONS.get(name, '调用工具')


def small_json(path):
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            if not stat_module.S_ISREG(os.fstat(stream.fileno()).st_mode):return {}
            raw=stream.read(MAX_JSON+1)
        if len(raw)>MAX_JSON:return {}
        value=json.loads(raw)
        return value if isinstance(value,dict) else {}
    except (OSError, ValueError, TypeError):return {}


def records(raw):
    # Only newline-committed records are visible; even valid JSON without its
    # final newline may still be an append in progress.
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b'\n'):continue
        try:
            value=json.loads(line)
            if isinstance(value,dict):yield value
        except (ValueError, UnicodeError):continue


def timestamp(value):
    if isinstance(value,str):
        try:
            from datetime import datetime
            value=datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
        except ValueError:return None
    return value if type(value) in (int,float) and math.isfinite(value) and value>0 else None


def project(job, events, stat, truncated, state, init, tail):
    recent=[]; public=[]; child_latest={}; current=None; current_at=-1; last=None
    def add(label, status='observed', at=None, actor=None, identity=None, distinct=False):
        nonlocal current,current_at,last
        at=timestamp(at)
        key=hashlib.sha256(str((identity,label,status,actor,at)).encode()).hexdigest()[:16]
        row={'id':key,'action':label,'state':status,'at':at}
        if actor:row['actor_id']=actor
        if distinct or not recent or any(recent[-1].get(k)!=row.get(k) for k in ('action','state','actor_id')):recent.append(row)
        if actor:child_latest[actor]=label
        if (at or 0)>=current_at:
            current=label+('（已完成，等待下一条动态）' if status=='done' and state.get('status')=='running' else '')
            current_at=at or 0
        if at:last=max(last or at,at)
    # Workspace hooks are protected observations of the actual operation. ACP
    # emits a second lifecycle (and normalized mirrors) whose step indexes do
    # not identify native hook steps. Prefer the native source for that actor /
    # tool; keep ACP as the source when no corresponding hook evidence exists.
    native={}; project_rows=[]
    for name in ('workspace-audit.jsonl','audit.jsonl','project-tools-audit.jsonl'):
        if not (job/name).exists() or (job/name).is_symlink():continue
        raw,info,cut=tail(job/name,65536)
        truncated=truncated or cut
        for index,row in enumerate(records(raw)):
            tool=row.get('tool')
            if not isinstance(tool,str) or not tool:continue
            if name=='project-tools-audit.jsonl':
                project_rows.append((name,row));continue
            actor=row.get('conversationId')
            step=row.get('stepIdx')
            identity=('step',step) if type(step) is int else ('args',row['args_sha256']) if isinstance(row.get('args_sha256'),str) else ('row',name,index)
            key=(actor,tool,identity)
            prior=native.get(key,{})
            # Post records can omit the preflight path / fixed executable.
            # Preserve those safe details while advancing the observed state.
            merged={**prior,**row}
            for field in ('path','command'):
                if not merged.get(field) and prior.get(field):merged[field]=prior[field]
            merged['_blocked']=prior.get('_blocked',False) or row.get('decision')=='deny' or row.get('ok') is False
            native[key]=merged
    authoritative={(actor,tool) for actor,tool,identity in native}
    for i,e in enumerate(events):
        step=e.get('step_update',{})
        if not isinstance(step,dict):step={}
        kind=step.get('step_type'); actor=step.get('conversation_id')
        at=e.get('at') or e.get('timestamp') or (stat.st_mtime if stat else None)
        if kind=='agent_thought' and type(step.get('activity_sequence')) is int and step['activity_sequence']>0:
            # Actual, timestamped protocol activity may advance the current phase
            # without publishing thought text or adding noise to the tool timeline.
            thought_at=timestamp(e.get('at') or e.get('timestamp'))
            if thought_at is not None:
                if thought_at>current_at:current,current_at='正在处理',thought_at
                last=max(last or thought_at,thought_at)
        elif kind=='tool' and (actor,step.get('tool_name')) not in authoritative:add(action(step.get('tool_name')), {'ACTIVE':'running','DONE':'done','ERROR':'error'}.get(step.get('state'),'observed'),at,actor,i)
        elif kind=='agent_response' and init.get('transport')=='acp':
            delta=step.get('text_delta')
            if isinstance(delta,str):public.append(delta)
            add('正在回复',at=at,actor=actor,identity=i)
        elif e.get('event')=='result':add('整理交付',at=at,identity=i)
        elif e.get('event')=='acp_tool':
            u=e.get('update',{})
            if isinstance(u,dict) and (init.get('conversation_id'),u.get('toolName') or u.get('name')) not in authoritative:add(action(u.get('toolName') or u.get('name')), {'completed':'done','failed':'error','in_progress':'running'}.get(u.get('status'),'observed'),at,actor=init.get('conversation_id'),identity=i)
    for identity,row in list(native.items())+[(('project',i),r) for i,(_,r) in enumerate(project_rows)]:
        tool=row.get('tool')
        label='生成预览' if tool=='run_command' and row.get('command',{}).get('executable')=='bridge-preview' else action(tool)
        if row.get('path'):label+=' · '+safe_text(os.path.basename(row['path']),90)
        blocked=row.get('_blocked') or row.get('decision')=='deny' or row.get('ok') is False
        add(label+('（已阻止）' if blocked else ''),'error' if blocked else 'done' if identity[0]=='project' or row.get('kind')=='post' else 'running',row.get('at'),row.get('conversationId'),identity,distinct=True)
    # Event streams lack per-record timestamps on some transports. The file
    # mtime is explicit local observation evidence, never a remote heartbeat.
    if stat and stat.st_size:last=max(last or 0,stat.st_mtime)
    guard=small_json(job/'workspace-guard-state.json')
    ns=guard.get('native_subagents',{}); ns=ns if isinstance(ns,dict) else {}
    source=ns.get('children',{}); source=source if isinstance(source,dict) else {}
    children=[]
    for cid,c in list(source.items())[:4]:
        if not isinstance(c,dict) or not re.fullmatch(r'[A-Za-z0-9-]{1,100}',cid):continue
        stopped=c.get('stop_observed') is True
        lifecycle=c.get('lifecycle',[])
        last_event=lifecycle[-1].get('event') if isinstance(lifecycle,list) and lifecycle and isinstance(lifecycle[-1],dict) else None
        status='已结束' if stopped else '本地取消，远端未知' if state.get('status') in ('cancelled','interrupted','cancelling') or state.get('cancel_requested') else '运行中，尚未观察到结束'
        children.append({'id':cid,'short_id':cid[:8],'role':{'reader':'材料阅读','builder':'实现','reviewer':'独立检查'}.get(c.get('role'),safe_text(c.get('role'),80) or '子任务'),
                         'status':status,'stop_observed':stopped,
                         'last_action':child_latest.get(cid) or {'SessionStart':'已启动','PreInvocation':'执行工具','Stop':'观察到结束'}.get(last_event,'等待下一条动态')})
    # Reports can preserve child identities after transient guard state disappears.
    if not children:
        report=small_json(job/'report.json').get('native_children_validation',{})
        if isinstance(report,dict):
            ids=set(report.get('active_or_stop_unobserved',[]))
            evidence=report.get('model_evidence',{})
            if isinstance(evidence,dict):ids.update(evidence)
            for cid in sorted(ids)[:4]:
                if isinstance(cid,str) and re.fullmatch(r'[A-Za-z0-9-]{1,100}',cid):children.append({'id':cid,'short_id':cid[:8],'role':'子任务（角色未记录）','status':'远端结束状态未知','stop_observed':False,'last_action':child_latest.get(cid,'等待下一条动态')})
    steering=small_json(job/'steering.json').get('records',[])
    for item in steering[-6:] if isinstance(steering,list) else []:
        if isinstance(item,dict):
            label={'queued':'补充要求等待发送','sent':'补充要求已送入会话','responded':'补充要求收到协议回复','unknown':'补充要求回执未确认','rejected':'补充要求未发送'}.get(item.get('status'))
            if label:add(label,at=item.get('updated_at'),identity=item.get('steering_id'))
    preview=safe_text(''.join(public))
    return {'current_action':current or ('排队中' if state.get('status')=='queued' else '等待下一条动态'),
            'last_effective_activity_at':last,'recent_events':sorted(recent,key=lambda e:e.get('at') or 0)[-12:],
            'public_response_preview':preview,'public_response_truncated':len(''.join(public))>8192 or bool(truncated and public),
            'children':children,'history_truncated':bool(truncated),
            'waiting_for_new_events':state.get('status') in ('queued','running','cancelling')}

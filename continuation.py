"""Bind provider conversations to reviewed local job provenance, never caller IDs."""
import json
from pathlib import Path
import re

_ID = re.compile(r'[A-Za-z0-9-]+')
_CONVERSATION = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')


def _read(path):
    if path.is_symlink():
        raise ValueError('Continuation records must be local regular files')
    value=json.loads(path.read_text())
    if not isinstance(value,dict):raise ValueError('Invalid continuation record')
    return value


def parent(root, job_id, thread_id):
    if not isinstance(job_id,str) or not _ID.fullmatch(job_id):raise ValueError('Invalid continuation job ID')
    job=Path(root)/'runs'/job_id
    if job.is_symlink():raise ValueError('Continuation job must not be a link')
    state=_read(job/'state.json');submission=_read(job/'submission.json');profile=_read(job/'requested-profile.json')
    if submission.get('thread_id') != thread_id or submission.get('dispatch_mode')!='auto':
        raise ValueError('Continuation belongs to a different chat or unscoped execution')
    if state.get('status') not in ('completed_unreviewed','failed_or_unverified'):
        raise ValueError('Wait for a terminal provider result before continuing')
    if state.get('kind')=='image':raise ValueError('Image generation uses a new task, not conversation continuation')
    report=_read(job/'report.json')
    if (report.get('model_matches') is not True or report.get('observed_model')!=profile.get('model')
            or report.get('native_status') not in ('SUCCESS','ERROR')):
        raise ValueError('Provider identity or terminal conversation evidence is missing')
    conversation=report.get('provider_session_id')
    if not conversation:
        # Upgrade legacy jobs only from their actual native init event, not state defaults.
        for line in (job/'events.jsonl').read_text().splitlines():
            try:event=json.loads(line)
            except ValueError:continue
            if event.get('event')=='init':
                init=event.get('init') or {}
                if init.get('model')!=profile.get('model'):raise ValueError('Native model evidence conflicts')
                conversation=init.get('conversation_id') or event.get('conversation_id');break
    if not isinstance(conversation,str) or not _CONVERSATION.fullmatch(conversation):
        raise ValueError('No verified provider conversation ID; start a new conversation')
    expected=(profile.get('continuation') or {}).get('provider_session_id')
    if (expected and expected!=conversation) or 'provider_conversation_identity_mismatch' in report.get('failure_reasons',[]):
        raise ValueError('Parent conversation identity failed verification; start a new conversation')
    return {'job_id':job_id,'state':state,'submission':submission,'profile':profile,'report':report,
            'provider_session_id':conversation}


def context(submission, profile):
    previous=profile.get('conversation_context') or {}
    spec=profile.get('workspace') or {}
    code=spec.get('code_repository')
    return {'files':sorted(set(previous.get('files',[]))|{str(Path(p).resolve()) for p in submission.get('files',[])}),
            'read_roots':sorted(set(previous.get('read_roots',[]))|
                                {p for p in spec.get('read_roots',[]) if p!=code}|set(spec.get('write_roots',[])))}


def resolve(root,args,profile,grant,workspace, *, executing_job=None):
    old=parent(root,args['continue_job_id'],args.get('thread_id'))
    if args.get('kind','copy')=='image':raise ValueError('Image continuation is not supported')
    if (profile['model'],profile['effort'])!=(old['profile']['model'],old['profile']['effort']):
        raise ValueError('Continue with the same actual model and effort, or start a new conversation')
    if profile.get('model_execution') != old['profile'].get('model_execution'):
        raise ValueError('Continue with the same fixed model execution contract, or start a new conversation')
    old_spec=old['profile'].get('workspace')
    if (old_spec or {}).get('subagents'):
        raise ValueError('Native-child sessions require a new root task; keep prior deliveries as scoped inputs')
    if bool(old_spec is not None)!=bool(workspace is not None):
        raise ValueError('Changing execution mode requires a new conversation')
    inherited=context(old['submission'],old['profile'])
    if not set(inherited['files'])<=set(grant.get('allowed_files',[])):
        raise ValueError('New scope must cover all files already shared in this conversation')
    if workspace is not None:
        if not set((old_spec or {}).get('network_hosts',[]))<=set(workspace.get('network_hosts',[])):
            raise ValueError('Continuation must retain prior public host context')
        if set((old_spec or {}).get('project_tools',[]))-set(workspace.get('project_tools',[])):
            raise ValueError('Continuation scope must cover prior project tool context')
        if (old_spec or {}).get('subagents',{})!=workspace.get('subagents',{}):
            raise ValueError('Changing native child configuration requires a new conversation')
        roots=workspace['read_roots']+workspace['write_roots']
        if any(not any(Path(p)==Path(r) or Path(r) in Path(p).parents for r in roots)
               for p in inherited['read_roots']):
            raise ValueError('New scope must cover earlier conversation input directories')
        if old_spec.get('protected_paths',[])!=workspace.get('protected_paths',[]):
            raise ValueError('Changed historical protection requires a new conversation')
        old_tree=(old['report'].get('workspace') or {}).get('worktree')
        if old_tree:
            if workspace.get('code_repository')!=old_tree['path']:
                raise ValueError('Code continuation must use the previous worktree as its source')
        elif workspace.get('code_repository'):
            raise ValueError('Adding a code worktree requires a new conversation')
    elif inherited['read_roots']:
        raise ValueError('Earlier directory context requires a workspace grant')
    # A single linear tip prevents queued branches from silently seeing each other's turns.
    for path in (Path(root)/'runs').glob('*/submission.json'):
        if path.parent.name==executing_job:continue
        try:item=_read(path)
        except (OSError,ValueError,UnicodeError):continue
        if item.get('continue_job_id')==old['job_id']:
            try:child=_read(path.parent/'state.json')
            except (OSError,ValueError,UnicodeError):child={}
            zero_start=(child.get('provider_started') is False or child.get('preflight_interrupted') is True)
            if zero_start and child.get('status') in ('bridge_error','interrupted','cancelled'):
                continue
            raise ValueError('Continue from the latest child job instead of branching this conversation')
    return {'session_cwd':old['report'].get('session_cwd'), 'parent_job_id':old['job_id'],'root_job_id':(old['profile'].get('continuation') or {}).get('root_job_id',old['job_id']),
            'provider_session_id':old['provider_session_id'],'context':inherited,
            'mode':'workspace' if workspace is not None else 'materials'}

"""Optional ACP workspace execution with per-session MCP and existing task guards."""
import json


def verify_tool_audit(updates, audit, conversation, model, model_execution=None):
    """Match protocol IDs or exact raw arguments, never human-facing titles."""
    from workspace_guard import _digest
    observed={};missing=[]
    for index,update in enumerate(updates):
        ident=update.get('toolCallId')
        if isinstance(ident,str) and ident:
            observed.setdefault(ident,{}).update(update)
        else:
            missing.append('missing_protocol_tool_id:'+str(index))
    from model_execution import tool_identity_allowed
    identity_policy={'model':model,'effort':'fixed' if model_execution else 'medium','model_execution':model_execution}
    rows={row['stepIdx']:row for row in audit if row.get('kind')=='pre'
          and row.get('conversationId')==conversation and tool_identity_allowed(identity_policy,row.get('modelName'))
          and type(row.get('stepIdx')) is int}
    consumed=set();matched=[]
    for ident,update in observed.items():
        raw=update.get('rawInput')
        digest=_digest(raw) if isinstance(raw,dict) else None
        choices=[i for i,row in rows.items() if i not in consumed and
                 (ident==str(i) or digest is not None and row.get('args_sha256')==digest)]
        if not choices:missing.append(ident);continue
        step=choices[0];consumed.add(step);matched.append({'tool_call_id':ident,'native_step':step})
    return {'observed':len(observed),'matched':matched,'missing':missing,'verified':not missing}


def run(root, profile, prompt, job, *, cancel, on_process, before_execute,
        command_broker, finish_commands):
    from bridge import atomic_json, ProviderNotStarted
    from workspace import prepare, configure_agent, public_record, collect
    from acp_execution import run as execute
    if not all(callable(x) for x in (before_execute,command_broker,finish_commands)):
        raise ValueError('ACP workspace requires the controlled task queue')
    spec=profile['workspace']
    if spec.get('subagents'):
        raise ValueError('Native subagents use the headless workspace route')
    try:
        record=prepare(job,spec,profile['model'],profile.get('timeout_seconds',300),effort=profile['effort'],model_execution=profile.get('model_execution'))
        configure_agent(job,record)
    except (OSError,ValueError) as error:
        raise ProviderNotStarted('ACP workspace preparation failed: '+str(error)) from error
    before_execute()
    opened=False;lease=None;report=None;no_prompt_started=False
    try:
        env=command_broker();opened=True
        env.update(TMPDIR=str(job/'cli-tmp'),PYTHONDONTWRITEBYTECODE='1')
        name=None;mcp=[]
        if record.get('project_tools'):
            root_id=(profile.get('continuation') or {}).get('root_job_id',job.name)
            name='bridge-project-'+root_id[-8:]+'_tools'
            record['project_server_name']=name
            policy=json.loads((job/'workspace-policy.json').read_text());policy['project_server_name']=name
            atomic_json(job/'workspace-policy.json',policy);atomic_json(job/'workspace.json',record)
            entry={'command':str(root/'.venv-http/bin/python'),
                   'args':[str(root/'project_tools_mcp.py'),str(job/'workspace-policy.json')],
                   'env':{k:env[k] for k in ('GEMINI_BRIDGE_PROJECT_URL','GEMINI_BRIDGE_PROJECT_TOKEN')}}
            from project_tools import list_tools
            schemas=list_tools({'project_tools':record['project_tools']})
            agent=job/'.agents/agents/bridge-workspace/agent.md'
            text=agent.read_text().replace('inheritMcp: false\n',
                 'inheritMcp: false\nmcpServers: '+json.dumps([{'name':name,**entry,
                     'enabledTools':[t['name'] for t in schemas],'timeoutSeconds':75}])+'\n',1)
            agent.write_text(text+'\nUse task MCP server '+name+' with these exact schemas:\n'+json.dumps(schemas,ensure_ascii=False)+'\n');agent.chmod(0o600)
            mcp=[{'name':name,'command':entry['command'],'args':entry['args'],
                  'env':[{'name':k,'value':v} for k,v in entry['env'].items()]}]
        from session_workspace import activate,bind_session,retire
        from workspace import _quote
        lease=activate(root,job,record,profile)
        # The stable cwd owns current hooks; the immutable job copy is evidence.
        hooks=job/'.agents/hooks.json';hooks.rename(job/'.agents/hooks.snapshot.json')
        hooks.write_text('{}')
        record['cli_args']=[arg for directory in lease['cli_extra_dirs'] for arg in ('--add-dir',directory)]
        with (job/'cli-sandbox.sb').open('a') as out:
            out.write('(deny file-read* (require-all (subpath '+_quote(root/'provider-sessions')+') (require-not (subpath '+_quote(lease['session_cwd'])+'))))\n')
            out.write('(deny file-write* (subpath '+_quote(root/'provider-sessions')+'))\n')
        runtime_profile=dict(profile,acp_session_cwd=lease['session_cwd'])
        report=execute(root,runtime_profile,prompt,job,cancel=cancel,on_process=on_process,
                       before_execute=before_execute,workspace_record=record,
                       workspace_environment=env,workspace_mcp_servers=mcp,
                       on_session_ready=lambda sid:bind_session(lease['lease_file'],job,sid))
    except Exception as error:
        no_prompt_started=getattr(error,'acp_no_prompt_started',False) is True
        raise
    finally:
        command_records=finish_commands() if opened else []
        if lease:
            terminal=bool(report and report.get('all_prompts_terminal') is True and report.get('acp_stop_reason') in ('end_turn','cancelled','max_tokens','max_turn_requests','refusal'))
            retire(lease['lease_file'],job,remote_terminal=terminal,
                   no_prompt_started=no_prompt_started or bool(report and report.get('provider_started') is False))
    report['workspace']=public_record(record)
    report['execution_mode']='workspace'
    report['session_workspace']={'cwd':lease['session_cwd'],'root_job_id':(profile.get('continuation') or {}).get('root_job_id',job.name),'current_job':job.name,'new_task_credentials':True}
    report['permission_scope']=record['enforcement']
    report['command_results']=command_records
    report['workspace_mcp']={'injection':'ACP session/new or resume','server':name,
                             'tools':record.get('project_tools',[]),'inherits_user_mcp':False}
    try:
        deliveries,changes=collect(job,record)
        audit_path=job/'workspace-audit.jsonl'
        audit=[json.loads(line) for line in audit_path.read_text().splitlines()] if audit_path.exists() else []
        evidence=verify_tool_audit(report.get('tool_calls',[]),audit,
                                   report.get('provider_session_id'),profile['model'],profile.get('model_execution'))
        report['workspace_guard_verification']=evidence
        if not evidence['verified']:
            report.setdefault('failure_reasons',[]).append('workspace_tool_audit_missing')
            if report['status'] not in ('cancelled','timeout'):report['status']='failed_or_unverified'
        from project_evidence import collect as collect_project
        report['project_tool_validation']=collect_project(job,deliveries)
        from preview_evidence import collect as collect_preview
        report['preview_validation']=collect_preview(job,command_records,audit,deliveries)
        guard=job/'workspace-guard-state.json'
        expected=set(json.loads(guard.read_text()).get('command_requests',{})) if guard.exists() else set()
        actual={r['record_id'] for r in command_records}
        report.update(deliverables=deliveries,workspace_changes=changes,
            workspace_audit={'allowed':sum(a.get('decision')=='auto_approve' for a in audit),
                             'blocked':sum(a.get('decision')=='deny' for a in audit)},
            command_validation={'requested':len(expected),'started':len(actual),
                'succeeded':sum(r.get('runner_exit_code')==0 for r in command_records),
                'not_started':len(expected-actual),'failed':sum(r.get('runner_exit_code')!=0 for r in command_records)})
        if deliveries and 'empty_response' in report.get('failure_reasons',[]):
            report['failure_reasons'].remove('empty_response')
            if not report['failure_reasons'] and report['status']=='failed_or_unverified':report['status']='completed_unreviewed'
    except (OSError,ValueError) as error:
        report.setdefault('failure_reasons',[]).append('workspace_collection_error: '+str(error))
        if report['status'] not in ('cancelled','timeout'):report['status']='failed_or_unverified'
    atomic_json(job/'report.json',report)
    return report

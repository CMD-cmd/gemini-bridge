"""ACP tasks through the controlled queue with explicit workspace capabilities."""
import base64
import copy
import hashlib
import json
import mimetypes
from pathlib import Path
import threading
import time

from acp_client import ACPClient, ACPError
from activity import ActivityWait, waiting_policy
from model_execution import validate as validate_model_execution

CANCEL_ACK_WAIT_SECONDS = 3
_TERMINAL_STOP_REASONS = {'end_turn', 'cancelled', 'max_tokens', 'max_turn_requests', 'refusal'}


def select_model(client, session, model, effort, model_execution=None):
    execution = validate_model_execution({'model':model, 'effort':effort,
                                          'model_execution':model_execution})
    offered = {x['modelId'] for x in session.get('models', {}).get('availableModels', [])}
    options = {o['id']: o for o in session.get('configOptions', [])}
    preselected=False
    if execution is not None and not offered and not options.get('model'):
        # Some fresh sessions omit the legacy model list until account bootstrap.
        # A configuration request is zero-generation. Require the provider's
        # exact option list AND acknowledgement, never infer from the local ID.
        updated=client.request('session/set_config_option',
                               {'sessionId':session['sessionId'],'configId':'model','value':model},timeout=45)
        options={o['id']:o for o in updated.get('configOptions',[])}
        option=options.get('model',{})
        if option.get('currentValue')!=model or model not in {o.get('value') for o in option.get('options',[])}:
            raise ValueError('ACP model configuration is not ready; no provider prompt was sent')
        preselected=True
    bases = [o['value'] for o in options.get('model', {}).get('options', [])
             if o.get('value') == model or (execution is None and model == o.get('value', '')+'-'+effort)]
    if model not in offered and (offered or not bases):
        raise ValueError('Requested model is not offered by this ACP session')
    if len(bases) != 1:
        raise ValueError('Cannot bind the exact ACP model and thinking level')
    sid = session['sessionId']
    expected = [('model', bases[0])]
    if execution is None:
        expected.append(('thought_level', effort))
    for key, value in expected:
        if preselected and key=='model':continue
        updated = client.request('session/set_config_option',
                                 {'sessionId':sid, 'configId':key, 'value':value}, timeout=45)
        options = {o['id']:o for o in updated.get('configOptions', [])}
        if options.get(key, {}).get('currentValue') != value:
            raise ValueError('ACP did not acknowledge selected '+key)
    if any(options.get(key, {}).get('currentValue') != value
           for key, value in expected):
        raise ValueError('ACP final model_identity_mismatch: model or thought_level changed')
    if execution is not None:
        options.pop('thought_level', None)
    return {'model':model, 'effort':effort, 'base_model':bases[0], 'matches':True,
            'source':'ACP offered model and acknowledged session config', 'config_options':options,
            **({'model_execution':copy.deepcopy(execution)} if execution is not None else {})}


def run(root, profile, prompt, job, *, cancel=None, on_process=None, before_execute=None,
        workspace_record=None, workspace_environment=None, workspace_mcp_servers=None, on_session_ready=None):
    from bridge import atomic_json, cli_environment, ensure_account_route, prepare_task_agent, check_output, VERSION, provider_failure_details, provider_execution_aborted, GENERATION_EXIT_GRACE_SECONDS
    from run_control import registry as controls
    workspace_mode=workspace_record is not None
    if profile.get('workspace') is not None and not workspace_mode:
        raise ValueError('ACP workspace requires a prepared workspace record')
    if workspace_mode:
        if workspace_record.get('native_subagents') or (profile.get('workspace') or {}).get('subagents'):
            raise ValueError('ACP workspace native subagents are not supported')
        if before_execute is None:
            raise ValueError('ACP workspace requires a before_execute preflight')
        if not workspace_record.get('cli_profile') or not isinstance(workspace_environment,dict) or not isinstance(workspace_mcp_servers,list):
            raise ValueError('ACP workspace requires its strict CLI profile, environment and scoped MCP servers')
    elif workspace_environment is not None or workspace_mcp_servers is not None:
        raise ValueError('ACP workspace capabilities require a workspace record')
    ensure_account_route()
    agent='bridge-workspace' if workspace_mode else prepare_task_agent(job, True, model=profile['model'])
    command=[str(root/'bin/agy'),'acp','--process-per-session','--agent',agent]
    environment=cli_environment(profile.get('diagnostic_proxy'))
    if workspace_mode:
        command=['/usr/bin/sandbox-exec','-f',str(workspace_record['cli_profile']),*command,*workspace_record.get('cli_args',[])]
        environment.update(workspace_environment)
    else:
        command.append('--sandbox')
    # Workspace preparation and collection belong to the caller. Material inputs
    # remain embedded and expose no file/terminal/MCP capability.
    chunks=[]; tools=[]; lock=threading.Lock(); sid=None; started=False; selection=None; failure=None
    stopped=False; reason=None; began=None; preflight_interrupted=False; error_evidence=None
    execution_wait=None; activity=None; last_live_write=None; last_live_state=None
    policy=waiting_policy(profile)
    thought_hashes=set(); thought_activity_sequence=0
    tool_steps={}
    provider_stop_reason=None; turn_result=None; cancel_requested=False
    def event(value):
        value.setdefault('at',time.time())
        with lock:
            with (job/'events.jsonl').open('a') as stream:stream.write(json.dumps(value,ensure_ascii=False)+'\n')
    def notification(method, params):
        nonlocal failure, thought_activity_sequence
        if method!='session/update':return
        if sid and params.get('sessionId')!=sid:
            failure='ACP notification belongs to a different session';return
        update=params.get('update',{});kind=update.get('sessionUpdate')
        # Store protocol observations, excluding thought content from public events.
        if kind=='agent_message_chunk' and update.get('content',{}).get('type')=='text':
            text=update['content'].get('text','');chunks.append(text)
            event({'event':'step_update','step_update':{'step_type':'agent_response','text_delta':text,'conversation_id':sid}})
        elif kind=='agent_thought_chunk' and started:
            content=update.get('content',{})
            text=content.get('text') if content.get('type')=='text' else None
            if isinstance(text,str) and text.strip():
                # Keep only an in-memory digest for deduplication. Neither text,
                # text length nor digest is persisted or exposed in reports.
                digest=hashlib.sha256(text.encode('utf-8',errors='replace')).digest()
                if digest not in thought_hashes:
                    thought_hashes.add(digest)
                    thought_activity_sequence+=1
                    event({'event':'step_update','step_update':{'step_type':'agent_thought',
                           'activity_sequence':thought_activity_sequence,'conversation_id':sid}})
        elif kind in ('tool_call','tool_call_update'):
            tools.append(update);event({'event':'acp_tool','update':update})
            if workspace_mode:
                tool_id=update.get('toolCallId')
                if isinstance(tool_id,str) and tool_id:
                    step=tool_steps.setdefault(tool_id,{'step_type':'tool','step_index':len(tool_steps),
                        'tool_name':'acp_tool','state':'UNKNOWN','conversation_id':sid})
                    # Titles identify progress only. A terminal state requires an
                    # explicit protocol status; a success-like title is no proof.
                    name=update.get('toolName') or update.get('name') or update.get('title') or update.get('kind')
                    if isinstance(name,str) and name:step['tool_name']=name
                    status=update.get('status')
                    if status is not None:
                        step['acp_status']=status
                        step['state']={'pending':'ACTIVE','in_progress':'ACTIVE',
                                       'completed':'DONE','failed':'ERROR'}.get(status,'UNKNOWN')
                    event({'event':'step_update','step_update':dict(step)})
        elif kind=='config_option_update':
            atomic_json(job/'acp-last-config.json',update)
            if selection is not None:
                selection['config_options'].update({o['id']:o for o in update.get('configOptions', [])
                    if o['id'] != 'thought_level' or not selection.get('model_execution')})
                expected={'model':selection['base_model']}
                if not selection.get('model_execution'):
                    expected['thought_level']=profile['effort']
                if any(selection['config_options'].get(key,{}).get('currentValue') != value
                       for key,value in expected.items()):
                    selection['matches']=False
                    failure='model_identity_mismatch: ACP configuration drifted during prompt'
                    atomic_json(job/'acp-model-evidence.json',selection)
    def request(method, params):
        event({'event':'acp_permission','method':method,'session_id':params.get('sessionId'),'decision':'denied'})
        if method=='session/request_permission':return {'outcome':{'outcome':'cancelled'}}
        raise ACPError('This task does not grant external client tools',code=-32601)
    client=ACPClient(command,
                     cwd=job,env=environment,stderr_path=job/'stderr.log',
                     on_notification=notification,on_request=request)
    try:
        client.start()
        atomic_json(job/'acp-process.json',{'pid':client.pid,'started_at':time.time(),'command_sha256':hashlib.sha256(json.dumps(command).encode()).hexdigest(),'session_cwd':profile.get('acp_session_cwd',str(job))})
        init=client.request('initialize',{'protocolVersion':1,'clientInfo':{'name':'gemini-bridge','version':VERSION},'clientCapabilities':{}},timeout=30)
        if init.get('protocolVersion')!=1:raise ValueError('Unsupported ACP protocol')
        atomic_json(job/'acp-initialize.json',init)
        prior=(profile.get('continuation') or {}).get('provider_session_id')
        session_cwd=profile.get('acp_session_cwd',str(job))
        params={'cwd':session_cwd,'mcpServers':copy.deepcopy(workspace_mcp_servers) if workspace_mode else []}
        if prior:
            if 'resume' not in init.get('agentCapabilities',{}).get('sessionCapabilities',{}):
                raise ValueError('ACP session resume is not supported')
            params['sessionId']=prior
        session=client.request('session/resume' if prior else 'session/new',params,timeout=50)
        sid=session.get('sessionId',prior)
        if not sid or prior and sid!=prior:raise ValueError('ACP session identity mismatch')
        session['sessionId']=sid
        atomic_json(job/'acp-session.json',session)
        if on_session_ready:on_session_ready(sid)
        selected=select_model(client,session,profile['model'],profile['effort'],profile.get('model_execution'))
        # Flush legitimate intermediate setting updates before tracking drift.
        client.drain_notifications(timeout=5)
        selection=selected
        atomic_json(job/'acp-model-evidence.json',selection)
        manifest=json.loads((job/'inputs.json').read_text());content=[{'type':'text','text':
            prompt if workspace_mode else 'Images are attached inline. Read the attached images directly; no tools are granted.\n'+prompt}]
        for item in manifest:
            if item['kind']=='image':
                data=Path(item['staged_path']).read_bytes()
                content.append({'type':'image','mimeType':mimetypes.guess_type(item['staged_path'])[0] or 'image/png',
                                'data':base64.b64encode(data).decode()})
        if before_execute:
            try:before_execute()
            except Exception:
                preflight_interrupted=True
                raise
        client.drain_notifications(timeout=5)
        if not selection['matches']:
            raise ValueError(failure or 'model_identity_mismatch: ACP configuration changed before prompt')
        if cancel and cancel.is_set():stopped=True;reason='cancelled'
        else:
            started=True;began=time.monotonic()
            activity=ActivityWait(profile,began,time.time()) if policy else None
            if on_process:on_process(client.pid)
            event({'event':'init','init':{'model':profile['model'],'conversation_id':sid,'transport':'acp',
                   'model_evidence':'ACP session configuration acknowledgement'}})
            answer={};done=threading.Event()
            def register_control():
                def steer(text, *, send_guard):
                    if before_execute:before_execute()
                    return client.request('session/prompt',{'sessionId':sid,'prompt':[{'type':'text','text':text}]},
                                          timeout=None,send_guard=send_guard)
                controls.register(job.name,sid,steer,job)
            def turn():
                try:answer['value']=client.request('session/prompt',{'sessionId':sid,'prompt':content},timeout=None,on_sent=register_control)
                except Exception as e:answer['error']=e
                finally:
                    controls.close(job.name)
                    done.set()
            threading.Thread(target=turn,daemon=True).start()
            deadline=began+profile.get('timeout_seconds',180)+GENERATION_EXIT_GRACE_SECONDS
            while True:
                if done.is_set():
                    if controls.wait_settled(job.name, timeout=.1):break
                else:
                    done.wait(.1)
                    if done.is_set() and controls.wait_settled(job.name):break
                now=time.monotonic()
                if activity is not None:
                    activity.read_events(job/'events.jsonl',now,time.time())
                    execution_wait=activity.metadata(now)
                    live_state=(execution_wait['observed_state'],execution_wait['soft_budget_exceeded'])
                    if last_live_write is None or now-last_live_write>=1 or live_state!=last_live_state:
                        try:atomic_json(job/'live-progress.json',execution_wait)
                        except OSError:pass
                        last_live_write,last_live_state=now,live_state
                    deadline=began+execution_wait['effective_deadline_seconds']
                if failure or cancel and cancel.is_set() or now>=deadline:
                    reason='cancelled' if cancel and cancel.is_set() else 'configuration_drift' if failure else 'timeout';stopped=True
                    controls.close(job.name)
                    client.notify('session/cancel',{'sessionId':sid})
                    cancel_requested=True
                    cancel_deadline=time.monotonic()+CANCEL_ACK_WAIT_SECONDS
                    while time.monotonic()<cancel_deadline:
                        if done.is_set() and controls.wait_settled(job.name, timeout=min(.05,max(0,cancel_deadline-time.monotonic()))):break
                        if not done.is_set():done.wait(min(.05,max(0,cancel_deadline-time.monotonic())))
                    break
            # Snapshot only the actual prompt result already received, including
            # during the bounded cancellation wait. Local intent is not an ACK.
            if done.is_set():
                if 'value' in answer:
                    turn_result=answer['value']
                    provider_stop_reason=turn_result.get('stopReason')
                    atomic_json(job/'acp-turn-result.json',turn_result)
                elif 'error' in answer:
                    raise answer['error']
            client.drain_notifications(timeout=5)
            if not stopped:
                reason=provider_stop_reason
    except Exception as error:
        if preflight_interrupted:
            error.acp_no_prompt_started = not started
            raise
        failure=str(error)
        if isinstance(error,ACPError):
            error_evidence={'message':str(error),'code':error.code,'data':error.data,'kind':error.kind}
    finally:
        controls.close(job.name)
        if sid:
            try:client.request('session/close',{'sessionId':sid},timeout=2)
            except Exception:pass
        client.close()
    if activity is not None:
        now=time.monotonic()
        activity.read_events(job/'events.jsonl',now,time.time())
        execution_wait=activity.metadata(now)
        try:atomic_json(job/'live-progress.json',execution_wait)
        except OSError:pass
    response=''.join(chunks)
    steering_evidence=controls.terminal_evidence(job.name)
    provider_turns_terminal=provider_stop_reason in _TERMINAL_STOP_REASONS and steering_evidence['all_sent_terminal']
    issues=[]
    if not provider_turns_terminal:issues.append('provider_prompt_terminal_unproven')
    for steering in controls.records(job.name):
        if steering['status']=='responded' and steering.get('stop_reason')!='end_turn':
            issues.append('steering_stop_reason:'+str(steering.get('stop_reason')))
    if failure:issues.append('acp_error: '+failure)
    if selection is not None and not selection['matches']:issues.append('model_identity_mismatch')
    provider_error_category,provider_error_message=provider_failure_details(error_evidence)
    if provider_execution_aborted(response):
        provider_error_category='provider_execution_aborted'
        provider_error_message='CLI 报告执行因错误终止，尚无可验收的交付；正常结束标记不代表生成成功。'
    if error_evidence and error_evidence.get('kind') in ('eof','transport','closed','timeout','protocol'):
        provider_error_category='transport_interrupted'
        provider_error_message='ACP连接中断，远端执行结果尚未确认。'
    provider_terminal_error=bool(error_evidence and error_evidence.get('kind')=='rpc'
        and type(error_evidence.get('code')) is int and steering_evidence['all_sent_terminal'])
    if provider_error_category:issues.append('provider_error:'+provider_error_category)
    if tools and not workspace_mode:issues.append('unexpected_tool_use_in_material_task')
    if provider_stop_reason!='end_turn':issues.append('acp_stop_reason:'+str(provider_stop_reason))
    contract=profile.get('acp_output_contract') or {}
    warnings=check_output(response,contract)
    if contract.get('strict'):issues.extend(warnings);warnings=[]
    if not response.strip():issues.append('empty_response')
    status=reason if reason in ('cancelled','timeout') else 'failed_or_unverified' if issues else 'completed_unreviewed'
    report={'job_id':job.name,'status':status,'response':response,'bridge_version':VERSION,
            'provider_started':started,'session_cwd':profile.get('acp_session_cwd',str(job)),'runner':'antigravity','transport':'acp','provider_session_id':sid,
            'requested_model':profile['model'],'requested_effort':profile['effort'],
            'observed_model':(selection['model'] if selection['matches'] else selection['config_options'].get('model',{}).get('currentValue')) if selection else None,
            'model_matches':bool(selection and selection['matches']),
            'model_verification':'ACP offered variant and acknowledged session configuration',
            'effort_verification':('ACP fixed model configuration acknowledgement; effort is not adjustable; no independent backend trace'
                if profile.get('model_execution') else 'ACP thinking-level acknowledgement; no independent backend trace'),
            'selected_agent':agent,'observed_agent':None,'agent_verification':'configured custom agent and observed tool calls',
            'native_status':'SUCCESS' if reason=='end_turn' and not failure and not provider_error_category and provider_turns_terminal and not any(x.startswith('steering_stop_reason:') for x in issues) else 'ERROR',
            'provider_turns_terminal':provider_turns_terminal,'all_prompts_terminal':provider_turns_terminal,
            'remote_terminal_proven':provider_turns_terminal,
            'steering_terminal_evidence':steering_evidence,
            'native_error':failure,'acp_error':error_evidence,'provider_error_category':provider_error_category,
            'provider_terminal_error':provider_terminal_error,
            'provider_error_message':provider_error_message,'execution_wait':execution_wait,
            'stream_delivery_evidence':{'nonempty_response_observed':bool(response.strip())},'failure_reasons':issues,'output_warnings':warnings,'tool_calls':tools,
            'local_stop_reason':reason if stopped else None,'acp_stop_reason':provider_stop_reason,
            'cancel_protocol':{'requested':cancel_requested,
                               'acknowledged':cancel_requested and provider_turns_terminal and (provider_stop_reason=='cancelled' or any(
                                   row.get('status')=='responded' and row.get('stop_reason')=='cancelled'
                                   for row in controls.records(job.name))),
                               'provider_stop_reason':provider_stop_reason},
            'duration_seconds':round(time.monotonic()-began,2) if began else 0,
            'billing':'existing account route; charge not independently verified',
            'image_input_count':sum(i['kind']=='image' for i in json.loads((job/'inputs.json').read_text())),
            'artifact_review':'pending'}
    if profile.get('model_execution') is not None:
        report['model_execution']=copy.deepcopy(profile['model_execution'])
    report['steering']=controls.records(job.name)
    atomic_json(job/'report.json',report)
    event({'event':'result','result':{'status':report['native_status'],'response':response,'error':failure}})
    return report

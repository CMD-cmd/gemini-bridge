"""Read-only MCP Apps resource. It never submits, cancels or reviews a job."""
import json
import time
from pathlib import Path

import anyio
from mcp import types
from mcp.server.lowlevel.helper_types import ReadResourceContents

URI = 'ui://gemini-bridge/task-card-v9.html'
PANEL_URI = 'ui://gemini-bridge/native-panel-v9.html'
LEGACY_PANEL_URIS = ('ui://gemini-bridge/native-panel-v8.html', 'ui://gemini-bridge/native-panel-v7.html', 'ui://gemini-bridge/native-panel-v6.html', 'ui://gemini-bridge/native-panel-v5.html', 'ui://gemini-bridge/native-panel-v4.html', 'ui://gemini-bridge/native-panel-v3.html', 'ui://gemini-bridge/native-panel-probe-v1.html', 'ui://gemini-bridge/native-panel-v2.html')
LEGACY_URIS = ('ui://gemini-bridge/task-card-v8.html', 'ui://gemini-bridge/task-card-v7.html', 'ui://gemini-bridge/task-card-v6.html', 'ui://gemini-bridge/task-card-v5.html', 'ui://gemini-bridge/task-card-v4.html', 'ui://gemini-bridge/task-card-v3.html', 'ui://gemini-bridge/task-card-v1.html', 'ui://gemini-bridge/task-card-v2.html')
MIME = 'text/html;profile=mcp-app'
ROOT = Path(__file__).resolve().parent
META = {'ui': {'resourceUri': URI}, 'openai/outputTemplate': URI}
UI_SECURITY = {'ui': {'prefersBorder': True, 'csp': {'connectDomains': [], 'resourceDomains': []}}}
PANEL_SECURITY = dict(UI_SECURITY, **{'openai/ui': {
    'availableDisplayModes': ['inline', 'fullscreen'], 'preferredDisplayMode': 'fullscreen'}})


def html_resource():
    copy = json.dumps(json.loads((ROOT/'web'/'task-card-copy.json').read_text()), ensure_ascii=False).replace('<', '\\u003c')
    script = (ROOT/'web'/'task-card.js').read_text().replace('</script', '<\\/script')
    return (ROOT/'web'/'task-card.html').read_text().replace('__CARD_COPY__', copy).replace('__CARD_SCRIPT__', script)


def panel_resource():
    script = (ROOT/'web'/'native-panel.js').read_text().replace('</script', '<\\/script')
    return (ROOT/'web'/'native-panel.html').read_text().replace('__PANEL_SCRIPT__', script)


def panel_probe_enabled(job_id):
    """Temporary opt-in for one existing task. All other cards remain unchanged."""
    try:
        raw = (ROOT/'.runtime'/'panel-probe.json').read_text()
        if len(raw) > 1024:
            return False
        value = json.loads(raw)
        return value.get('job_id') == job_id and time.time() < float(value.get('expires_at', 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def delivery_sources(entries, manifest):
    """Add one fixed provenance label to already verified downloadable entries."""
    trusted={(str(row.get('id')),row.get('sha256')) for row in manifest or []
             if isinstance(row,dict) and isinstance(row.get('sha256'),str) and row.get('source')=='trusted_project_tool'}
    return [dict(entry,source='trusted_project_tool') if isinstance(entry.get('sha256'),str) and (str(entry.get('id')),entry.get('sha256')) in trusted
            else dict(entry) for entry in entries or [] if isinstance(entry,dict)]


def panel_snapshot(job_id):
    from viewer import Observer, pick, SUMMARY_KEYS
    observer=Observer(ROOT)
    detail=observer.detail(job_id)
    manifest=observer.read(observer.job(job_id),'deliverables.json',[])
    detail['deliverables']=delivery_sources(detail.get('deliverables'),manifest if isinstance(manifest,list) else [])
    return {**{key:detail.get(key) for key in ('prompt','inputs','progress','acceptance','workspace','deliverables')},
            'state':pick(detail.get('state'), SUMMARY_KEYS),
            'report':{key:(detail.get('report') or {}).get(key) for key in
                      ('response','observed_model','duration_seconds','usage')},
            'warnings':detail.get('warnings') or []}


def compact_card(value, acceptance=None):
    """Project already-authorized status data; no prompts, paths or credentials."""
    state = value.get('state') or {}
    report = value.get('report') or {}
    progress = value.get('progress') or {}
    acceptance = acceptance if isinstance(acceptance, dict) else {}
    return {'job_id': state.get('job_id'), 'title': state.get('title') or state.get('job_id'),
            'status': state.get('status'), 'review': state.get('review', 'pending'),
            'requested_model': state.get('model'), 'requested_effort': state.get('effort'),
            'observed_model': report.get('observed_model') or progress.get('observed_model'),
            'response': report.get('response') or '',
            'response_truncated': bool(report.get('response_truncated')),
            'created_at': state.get('created_at'), 'updated_at': state.get('updated_at'),
            'input_count': state.get('input_count'),
            'workspace':report.get('workspace') or state.get('workspace'),
            'delivery_count':len(report.get('deliverables') or []),
            'deliverables':value.get('deliverables') or [],
            'criteria': acceptance.get('criteria') or state.get('acceptance') or [],
            'review_note': acceptance.get('note'), 'reviewed_at': acceptance.get('reviewed_at'),
            'error': state.get('error'),
            'task_view_url': value.get('task_view_url'),
            'progress': {key:progress[key] for key in ('stage','label','reported_status','requires_attention',
                'no_recent_stream_activity','soft_budget_exceeded','meaningful_events','remaining_wait_seconds',
                'execution_elapsed_seconds','waiting_mode','activity') if key in progress}}


def review_record(job_id):
    # Reuse the viewer's bounded, symlink-safe reader; expose only review fields.
    from viewer import Observer
    observer = Observer(ROOT)
    try:
        return observer.read(observer.job(job_id), 'acceptance.json', [])
    except (OSError, ValueError):
        return None


def register(server):
    @server.list_resources()
    async def resources():
        return [types.Resource(uri=URI, name='Gemini Bridge task card', mimeType=MIME,
                               description='Read-only task status and delivery', **{'_meta': UI_SECURITY}),
                types.Resource(uri=PANEL_URI, name='Gemini Bridge task details', mimeType=MIME,
                               description='Read-only task card and native detail panel', **{'_meta': PANEL_SECURITY})]

    @server.read_resource()
    async def read_resource(uri):
        if str(uri) in (PANEL_URI, *LEGACY_PANEL_URIS):
            return [ReadResourceContents(content=panel_resource(), mime_type=MIME, meta=PANEL_SECURITY)]
        if str(uri) not in (URI, *LEGACY_URIS):
            raise ValueError('Unknown UI resource')
        html = html_resource()
        return [ReadResourceContents(content=html, mime_type=MIME, meta=UI_SECURITY)]

    async def call_tool(name, arguments):
        import mcp_server
        native_panel = name == 'bridge_native_panel'
        arguments = dict(arguments)
        if native_panel and not arguments.get('job_id'):
            try:
                probe = json.loads((ROOT/'.runtime'/'panel-probe.json').read_text())
                if panel_probe_enabled(probe.get('job_id')):
                    arguments['job_id'] = probe['job_id']
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        if not arguments.get('job_id'):
            return types.CallToolResult(structuredContent={'needs_job_id':True},
                content=[types.TextContent(type='text', text='Choose an existing task ID; no job was created.')],
                **{'_meta':{'ui':{'resourceUri':PANEL_URI}, 'openai/outputTemplate':PANEL_URI}})
        with anyio.CancelScope(shield=True):
            raw = await anyio.to_thread.run_sync(lambda:mcp_server.handle({
                'jsonrpc':'2.0','id':1,'method':'tools/call',
                'params':{'name':'bridge_status','arguments':{'job_id':arguments['job_id']}}}))
        result = raw['result']
        if result.get('isError'):
            return types.CallToolResult(**result)
        value = json.loads(result['content'][0]['text'])
        acceptance = await anyio.to_thread.run_sync(lambda:review_record(arguments['job_id']))
        detail=(result.get('_meta') or {}).get('panel_detail')
        if native_panel and detail is None:
            detail = await anyio.to_thread.run_sync(lambda:panel_snapshot(arguments['job_id']))
        if isinstance(detail,dict):value['deliverables']=detail.get('deliverables') or []
        card = compact_card(value, acceptance)
        meta = META
        if native_panel:
            meta = {'ui':{'resourceUri':PANEL_URI}, 'openai/outputTemplate':PANEL_URI,
                    'panel_detail':detail, 'panel_card':card}
        compact = {'job_id':card['job_id'],'status':card['status'],'review':card['review'],
                   'task_view_url':card['task_view_url']}
        if value.get('cli_update'):
            # Polling/status reads only prepare the notice. This outer native
            # tool returns it to Codex; legacy card polling must not claim it.
            if native_panel:
                from cli_updates import attach_notice
                compact = attach_notice(compact)
            else:
                compact['cli_update'] = value['cli_update']
        return types.CallToolResult(structuredContent={'job_id':card['job_id']} if native_panel else {'card':card},
            content=[types.TextContent(type='text', text=json.dumps(compact, ensure_ascii=False))], **{'_meta': meta})

    return {'tools':[types.Tool(name='bridge_task_card',
        description='Show or refresh an existing Gemini Bridge task card. Read-only: never generates, cancels, retries or changes acceptance. Full details remain at task_view_url.',
        inputSchema={'type':'object','properties':{'job_id':{'type':'string','minLength':1,
            'maxLength':100,'pattern':'^[A-Za-z0-9-]+$'}},'required':['job_id'],'additionalProperties':False},
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
        **{'_meta':META}), types.Tool(name='bridge_native_panel', title='Gemini Bridge 任务详情',
        description='Open the native read-only detail panel for an existing Gemini Bridge task. Never submits or retries generation. The panel requests native display changes without chat messages.',
        inputSchema={'type':'object','properties':{'job_id':{'type':'string','minLength':1,
            'maxLength':100,'pattern':'^[A-Za-z0-9-]+$'}},'additionalProperties':False},
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
        **{'_meta':{'ui':{'resourceUri':PANEL_URI}, 'openai/outputTemplate':PANEL_URI,
                   'openai/ui':{'entrypoints':[{'type':'thread'}]}}})], 'call_tool':call_tool}

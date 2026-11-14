"""Loopback-only authenticated task service. No external network listener."""
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import threading
from urllib.parse import urlparse
from bridge import ROOT, VERSION, SOURCE_FINGERPRINT, atomic_json, load_profile, get_model_catalog
from jobs import Jobs
from input_limits import load_limits

RUNTIME = ROOT / '.runtime'
DISCOVERY = Path.home() / 'Library' / 'Application Support' / 'GeminiBridge'

def available_features():
    features=['native_workspace','frozen_deliverables','isolated_code_worktree','cli_doctor',
              'submission_preview','scoped_continuation','compact_summary','bounded_retry',
              'acp_material_tasks','workspace_preview','live_task_activity']
    capability=json.loads((ROOT/'native-children-capability.json').read_text())
    if capability.get('status')=='qualified':features.append('native_readonly_child')
    elif capability.get('status')=='trial':features.append('native_child_trial')
    if capability.get('project_tools_status')=='qualified':features.append('project_tools')
    elif capability.get('project_tools_status')=='trial':features.append('project_tools_trial')
    for status,feature in [('acp_workspace_status','acp_workspace'),('default_acp_status','capability_transport_routing'),('steering_status','live_steering'),('recovery_status','read_only_context_recovery'),('preview_actions_status','interactive_full_page_preview'),('research_status','public_research_and_documents')]:
        if capability.get(status)=='qualified':features.append(feature)
    if set(capability.get('qualified_roles',[]))>={'reader','builder','reviewer'}:features.append('native_reader_builder_reviewer')
    return features

def main():
    import catalog_cache
    catalog_cache.enable()
    RUNTIME.mkdir(exist_ok=True, mode=0o700)
    lock = (RUNTIME / 'service.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    token = secrets.token_urlsafe(32)
    jobs = Jobs()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def send(self, value, code=200):
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def authorized(self):
            # Reject browser-origin requests until a reviewed GUI is installed.
            return (self.headers.get('Authorization') == 'Bearer ' + token
                    and not self.headers.get('Origin')
                    and self.headers.get('Host') == '127.0.0.1:' + str(self.server.server_port))
        def do_GET(self):
            if not self.authorized():
                return self.send({'error': 'Unauthorized'}, 403)
            try:
                path = urlparse(self.path).path
                if path == '/health':
                    value = {'ready': True, 'version': VERSION, 'source_fingerprint': SOURCE_FINGERPRINT, 'pid': os.getpid(), 'input_limits':load_limits(ROOT), 'features':available_features()}
                elif path == '/doctor':
                    from cli_capabilities import probe
                    value=probe(ROOT)
                elif path == '/jobs':
                    value = {'jobs': jobs.list()}
                elif path.startswith('/jobs/'):
                    value = jobs.summary(path.split('/')[2]) if path.endswith('/summary') else jobs.report(path.split('/')[2])
                elif path == '/profile':
                    value = load_profile(ROOT / 'profile.json')
                elif path == '/models':
                    value = {'catalog': get_model_catalog(force_refresh=True), 'catalog_observation':__import__('catalog_cache').metadata(), 'routing_config': __import__('model_selection').load_config(), 'mapped_models': json.loads((__import__('task_mode').ROUTING/'policy.json').read_text()).get('external_models',{}), 'automatic_specialist_budget': __import__('model_selection').budget_status()}
                else:
                    return self.send({'error': 'Not found'}, 404)
                self.send(value)
            except (ValueError, FileNotFoundError, subprocess.SubprocessError) as error:
                self.send({'error': str(error)}, 400)
        def do_POST(self):
            parsed = urlparse(self.path)
            parts = parsed.path.split('/')
            command_request = len(parts) == 4 and parts[1] == 'jobs' and parts[3] == 'commands'
            project_request = len(parts) == 4 and parts[1] == 'jobs' and parts[3] == 'project-tools'
            native_scope_request = len(parts) == 4 and parts[1] == 'jobs' and parts[3] == 'native-scope'
            local_request = (not self.headers.get('Origin') and not parsed.query
                             and self.headers.get('Host') == '127.0.0.1:' + str(self.server.server_port))
            if not local_request or not (command_request or project_request or native_scope_request) and not self.authorized():
                return self.send({'error': 'Unauthorized'}, 403)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= load_limits(ROOT)['MAX_HTTP_BYTES']:
                    raise ValueError('Invalid request size')
                args = json.loads(self.rfile.read(length))
                if not isinstance(args, dict):
                    raise ValueError('Request must be an object')
                path = urlparse(self.path).path
                parts = path.split('/')
                if native_scope_request:
                    auth=self.headers.get('Authorization','')
                    value=jobs.native_scope_check(parts[2],auth[7:] if auth.startswith('Bearer ') else '',args)
                elif project_request:
                    auth=self.headers.get('Authorization','')
                    value=jobs.project_tool_submit(parts[2],auth[7:] if auth.startswith('Bearer ') else '',args)
                elif command_request:
                    auth = self.headers.get('Authorization', '')
                    value = jobs.command_submit(parts[2], auth[7:] if auth.startswith('Bearer ') else '', args)
                elif path == '/plan':
                    value = jobs.plan(args)
                elif path == '/doctor':
                    from cli_capabilities import probe
                    if set(args)-{'refresh'} or type(args.get('refresh',False)) is not bool:raise ValueError('Invalid doctor request')
                    value=probe(ROOT,refresh=args.get('refresh',False))
                elif path == '/jobs':
                    value = jobs.submit(args)
                elif len(parts)==4 and parts[1]=='jobs' and parts[3]=='recover':
                    if set(args)-{'thread_id','probe'} or 'thread_id' not in args:raise ValueError('Recovery requires owner thread_id and optional probe')
                    from recovery import inspect
                    value=inspect(ROOT,parts[2],args['thread_id'],probe=args.get('probe',False))
                elif len(parts)==4 and parts[1]=='jobs' and parts[3]=='steer':
                    value=jobs.steer(parts[2],args)
                elif len(parts) == 4 and parts[1] == 'jobs' and parts[3] == 'retry':
                    if set(args) != {'thread_id'}:
                        raise ValueError('Retry requires only the current thread_id')
                    value = jobs.retry(parts[2], args['thread_id'])
                elif path == '/task-mode':
                    value = jobs.modes.control(args)
                    try:
                        value['automatic_specialist_budget']=jobs.specialist_budget.status(__import__('model_selection').load_config()['specialist_budget'],task_id=value.get('state',{}).get('task_id'))
                    except (ValueError,OSError):
                        value['automatic_specialist_budget']={'allowed':False,'reason':'local ledger unavailable; ordinary Flash remains available'}
                    if args.get('action') in ('disable', 'complete'):
                        for job_id in value['state']['job_ids']:
                            jobs.cancel(job_id)
                elif len(parts) == 4 and parts[1] == 'jobs' and parts[3] == 'cancel':
                    value = jobs.cancel(parts[2])
                elif len(parts) == 4 and parts[1] == 'jobs' and parts[3] == 'review':
                    value = jobs.review(parts[2], args)
                elif len(parts) == 4 and parts[1] == 'jobs' and parts[3] == 'revalidate':
                    if args:raise ValueError('Local revalidation takes no generation or scope arguments')
                    value = jobs.revalidate_material(parts[2])
                else:
                    return self.send({'error': 'Not found'}, 404)
                self.send(value)
            except (ValueError, FileNotFoundError, KeyError, UnicodeError) as error:
                self.send({'error': str(error)}, 400)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    jobs.command_base_url = 'http://127.0.0.1:' + str(server.server_port)
    DISCOVERY.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(DISCOVERY / 'service.json', {'pid': os.getpid(), 'port': server.server_port, 'token': token})
    atomic_json(RUNTIME / 'service.json', {'pid': os.getpid(), 'port': server.server_port, 'token': token})
    def stop(signum, frame):
        for state in jobs.list():
            if state['status'] in ('queued', 'running', 'cancelling'):
                jobs.cancel(state['job_id'])
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        jobs.queue.join()
        server.server_close()
        (RUNTIME / 'service.json').unlink(missing_ok=True)
        (DISCOVERY / 'service.json').unlink(missing_ok=True)

if __name__ == '__main__':
    main()

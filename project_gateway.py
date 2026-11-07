"""Task-specific capabilities for the native project's read-only MCP server."""
import hmac
import json
from pathlib import Path
import secrets
import threading


class ProjectGateway:
    def __init__(self, root):
        self.root=Path(root);self.lock=threading.RLock();self.tokens={}

    def open(self, job_id, base_url):
        with self.lock:
            if job_id in self.tokens:raise ValueError('Project tool capability is already open')
            token=secrets.token_urlsafe(32);self.tokens[job_id]=token
            return {'GEMINI_BRIDGE_PROJECT_URL':base_url+'/jobs/'+job_id+'/project-tools',
                    'GEMINI_BRIDGE_PROJECT_TOKEN':token}

    def close(self, job_id):
        with self.lock:self.tokens.pop(job_id,None)

    def authorize_scope(self,job_id,token):
        with self.lock:
            expected=self.tokens.get(job_id)
            if not expected or not isinstance(token,str) or not hmac.compare_digest(expected,token):
                raise ValueError('Native scope capability is unavailable')
        from project_tools import authorize_scope
        policy=json.loads((self.root/'runs'/job_id/'workspace-policy.json').read_text())
        if Path(policy['job_dir']).resolve()!=(self.root/'runs'/job_id).resolve():raise ValueError('Foreign task policy')
        authorize_scope(policy)
        return {'allowed':True}

    def call(self, job_id, token, request):
        with self.lock:
            expected=self.tokens.get(job_id)
            if not expected or not isinstance(token,str) or not hmac.compare_digest(expected,token):
                raise ValueError('Project tool capability is unavailable')
        if (not isinstance(request,dict) or set(request)!={'tool','arguments','root_conversation_id'}
                or not isinstance(request['tool'],str) or not isinstance(request['arguments'],dict)):
            raise ValueError('Invalid project tool request')
        from project_tools import handle_call
        policy=json.loads((self.root/'runs'/job_id/'workspace-policy.json').read_text())
        if Path(policy['job_dir']).resolve()!=(self.root/'runs'/job_id).resolve():
            raise ValueError('Project tool policy belongs to another job')
        return handle_call(policy,request['tool'],request['arguments'],request['root_conversation_id'])

"""Fresh scope authorization for native hooks through their task capability."""
import http.client
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def verify(policy):
    endpoint=os.environ.get('GEMINI_BRIDGE_PROJECT_URL','');token=os.environ.get('GEMINI_BRIDGE_PROJECT_TOKEN','')
    parsed=urlsplit(endpoint);expected='/jobs/'+Path(policy['job_dir']).name+'/project-tools'
    if (parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path!=expected
            or not token or '\n' in token or '\r' in token):
        raise ValueError('Fresh task scope capability is unavailable')
    connection=http.client.HTTPConnection('127.0.0.1',parsed.port,timeout=3)
    try:
        connection.request('POST',expected.rsplit('/',1)[0]+'/native-scope',body=b'{}',
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        response=connection.getresponse();body=response.read(4097)
        if response.status!=200 or len(body)>4096 or json.loads(body).get('allowed') is not True:
            raise ValueError('Task scope is closed, expired or unavailable')
    finally:connection.close()

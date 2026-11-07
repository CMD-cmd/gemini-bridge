"""Stdio MCP adapter; authorization and reads run in the host task broker."""
import http.client
import json
import os
import re
import sys
import time
import stat
from pathlib import Path
from urllib.parse import urlsplit

MAX_RESPONSE_BYTES = 1024 * 1024


def _load_launch_policy(path):
    """Read the host-fixed launch config, not a model-supplied source path.

    The CLI sandbox denies opening the runs/ ancestor as a directory but permits
    this job's own control file. Source reads stay in the host broker's dirfd walk.
    """
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts or path.name!='workspace-policy.json':
        raise ValueError('Invalid launch policy path')
    fd=os.open(str(path),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid():
            raise ValueError('Launch policy must be an owned regular file')
        data=stream.read(2*1024*1024+1)
    if len(data)>2*1024*1024:raise ValueError('Launch policy exceeds limit')
    policy=json.loads(data)
    if not isinstance(policy,dict) or policy.get('job_dir')!=str(path.parent):
        raise ValueError('Launch policy belongs to another job')
    return policy


def _observe(policy, phase):
    """Private startup evidence only; never include capabilities or tool inputs."""
    try:
        path=Path(policy['job_dir'])/'cli-tmp/project-mcp-startup.jsonl'
        fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'a') as output:
            output.write(json.dumps({'phase':phase,'at':time.time(),'pid':os.getpid()})+'\n')
    except (OSError,KeyError,TypeError):
        pass  # Diagnostics must not change authorization or execution.


def _clear_environment():
    keep = {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'LC_CTYPE', 'SYSTEMROOT',
            'PYTHONDONTWRITEBYTECODE', 'PYTHONIOENCODING', 'VIRTUAL_ENV'}
    for key in list(os.environ):
        if key not in keep:
            del os.environ[key]


def _broker(broker_url, broker_token):
    if not isinstance(broker_url, str) or not isinstance(broker_token, str) or not broker_token or any(c in broker_token for c in '\r\n'):
        raise ValueError('Missing project broker capability')
    parsed = urlsplit(broker_url)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or not parsed.port or parsed.netloc != '127.0.0.1:' + str(parsed.port)
            or not re.fullmatch(r'/jobs/[A-Za-z0-9_-]+/project-tools', parsed.path)):
        raise ValueError('Invalid project broker URL')
    host, port, route = parsed.hostname, parsed.port, parsed.path
    authority = parsed.netloc

    def forward(name, arguments, rootid):
        connection = http.client.HTTPConnection(host, port, timeout=70 if name in ('pubmed_search','literature_search','public_page','document_extract') else 10)
        try:
            body = json.dumps({'tool': name, 'arguments': arguments,
                               'root_conversation_id': rootid}, allow_nan=False).encode()
            # HTTPConnection never reads proxy configuration and never follows redirects.
            connection.request('POST', route, body=body, headers={
                'Host': authority, 'Authorization': 'Bearer ' + broker_token,
                'Content-Type': 'application/json', 'Accept': 'application/json'})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError('Project broker refused request')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
                raise ValueError('Invalid project broker response size')
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError('Project broker response exceeds byte limit')
            result = json.loads(data)
            if not isinstance(result, dict) or type(result.get('ok')) is not bool:
                raise ValueError('Invalid project broker response')
            return result
        except (OSError, ValueError, TypeError, http.client.HTTPException):
            return {'ok': False, 'error': 'Project broker request failed'}
        finally:
            connection.close()
    return forward


def create_server(policy, broker_url=None, broker_token=None):
    forward = _broker(broker_url, broker_token)
    from mcp import types
    from mcp.server import Server
    from project_tools import list_tools, ROOT_META_KEY
    import anyio
    server = Server('bridge-project-tools', version='1.0.0')

    @server.list_tools()
    async def tools_list():
        _observe(policy,'tools/list')
        return [types.Tool(**item) for item in list_tools(policy)]

    @server.call_tool(validate_input=False)
    async def tools_call(name, arguments):
        meta = server.request_context.meta
        raw = meta.model_dump(by_alias=True) if meta is not None else {}
        result = await anyio.to_thread.run_sync(lambda: forward(name, arguments, raw.get(ROOT_META_KEY)))
        return types.CallToolResult(isError=not result['ok'], content=[
            types.TextContent(type='text', text=json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')))])
    return server


async def _run(policy, broker_url, broker_token):
    from mcp.server.stdio import stdio_server
    server = create_server(policy, broker_url=broker_url, broker_token=broker_token)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    broker_url = os.environ.get('GEMINI_BRIDGE_PROJECT_URL')
    broker_token = os.environ.get('GEMINI_BRIDGE_PROJECT_TOKEN')
    _clear_environment()
    if len(sys.argv) != 2:
        print('Usage: project_tools_mcp.py POLICYFILE', file=sys.stderr)
        return 2
    try:
        _broker(broker_url, broker_token)  # Fail before initializing without a capability.
        policy = _load_launch_policy(sys.argv[1])
        _observe(policy,'process_started')
        import anyio
        anyio.run(_run, policy, broker_url, broker_token)
    except Exception as error:
        print('Project tools MCP stopped: ' + type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

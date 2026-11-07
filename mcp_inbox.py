"""Read-only result adapter for an Antigravity-side MCP client. No dispatch loop."""
import json
import sys
import mcp_server

ALLOWED = {'bridge_jobs', 'bridge_status', 'bridge_report'}
original_call = mcp_server.call_tool

def readonly_call(name, args):
    if name not in ALLOWED:
        raise ValueError('Read-only inbox: execution, cancellation and review are unavailable')
    return original_call(name, args)

mcp_server.TOOLS = [tool for tool in mcp_server.TOOLS if tool['name'] in ALLOWED]
mcp_server.call_tool = readonly_call

if __name__ == '__main__':
    for line in sys.stdin:
        try:
            response = mcp_server.handle(json.loads(line))
        except Exception:
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Invalid request'}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)

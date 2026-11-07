"""Optional SDK client for the parallel HTTP MCP. Credentials stay local."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def request(method, name=None, arguments=None):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    import httpx
    from http_mcp_runtime import read_state, healthy
    state = read_state()
    if not healthy(state):
        raise RuntimeError('Parallel HTTP MCP is not ready; run http_mcp_runtime.py ensure')
    def local_client(**kwargs):
        return httpx.AsyncClient(trust_env=False, **kwargs)
    async with streamablehttp_client(state['url'], headers={'Authorization':'Bearer '+state['token']},
                                     httpx_client_factory=local_client) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if method == 'list':
                result = await session.list_tools()
            elif method == 'resources':
                result = await session.list_resources()
            elif method == 'read':
                from pydantic import AnyUrl
                result = await session.read_resource(AnyUrl(name))
            elif method == 'call':
                result = await session.call_tool(name, arguments or {})
            else:
                raise ValueError('Unknown method')
            return result.model_dump(mode='json', by_alias=True, exclude_none=True)


def unwrap(result):
    if result.get('structuredContent'):
        return result['structuredContent']
    for block in result.get('content', []):
        if block.get('type') == 'text':
            try:
                return json.loads(block['text'])
            except ValueError:
                pass
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('method', choices=['list','call','resources','read'])
    parser.add_argument('name', nargs='?')
    parser.add_argument('--request-file', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    data = json.loads(args.request_file.read_text()) if args.request_file else {}
    started = time.monotonic()
    result = asyncio.run(request(args.method, args.name, data))
    value = {'transport':'streamable-http','elapsed_ms':round(1000*(time.monotonic()-started)),
             'isError':bool(result.get('isError')), 'value':unwrap(result)}
    if args.output:
        args.output.write_text(json.dumps(value,ensure_ascii=False,indent=2))
        print(json.dumps({'saved':str(args.output),'elapsed_ms':value['elapsed_ms'],'isError':value['isError']}))
    else:
        print(json.dumps(value,ensure_ascii=False,indent=2))

"""Independent loopback Streamable HTTP transport for the existing bridge.

register_extensions(server) may register resources/prompts without changing tools.
Tool execution reuses the stdio adapter once imported; no hot reload is performed.
"""
import json
import secrets

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

HOST = "127.0.0.1"
PORT = 50252
from input_limits import load_limits
from pathlib import Path
MAX_BODY = load_limits(Path(__file__).resolve().parent)['MAX_HTTP_BYTES']


def create_app(token, instance, adapter=None, register_extensions=None, port=PORT):
    if adapter is None:
        import mcp_server as adapter
    server = Server("gemini-bridge-http", version=adapter.VERSION,
                    instructions=getattr(adapter, "SERVER_INSTRUCTIONS", None))
    definitions = {item["name"]: item for item in adapter.TOOLS}
    extensions = register_extensions(server) if register_extensions else None
    extensions = extensions or {}
    extra_names = set()
    for item in extensions.get("tools", []):
        definition = item.model_dump(by_alias=True, exclude_none=True) if isinstance(item, types.Tool) else item
        name = definition["name"]
        if name in definitions:
            raise ValueError(f"Duplicate extension tool: {name}")
        definitions[name] = definition
        extra_names.add(name)
    if extra_names and not callable(extensions.get("call_tool")):
        raise ValueError("Extension tools require an async call_tool handler")

    @server.list_tools()
    async def list_tools():
        return [types.Tool(**item) for item in definitions.values()]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name not in definitions:
            return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text="Unknown tool")])
        if name in extra_names:
            return await extensions["call_tool"](name, arguments)
        # The SDK validates JSON Schema before entering this function. A worker
        # thread keeps slow observations/preflight from blocking status requests.
        # Shielding does not cancel a submitted bridge job on disconnect.
        with anyio.CancelScope(shield=True):
            response = await anyio.to_thread.run_sync(lambda: adapter.handle({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}))
        return types.CallToolResult(**response["result"])

    authority = f"{HOST}:{port}"
    manager = StreamableHTTPSessionManager(
        server, stateless=True, json_response=True, max_request_body_size=MAX_BODY,
        security_settings=TransportSecuritySettings(
            allowed_hosts=[authority], allowed_origins=[f"http://{authority}"]))

    class Application:
        mcp_server = server

        async def __call__(self, scope, receive, send):
            if scope["type"] == "lifespan":
                async with manager.run():
                    while True:
                        message = await receive()
                        if message["type"] == "lifespan.startup":
                            await send({"type": "lifespan.startup.complete"})
                        elif message["type"] == "lifespan.shutdown":
                            await send({"type": "lifespan.shutdown.complete"})
                            return
            if scope["type"] != "http":
                return

            async def reply(status, data):
                body = json.dumps(data).encode()
                extra = [(b"allow", b"POST")] if status == 405 else []
                await send({"type": "http.response.start", "status": status,
                            "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")] + extra})
                await send({"type": "http.response.body", "body": body})

            pairs = scope.get("headers", [])
            headers = {key.lower(): value for key, value in pairs}
            # Reject duplicate security headers rather than accepting a proxy's
            # interpretation of one member of an ambiguous header set.
            if any(sum(key.lower() == field for key, _ in pairs) > 1
                   for field in (b"host", b"origin", b"authorization")):
                return await reply(400, {"error": "Ambiguous headers"})
            if headers.get(b"host") != authority.encode():
                return await reply(421, {"error": "Invalid host"})
            if b"origin" in headers and headers[b"origin"] != f"http://{authority}".encode():
                return await reply(403, {"error": "Invalid origin"})
            if scope["path"] == "/health" and scope["method"] == "GET":
                return await reply(200, {"status": "ok", "transport": "streamable-http", "instance": instance})
            if scope["path"] != "/mcp":
                return await reply(404, {"error": "Not found"})
            if not secrets.compare_digest(headers.get(b"authorization", b""), ("Bearer " + token).encode()):
                return await reply(401, {"error": "Authentication required"})
            if scope["method"] != "POST":
                return await reply(405, {"error": "Only POST is supported"})
            length = headers.get(b"content-length")
            if length is not None:
                try:
                    if int(length) < 0:
                        raise ValueError()
                    if int(length) > MAX_BODY:
                        return await reply(413, {"error": "Request body too large"})
                except ValueError:
                    return await reply(400, {"error": "Invalid content length"})
            # Bound chunked bodies too, before the SDK parses JSON.
            chunks, size = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > MAX_BODY:
                    return await reply(413, {"error": "Request body too large"})
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
                return await receive()

            await manager.handle_request(scope, replay, send)

    return Application()


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    import uvicorn
    from task_card import register
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", required=True)
    parser.parse_args()
    state = json.loads((Path(__file__).resolve().parent / ".runtime" / "http-mcp.json").read_text())
    uvicorn.run(create_app(state["token"], state["instance"], register_extensions=register), host=HOST, port=PORT,
                access_log=False, log_level="warning", timeout_graceful_shutdown=40)

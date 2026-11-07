"""Real local HTTP + official SDK tests; never dispatch a Gemini job."""
import asyncio
from contextlib import asynccontextmanager
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

try:
    import httpx
    import uvicorn
    from mcp import ClientSession, types
    from mcp.client.streamable_http import streamable_http_client
except ImportError:
    HAS_HTTP = False
else:
    HAS_HTTP = True
    import http_mcp
    import mcp_server


@unittest.skipUnless(HAS_HTTP, 'Optional HTTP tests use .venv-http; legacy bridge has no new dependency')
class HTTPTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls = []
        cls.slow_started = threading.Event()
        cls.slow_release = threading.Event()
        cls.slow_finished = threading.Event()

        def dispatch(name, arguments):
            cls.calls.append((name, arguments))
            if name == "bridge_models":
                cls.slow_started.set()
                cls.slow_release.wait(5)
                cls.slow_finished.set()
            if arguments.get("job_id") == "error":
                raise ValueError("original bridge error")
            return {"name": name, "arguments": arguments}

        cls.patches = [patch.object(mcp_server, "call_tool", dispatch),
                       patch.object(mcp_server.progress, "enrich", lambda v: dict(v, enriched=True)),
                       patch.object(mcp_server.viewer, "attach_links", lambda v: dict(v, linked=True)),
                       patch("cli_updates.attach_notice", lambda v, **kwargs: dict(v, update_checked=True))]
        for item in cls.patches:
            item.start()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        cls.url = f"http://127.0.0.1:{cls.port}/mcp"

        def extensions(server):
            @server.list_resources()
            async def resources():
                return [types.Resource(uri="bridge://test", name="test", mimeType="text/plain")]

            async def call_tool(name, arguments):
                return types.CallToolResult(content=[types.TextContent(type="text", text="extension")])

            return {"tools": [types.Tool(name="test_extension", inputSchema={
                "type": "object", "properties": {}, "additionalProperties": False},
                **{"_meta":{"ui":{"resourceUri":"ui://test/index.html"}}})], "call_tool": call_tool}

        app = http_mcp.create_app("test-secret", "test-instance", register_extensions=extensions, port=cls.port)
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cls.port,
                                                  access_log=False, log_level="critical"))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [sock]}, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("Test HTTP server did not start")
            time.sleep(.02)

    @classmethod
    def tearDownClass(cls):
        cls.slow_release.set()
        cls.server.should_exit = True
        cls.thread.join(10)
        for item in reversed(cls.patches):
            item.stop()

    @asynccontextmanager
    async def session(self):
        async with httpx.AsyncClient(headers={"Authorization": "Bearer test-secret"}, trust_env=False) as client:
            async with streamable_http_client(self.url, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    self.assertEqual(initialized.instructions, mcp_server.SERVER_INSTRUCTIONS)
                    yield session

    async def test_initialize_list_call_and_resource_extension(self):
        async with self.session() as session:
            tools = await session.list_tools()
            self.assertEqual({item.name for item in tools.tools}, {item["name"] for item in mcp_server.TOOLS} | {"test_extension"})
            extension = next(item for item in tools.model_dump(by_alias=True)["tools"] if item["name"] == "test_extension")
            self.assertEqual(extension["_meta"]["ui"]["resourceUri"], "ui://test/index.html")
            self.assertNotIn("meta", extension)
            result = await session.call_tool("bridge_jobs", {})
            value = json.loads(result.content[0].text)
            self.assertFalse(result.isError)
            self.assertTrue(value["enriched"] and value["linked"] and value["update_checked"])
            self.assertEqual(value["mcp_adapter_version"], mcp_server.VERSION)
            self.assertEqual((await session.list_resources()).resources[0].name, "test")
            self.assertEqual((await session.call_tool("test_extension", {})).content[0].text, "extension")

    async def test_schema_and_unknown_tool_do_not_dispatch(self):
        async with self.session() as session:
            for name, arguments in [
                ("bridge_jobs", {"unexpected": True}),
                ("bridge_submit", {}),
                ("bridge_submit", {"prompt": "x", "image_count": True}),
                ("bridge_submit", {"prompt": "x", "output_contract": {"bad": 1}}),
                ("bridge_submit", {"prompt": "x", "effort": "invalid"}),
                ("test_extension", {"unexpected": True}),
                ("unknown", {})]:
                before = len(self.calls)
                result = await session.call_tool(name, arguments)
                self.assertTrue(result.isError, (name, arguments))
                self.assertEqual(len(self.calls), before)

    async def test_duplicate_extension_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate extension"):
            http_mcp.create_app("test", "test", register_extensions=lambda server: {
                "tools": [mcp_server.TOOLS[0]], "call_tool": lambda *args: None})

    async def test_disconnect_does_not_cancel_dispatch(self):
        self.slow_started.clear()
        self.slow_release.clear()
        self.slow_finished.clear()
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "bridge_models", "arguments": {}}}).encode()
        header = (f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                  "Authorization: Bearer test-secret\r\nContent-Type: application/json\r\n"
                  f"Accept: application/json, text/event-stream\r\nContent-Length: {len(body)}\r\n\r\n")
        writer.write(header.encode() + body)
        await writer.drain()
        try:
            self.assertTrue(await asyncio.to_thread(self.slow_started.wait, 2))
            writer.close()
            await writer.wait_closed()
        finally:
            self.slow_release.set()
        self.assertTrue(await asyncio.to_thread(self.slow_finished.wait, 2))

    async def test_original_error(self):
        async with self.session() as session:
            result = await session.call_tool("bridge_status", {"job_id": "error"})
            self.assertTrue(result.isError)
            self.assertEqual(result.content[0].text, "original bridge error")

    async def test_concurrent_independent_requests(self):
        self.slow_started.clear()
        self.slow_release.clear()
        async with self.session() as first, self.session() as second:
            slow = asyncio.create_task(first.call_tool("bridge_models", {}))
            try:
                await asyncio.wait_for(asyncio.to_thread(self.slow_started.wait, 2), 3)
                result = await asyncio.wait_for(second.call_tool("bridge_jobs", {}), 1)
                self.assertFalse(result.isError)
                self.assertFalse(slow.done())
            finally:
                self.slow_release.set()
                await slow

    async def test_http_boundaries(self):
        auth = {"Authorization": "Bearer test-secret"}
        async with httpx.AsyncClient(trust_env=False) as client:
            self.assertEqual((await client.post(self.url, json={})).status_code, 401)
            self.assertEqual((await client.post(self.url, json={}, headers={**auth, "Origin": "https://evil.test"})).status_code, 403)
            self.assertEqual((await client.post(self.url, json={}, headers={**auth, "Host": "evil.test"})).status_code, 421)
            self.assertEqual((await client.post(self.url, content=b"x" * (http_mcp.MAX_BODY + 1), headers=auth)).status_code, 413)

            async def chunks():
                yield b"x" * http_mcp.MAX_BODY
                yield b"x"
            self.assertEqual((await client.post(self.url, content=chunks(), headers=auth)).status_code, 413)
            health = await client.get(self.url.replace("/mcp", "/health"))
            self.assertNotIn("token", health.text)
            self.assertEqual(health.json()["status"], "ok")
            # JSON response mode intentionally has no standalone SSE stream.
            self.assertEqual((await client.get(self.url, headers={**auth, "Accept": "text/event-stream"})).status_code, 405)


if __name__ == "__main__":
    unittest.main()

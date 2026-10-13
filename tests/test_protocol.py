"""Exercise two independent stdio MCP processes sharing only a mailbox database."""

import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_servers_can_exchange_messages(self):
        sender, recipient = str(uuid4()), str(uuid4())
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "codex-fixture"
            fake.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
ids = json.loads(os.environ['FIXTURE_THREADS'])
if sys.argv[1] == 'queue':
    print('Queued fixture message')
    sys.exit(0)
for line in sys.stdin:
    req = json.loads(line)
    if 'id' not in req:
        continue
    method, params = req['method'], req.get('params', {})
    if method == 'initialize':
        result = {}
    elif method == 'thread/read' and params['threadId'] in ids:
        result = {'thread': {'id': params['threadId']}}
    elif method == 'thread/list':
        result = {'data': [{'id': i, 'name': i} for i in ids], 'nextCursor': None}
    else:
        print(json.dumps({'id': req['id'], 'error': {'message': 'Unknown thread'}}), flush=True)
        continue
    print(json.dumps({'id': req['id'], 'result': result}), flush=True)
''')
            fake.chmod(0o700)
            env = {**os.environ, "CODEX_MESSENGER_DATA": str(Path(temporary) / "mail"),
                   "CLAUDE_CONFIG_DIR": str(Path(temporary) / "claude"),
                   "CODEX_MESSENGER_CODEX": str(fake),
                   "FIXTURE_THREADS": json.dumps([sender, recipient])}
            params = StdioServerParameters(command=sys.executable,
                                          args=[str(ROOT / "scripts/server.py")], env=env)
            async with AsyncExitStack() as stack:
                sessions = []
                for _ in range(2):
                    read, write = await stack.enter_async_context(stdio_client(params))
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await session.initialize()
                    sessions.append(session)
                first, second = sessions
                names = {tool.name for tool in (await first.list_tools()).tools}
                self.assertEqual(names, {"ListAgents", "SendMessage", "ReadNotifications"})

                async def call(session, name, args):
                    result = await session.call_tool(name, args)
                    self.assertFalse(result.isError, result)
                    return json.loads(result.content[0].text)

                listed = await call(first, "ListAgents", {})
                self.assertEqual(len(listed["agents"]), 2)
                sent = await call(first, "SendMessage", {
                    "to": recipient, "from_thread": sender, "message": "Process A to B",
                })
                self.assertEqual(sent["status"], "queued")
                received = await call(second, "ReadNotifications", {"for_thread": recipient})
                self.assertEqual(received["messages"][0]["body"], "Process A to B")
                await call(second, "SendMessage", {
                    "to": sender, "from_thread": recipient, "message": "Process B to A",
                    "delivery": "inbox",
                })
                reply = await call(first, "ReadNotifications", {"for_thread": sender})
                self.assertEqual(reply["messages"][0]["body"], "Process B to A")
                read_again = await call(second, "ReadNotifications", {"for_thread": recipient})
                self.assertEqual(read_again["messages"], [])
                invalid = await second.call_tool("ReadNotifications", {"for_thread": recipient, "limit": 0})
                self.assertTrue(invalid.isError)


if __name__ == "__main__":
    unittest.main()

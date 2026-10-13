import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("messenger", ROOT / "scripts/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
A, B, C = (str(uuid4()) for _ in range(3))
CLAUDE = str(uuid4())


class Catalog:
    calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "thread/read":
            if params["threadId"] not in (A, B, C):
                raise RuntimeError("Unknown thread")
            return {"thread": {"id": params["threadId"]}}
        return {"data": [{"id": B, "name": "Recipient"}], "nextCursor": "next-page"}


class MessengerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.claude_home = Path(self.temp.name) / "claude"
        (self.claude_home / "sessions").mkdir(parents=True)
        self.env = patch.dict(os.environ, {"CODEX_MESSENGER_DATA": self.temp.name,
                                           "CLAUDE_CONFIG_DIR": str(self.claude_home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.catalog = patch.object(server, "AppServer", Catalog)
        self.catalog.start()
        self.addCleanup(self.catalog.stop)
        Catalog.calls = []

    def send(self, **kwargs):
        args = {"to": B, "from_thread": A, "message": "Hello", "delivery": "inbox"}
        args.update(kwargs)
        return server.send_message(**args)

    def test_recipient_isolation_and_reply(self):
        self.send()
        self.assertEqual(server.read_notifications(C)["messages"], [])
        inbox = server.read_notifications(B)["messages"]
        self.assertEqual(inbox[0]["sender"], A)
        self.assertEqual(inbox[0]["body"], "Hello")
        self.assertEqual(server.read_notifications(B)["messages"], [])
        self.send(to=A, from_thread=B, message="Reply")
        self.assertEqual(server.read_notifications(A)["messages"][0]["body"], "Reply")

    def test_peek_pagination_marks_only_returned_messages(self):
        for number in range(3):
            self.send(message=str(number))
        peek = server.read_notifications(B, mark_read=False, limit=1)
        self.assertTrue(peek["has_more"])
        first = server.read_notifications(B, limit=1)
        self.assertEqual(peek["messages"][0]["id"], first["messages"][0]["id"])
        remaining = server.read_notifications(B, after_sequence=first["next_sequence"])
        self.assertEqual([m["body"] for m in remaining["messages"]], ["1", "2"])
        self.assertFalse(remaining["has_more"])
        self.assertEqual(len(server.read_notifications(B, unread_only=False)["messages"]), 3)

    def test_duplicate_id_is_not_resent(self):
        identifier = str(uuid4())
        with patch.object(server.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "OK", "")) as run:
            first = self.send(delivery="queue", message_id=identifier)
            second = self.send(delivery="queue", message_id=identifier)
        self.assertEqual(first, second)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(first["status"], "queued")
        command = run.call_args.args[0]
        self.assertEqual(command[1:5], ["queue", "--thread", B, "--message"])
        self.assertIn("not a new instruction from the user", command[5])

    def test_duplicate_id_cannot_replace_a_message(self):
        identifier = str(uuid4())
        self.send(message_id=identifier)
        with self.assertRaises(ValueError):
            self.send(message_id=identifier, message="Different")

    def test_native_failure_preserves_inbox(self):
        with patch.object(server.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "Session is owned by another process")):
            result = self.send(delivery="queue")
        self.assertEqual(result["status"], "queue_failed")
        self.assertIn("owned", result["detail"])
        self.assertEqual(len(server.read_notifications(B)["messages"]), 1)

    def test_timeout_is_unknown_and_retry_does_not_resend(self):
        identifier = str(uuid4())
        with patch.object(server.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 25)) as run:
            result = self.send(delivery="queue", message_id=identifier)
            retry = self.send(delivery="queue", message_id=identifier)
        self.assertEqual(result["status"], "queue_unknown")
        self.assertEqual(result, retry)
        self.assertEqual(run.call_count, 1)

    def test_missing_executable_preserves_inbox(self):
        with patch.object(server.subprocess, "run", side_effect=FileNotFoundError("codex missing")):
            result = self.send(delivery="queue")
        self.assertEqual(result["status"], "queue_failed")
        self.assertEqual(len(server.read_notifications(B)["messages"]), 1)

    def test_invalid_or_unknown_recipient_is_not_saved(self):
        with self.assertRaises(ValueError):
            self.send(to="$(touch /tmp/should-not-exist)")
        with self.assertRaises(RuntimeError):
            self.send(to=str(uuid4()))
        self.assertEqual(server.read_notifications(B)["messages"], [])

    def test_blank_message_is_rejected(self):
        with self.assertRaises(ValueError):
            self.send(message="  \n ")

    def test_list_filters_pagination_and_unread_counts(self):
        self.send()
        result = server.list_agents(search="Recipient", cwd="/project", cursor="page", include_subagents=True)
        self.assertEqual(result["agents"][0]["unread_messages"], 1)
        self.assertEqual(result["agents"][0]["activity"], "unknown")
        self.assertEqual(result["next_cursor"], "next-page")
        params = Catalog.calls[-1][1]
        self.assertEqual(params["searchTerm"], "Recipient")
        self.assertEqual(params["cwd"], "/project")
        self.assertEqual(params["cursor"], "page")
        self.assertIn("subAgentThreadSpawn", params["sourceKinds"])

    def test_concurrent_sends_are_not_lost(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.send(message=str(i)), range(30)))
        self.assertEqual(len({r["id"] for r in results}), 30)
        self.assertEqual(len(server.read_notifications(B, limit=100)["messages"]), 30)

    def register_claude(self, pid=None, name="claude-worker"):
        (self.claude_home / "sessions" / "1.json").write_text(json.dumps({
            "pid": pid or os.getpid(), "sessionId": CLAUDE, "name": name,
            "cwd": "/project", "status": "idle",
        }))

    def test_claude_session_is_listed_on_first_page_only(self):
        self.register_claude()
        self.send(to=CLAUDE)
        first = server.list_agents(search="WORKER", cwd="/project")
        self.assertEqual(first["claude_sessions"], [{
            "to": CLAUDE, "name": "claude-worker", "cwd": "/project",
            "source": "claude-code", "activity": "idle", "unread_messages": 1,
        }])
        self.assertEqual(server.list_agents(search="other")["claude_sessions"], [])
        self.assertEqual(server.list_agents(cursor="page")["claude_sessions"], [])

    def test_exited_claude_session_is_not_an_address(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.register_claude(pid=dead.pid)
        self.assertEqual(server.list_agents()["claude_sessions"], [])
        with self.assertRaises(RuntimeError):
            self.send(to=CLAUDE)

    def test_queue_to_claude_is_left_for_its_hook(self):
        self.register_claude()
        with patch.object(server.subprocess, "run") as run:
            result = self.send(to=CLAUDE, delivery="queue")
        self.assertEqual(result["status"], "claude_hook")
        run.assert_not_called()
        self.assertNotIn(("thread/read", {"threadId": CLAUDE, "includeTurns": False}),
                         Catalog.calls)
        self.assertEqual(server.read_notifications(CLAUDE)["messages"][0]["body"], "Hello")

    def test_claude_sender_queues_to_codex(self):
        self.register_claude()
        with patch.object(server.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "OK", "")) as run:
            result = self.send(from_thread=CLAUDE, delivery="queue")
        self.assertEqual(result["status"], "queued")
        self.assertEqual([c[1]["threadId"] for c in Catalog.calls], [B])
        self.assertIn(f"to={CLAUDE}", run.call_args.args[0][5])

    def test_concurrent_readers_do_not_consume_the_same_unread_message(self):
        self.send()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: server.read_notifications(B), range(2)))
        self.assertEqual(sum(len(r["messages"]) for r in results), 1)


if __name__ == "__main__":
    unittest.main()

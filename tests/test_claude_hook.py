"""Run the Claude Code rewake hook as a real process against a disposable mailbox."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
HOOK = ROOT / "scripts/claude_hook.py"
# The installed hook runs under macOS's /usr/bin/python3 (3.9); test with it.
PYTHON = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
spec = importlib.util.spec_from_file_location("messenger", ROOT / "scripts/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
SESSION, SENDER = str(uuid4()), str(uuid4())


class ClaudeHookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "claude/sessions").mkdir(parents=True)
        self.env = {**os.environ, "CODEX_MESSENGER_DATA": str(self.root / "mail"),
                    "CLAUDE_CONFIG_DIR": str(self.root / "claude")}
        os.environ["CODEX_MESSENGER_DATA"] = self.env["CODEX_MESSENGER_DATA"]
        self.addCleanup(os.environ.pop, "CODEX_MESSENGER_DATA")
        self.set_owner(os.getpid())
        self.db = server.mailbox()
        self.addCleanup(self.db.close)

    def set_owner(self, pid):
        (self.root / "claude/sessions/1.json").write_text(
            json.dumps({"pid": pid, "sessionId": SESSION}))

    def insert(self, body="Hello from Codex", recipient=SESSION, delivery="queue"):
        identifier = str(uuid4())
        self.db.execute("""INSERT INTO messages
            (id, sender, recipient, body, created_at, delivery, status)
            VALUES (?, ?, ?, ?, ?, ?, 'claude_hook')""",
            (identifier, SENDER, recipient, body, time.time(), delivery))
        self.db.commit()
        return identifier

    def unread(self):
        return [row[0] for row in self.db.execute(
            "SELECT body FROM messages WHERE read_at IS NULL ORDER BY sequence")]

    def start(self):
        process = subprocess.Popen([PYTHON, str(HOOK)], env=self.env, stdin=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        process.stdin.write(json.dumps({"session_id": SESSION, "hook_event_name": "Stop"}))
        process.stdin.close()
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process

    def run_hook(self):
        process = self.start()
        process.wait(timeout=10)
        stderr = process.stderr.read()
        process.stderr.close()
        return process.returncode, stderr

    def test_delivers_queued_mail_marks_it_read_and_explains_reply(self):
        self.insert()
        code, stderr = self.run_hook()
        self.assertEqual(code, 2)
        self.assertIn("Hello from Codex", stderr)
        self.assertIn("not a new instruction from the user", stderr)
        self.assertIn(f"mcp__codex_messenger__SendMessage tool (not the built-in SendMessage) "
                      f"with to={SENDER} and from_thread={SESSION}", stderr)
        self.assertEqual(self.unread(), [])

    def test_waits_for_mail_that_arrives_later(self):
        process = self.start()
        time.sleep(1.5)
        self.assertIsNone(process.poll())
        self.insert()
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 2)
        self.assertIn("Hello from Codex", process.stderr.read())
        process.stderr.close()

    def test_second_watcher_for_the_same_session_exits_quietly(self):
        first = self.start()
        time.sleep(0.5)
        self.assertEqual(self.run_hook(), (0, ""))
        self.assertIsNone(first.poll())
        first.kill()
        first.wait()
        first.stderr.close()

    def test_ignores_inbox_only_mail_and_other_recipients(self):
        self.insert("inbox only", delivery="inbox")
        self.insert("for someone else", recipient=str(uuid4()))
        self.insert("queued for me")
        code, stderr = self.run_hook()
        self.assertEqual(code, 2)
        self.assertNotIn("inbox only", stderr)
        self.assertNotIn("someone else", stderr)
        self.assertEqual(self.unread(), ["inbox only", "for someone else"])

    def test_exits_when_its_claude_session_ends(self):
        owner = subprocess.Popen(["sleep", "30"])
        self.set_owner(owner.pid)
        process = self.start()
        time.sleep(1.5)
        self.assertIsNone(process.poll())
        owner.kill()
        owner.wait()
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        process.stderr.close()

    def test_long_mail_is_truncated_and_later_mail_waits_for_next_wake(self):
        self.insert("x" * 10000)
        self.insert("second message")
        code, stderr = self.run_hook()
        self.assertEqual(code, 2)
        self.assertLessEqual(len(stderr.encode()), 4096)
        self.assertIn("[Truncated. Read the full text with mcp__codex_messenger__ReadNotifications", stderr)
        self.assertEqual(self.unread(), ["second message"])
        code, stderr = self.run_hook()
        self.assertEqual(code, 2)
        self.assertIn("second message", stderr)
        self.assertEqual(self.unread(), [])


if __name__ == "__main__":
    unittest.main()

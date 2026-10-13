"""Standard-library access to Codex's local catalog and native submission queue."""

import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time


def codex_binary():
    return os.environ.get("CODEX_MESSENGER_CODEX") or shutil.which("codex") or str(
        Path.home() / ".local/bin/codex"
    )


class AppServer:
    """Short-lived catalog/queue connection; never resume another session."""

    def __init__(self, timeout=20):
        self.timeout = timeout

    def __enter__(self):
        self.process = subprocess.Popen(
            [codex_binary(), "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.sequence = 0
        try:
            self.call("initialize", {"clientInfo": {
                "name": "codex_session_messenger", "version": "0.1.0",
            }, "capabilities": {"experimentalApi": True}})
            self.process.stdin.write(b'{"method":"initialized"}\n')
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        self.selector.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()

    def call(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.process.stdin.write((json.dumps({
            "id": request_id, "method": method, "params": params,
        }) + "\n").encode())
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if b"\n" not in self.buffer:
                if not self.selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Codex app-server closed the connection.")
                self.buffer += chunk
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                response = json.loads(line)
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    raise RuntimeError(response["error"].get("message", "Codex RPC failed"))
                return response["result"]
        raise TimeoutError(f"Codex {method} timed out.")

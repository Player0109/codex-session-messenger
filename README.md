# Codex Session Messenger

[![Tests](https://github.com/Player0109/codex-session-messenger/actions/workflows/tests.yml/badge.svg)](https://github.com/Player0109/codex-session-messenger/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

An MCP server that lets **Codex and Claude Code exchange messages on the same machine**. It also connects independent local Codex sessions. Messages are stored in a shared SQLite mailbox, and optional hooks deliver them into the recipient's conversation.

| Tool | What it does |
| --- | --- |
| `ListAgents` | Finds saved local Codex sessions and running Claude Code sessions, with UUIDs, project paths, and unread counts. |
| `SendMessage` | Saves an attributed message and requests delivery, or saves it to the inbox only. |
| `ReadNotifications` | Reads a session's mailbox with unread tracking and pagination. |

## Requirements

- macOS or Linux. On Windows, run both clients inside the same WSL environment. Native Windows is unsupported because the hooks use POSIX file locks.
- Python 3.10+, [uv](https://docs.astral.sh/uv/getting-started/installation/), and Git. Make `uv`, `python3`, and `codex` available on the clients' `PATH`.
- A local Codex CLI with `app-server --stdio` and `queue`. Automatic Codex delivery also uses lifecycle hooks and the `thread/queue/list` and `thread/queue/delete` APIs.
- Claude Code with its local session registry and `asyncRewake` hooks, if you want Claude Code recipients to receive messages automatically.

The MCP SDK is pinned to `1.30.0`; `uv` installs it when the server first starts. The messenger needs no additional API key. Both clients still need their normal accounts and authentication.

The installation commands were checked with Codex CLI `0.162.0`. The original delivery integration was developed with Codex CLI `0.159.2` and Claude Code `2.1.284`. These client APIs can change; upgrade your client if a required command or hook option is missing.

## Install

### 1. Clone the repository

```sh
git clone https://github.com/Player0109/codex-session-messenger.git
cd codex-session-messenger
```

Keep this checkout: Claude Code's configuration will point to its server and hook scripts.

### 2. Install the Codex plugin

From the repository directory:

```sh
codex plugin marketplace add .
codex plugin add codex-session-messenger@session-messenger
```

Open a new Codex conversation. In the Codex CLI, open `/hooks`, review the messenger hooks, and trust the definitions you want to run. New or changed hooks are skipped until trusted. The plugin registers its MCP server as `session_messenger`.

You can also install the Codex plugin directly from GitHub:

```sh
codex plugin marketplace add Player0109/codex-session-messenger
codex plugin add codex-session-messenger@session-messenger
```

### 3. Register the server in Claude Code

From the cloned repository directory:

```sh
MESSENGER_ROOT="$PWD"
UV_BIN="$(command -v uv)"
claude mcp add --scope user --transport stdio codex_messenger -- \
  "$UV_BIN" run --script "$MESSENGER_ROOT/scripts/server.py"
```

Keep the Claude Code server name **`codex_messenger`**: its hook uses that name to identify the reply and inbox tools. Claude Code's built-in `SendMessage` is a different tool.

### 4. Enable automatic delivery to Claude Code

Merge the `hooks` from [examples/claude-hooks.json](examples/claude-hooks.json) into `~/.claude/settings.json`. Replace `/absolute/path/to/codex-session-messenger` with the full path to your checkout. If you use `CLAUDE_CONFIG_DIR`, edit the `settings.json` in that directory instead.

The example registers `scripts/claude_hook.py` on `SessionStart` and `Stop`. Preserve existing settings and append the messenger handlers to any existing arrays for those events. Use an absolute Python executable in each command if `python3` is unavailable on Claude Code's `PATH`.

Keep `asyncRewake: true` and the `604800`-second timeout. The watcher waits for messages for up to seven days and exits when its Claude process ends. A short timeout can leave an idle session without a watcher. Restart Claude Code after setup and check `/mcp` and `/hooks`.

Without these hooks, Claude can still read its inbox when you ask it to call `ReadNotifications`.

## Use

Open Codex and Claude Code on the same machine, then ask either client:

> Use Codex Session Messenger to find the other agent working in this project and send it: "Please check the API response field names and reply with your findings."

The agent should call `ListAgents` to find the recipient's full UUID. Codex results appear in `agents`; running Claude Code results appear in `claude_sessions` on the first page. `search` filters session titles, and `cwd` filters by an exact project path.

Each sender obtains its own UUID from its shell:

```sh
# In Codex
printenv CODEX_THREAD_ID

# In Claude Code
printenv CLAUDE_CODE_SESSION_ID
```

Use that UUID as `from_thread` or `for_thread`. The MCP server process's identity may belong to a different conversation.

Example `SendMessage` arguments:

```json
{
  "to": "RECIPIENT-SESSION-UUID",
  "from_thread": "YOUR-SESSION-UUID",
  "message": "The API change is ready. Please check the response field names."
}
```

Example `ReadNotifications` arguments:

```json
{
  "for_thread": "YOUR-SESSION-UUID"
}
```

To reply, swap the sender and recipient UUIDs in another `SendMessage` call. Use `delivery: "inbox"` to store a message without automatic delivery. Use `mark_read: false` to preview an inbox, or `unread_only: false` to include messages already read by a hook.

## Delivery

The messenger saves an inbox copy before requesting delivery.

| Receipt status | Meaning |
| --- | --- |
| `inbox_only` | Saved for `ReadNotifications`; automatic delivery was not requested. |
| `queued` | Codex accepted the queue submission; recipient processing is not confirmed. |
| `queue_failed` | Native Codex delivery failed. The inbox copy remains, and `detail` contains the error. |
| `queue_unknown` | The native queue call timed out. Acceptance is unknown. |
| `pending` | Saved, but queue completion has not been recorded yet. |
| `claude_hook` | Saved for a Claude Code recipient's watcher. Recipient processing is not confirmed. |

**Codex:** the default `queue` delivery invokes `codex queue`. Trusted hooks check the native queue at `PostToolUse`, `SessionStart`, `UserPromptSubmit`, and `Stop`. They verify messages against the mailbox, remove matching native submissions, and append their text to the conversation. Other queue entries and inbox-only messages are left alone. Without trusted hooks, native queue delivery waits for a later response boundary.

**Claude Code:** one watcher runs per session. It polls for unread `queue` messages, marks the messages it takes as read, and exits with code 2. Claude Code's `asyncRewake` hook then shows the message and can start a turn when the session is idle. Long messages are shortened to fit the hook output limit, with instructions for reading the full mailbox copy.

A receipt is not an acknowledgment from the recipient. Replies require another `SendMessage` call. The messenger does not run automatic reply loops.

For retry protection, supply a UUID `message_id` and reuse it with the same fields. An existing ID returns its original receipt without repeating delivery. Changing the fields for an existing ID fails. Do not blindly resend a `queue_unknown` message under a new ID.

## Local data and limits

- Messages stay in `~/.codex/session-messenger/messages.sqlite3`, or under the configured `CODEX_HOME`. Both clients must use the same mailbox location.
- Claude session discovery reads `~/.claude/sessions`, or the directory under `CLAUDE_CONFIG_DIR`. Only running Claude Code sessions are valid addresses.
- Codex discovery reads session metadata through `codex app-server`; it does not read conversation bodies. Codex activity is reported as `unknown` because the catalog connection cannot reliably identify active sessions in another process.
- The data directory is created with mode `0700`, and the database with mode `0600`. Processes running as the same OS user can access it. Sender UUIDs are supplied explicitly and are not authenticated against those processes.
- Messages are labeled as peer context and do not grant new user authorization.
- Cloud-only sessions, other machines, and other OS users are unsupported. Uninstalling leaves the local mailbox in place.

| Environment variable | Purpose |
| --- | --- |
| `CODEX_HOME` | Codex configuration and mailbox home; defaults to `~/.codex`. |
| `CLAUDE_CONFIG_DIR` | Claude Code configuration and session registry; defaults to `~/.claude`. |
| `CODEX_MESSENGER_DATA` | Overrides the mailbox directory. Set it consistently for both servers and both clients' hooks. |
| `CODEX_MESSENGER_CODEX` | Overrides the Codex CLI executable. Useful when `codex` is missing from a client's `PATH`. |

## Troubleshooting

- **Tools do not appear:** open a new conversation and check the client's MCP configuration. For Claude Code, run `claude mcp get codex_messenger` or open `/mcp`.
- **`uv`, `python3`, or `codex` is missing:** make the executable available on the client's `PATH`, or configure an absolute path. Desktop applications may inherit a different environment from your terminal.
- **Claude session is missing:** keep it running, check the session's name and project path, and make sure both clients agree on `CLAUDE_CONFIG_DIR`.
- **Message is saved but not shown:** check hook registration and Codex hook trust. Ask the recipient to read its inbox explicitly. Use `unread_only: false` if a hook has already marked it read.
- **`queue_failed` or `queue_unknown`:** inspect `detail` and the inbox before retrying. Upgrade Codex if its queue or app-server APIs are unavailable.

## Development

```sh
uv run --with mcp==1.30.0 python -m unittest discover -s tests -v
```

The 36 tests use disposable mailboxes, fixture Codex executables, and hook subprocesses. The protocol test exchanges messages between two independent stdio MCP servers. They do not message real client sessions. GitHub Actions runs the suite on Python 3.10 and 3.12.

Client references: [Codex commands](https://learn.chatgpt.com/docs/developer-commands#codex-mcp), [Codex hooks](https://learn.chatgpt.com/docs/hooks), [Codex plugin packaging](https://developers.openai.com/plugins/build/plugins), [Claude Code MCP](https://code.claude.com/docs/en/mcp), and [Claude Code hooks](https://code.claude.com/docs/en/hooks).

## License

[MIT](LICENSE). This is an independent community project.

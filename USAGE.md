# Usage

This page covers daily use. For installation see [README.md](README.md).

## First message

The bot ignores everyone who is not in `ALLOWED_USER_IDS`. If that list is empty, only `/whoami` answers, so you can look up your own id.

Send `/unlock 123456` with the current code from your authenticator app. After `max_failures` wrong codes the bot locks you out for `lockout_minutes`. The unlock lasts `session_hours`, and it ends earlier after `idle_lock_hours` without a message.

Each bot keeps one pinned message in your private chat that shows whether the bridge is locked or unlocked, and until when. It is edited in place whenever the state changes, so you can check it without sending anything. Set `status_pin = false` in `[general]` to turn it off.

## Commands

| Command | What it does |
| --- | --- |
| `/unlock <code>` | Unlock with a 2FA code |
| `/lock` | End the unlocked session now |
| `/exit`, `/new` | Clear the context and start a new session |
| `/stop` | Kill the running task. `/stop all` also clears the queue |
| `/status` | Lock state, running task, session |
| `/cwd [path]` | Show or set the working folder (only inside `allowed_cwd_roots`) |
| `/modelstart`, `/modelstop` | Start or stop the local model (only for bots with `needs_local_model`) |
| `/help` | List the commands |

Anything else is a task for the agent.

## Working with the agent

- While a task runs, one status message is edited with the elapsed time, the current tool and subagents. Messages sent meanwhile wait in a queue.
- The reply ends with a context line such as `42% context`. The Claude bot also shows the cost of the run.
- Replies longer than `document_threshold_chars` are sent as a file. Other messages are split at Telegram's limit of 4096 characters.
- Send a photo, document or voice message and the agent gets the file path. Voice is transcribed first when the `voice` extra is installed. Downloads older than `inbox_retention_days` are deleted.
- The session resumes after a restart. The bridge tells you when it is back online.

## Getting files back

Ask the agent to send a file. It should write the file to `<home>/outbox` (or a folder from `outbox_dirs`) and put `[tg-file] <absolute path>` on its own line in the final reply. The bridge uploads it, up to 10 files of 50 MB each. A refused file is named in the chat with the reason: outside the allowed folders, blocked file or folder, not found, or too large.

## Chat mode for a local model

A bot with `needs_local_model = true` can run in three modes (`mode` in its config):

- `agent`: every message goes to the agent.
- `chat`: every message is one direct call to the model. Fast, but no tools and no files.
- `auto`: conversation goes to chat, and messages that ask for an action (files, commands, code, web and so on) go to the agent. A follow-up like "do it" stays with the agent when the previous turn was handled there.

Start a message with `!` to force the agent, or with `?` to force chat. Messages with a non-image attachment always go to the agent.

If memory is enabled for the bot (`[bots.<name>.memory]`), the notes in `dir` (`*.md`, `README.md` first, limited in size) are part of the chat system prompt. A message that looks like a save request or states a lasting fact ("remember that...", "my name is...") is answered in chat and then saved by an agent run that follows your `save_prompt`. The reply says so with `notice_text`. With memory off there is no notes summary, no save detection and no save run.

## Sending messages from scripts

```
telegram-bridge notify "Build finished"
telegram-bridge notify "See file" --file ~/.telegram-bridge/outbox/log.txt --silent
some-command | telegram-bridge notify --bot claude
```

`notify` needs no running bridge. It sends to every id in `ALLOWED_USER_IDS`, masks secrets, and applies the same file rules as the agent. Exit codes: 0 ok, 2 usage or config, 4 Telegram or network, 5 a file was not delivered. On failure it prints one line without the token.

## Configuration

`config.toml` in the home folder holds everything except secrets. Placeholders that work in strings: `{home}`, `{code_dir}`, `{cwd}`. The example file (`src/telegram_bridge/config.example.toml`) lists every option with a comment.

Per bot you can also set `agent_triggers` and `save_triggers` (lists of regular expressions, matched against lowercase text without accents) to replace the built-in Czech and English word lists.

## Troubleshooting

Run `telegram-bridge doctor` first. Logs are in `<home>/logs/bridge.log`, and secrets are masked there too. If the bridge says it is already running, a live process holds the lock. A lock file left behind by a crash does not block a start.

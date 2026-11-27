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
| `/back` | Return to the session that an idle reset replaced (after 60 min idle, a session over 30k tokens starts fresh because the prompt cache expired; `idle_new_session_minutes` / `idle_new_session_tokens` per bot, 0 = off) |
| `/stop` | Kill the running task. `/stop all` also clears the queue |
| `/resume`, `/drop` | Continue or forget the task that a restart interrupted (see below) |
| `/status` | Lock state, running task, session |
| `/cwd [path]` | Show or set the working folder (only inside `allowed_cwd_roots`) |
| `/voice [on\|off]` | Voice replies for this chat (only for bots with a `[bots.<name>.voice]` table) |
| `/modelstart`, `/modelstop` | Start or stop the local model (only for bots with `needs_local_model`) |
| `/help` | List the commands |

Anything else is a task for the agent.

## Working with the agent

- While a task runs, one status message is edited with the elapsed time, the current tool and subagents. Messages sent meanwhile wait in a queue.
- The reply ends with a context line such as `42% context`.
- If the agent leaves work running in the background, its answer comes right away and the result follows as another message. New messages wait until the background work ends.
- If Telegram cannot be reached, the answer is saved (with secrets masked, in `inflight.json`) and sent again every 30 seconds, also after a restart, for up to 7 days. After a timeout Telegram may have received it already, so the bridge first looks for it: it forwards the few messages that could be it to the same chat, silently, compares them and deletes the copies at once.
- Editing a message you already sent does not run it. Send a new one.
- Replies longer than `document_threshold_chars` are sent as a file. Other messages are split at Telegram's limit of 4096 characters.
- Send a photo, document or voice message and the agent gets the file path. Voice is transcribed first when the `voice` extra is installed. Downloads older than `inbox_retention_days` are deleted.
- Videos, round video notes and GIFs work too, up to the 20 MB limit of Telegram bots. With ffmpeg installed, the bridge takes `video_frames` still frames (doubled for clips over 60 seconds, at most 8) and transcribes the audio when `video_transcribe` is on. The agent gets the video path, the transcript and the frame paths. Without ffmpeg only the file is passed on.
- The session resumes after a restart. The bridge tells you when it is back online.
- If a restart cuts a task off, or messages were still waiting in the queue, the bridge tells you once it is back and shows what was interrupted. It never continues on its own. Send `/resume` to run the task again in the same session (the agent is told to check what was already done first), followed by the waiting messages, or `/drop` to forget them. Unanswered offers expire after 7 days.

## Getting files back

Ask the agent to send a file. It should write the file to `<home>/outbox` (or a folder from `outbox_dirs`) and put `[tg-file] <absolute path>` on its own line in the final reply. The bridge uploads it, up to 10 files of 50 MB each. A refused file is named in the chat with the reason: outside the allowed folders, blocked file or folder, not found, or too large.

## Chat mode for a local model

A bot with `needs_local_model = true` can run in three modes (`mode` in its config):

- `agent`: every message goes to the agent.
- `chat`: every message is one direct call to the model. Fast, but no tools and no files.
- `auto`: conversation goes to chat, and messages that ask for an action (files, commands, code, web and so on) go to the agent. A follow-up like "do it" stays with the agent when the previous turn was handled there.

Start a message with `!` to force the agent, or with `?` to force chat. Messages with a non-image attachment always go to the agent.

If memory is enabled for the bot (`[bots.<name>.memory]`), the notes in `dir` (`*.md`, `README.md` first, limited in size) are part of the chat system prompt. A message that looks like a save request or states a lasting fact ("remember that...", "my name is...") is answered in chat and then saved by an agent run that follows your `save_prompt`. The reply says so with `notice_text`. With memory off there is no notes summary, no save detection and no save run.

## Voice replies

The agent can answer with a Telegram voice message first and the full text after it. By default replies are text only, even when you send a voice message.

Setup:

1. `uv sync --extra tts` and install ffmpeg (it must be on PATH).
2. Add a table per bot in `config.toml`:

```toml
[bots.claude.voice]
voices = { cs = "cs-CZ-AntoninNeural", sk = "sk-SK-LukasNeural", en = "en-US-AndrewNeural" }
default_lang = "en"
```

`voices` maps a language code (any codes you like) to an edge-tts voice. `default_lang` is used when the agent gives no language or one that is not in the table. `edge-tts --list-voices` lists all voices. Without the table voice replies are off for that bot and `/voice` says so. `telegram-bridge doctor` shows whether edge-tts and ffmpeg are found and which voices are set.

Using it:

- `/voice on` turns voice mode on for this chat and this bot, `/voice off` turns it off, `/voice` shows the state. Voice mode is kept in memory only and turns off whenever the session locks (`/lock`, idle lock, expiry) or the bridge restarts.
- For a single spoken answer, just ask for it in your message ("answer by voice", "odpověz hlasem"). The agent decides itself; the bridge does not look for keywords.
- The agent ends its reply with a block `[tg-voice cs]`, a short version for listening, and `[/tg-voice]`. The bridge removes the block from the text, speaks it with the voice for that language and sends the voice message before the text. The spoken text is masked like all other output and cut at about 1500 characters. In voice mode, a reply without a block is spoken from a cleaned copy of its text (no code or markdown).
- It works for the local-model bot in fast chat mode too.
- If speaking fails (edge-tts or ffmpeg missing, no internet), the text still arrives, followed by one line such as `🔇 voice reply failed: ffmpeg is not installed`.

Privacy: the spoken text is sent to Microsoft's online text-to-speech service. Do not turn voice replies on if that is not acceptable for what you discuss.

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

## Requirements in detail

### Voice messages

Install the `voice` extra (faster-whisper) and ffmpeg. The default model is `large-v3-turbo`. It needs about 1.6 GB of VRAM on an NVIDIA GPU (CUDA, `cuda` extra on Windows and Linux), or it runs on the CPU, more slowly. The model is about 1.6 GB and downloads on first use.

### Local model (second bot)

Any OpenAI-compatible local server can work. The example config is for Qwen served by Strata.

How much memory you need depends on the model and quantisation. RAM and VRAM add up, so a model can be split between the GPU and system memory. Rough figures for Qwen:

| Model and quantisation | RAM + VRAM |
|---|---|
| Qwen coder version | about 32 GB |
| Qwen3.8-Flash-Next, Q2_0 | about 38 GB |
| Qwen3.8-Flash-Next, IQ3_S, 128K context | about 55 GB |

Agent mode (the bot doing things on the PC) needs a model with tool calling. Photos need a model with image support. The first start loads the model and can take a few minutes.

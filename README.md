# AI-TelegramBridge

telegram-bridge lets you drive [Claude Code](https://claude.com/claude-code) on your own computer from Telegram. Optionally it can also drive a local model through a second bot. You write to a bot, the bridge runs the agent on your PC and sends the answer back.

You can send text, photos, files and voice messages. You get back answers, files (the agent marks them with `[tg-file]` lines), live progress while a task runs, and a pinned message that shows whether the bridge is locked or unlocked.

It uses long polling. There is nothing to host, no port to open and no public IP needed.

## Warning: full access

This bot can run any command on this computer. The whitelist, 2FA and the safety rules in the prompt are what protect it.

The example config starts the agent with `--permission-mode bypassPermissions`, which means it never asks before running a command. Read [SECURITY.md](SECURITY.md) before you put a token in `.env`.

## Features

- One process, one or two bots (for example a Claude bot and a local-model bot).
- Whitelist of Telegram user ids plus TOTP 2FA (`/unlock 123456`), a session limit and an idle lock.
- Progress message while a task runs: current tool, steps, subagents, elapsed time.
- Secret masking on everything that leaves the bridge (messages, logs, `notify`).
- Files and photos in, files out. Voice messages are transcribed with Whisper (optional).
- Context meter, session resume, working folder per chat (`/cwd`), task queue, `/stop`.
- Optional fast chat mode for a local model, with an optional memory folder.
- `notify` command to send a message or file from any script.
- `doctor` command that checks the installation without changing anything.
- Autostart on Windows (Task Scheduler), Linux (systemd user unit) and macOS (launchd).

## Requirements

### Required

- Windows 10/11, Linux or macOS. Windows is used every day. Linux and macOS are tested in CI.
- Python 3.12 and [uv](https://docs.astral.sh/uv/).
- Claude Code CLI, installed and logged in. You need a Claude subscription (Pro or Max, for example) or an Anthropic API key.
- A Telegram account and a bot token from [@BotFather](https://t.me/BotFather).
- An authenticator app (Google Authenticator, Microsoft Authenticator or similar) for TOTP 2FA.
- Internet access.

The bridge itself is light. Any machine that runs Claude Code is enough. Plan for about 200 MB of RAM for the bridge, plus disk space for the virtual environment.

### Optional: voice messages

Install the `voice` extra (faster-whisper) and ffmpeg. The default model is `large-v3-turbo`. It needs about 1.6 GB of VRAM on an NVIDIA GPU (CUDA, `cuda` extra on Windows and Linux), or it runs on the CPU, more slowly. The model is about 1.6 GB and downloads on first use.

### Optional: local model (second bot)

Any OpenAI-compatible local server can work. The example config is for Qwen served by Strata.

How much memory you need depends on the model and quantisation you pick in Strata. RAM and VRAM add up, so a model can be split between the GPU and system memory. Rough figures for Qwen:

| Model and quantisation | RAM + VRAM |
|---|---|
| Qwen coder version | about 32 GB |
| Qwen3.8-Flash-Next, Q2_0 | about 38 GB |
| Qwen3.8-Flash-Next, IQ3_S, 128K context | about 55 GB |

Whatever model you choose needs tool calling for agent mode (the bot doing things on the PC). Sending photos to the local bot needs a model with image support. The first start loads the model and can take a few minutes.

## Install

### Windows (PowerShell)

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
git clone https://github.com/jan-osicka/AI-TelegramBridge
cd AI-TelegramBridge
uv sync --extra voice --extra qr
```

On an NVIDIA GPU add `--extra cuda` to the last command. For voice, also install ffmpeg (for example `winget install ffmpeg`).

### Linux and macOS

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/jan-osicka/AI-TelegramBridge
cd AI-TelegramBridge
uv sync --extra voice --extra qr
```

On Linux with an NVIDIA GPU add `--extra cuda`. For voice, install ffmpeg with your package manager (`apt install ffmpeg`, `brew install ffmpeg`). Without voice, use `uv sync --extra qr`.

Then install Claude Code by following [its setup guide](https://docs.claude.com/en/docs/claude-code/setup), start `claude` once and log in.

## Setup

1. Open Telegram, talk to [@BotFather](https://t.me/BotFather), send `/newbot` and copy the token.
2. Run `uv run telegram-bridge setup`. It creates the home folder `~/.telegram-bridge` (or the folder in `TELEGRAM_BRIDGE_HOME`) with `inbox`, `outbox` and `logs`, a `.env` template, a TOTP secret, a QR code (`totp-qr.png`) and a copy of the example config as `config.toml`. It also restricts the folder to your user. Add `--whisper` to download the voice model now.
3. Scan `totp-qr.png` with your authenticator app, then delete the file. It contains the secret.
4. Put the bot token into `.env` as `CLAUDE_BOT_TOKEN=...`.
5. Find your numeric Telegram id. While `ALLOWED_USER_IDS` is empty, the bot answers `/whoami`. Start the bridge once (`uv run telegram-bridge run`), send `/whoami` to your bot, then stop it with Ctrl+C.
6. Put the id into `.env` as `ALLOWED_USER_IDS=123456789` (comma separated for more people).
7. Edit `config.toml` in the home folder. The settings to check are `cwd` (where the agent works), `allowed_cwd_roots` (the folders `/cwd` may point to) and `outbox_dirs` (extra folders the agent may send files from). Secrets never go into `config.toml`.
8. Check everything: `uv run telegram-bridge doctor --online`. Fix every FAIL.
9. Start the bridge: `uv run telegram-bridge run`.
10. In Telegram send `/unlock 123456`, with the current code from your authenticator app. Then write a task.

Only one instance can run at a time. A second one exits with a message.

## Run in the background

```
uv run telegram-bridge service print      # show what would be installed
uv run telegram-bridge service install
uv run telegram-bridge service remove
```

- Windows: registers a Task Scheduler task named `TelegramBridge` that starts at logon and restarts on failure. Start it now with `Start-ScheduledTask -TaskName TelegramBridge`.
- Linux: writes `~/.config/systemd/user/telegram-bridge.service` and runs `systemctl --user enable --now telegram-bridge.service`.
- macOS: writes `~/Library/LaunchAgents/com.telegram-bridge.plist` and loads it with `launchctl`.

All three start `telegram-bridge run --service` from the project virtual environment.

## Using it

Commands (the two model commands appear only for a bot with `needs_local_model`):

| Command | What it does |
| --- | --- |
| `/unlock <code>` | Unlock with a 2FA code |
| `/lock` | End the unlocked session |
| `/exit`, `/new` | Clear the context and start a new session |
| `/stop` | Kill the running task. `/stop all` also clears the queue |
| `/status` | Lock state, running task, session |
| `/cwd [path]` | Show or set the working folder |
| `/modelstart` | Start the local model server |
| `/modelstop` | Stop the local model server |
| `/help` | List the commands |

Anything else is a task for the agent. More detail, including chat mode for a local model, is in [USAGE.md](USAGE.md).

**Sending files back.** The agent puts a line like this in its final reply:

```
[tg-file] /home/me/.telegram-bridge/outbox/report.pdf
[tg-file] "/home/me/.telegram-bridge/outbox/my report.pdf" | caption text
```

The bridge removes the line and uploads the file, at most 10 files and 50 MB each. A file is sent only from `<home>/outbox` or a folder listed in `outbox_dirs`. A refused file is reported in the chat with the reason. Set `outgoing_files = false` in `[general]` to turn this off.

**Notify from scripts.** No running bridge is needed.

```
uv run telegram-bridge notify "Backup finished"
uv run telegram-bridge notify "Report attached" --file /path/to/outbox/report.pdf
echo "long text" | uv run telegram-bridge notify --silent
```

Options: `--bot NAME`, `--file PATH` (repeatable), `--silent`. Exit codes: 0 ok, 2 usage or config error, 4 Telegram or network failure, 5 a file was refused or failed to upload (the text is still sent).

**Pinned status message.** Each bot keeps one pinned message in your private chat. It shows whether the bridge is locked or unlocked, and until when. Set `status_pin = false` in `[general]` to turn it off.

## Security model

Only whitelisted ids get an answer. 2FA is needed to use the agent. The session expires. Secrets are masked on the way out. The agent can send only files from the outbox. Details and limits are in [SECURITY.md](SECURITY.md).

## Troubleshooting

- Run `uv run telegram-bridge doctor` (add `--online` to call Telegram and the local model). It prints OK, WARN or FAIL, is read-only, and its output has no secrets.
- Logs are in `<home>/logs/bridge.log`, which is `~/.telegram-bridge/logs/bridge.log` by default.
- The log line "409 Conflict" means another program is polling the same bot token. Stop the other copy or use a different token.
- Voice does not work: doctor shows whether the `voice` extra, the Whisper model and ffmpeg are present. Install what is missing (`uv sync --extra voice`, ffmpeg) and run `uv run telegram-bridge setup --whisper`.
- "Already running": a live process holds the lock. A lock file left by a crash does not block a start.

## Updating and uninstalling

Update:

```
git pull
uv sync --extra voice --extra qr
```

Use the same extras as before. Then restart the service (on Windows: `Stop-ScheduledTask -TaskName TelegramBridge; Start-ScheduledTask -TaskName TelegramBridge`).

Uninstall: run `uv run telegram-bridge service remove`, then delete the project folder and `~/.telegram-bridge`. Revoke the bot token in @BotFather if you no longer need it.

## Tests

```
uv sync --extra qr
uv run pytest -q
```

## Credits

The secret-detection patterns and several other ideas and patterns come from [Agent2Telegram](https://github.com/petrludwig-collab/Agent2Telegram), MIT licence. See `NOTICE`.

## License

MIT. See `LICENSE`.

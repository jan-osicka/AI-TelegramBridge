# Changelog

## 0.3.0 (2026-11-15)

- Video, round video notes and GIFs. ffmpeg takes still frames and the audio is transcribed, so the agent can "watch" a clip. Clear reply for files over Telegram's 20 MB limit.
- Resume after a restart. A running task and the queued messages are saved to a journal. After a restart the bridge says what was cut off and waits for `/resume` or `/drop`. It never continues on its own.
- Idle reset. A large Claude session that sat idle for over an hour starts fresh on the next message, because the prompt cache has expired and resuming would re-read the whole context at full price. `/back` returns to the old session, also after a restart.
- Pinned lock status message per bot.

## 0.2.0 (2026-11-13)

First public release. One package with one CLI (`run`, `notify`, `doctor`, `setup`, `service`), personal config moved out of the repo, `[tg-file]` outgoing files with an outbox allow-list, secret redaction, single-instance lock, live subagent progress, autostart on Windows, Linux and macOS, CI on all three.

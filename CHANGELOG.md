# Changelog

## 0.4.0 (2026-11-18)

- Seed phrases. Redaction now masks 12 or more words in a row from the English BIP39 wordlist, so a crypto wallet seed does not reach Telegram. Words joined by `-`, `_` or `.` and runs with many repeats don't count, which keeps code and UI text out.
- Voice replies. The agent can answer with a Telegram voice message in addition to the text, for listening while driving. Text goes through edge-tts (Microsoft neural voices) and ffmpeg. Per-bot `[bots.<name>.voice]` table, `/voice on|off`, optional `tts` extra. Off unless configured. The spoken text is sent to Microsoft's TTS service.
- `/voice` appears in the Telegram command menu only for bots that have voice replies configured.
- The reply footer shows only the context percentage. The run cost is gone, because on a subscription it means nothing.

## 0.3.0 (2026-11-15)

- Video, round video notes and GIFs. ffmpeg takes still frames and the audio is transcribed, so the agent can "watch" a clip. Clear reply for files over Telegram's 20 MB limit.
- Resume after a restart. A running task and the queued messages are saved to a journal. After a restart the bridge says what was cut off and waits for `/resume` or `/drop`. It never continues on its own.
- Idle reset. A large Claude session that sat idle for over an hour starts fresh on the next message, because the prompt cache has expired and resuming would re-read the whole context at full price. `/back` returns to the old session, also after a restart.
- Pinned lock status message per bot.

## 0.2.0 (2026-11-13)

First public release. One package with one CLI (`run`, `notify`, `doctor`, `setup`, `service`), personal config moved out of the repo, `[tg-file]` outgoing files with an outbox allow-list, secret redaction, single-instance lock, live subagent progress, autostart on Windows, Linux and macOS, CI on all three.

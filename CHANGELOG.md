# Changelog

## 0.5.0 (2026-11-27)

- Background jobs. When a run starts background work, its answer is delivered right away and the result of the job follows as a second message. Before, only the last one arrived.
- Answers are saved until Telegram accepts them. A network failure no longer loses a finished answer: it is sent again every 30 seconds, also after a restart, for up to 7 days. Before sending again the bridge checks whether the message did arrive, so a timeout does not cause a duplicate.
- Stickers, locations, contacts and other unsupported messages get a short reply. A document over 20 MB is no longer called a video.
- Editing a sent message does not run it again.
- If a run dies without a result, text of a subagent is never shown as the answer.
- The startup notice no longer says the context is kept when the saved session is big and idle. It says the next message starts a new session, and `/back` returns to the old one.

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

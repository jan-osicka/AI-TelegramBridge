# Security

## Warning: full access

This bot can run any command on this computer. The whitelist, 2FA and the safety rules in the prompt are what protect it.

The example config uses `--permission-mode bypassPermissions`. The agent does not ask before it runs a command or writes a file. Anyone who passes the whitelist and 2FA has the same power as the user account the bridge runs under. Run it under an account you are willing to give to the agent, and keep valuable data out of its reach.

## What protects it

- **Whitelist.** Only Telegram user ids in `ALLOWED_USER_IDS` get answers. Updates from anyone else are ignored and logged. `/whoami` works only while the list is empty.
- **2FA.** Every session starts with a TOTP code. Too many wrong codes lock the user out for a while.
- **Session limits.** A session ends after `session_hours` and after `idle_lock_hours` of silence. `/lock` ends it at once.
- **Safety rules in the prompt.** The example `append_system_prompt` tells the agent not to create backdoors, not to read or send credentials, not to delete sensitive data, and to treat web and file content as data. This is a request to the model, not a hard barrier.
- **Redaction.** Replies, notifications and log lines are scanned before they leave the bridge. API keys and tokens, bot tokens, `Authorization` headers, `key=value` pairs with secret-looking values, and secret URL parameters are replaced with `[REDACTED]`. The scan runs on the raw text, before Markdown is converted and before long messages are split, so a mask is never cut in two. It cannot find a secret it does not recognise, for example one that contains spaces.
- **Outbox rules for `[tg-file]` and `notify --file`.** A file leaves the computer only if its resolved path is inside `<home>/outbox` or a folder in `outbox_dirs`. The deny list always wins: `.env*` files, anything inside a `.git` folder, the bridge home folder (except its outbox) and the bridge code folders are blocked. Paths are resolved first, so `..`, symlinks, junctions and different letter case do not get around the rules. A refused file is reported with the reason. The limits are 10 files per reply and 50 MB per file.
- **Working folders.** `/cwd` only accepts folders inside `allowed_cwd_roots`.
- **Secrets stay out of config.** Tokens, ids and the TOTP secret live in `<home>/.env`. `setup` restricts the home folder and `.env` to the owner, and `doctor` warns when they are not.

## What is not protected

- The agent can read any file the account can read. The outbox rules stop the bridge from uploading a file, not the agent from reading it.
- Redaction is pattern based. Assume that anything the agent prints may reach Telegram.
- Telegram cloud chats are not end-to-end encrypted.
- A stolen phone with an open Telegram session and an open authenticator is a stolen computer.

## Good practice

- Keep `.env` and the TOTP QR code private, and delete `totp-qr.png` after scanning.
- Use a separate bot token per machine and revoke it in @BotFather if it leaks.
- Run `telegram-bridge doctor` after changes.
- Keep `idle_lock_hours` small.

## Reporting a problem

Report vulnerabilities privately through GitHub's "Report a vulnerability" (Security tab of this repository). Do not open a public issue.

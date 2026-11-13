"""Manual e2e: run the cloud claude CLI via Runner, then resume the session."""
import asyncio
import sys
from pathlib import Path

from telegram_bridge import settings
from telegram_bridge.runner import Runner


async def main():
    cfg = settings.load_config()
    r = Runner(cfg["bots"][sys.argv[1] if len(sys.argv)>1 else "claude"], cfg["general"]["cwd"])
    events = []

    async def on_ev(e):
        events.append(e["kind"])

    out, sid, err = await r.run("Reply with exactly: OK", None, on_ev)
    print("1:", repr(out), sid, err, events)
    out2, sid2, err2 = await r.run("What did I ask you to reply with? One word.", sid, on_ev)
    print("2:", repr(out2), sid2, err2)


asyncio.run(main())


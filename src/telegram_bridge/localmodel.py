"""On-demand lifecycle of the local model server: start, wait until ready, idle auto-stop."""
import asyncio
import json
import logging
import os
import time
import urllib.request

from . import osutil

log = logging.getLogger("bridge.localmodel")


class LocalModelError(Exception):
    pass


def http_check(url):
    """True when GET <url>/v1/models returns JSON that lists at least one model."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/v1/models", timeout=3) as r:
            if r.status != 200:
                return False
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return False
    items = data.get("data") if isinstance(data, dict) else None
    return bool(items)


launch_detached = osutil.launch_detached


def run_stop(cmd):
    osutil.run_hidden(cmd, 120)


class LocalModelManager:
    def __init__(self, url, start_cmd, stop_cmd, startup_timeout_s=360, idle_stop_minutes=20,
                 poll_s=3.0, check=http_check, launcher=launch_detached, stopper=run_stop,
                 clock=time.monotonic, log_path=None, busy_file=None, busy_fresh_s=180, wall=time.time):
        self.url, self.start_cmd, self.stop_cmd = url, start_cmd, stop_cmd
        self.timeout_s = float(startup_timeout_s)
        self.idle_s = float(idle_stop_minutes) * 60
        self.poll_s = poll_s
        self.check, self.launcher, self.stopper, self.clock = check, launcher, stopper, clock
        self.log_path = log_path
        # Another local client of the same server may keep this file fresh while it uses the server.
        self.busy_file, self.busy_fresh_s, self.wall = busy_file, float(busy_fresh_s), wall
        self.busy = 0
        self.last_activity = clock()
        self.launches = 0
        self._starting = None  # shared future-like task while a startup is in progress

    async def is_up(self):
        return await asyncio.to_thread(self.check, self.url)

    @property
    def starting(self):
        return self._starting is not None and not self._starting.done()

    def touch(self):
        self.last_activity = self.clock()

    async def ensure(self):
        """Make sure the server is serving. Returns True if this call had to start it.
        Concurrent callers share one startup. Raises LocalModelError on failure/timeout."""
        if self.starting:
            await asyncio.shield(self._starting)
            return False
        if await self.is_up():
            self.touch()
            return False
        if self.starting:  # another caller began while we were checking
            await asyncio.shield(self._starting)
            return False
        self._starting = asyncio.ensure_future(self._start())
        await asyncio.shield(self._starting)
        return True

    async def _start(self):
        log.info("starting local model server: %s", self.start_cmd)
        self.launches += 1
        try:
            await asyncio.to_thread(self.launcher, self.start_cmd, self.log_path)
        except Exception as e:
            raise LocalModelError(f"could not launch the local model server: {e}") from e
        t0 = self.clock()
        while True:
            if await self.is_up():
                log.info("Local model ready after %.0f s", self.clock() - t0)
                self.touch()
                return
            if self.clock() - t0 >= self.timeout_s:
                raise LocalModelError(f"Local model did not become ready within {self.timeout_s / 60:.0f} min "
                                f"(no model at {self.url}/v1/models)")
            await asyncio.sleep(self.poll_s)

    async def stop(self):
        log.info("stopping local model server")
        try:
            await asyncio.to_thread(self.stopper, self.stop_cmd)
        except Exception as e:
            raise LocalModelError(f"stop command failed: {e}") from e
        self.touch()

    def external_busy(self):
        """True while busy_file was touched within busy_fresh_s (another local client is using the server)."""
        if not self.busy_file:
            return False
        try:
            return self.wall() - os.path.getmtime(self.busy_file) < self.busy_fresh_s
        except OSError:
            return False

    async def maybe_idle_stop(self):
        """Stop the server when idle too long. Returns True if it was stopped."""
        if self.idle_s <= 0 or self.busy or self.starting:
            return False
        if self.clock() - self.last_activity < self.idle_s:
            return False
        if self.external_busy():
            self.touch()
            return False
        if not await self.is_up():
            return False
        log.info("Local model idle for %.0f min, stopping", (self.clock() - self.last_activity) / 60)
        await self.stop()
        return True

    async def adopt_running(self):
        """If Strata is already serving (e.g. orphaned by a bridge restart), treat it as ours
        and start the idle timer so it gets auto-stopped. Returns True if adopted."""
        if await self.is_up():
            log.info("Local model server already running, adopting (idle stop after %.0f min)", self.idle_s / 60)
            self.touch()
            return True
        return False

    async def idle_loop(self, interval=30):
        try:
            await self.adopt_running()
        except Exception:
            log.exception("adopt failed")
        while True:
            await asyncio.sleep(interval)
            try:
                await self.maybe_idle_stop()
            except Exception:
                log.exception("idle stop failed")

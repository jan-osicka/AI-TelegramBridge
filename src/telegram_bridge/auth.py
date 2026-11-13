"""Whitelist + TOTP session logic. No Telegram imports, fully testable."""
import hmac
import json
import os
import time
from pathlib import Path

import pyotp

TOTP_STEP = 30


class Auth:
    def __init__(self, allowed_ids, totp_secret, session_hours=12.0,
                 max_failures=5, lockout_minutes=15.0, clock=time.time, state_path=None, idle_lock_hours=0.0):
        self.allowed = set(int(i) for i in allowed_ids)
        self.secret = totp_secret
        self.session_s = session_hours * 3600
        self.max_failures = max_failures
        self.lockout_s = lockout_minutes * 60
        self.idle_s = float(idle_lock_hours or 0) * 3600   # 0 = no idle lock
        self.clock = clock
        self._expiry = {}        # uid -> unlock expiry
        self._activity = {}      # uid -> time of last accepted message/command
        self._saved_activity = {}  # uid -> activity value last written to disk
        self._fails = {}         # uid -> count
        self._locked_until = {}  # uid -> ts
        self._last_counter = 0   # last accepted TOTP time-step (replay protection)
        self.state_path = Path(state_path) if state_path else None
        self._load()

    # ---- persistence (never stores the secret or codes)
    def _load(self):
        if not self.state_path:
            return
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            now = self.clock()
            self._expiry = {int(k): float(v) for k, v in d.get("expiry", {}).items() if float(v) > now}
            self._activity = {int(k): float(v) for k, v in d.get("activity", {}).items() if int(k) in self._expiry}
            for u in self._expiry:   # state from before idle lock existed: count from now
                self._activity.setdefault(u, now)
            self._saved_activity = dict(self._activity)
            self._fails = {int(k): int(v) for k, v in d.get("fails", {}).items()}
            self._locked_until = {int(k): float(v) for k, v in d.get("locked_until", {}).items() if float(v) > now}
            self._last_counter = int(d.get("last_counter", 0))
        except FileNotFoundError:
            pass
        except Exception:
            # corrupt state -> stay locked (fail closed)
            self._expiry, self._fails, self._locked_until, self._last_counter = {}, {}, {}, 0
            self._activity, self._saved_activity = {}, {}

    def _save(self):
        if not self.state_path:
            return
        now = self.clock()
        self._expiry = {u: t for u, t in self._expiry.items() if t > now}
        self._locked_until = {u: t for u, t in self._locked_until.items() if t > now}
        self._activity = {u: t for u, t in self._activity.items() if u in self._expiry}
        self._saved_activity = dict(self._activity)
        fails = {u: n for u, n in self._fails.items() if n}
        counter = self._last_counter if self._last_counter >= int(now // TOTP_STEP) - 2 else 0
        data = {"expiry": {str(u): t for u, t in self._expiry.items()},
                "activity": {str(u): t for u, t in self._activity.items()},
                "fails": {str(u): n for u, n in fails.items()},
                "locked_until": {str(u): t for u, t in self._locked_until.items()},
                "last_counter": counter}
        try:
            if not (self._expiry or fails or self._locked_until or counter):
                self.state_path.unlink(missing_ok=True)
                return
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def is_allowed(self, uid):
        return uid in self.allowed

    def whoami_enabled(self):
        return not self.allowed

    def is_unlocked(self, uid):
        exp = self._expiry.get(uid)
        if exp is None:
            return False
        now = self.clock()
        if now >= exp:
            del self._expiry[uid]
            self._save()
            return False
        if self.idle_s and now - self._activity.get(uid, now) >= self.idle_s:
            del self._expiry[uid]   # idle too long: lock (needs a fresh 2FA code)
            self._save()
            return False
        return True

    def touch(self, uid):
        """Record activity (any accepted message or command) from an unlocked uid."""
        if uid not in self._expiry:
            return
        now = self.clock()
        self._activity[uid] = now
        if now - self._saved_activity.get(uid, 0) >= 60:   # persist at most once a minute
            self._save()

    def expiry(self, uid):
        return self._expiry.get(uid) if self.is_unlocked(uid) else None

    def lockout_remaining(self, uid):
        return max(0.0, self._locked_until.get(uid, 0) - self.clock())

    def lock(self, uid):
        self._expiry.pop(uid, None)
        self._save()

    def unlock(self, uid, code):
        """Returns (ok, message)."""
        if not self.is_allowed(uid):
            return False, "denied"
        rem = self.lockout_remaining(uid)
        if rem > 0:
            return False, f"Too many failures. Locked out for {int(rem // 60) + 1} more min."
        if not self.secret:
            return False, "TOTP_SECRET is not configured."
        code = (code or "").strip().replace(" ", "")
        now = self.clock()
        counter = None
        if code.isdigit() and len(code) == 6:
            totp = pyotp.TOTP(self.secret)
            base = int(now // TOTP_STEP)
            for c in (base - 1, base, base + 1):
                if c > self._last_counter and hmac.compare_digest(totp.at(c * TOTP_STEP), code):
                    counter = c
                    break
        if counter is not None:
            self._last_counter = counter
            self._fails[uid] = 0
            self._expiry[uid] = now + self.session_s
            self._activity[uid] = now
            self._save()
            return True, "Unlocked."
        n = self._fails.get(uid, 0) + 1
        self._fails[uid] = n
        if n >= self.max_failures:
            self._fails[uid] = 0
            self._locked_until[uid] = now + self.lockout_s
            self._save()
            return False, f"Too many failures. Locked out for {int(self.lockout_s // 60)} min."
        self._save()
        return False, "Invalid code."

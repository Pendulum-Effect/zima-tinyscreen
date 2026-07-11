#!/usr/bin/env python3
"""
Auth tests: the optional PIN layer added in 0.9.5.

Covers the full lifecycle (disabled -> set -> login -> change ->
lock-stats toggle -> disable), the guard's interaction with the CSRF
layer, brute-force lockout, on-disk hygiene of auth.json, and -- most
importantly -- that with no PIN set, absolutely nothing changes
(auth is OFF by default and must stay invisible until opted into).

Run:  python3 tests/test_auth.py
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

_TMP = tempfile.TemporaryDirectory(prefix="tinyscreen-auth-tests-")
TMP = Path(_TMP.name)
os.environ["TINYSCREEN_STATUS_FILE"] = str(TMP / "status.json")
os.environ["TINYSCREEN_STATE_DIR"] = str(TMP / "state")
os.environ.pop("TINYSCREEN_SERIAL_PORT", None)

import server  # noqa: E402

STATE = Path(os.environ["TINYSCREEN_STATE_DIR"])
HDRS = {server.CUSTOM_REQUEST_HEADER: "1"}


def setUpModule():
    # No test here touches serial/collector behavior; keep the watchdog
    # from repeatedly spawning the real collector during the run.
    sleeper = TMP / "sleeper.py"
    sleeper.write_text("import time\ntime.sleep(3600)\n")
    server.COLLECTOR_SCRIPT = sleeper


class AuthTestBase(unittest.TestCase):
    """Each test starts with auth disabled and the lockout cleared."""

    def setUp(self):
        (STATE / server.AUTH_FILE_NAME).unlink(missing_ok=True)
        with server._login_lock:
            server._login_failures = 0
            server._login_locked_until = 0.0
        self.client = server.app.test_client()

    # -- helpers ---------------------------------------------------------

    def enable_pin(self, pin="1234", client=None, **extra):
        c = client or self.client
        return c.post("/api/auth/set_pin", json={"new_pin": pin, **extra},
                      headers=HDRS)

    def login(self, pin, client=None):
        c = client or self.client
        return c.post("/api/auth/login", json={"pin": pin}, headers=HDRS)

    def fresh_client(self):
        """A client with no session cookie -- a different LAN device."""
        return server.app.test_client()


class TestDisabledByDefault(AuthTestBase):
    def test_status_reports_disabled(self):
        body = self.client.get("/api/auth/status").get_json()
        self.assertEqual(body, {"ok": True, "enabled": False,
                                "authed": False, "lock_stats": False})

    def test_no_pin_means_no_behavior_change(self):
        """The entire feature must be invisible until opted into: GETs
        open, POSTs governed only by the CSRF guard exactly as before."""
        self.assertEqual(self.client.get("/api/last_flash").status_code, 200)
        self.assertEqual(self.client.get("/api/app_version").status_code, 200)
        # Headerless POST: still the CSRF 403, NOT an auth 401.
        self.assertEqual(self.client.post("/api/reset_device").status_code, 403)
        # Headered POST: reaches the endpoint (400 = "no device", i.e.
        # it got past every guard).
        r = self.client.post("/api/reset_device", headers=HDRS)
        self.assertEqual(r.status_code, 400)
        self.assertIn("plugged in", r.get_json()["error"])

    def test_login_without_pin_set_is_400(self):
        self.assertEqual(self.login("1234").status_code, 400)


class TestEnableAndGuard(AuthTestBase):
    def test_first_set_needs_no_current_pin_and_logs_setter_in(self):
        r = self.enable_pin("9999")
        self.assertEqual(r.get_json(), {"ok": True, "enabled": True,
                                        "lock_stats": False})
        # The setter keeps working without a separate login step...
        self.assertEqual(self.client.post("/api/reset_device",
                                          headers=HDRS).status_code, 400)
        # ...but a different device now gets the auth challenge.
        other = self.fresh_client()
        r = other.post("/api/reset_device", headers=HDRS)
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.get_json()["auth_required"])

    def test_csrf_guard_still_runs_first(self):
        """A cross-site POST must get the CSRF 403, not a 401 that leaks
        whether a PIN is enabled."""
        self.enable_pin("9999")
        r = self.fresh_client().post("/api/reset_device")
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("auth_required", r.get_json())

    def test_gets_stay_open_unless_stats_locked(self):
        self.enable_pin("9999")
        other = self.fresh_client()
        self.assertEqual(other.get("/api/last_flash").status_code, 200)
        self.assertEqual(other.get("/api/auth/status").status_code, 200)

    def test_static_pages_never_gated(self):
        self.enable_pin("9999", lock_stats=True)
        other = self.fresh_client()
        for page in ("/dashboard.html", "/wizard.html"):
            self.assertEqual(other.get(page).status_code, 200, page)

    def test_login_grants_session(self):
        self.enable_pin("9999")
        other = self.fresh_client()
        self.assertEqual(other.post("/api/reset_device", headers=HDRS).status_code, 401)
        r = self.login("9999", client=other)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(other.post("/api/reset_device", headers=HDRS).status_code, 400)

    def test_session_cookie_flags(self):
        self.enable_pin("9999")
        other = self.fresh_client()
        r = self.login("9999", client=other)
        cookie = r.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("Secure", cookie)  # deliberate: HTTP LAN listener

    def test_wrong_pin_rejected(self):
        self.enable_pin("9999")
        other = self.fresh_client()
        self.assertEqual(self.login("0000", client=other).status_code, 403)
        self.assertEqual(other.post("/api/reset_device", headers=HDRS).status_code, 401)


class TestLockStats(AuthTestBase):
    def test_toggle_gates_and_ungates_reads(self):
        self.enable_pin("9999", lock_stats=True)
        other = self.fresh_client()
        r = other.get("/api/last_flash")
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.get_json()["auth_required"])
        # status/login must remain reachable or nobody could ever unlock
        self.assertEqual(other.get("/api/auth/status").status_code, 200)
        self.login("9999", client=other)
        self.assertEqual(other.get("/api/last_flash").status_code, 200)

        # Toggle off (requires current pin), reads open again
        r = self.client.post("/api/auth/set_pin",
                             json={"current_pin": "9999", "lock_stats": False},
                             headers=HDRS)
        self.assertEqual(r.get_json()["lock_stats"], False)
        self.assertEqual(self.fresh_client().get("/api/last_flash").status_code, 200)

    def test_toggle_requires_current_pin(self):
        self.enable_pin("9999")
        r = self.client.post("/api/auth/set_pin",
                             json={"current_pin": "wrong", "lock_stats": True},
                             headers=HDRS)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(server._load_auth()["lock_stats"])


class TestChangeAndDisable(AuthTestBase):
    def test_change_requires_current_and_invalidates_old(self):
        self.enable_pin("1111")
        # Missing/wrong current pin: refused, even from a logged-in session
        for payload in ({"new_pin": "2222"},
                        {"new_pin": "2222", "current_pin": "9999"}):
            r = self.client.post("/api/auth/set_pin", json=payload, headers=HDRS)
            self.assertEqual(r.status_code, 403)
        # Correct current pin: changed
        r = self.client.post("/api/auth/set_pin",
                             json={"current_pin": "1111", "new_pin": "2222"},
                             headers=HDRS)
        self.assertTrue(r.get_json()["ok"])
        other = self.fresh_client()
        self.assertEqual(self.login("1111", client=other).status_code, 403)
        self.assertEqual(self.login("2222", client=other).status_code, 200)

    def test_change_preserves_lock_stats(self):
        self.enable_pin("1111", lock_stats=True)
        self.client.post("/api/auth/set_pin",
                         json={"current_pin": "1111", "new_pin": "2222"},
                         headers=HDRS)
        self.assertTrue(server._load_auth()["lock_stats"])

    def test_disable_requires_current_pin(self):
        self.enable_pin("1111")
        r = self.client.post("/api/auth/set_pin",
                             json={"disable": True, "current_pin": "wrong"},
                             headers=HDRS)
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/auth/set_pin",
                             json={"disable": True, "current_pin": "1111"},
                             headers=HDRS)
        self.assertEqual(r.get_json(), {"ok": True, "enabled": False})
        self.assertFalse((STATE / server.AUTH_FILE_NAME).exists())
        # Fully open again
        self.assertEqual(self.fresh_client().post("/api/reset_device",
                                                  headers=HDRS).status_code, 400)

    def test_deleting_auth_file_is_the_recovery_path(self):
        """The documented forgot-my-pin recovery: remove auth.json from
        the state dir. Must take effect immediately, no restart."""
        self.enable_pin("1111")
        other = self.fresh_client()
        self.assertEqual(other.post("/api/reset_device", headers=HDRS).status_code, 401)
        (STATE / server.AUTH_FILE_NAME).unlink()
        self.assertEqual(other.post("/api/reset_device", headers=HDRS).status_code, 400)

    def test_pin_length_limits(self):
        for bad in ("123", "x" * 129, "", None, 1234, ["1", "2"]):
            r = self.enable_pin(bad)
            self.assertEqual(r.status_code, 400, repr(bad))
        self.assertIsNone(server._load_auth())


class TestOnDiskHygiene(AuthTestBase):
    def test_auth_file_perms_and_shape(self):
        self.enable_pin("s3cret-pin")
        f = STATE / server.AUTH_FILE_NAME
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        cfg = json.loads(f.read_text())
        self.assertEqual(cfg["algo"], "pbkdf2-sha256")
        self.assertGreaterEqual(cfg["iterations"], 600_000)
        self.assertNotIn("s3cret-pin", f.read_text())  # never plaintext
        self.assertEqual(len(bytes.fromhex(cfg["salt"])), 16)
        self.assertTrue(server._verify_pin("s3cret-pin", cfg))
        self.assertFalse(server._verify_pin("s3cret-pim", cfg))

    def test_salts_differ_between_sets(self):
        self.enable_pin("1111")
        salt1 = server._load_auth()["salt"]
        self.client.post("/api/auth/set_pin",
                         json={"current_pin": "1111", "new_pin": "1111"},
                         headers=HDRS)
        self.assertNotEqual(server._load_auth()["salt"], salt1)

    def test_corrupt_auth_file_fails_open_with_warning_shape(self):
        """A mangled auth.json must behave as 'no PIN set' (recovery by
        deletion should never be needed twice) rather than bricking the
        API with 500s."""
        self.enable_pin("1111")
        (STATE / server.AUTH_FILE_NAME).write_text("{not json")
        self.assertIsNone(server._load_auth())
        self.assertEqual(self.fresh_client().post("/api/reset_device",
                                                  headers=HDRS).status_code, 400)


class TestLockout(AuthTestBase):
    def test_five_failures_lock_and_expiry_unlocks(self):
        self.enable_pin("9999")
        orig = server._LOCKOUT_SECONDS
        server._LOCKOUT_SECONDS = 1  # keep the test fast
        try:
            other = self.fresh_client()
            for _ in range(server._LOCKOUT_THRESHOLD):
                self.assertEqual(self.login("0000", client=other).status_code, 403)
            # Locked: even the CORRECT pin is refused with 429 + retry hint
            r = self.login("9999", client=other)
            self.assertEqual(r.status_code, 429)
            self.assertGreaterEqual(r.get_json()["retry_in"], 1)
            time.sleep(1.2)
            self.assertEqual(self.login("9999", client=other).status_code, 200)
        finally:
            server._LOCKOUT_SECONDS = orig

    def test_success_resets_failure_count(self):
        self.enable_pin("9999")
        other = self.fresh_client()
        for _ in range(server._LOCKOUT_THRESHOLD - 1):
            self.login("0000", client=other)
        self.assertEqual(self.login("9999", client=other).status_code, 200)
        # Counter reset: four more wrong guesses don't lock
        for _ in range(server._LOCKOUT_THRESHOLD - 1):
            self.assertEqual(self.login("0000", client=other).status_code, 403)


class TestSessionRevocation(AuthTestBase):
    """0.9.5.1: rotating or removing the PIN must revoke every existing
    session -- the whole reason to change a PIN is that some device
    shouldn't have access anymore."""

    def test_pin_change_revokes_other_sessions(self):
        self.enable_pin("1111")
        other = self.fresh_client()
        self.login("1111", client=other)
        self.assertEqual(other.post("/api/reset_device", headers=HDRS).status_code, 400)
        # The owner changes the PIN from their own device...
        self.client.post("/api/auth/set_pin",
                         json={"current_pin": "1111", "new_pin": "2222"},
                         headers=HDRS)
        # ...the other device's 30-day cookie dies with the old PIN,
        self.assertEqual(other.post("/api/reset_device", headers=HDRS).status_code, 401)
        self.assertFalse(other.get("/api/auth/status").get_json()["authed"])
        # while the changer keeps working (new-generation session).
        self.assertEqual(self.client.post("/api/reset_device", headers=HDRS).status_code, 400)

    def test_disable_reenable_revokes_old_sessions(self):
        self.enable_pin("1111")
        other = self.fresh_client()
        self.login("1111", client=other)
        self.client.post("/api/auth/set_pin",
                         json={"disable": True, "current_pin": "1111"}, headers=HDRS)
        self.enable_pin("3333")  # fresh generation
        self.assertEqual(other.post("/api/reset_device", headers=HDRS).status_code, 401)

    def test_lock_stats_toggle_preserves_sessions(self):
        """A preference flip is not a credential change; nobody should
        get logged out by it."""
        self.enable_pin("1111")
        other = self.fresh_client()
        self.login("1111", client=other)
        self.client.post("/api/auth/set_pin",
                         json={"current_pin": "1111", "lock_stats": True},
                         headers=HDRS)
        self.assertEqual(other.get("/api/last_flash").status_code, 200)
        self.assertEqual(other.post("/api/reset_device", headers=HDRS).status_code, 400)

    def test_gen_less_auth_file_from_0950_still_works(self):
        """Backward compat: an auth.json written by 0.9.5.0 has no gen
        field. Logins against it must work (session gen None == cfg gen
        None), and the first PIN change upgrades it to a gen'd file."""
        salt = os.urandom(16)
        cfg = {"algo": "pbkdf2-sha256", "iterations": 600_000,
               "salt": salt.hex(),
               "hash": server._hash_pin("oldpin", salt, 600_000).hex(),
               "lock_stats": False}
        (STATE / server.AUTH_FILE_NAME).write_text(json.dumps(cfg))
        other = self.fresh_client()
        self.assertEqual(self.login("oldpin", client=other).status_code, 200)
        self.assertEqual(other.post("/api/reset_device", headers=HDRS).status_code, 400)
        other.post("/api/auth/set_pin",
                   json={"current_pin": "oldpin", "new_pin": "newpin"}, headers=HDRS)
        self.assertIn("gen", server._load_auth())


class TestSetPinLockout(AuthTestBase):
    """0.9.5.1: set_pin's current_pin check shares the login lockout.
    In 0.9.5.0 it had no rate limit. A sessionless attacker never
    reaches it (the auth guard 401s first -- asserted below), so the
    real exposure was narrower but still exactly the scenario
    current_pin exists to resist: someone holding a HIJACKED SESSION
    could brute-force current_pin at full speed to take over or remove
    the lock."""

    def test_sessionless_attacker_never_reaches_set_pin(self):
        self.enable_pin("9999")
        r = self.fresh_client().post("/api/auth/set_pin",
                                     json={"current_pin": "0000", "new_pin": "hacked"},
                                     headers=HDRS)
        self.assertEqual(r.status_code, 401)

    def test_session_holder_guessing_current_pin_hits_shared_lockout(self):
        self.enable_pin("9999")
        # A hijacked-cookie attacker: valid session, unknown PIN.
        hijacker = self.fresh_client()
        self.login("9999", client=hijacker)  # stands in for the stolen cookie
        for _ in range(server._LOCKOUT_THRESHOLD):
            r = hijacker.post("/api/auth/set_pin",
                              json={"current_pin": "0000", "new_pin": "hacked"},
                              headers=HDRS)
            self.assertEqual(r.status_code, 403)
        r = hijacker.post("/api/auth/set_pin",
                          json={"current_pin": "0001", "new_pin": "hacked"},
                          headers=HDRS)
        self.assertEqual(r.status_code, 429)
        self.assertIn("retry_in", r.get_json())
        # The SAME counter locks login too -- one pool of attempts total.
        self.assertEqual(self.login("9999", client=self.fresh_client()).status_code, 429)
        # And the PIN was never changed.
        self.assertTrue(server._verify_pin("9999", server._load_auth()))

    def test_login_failures_lock_set_pin_too(self):
        self.enable_pin("9999")  # self.client now holds a valid session
        attacker = self.fresh_client()
        for _ in range(server._LOCKOUT_THRESHOLD):
            self.login("0000", client=attacker)
        # Even the legitimate, logged-in owner shares the pool: one
        # global gate on ALL pin verification, visible to everyone.
        r = self.client.post("/api/auth/set_pin",
                             json={"current_pin": "9999", "new_pin": "x" * 8},
                             headers=HDRS)
        self.assertEqual(r.status_code, 429)


class TestSecurityHeaders(AuthTestBase):
    def test_frame_denial_and_sniffing_headers_everywhere(self):
        for path in ("/dashboard.html", "/wizard.html", "/api/last_flash"):
            r = self.client.get(path)
            self.assertEqual(r.headers.get("X-Frame-Options"), "DENY", path)
            self.assertEqual(r.headers.get("Content-Security-Policy"),
                             "frame-ancestors 'none'", path)
            self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff", path)

    def test_auth_responses_never_cached(self):
        self.assertEqual(self.client.get("/api/auth/status")
                         .headers.get("Cache-Control"), "no-store")
        self.enable_pin("1234")
        r = self.login("1234", client=self.fresh_client())
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    ok = result.wasSuccessful()
    server.collector.pause()  # don't leave a child holding our stdout pipe
    print("ALL AUTH TESTS PASS" if ok else "AUTH TESTS FAILED")
    sys.exit(0 if ok else 1)

# Road to 1.0

**This file is a working handover document** between development sessions
(the project is developed in AI-assisted rounds, and long chats
eventually hit context limits). It carries the current state, what's
done, and what's next, so any session can pick up where the last left
off. **Delete this file at the final 1.0 release.**

Snapshot as of **0.9.5.1** (2026-07-11).

## How to get oriented fast

- `README.md` describes the app as it actually is -- trust it, it was
  rewritten and verified at 0.9.0.
- Run the test suite (commands in README "Testing"); everything passes
  as of this writing (35 updater tests, collector health, firmware
  logic compiled against stubs).
- Two version numbers exist on purpose: the app (`VERSION`, currently
  0.9.3.0) and the firmware (`FIRMWARE_VERSION` in
  `firmware/src/main.cpp`, currently 1.18.0). CI refuses to build if
  `VERSION` disagrees with the newest `CHANGELOG.json` entry.

## Done

- [x] **0.9.0** Repo spring-cleaning: README rewrite, compiler-warnings
      pass (2 real firmware bugs), test suite consolidation.
- [x] **0.9.1** Web surface hardening: CSRF/cross-site guard on all
      state-changing endpoints, global 1 MiB request-body cap.
- [x] **0.9.2** More security tidying: cert uploads HTTPS-only, generic
      client-facing error messages (details to logs), strict IANA
      timezone validation.
- [x] **NAS pool detection verified accurate against a real
      ZimaOS-created storage pool** (hardware-validated 2026-07-11; was
      the biggest known unknown).
- [x] **0.9.3 hardware verification round passed** (2026-07-11):
      cheroot on both ports, WebSerial flash from :8990 with the
      vendored esp-web-tools bundle, cert-upload hot-swap, self-update
      cycle -- all confirmed on the real box.
- [x] **0.9.3** Infrastructure hardening:
  - [x] Production WSGI server (cheroot) on both listeners, replacing
        Flask's dev server; cert hot-swap preserved (same SSLContext
        object trick, see `run_https()` in `app/server.py`).
  - [x] esp-web-tools vendored at image build time (pinned 10.2.1,
        official `dist/web/` self-host bundle) instead of loaded from
        unpkg; pinned CDN import kept only as a dev-checkout fallback.
  - [x] All Python deps pinned exactly (`app/requirements.txt`,
        `collector/requirements.txt`; versions verified against PyPI
        2026-07-11).
  - [x] Versioned Docker image tags (`:0.9.3.0` alongside `:latest`) +
        CI check that VERSION matches the newest CHANGELOG entry.

- [x] **0.9.4** Testing infrastructure:
  - [x] `tests/test_serial_endpoints.py`: 19 tests for `/api/flash`,
        `/api/configure`, `/api/reset_device` against a pty-pair fake
        device (server runs its REAL stty/raw-fd path on the pty slave)
        and a fake `esptool` on PATH. Covers the exact esptool v5
        invocation contract, board 0/1 variant selection, failure ->
        BOOT-button hint, last_flash persistence, ack/no-ack/garbage
        replies, timezone validation on the wire, CSRF coverage of all
        three endpoints, and -- via an ordering log all fakes append to
        -- the pause -> serial work -> resume choreography on every
        path including failures. Learned in the making: ack matching is
        an exact substring match on ArduinoJson's COMPACT serialization
        (no spaces); any fake device must reply byte-compatibly.
  - [x] CI `checks` job now gates the image push: all four host-side
        suites run on every build (previously CI never ran tests).
  - [x] CI guard against firmware copy drift (byte-diff of both trees).

- [x] **0.9.4 hardware round passed** (2026-07-11): real flash + settings
      save confirmed; first CI run of the checks job green.
- [x] **0.9.5** Optional dashboard PIN + written security model:
  - [x] pbkdf2-sha256 (600k iters, per-set salt, 0600 auth.json in the
        state dir), Flask signed-cookie sessions (HttpOnly, SameSite=Lax,
        30d, NO Secure flag -- HTTP LAN listener is primary), global
        login rate limit (5 fails -> 60s). Guard runs AFTER the CSRF
        guard. Changing/disabling/toggling requires the CURRENT pin in
        the body, never just a session. lock_stats toggle extends the
        gate to GET /api/*; static pages never gated. Recovery = delete
        auth.json (takes effect immediately; auth config is re-read per
        request on purpose).
  - [x] Dashboard: fetch() wrapper intercepts 401+auth_required, shows a
        PIN overlay, retries the original request -- call sites don't
        know auth exists. Concurrent 401s share one overlay. PIN card +
        management subview in General (NOTE: like all General cards,
        device-gated behind general-content -- hidden when no display is
        connected; pre-existing behavior, revisit someday?).
  - [x] tests/test_auth.py (18 tests) incl. "no PIN set changes nothing"
        regression; suite wired into CI. E2E overlay + subview flows
        verified with playwright during development.
  - [x] README "Security model" section: LAN trust statement, what the
        CSRF guard covers, PIN design, and the honest privileged+socket
        rationale (socket = root-equivalent regardless of the privileged
        flag; shrinking to cgroup rules is brittle AND cosmetic -- the
        decision is documented, not deferred).

- [x] **0.9.5 forgot-PIN recovery path verified on hardware**
      (2026-07-11): deleting auth.json unlocks immediately, as documented.
- [x] **0.9.5.1** PIN hardening patch (self-audit findings):
  - [x] Session generations: auth.json carries a random "gen"; sessions
        record the gen they logged in under; guard requires a match. PIN
        set/change/disable rotates gen -> every other session revoked;
        the changer is re-granted under the new gen; lock_stats toggle
        deliberately preserves gen. 0.9.5.0 gen-less auth.json keeps
        working (None == None) until the first change upgrades it.
  - [x] Shared lockout across ALL pin verification (login + set_pin's
        current_pin). Real 0.9.5.0 exposure, precisely stated: the auth
        guard already blocked sessionless callers from set_pin, but a
        HIJACKED SESSION could brute current_pin without limit -- the
        exact takeover current_pin exists to resist.
  - [x] Security headers on every response: X-Frame-Options DENY +
        CSP frame-ancestors 'none' (clickjacking vs logged-in sessions),
        nosniff, Referrer-Policy no-referrer; Cache-Control no-store on
        /api/auth/*.
  - [x] 5 new tests (31 total in test_auth.py); serial/updater/collector
        suites + browser E2E re-verified with the new headers.

## Next up (suggested order)

- [ ] **Hardware round for 0.9.5 + 0.9.5.1**: enable the PIN on the real
      box; overlay on the phone browser you actually use; one flash +
      one settings save while locked (session cookie over plain HTTP);
      change the PIN and confirm a second signed-in device gets locked
      out; IMPORTANT: confirm ZimaOS opens the app in a tab, not an
      iframe -- the new frame-denial headers would blank the dashboard
      inside an embedding UI (relax to a frame-ancestors allowlist of
      the ZimaOS origin if so; see _security_headers in server.py).
- [ ] **Placeholder metadata pass** -- `docker-compose.customapp.yml`
      x-casaos block has `author: you` / placeholder icon URL; verify an
      icon asset actually exists at the referenced path so the ZimaOS
      store tile isn't broken.
- [ ] **RELEASING.md** -- short checklist: bump VERSION + CHANGELOG
      (CI enforces they match), bump FIRMWARE_VERSION only when firmware
      actually changed, sync firmware_arduino/, push, verify tags on
      Docker Hub.
- [ ] **(Nice-to-have)** Extract dashboard.html's JS into an adjacent
      file for diffability + unit-testability; consider a full pip lock
      (hashes) on top of the exact pins.

## Known constraints / gotchas (learned the hard way)

- Arduino_GFX must stay pinned at exactly 1.4.9 (PlatformIO
  incompatibility in 1.6.x -- see comment in `firmware/platformio.ini`).
- Board 1 needs USB CDC On Boot: Enabled; board 0 the opposite. That's
  why CI builds two binaries from one source.
- esptool is on v5.x command syntax (`write-flash`, dashed) -- don't
  downgrade below 5.
- RAM is GiB, disks are decimal GB (deliberate, matches OS vs vendor
  labeling -- documented in README).
- The `/proc/1/net/dev` bind mount is what makes network stats work
  without `network_mode: host`; don't "simplify" it away.

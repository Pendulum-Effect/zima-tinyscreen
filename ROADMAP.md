# Road to 1.0

**This file is a working handover document** between development sessions
(the project is developed in AI-assisted rounds, and long chats
eventually hit context limits). It carries the current state, what's
done, and what's next, so any session can pick up where the last left
off. **Delete this file at the final 1.0 release.**

Snapshot as of **0.9.3.0** (2026-07-11).

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

## Next up (suggested order)

- [ ] **Hardware round for 0.9.3 verification** (nothing here is
      testable without the real box):
  - cheroot serving both ports on the ZimaBlade; WebSerial still happy
    on :8990; cert upload hot-swap still instant.
  - Wizard flashing with the vendored esp-web-tools bundle, ideally with
    the box's internet unplugged to prove the CDN is truly gone.
  - One full self-update cycle (old image -> 0.9.3.0) to confirm the
    pinned-deps image behaves; check `esptool` v5 flash via `/api/flash`.
- [ ] **Test the `/api/flash` and `/api/configure` serial paths** --
      the two endpoints that pause/resume the collector around exclusive
      serial access have no coverage. A pty-pair fake serial device
      (same philosophy as the existing fake-dockerd tests) could cover
      the pause/resume choreography, esptool failure -> hint path, and
      configure-ack handling.
- [ ] **CI guard against firmware copy drift** -- `firmware/src/` and
      `firmware_arduino/` are byte-identical today but only by
      discipline. Add a workflow step that diffs them and fails on drift
      (or generate the .ino tree in CI and stop committing it).
- [ ] **Security posture decision for 1.0** -- the app is deliberately
      unauthenticated on the LAN while being a privileged container with
      the Docker socket. Either add an optional dashboard PIN/token, or
      write the trust model down explicitly (short THREAT_MODEL section
      in the README). Also check whether `privileged: true` can shrink
      to `device_cgroup_rules` + the `/dev` bind under ZimaOS's compose
      form.
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

# Releasing TinyScreen

The short version of every release, so nothing gets forgotten. The CI
gates catch most slips, but catching them locally is faster.

## Every release

1. **Bump `VERSION`** (MAJOR.MINOR.FEATURE.ITERATION) and **add the
   matching `CHANGELOG.json` entry** in the same change -- CI fails the
   build if they disagree. Entry titles never contain the version
   number; the version lives only in the entry's `"version"` field.
2. **Firmware version** (`FIRMWARE_VERSION` in `firmware/src/main.cpp`)
   is bumped **only when the firmware's bytes change** -- code, fonts,
   or the splash bitmap (`tiny_logo.h`). Dashboard/server-only releases
   leave it alone, so deploys don't masquerade as firmware updates.
3. **Keep the two firmware trees byte-identical**: `firmware/src/` is
   the source of truth; copy `main.cpp` to
   `firmware_arduino/zima_tinyscreen/zima_tinyscreen.ino` and any
   changed headers (`tiny_fonts.h`, `tiny_logo.h`) alongside it. CI's
   drift guard fails the build on any mismatch.
4. **Run all five test suites** (CI runs them too, as a push gate):
   - `python3 tests/test_auth.py`
   - `python3 tests/test_serial_endpoints.py`
   - `python3 tests/test_updater.py`
   - `python3 tests/test_collector_health.py`
   - `g++ -std=c++17 -Wall -Wextra -Wno-unused-parameter -I tests/firmware_stubs -o /tmp/fw_test tests/test_firmware_logic.cpp && /tmp/fw_test`
   The firmware suite should compile with **zero warnings**.
5. **Never commit build or cache litter**: `__pycache__/`, `*.pyc`,
   `firmware/.pio/`, `tests/shots/`, `webflasher/vendor/` are all
   gitignored -- if uploading files through the GitHub web UI, remember
   the web UI only adds/overwrites, it never deletes.
6. **Push to `master`.** CI then: syncs-checks the firmware trees,
   runs the suites, builds BOTH firmware variants (bridge + native),
   stages `boot_app0.bin` into the WebSerial manifests, and pushes the
   Docker image tagged `:latest` **and** `:<VERSION>` -- every release
   is a pinnable, roll-back-able artifact.

## After CI goes green

7. On the ZimaOS box, the app's **Check for Update** pulls the new
   image. If the firmware version changed, the General tab offers the
   flash -- do it and confirm the device reports the new version.
8. Anything marked **HARDWARE VERIFY** in `ROADMAP.md` for this release
   gets checked on real glass; tick it off or file what broke.

## When compose files or the workflow change

`app/docker-compose*.yml` and `.github/workflows/*` are not delivered
by the self-updater -- they change rarely and are updated by hand.
A release that touches them must call it out loudly (the collaboration
convention: the full updated file ships in the release notes).

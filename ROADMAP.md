# Road to 1.0

**This file is a working handover document** between development sessions
(the project is developed in AI-assisted rounds, and long chats
eventually hit context limits). It carries the current state, what's
done, and what's next, so any session can pick up where the last left
off. **Delete this file at the final 1.0 release.**

Snapshot as of **0.9.8.4** (2026-07-13).

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

- [x] **0.9.5.2** PIN polish + serial safety (second self-audit):
  - [x] Wizard was BROKEN on a PIN-enabled box (its server-side
        flash/configure path got bare 401s). Auth overlay extracted to
        webflasher/auth_overlay.js (self-contained: injects its own
        styles with var() fallbacks, idempotent, marker flag
        window.__tinyscreenAuthOverlay); dashboard and wizard both
        include it; dashboard's inline copy and pin CSS removed.
  - [x] Exclusive-serial mutex: configure/reset/current_config/flash
        serialize through _begin/_end_exclusive_serial; concurrent
        caller gets 409 {busy:true}. Fixes real interleaving corruption
        (two tabs sufficed), not just hostile spam.
  - [x] Lockout backoff: doubles per consecutive lockout, 1h cap, streak
        decays after a quiet hour but deliberately NOT on successful
        login. Lockouts logged to stderr with source address.
  - [x] TLS 1.2 floor explicit; CI advisory pip-audit (non-blocking).
  - [x] Suites now: auth 32, serial 20; wizard + dashboard overlay flows
        E2E-verified in a real browser.

## Diminishing-returns note (read before proposing 0.9.5.3)

Three consecutive security rounds have now happened. What's left is
gold-plating for a home appliance: GH-action SHA pinning, npm tarball
integrity pinning (needs a hash from a trusted first CI run), CSP
script-src for the inline-script pages, per-endpoint rate limits.
None are worth a dedicated round; fold them into other work or skip.

- [x] **0.9.5.3** Hardware-round findings fixed (busy message verified
      on the real box; two dead buttons reported):
  - [x] Reset Device button was NEVER wired to anything in any version
        -- now arm-then-fire -> POST /api/reset_device -> visible
        outcome + wizard pointer.
  - [x] "Turn off does nothing" root cause: offStatus was written on
        every click but NEVER APPENDED to the DOM -- all feedback since
        0.9.5.0 went to a detached element. Lesson recorded: E2E tests
        asserted the side effect (auth.json deleted) but not the
        VISIBLE feedback; assert what the user sees.
  - [x] Empty current-PIN is now a 400 UX slip, NOT a counted attempt
        (five absent-minded Turn off clicks used to lock the owner out);
        client shows an instant local message and never sends it.
  - [x] PIN settings demands login up front for sessionless visitors
        (requirePin exposed as window.__tinyscreenRequirePin) instead of
        bouncing each button off the guard mid-action.
  - [x] Cache-Control: no-cache on .html/.js (ETag revalidation) so app
        updates can't leave browsers on a stale page.

- [x] **0.9.5.4** Confirmation modals + branding (hardware-round feedback):
  - [x] Generic confirmModal() helper in the update modal's visual
        language (promise-less onConfirm contract: {ok} closes,
        {ok, keepOpen} for navigation, {ok:false, error} stays open for
        retry). Reset and PIN-off both use it.
  - [x] Reset Device: confirm -> POST /api/reset_device -> straight to
        wizard.html (the old outcome text referenced a wizard link that
        doesn't exist on the dashboard).
  - [x] Turn off PIN: modal collects the PIN itself; decoupled from the
        change-card's Current PIN field.
  - [x] Branding: favicons (all sizes + .ico) at webflasher root with
        link tags in dashboard + wizard; sidebar wordmark
        (branding/TinyScreen_Logo.png, 58px/30px mobile); About leads
        with branding/TinyScreen_Icon.png, text wrapping beside it
        (shipped in-app, same no-remote-fetch reasoning as the vendored
        flasher JS; about.json's logo_url is now ignored). logo-slot
        placeholder CSS retired.

- [x] **0.9.6.0** Polish round 1 (hardware-round feedback):
  - [x] REAL BUG: `const cfgData` was reassigned by the cached-config
        fallback -- threw in every browser (Safari wording: "Attempted
        to assign to readonly property") the moment the device was
        unplugged WITH a cache present, killing the General tab. The
        E2Es never caught it because fresh sandboxes have no cache.
        Fixed (let); the designed cached-browsing mode works for the
        first time in several releases.
  - [x] /api/current_config finally has committed coverage (happy path,
        cache write contract, unplugged-with/without-cache) and the
        shared FakeDevice now inlines a realistic config in get_config
        acks (DEFAULT_CONFIG). LESSON: a "GET" endpoint with exclusive
        serial access went untested because it didn't look
        state-changing.
  - [x] Fresh-install no-device General: app-level cards stay usable
        (device card shows "No display connected" + wizard link);
        layouts/screen get banners. About was already independent.
  - [x] Cert actions through confirmModal (install + revert), errors
        retryable in-modal, Current-certificate card refreshes after.
  - [x] HTTP: upload card fully replaced by an explanation + "Open the
        HTTPS dashboard" button -- no path to keying material on the
        wire (server already rejected it; UI now can't offer it).
  - [x] Sidebar wordmark spans the nav-pill width (verified 170px ==
        170px programmatically + screenshot).
  - [ ] Consider a CI playwright smoke job (chromium on runners is
        cheap): the const bug is exactly the class the host-side suites
        can't see. Candidate for a later 0.9.6.x.

- [x] **0.9.6.1** Screensaver features + layout polish (FIRMWARE 1.19):
  - [x] Firmware: saver_brightness (NVS "saverBri", clamps 0-100,
        reported in get_config) + "temp" saver style (drawSaverTemp:
        big centered temp, tempColorFor ramp). New wantedBacklightPct()
        is the single backlight-policy source (applyBrightness + the
        once-a-second check both call it); drawing savers take
        min(effective, saverBrightness) -- never brighten past night
        mode. FIRMWARE_VERSION now two-part: "1.19".
  - [x] Stub fidelity fix: JsonVariantStub had no operator bool(), so
        every `config.x = doc["x"]` BOOL assignment silently read the
        unset int slot and produced false -- no prior test ever read a
        bool through set_config. Fixed + exact-match char* subscript
        overload to keep the build warning-free. LESSON: stub gaps hide
        exactly the code paths they fail to model.
  - [x] Dashboard: saver brightness slider (hidden for "blank"),
        Temperature style radio, style whitelist ['clock','blank',
        'temp'], payload + server passthrough + FakeDevice
        DEFAULT_CONFIG all carry saver_brightness.
  - [x] Screen tab reordered: Brightness > Screensaver > Night Mode >
        Rotation > Aspect.
  - [x] Skeletons: desktop skel-card now wears the .card box exactly
        (was capped 640px); Debugging subview shows skeletons during
        its four fetches.
  - [x] Mobile General: "health" row was missing from the phone grid
        template, so the pill auto-placed into an implicit column OFF
        the card. Row added + pill wraps centered.

- [x] **0.9.7.0** Interaction polish:
  - [x] Traveling nav pill: one absolutely-positioned .nav-indicator
        slides (transform + width/height, springy cubic-bezier) to the
        active item; geometry re-derived from the live DOM per move so
        desktop rail and phone bar share the code. Re-syncs on load,
        resize, and wordmark image load (the img changes sidebar
        layout). Base .nav-item.active background removed -- the pill
        owns it now.
  - [x] Hover language: .btn lift + teal glow (+press state), chips
        warm borders, toggle focus ring, nav icon nudge. All in one
        end-of-stylesheet block; everything guarded by
        prefers-reduced-motion.
  - [x] New TinyScreen_Logo.png (1491x800 recrop).
  - [x] E2E: pill alignment asserted at rest on BOTH form factors,
        caught mid-flight during travel, hover glow computed-style
        asserted; screenshots reviewed.
  - NOTE for hardware round: sidebar hover/travel is worth one quick
    look on a real phone (no hover there; travel should still glide).

- [x] **0.9.7.1** PIN + certificate page restructure:
  - [x] confirmModal grew inputs[] (stacked fields -> onConfirm gets an
        array; withPin stays scalar for old callers) and buildBody(
        holder, ctx) with ctx.setConfirmEnabled for arbitrary content.
  - [x] PIN view: modal-first cards (Change the PIN / Lock stats
        viewing / Turn the PIN off -> Disable PIN) + Forgot PIN? card;
        all buttons btn-uniform. lock_stats moved out of the old
        save-everything form into its own PIN-confirmed toggle card.
        First-time Set a PIN stays inline (two fields + checkbox).
  - [x] Cert upload: popup with ghost file-picker buttons; Install
        Certificate disabled until both files picked (the "Pick both
        files first" scolding is structurally impossible now).
  - [x] _validate_pair rejections rewritten for humans, with SPECIFIC
        detection of: swapped cert/key, non-PEM files, encrypted keys,
        and KEY_VALUES_MISMATCH -> "they aren't a pair" (this was the
        confusing pictured error; raw OpenSSL constants no longer
        surface). TestCertPairMessages pins all five messages.
  - [x] Tip text -> .info-card with icon; HTTP-gate + Revert buttons
        uniform.

- [x] **0.9.7.2** Firmware/flash-pipeline review (1.69" won't enumerate
      after erase+flash despite "successful"):
  - REVIEWED CLEAN: setup() boot order (no GPIO before configuration,
    no while(!Serial), CDC enumerates pre-setup); blank-NVS first boot
    is deliberately inert; 1.19 changes all sit behind config/saver
    activity; variant flags unchanged since 1.18 flashed this exact
    unit successfully; manifests have correct S3 offsets (bootloader@0,
    partitions@0x8000, app@0x10000); NATIVE_USB_BOARDS=[1] mapping ok.
  - FOUND + FIXED: manifests flashed THREE parts where pio upload
    flashes FOUR -- boot_app0.bin@0xE000 (otadata init) was missing.
    Post-erase boot therefore leaned on the IDF bootloader's
    empty-otadata fallback. Probably not the root cause (the fallback
    boots ota_0) but the only discrepancy found; CI now stages
    boot_app0.bin from the framework package and fails loudly if
    absent; TestFlasherManifests pins the 4-part set for both
    variants.
  - NOTED (not changed): the native env inherits ARDUINO_USB_MODE from
    the board definition rather than pinning it; pin explicitly if USB
    behavior ever needs debugging.
  - DIAGNOSTIC PLAN for the unit (decisive step is the boot console):
    (1) after a "successful" flash, unplug/replug WITHOUT BOOT held,
    then check whether ANY port appears (Mac: ls /dev/cu.*) -- present
    but wizard-undetected points at flow/port-picking; absent points
    deeper. (2) esp-web-tools "Logs & console" AFTER the post-flash
    reset: ROM/bootloader output names the failure (flash read errors /
    invalid header -> dying flash chip = hardware; clean bootloader log
    then silence -> app-level, reopen this review). (3) if download
    mode remains 100% reliable while the app never enumerates, that
    asymmetry itself indicates failing flash-XIP or PSRAM, i.e.
    hardware.

- [x] **0.9.7.3** Flash-failure mystery RESOLVED (boot console captured):
  - The unit was healthy the whole time: boot log showed
    SPI_FAST_FLASH_BOOT + clean app entry + stable enumeration
    (usbmodem present). "Not detected" was the LAST MILE: most likely
    the esp-web-tools dialog still holding the port during the
    wizard's configure step.
  - Two console lines explained: "nvs_open failed: NOT_FOUND" =
    read-only open of a namespace that doesn't exist on a
    freshly-erased chip (EXPECTED on factory-first boot; firmware 1.20
    now opens read-write, which creates the namespace silently --
    nothing is written until the first set_config). "PSRAM ID read
    error" = module most likely has no PSRAM chip; flag makes it
    probe, log, and continue.
  - [ ] FOLLOW-UP: confirm which module variants (with/without PSRAM)
        ship on boards 0 and 1; if neither has PSRAM, drop
        -DBOARD_HAS_PSRAM to silence the probe error (do NOT change
        blind -- current bits demonstrably boot, and the canvas lives
        in internal RAM either way at 240x280).
  - [ ] FOLLOW-UP: wizard configure step could auto-detect "port held
        by another connection" and say so explicitly.

- [x] **0.9.7.4** ROOT CAUSE of the 1.69" saga (screenshot showed the
      smoking gun: an "ESP32-S3-LCD-1.3" model card on 1.69" hardware):
  - Mechanism: unconfigured device reports its DEFAULTS (board 0,
    configured:false). Dashboard ignored the configured flag and
    rendered a real board-0 device; its saves echoed board back;
    build_set_config_payload ALSO defaulted board:0; firmware flipped
    configured=true on any set_config. Result: one casual layouts save
    -> device configured as board 0 -> board-0 display pins collide
    with native USB data lines on the 1.69" -> USB flaps with the draw
    cadence = the 1,2,3-1,2 clicking + vanishing port. Both incidents,
    same mechanism; the "NVS corruption" theory was wrong.
  - Fixed at all three layers (any one suffices): dashboard renders
    configured:false as "New display detected" + read-only tabs and
    sends NO board on saves; server includes board only-if-present
    (was: default 0); firmware 1.21 only becomes configured via a
    command explicitly naming the board.
  - Tests: firmware (boardless save can't configure; field still
    applies; explicit board configures + restarts), payload (no board
    key by default), E2E (unconfigured rendering + wire payload has no
    board).
  - Unit recovery: full erase -> flash -> replug WITHOUT BOOT held
    (the boot:0x1 log showed BOOT still down at reset) -> wizard setup
    choosing the 1.69" TOUCH board.
  - [ ] FOLLOW-UP: wizard could show "detected an unconfigured device"
        and preselect nothing, making board choice an explicit step
        that can't be skimmed past.

- [x] **0.9.7.5** Mac/WebSerial configure path fixed (hardware round on
      0.9.7.4 confirmed the ZimaBlade path works end-to-end; the
      computer path left devices unconfigured):
  - Root causes, both in pushDefaultConfigViaWebSerial: (1) OPENING
    the port can RESET native-USB boards (the USB-serial peripheral
    doubles as reset control); the old code wrote set_config
    immediately, the bytes landed mid-boot and were lost, and the
    status lied "Sent -- likely worked". (2) The old read loop raced
    reads against timeouts; an orphaned read could swallow the ack
    chunk even when configuration succeeded.
  - Rewrite: single background reader pump into a buffer (loses
    nothing); 1800ms boot pause after open; send-and-verify with 3
    idempotent retries at 2500ms deadlines; honest failure message
    (nothing saved, retry, check Logs & console); success message
    notes the screen lights up + one restart.
  - Setup links (#model-sku a, tab error banners) themed teal --
    they rendered as default blue/purple in user screenshots.
  - [ ] HARDWARE VERIFY: re-test the full Mac path end-to-end --
        flash, configure (should see the boot pause + ack), screen
        lights while still on the Mac, then move to the ZimaBoard
        and confirm it arrives CONFIGURED.

## Known bugs to fix before final release

- [ ] **Mac/WebSerial configure still leaves the device unconfigured**
      (0.9.7.5's boot-pause + verified retries did NOT fix it; the
      ZimaBlade path recovers it every time). Next diagnostic: capture
      the wizard's on-screen status during the configure step (did it
      claim an ack? which attempt?) plus the boot console right after.
      Suspects: the open-triggered reset taking longer than 1.8s on
      this unit; the ack arriving on a DIFFERENT port instance after
      re-enumeration; or writes landing while the CDC host buffer isn't
      drained. Consider: configure step re-requesting the port fresh,
      or moving first-config entirely to the ZimaBlade path in the
      wizard copy.

- [x] **0.9.8.0** Screen-feel round (FIRMWARE 1.22):
  - [x] Rolling numbers on every layout's primary readout: shared
        engine (rollStep state + decideRollAction pure core +
        renderRollFrame) with anchor wrappers (centered, bottom-right,
        top-right, left-baseline). Wired: ring gauge big value (cpu/
        ram/temp/mmc/nas ring styles), dial number, mist temp jumbo,
        net bars both rates (sub-slots 0/1), net graph both rates.
        Right-edge anchored mid-roll (reference video: 47->117 keeps
        the 7 planted). Direction follows the value (grew = up).
        ROLL_COOLDOWN_MS=4000: displayed value HOLDS then rolls to the
        freshest -- never more than one roll per 4s per readout. Frame
        pacing drops 200ms->33ms only while a roll is active.
  - [x] aspect_mode 0/1/2 replaces squareFit internally (NVS
        "aspectMode", migration from squareFit, legacy square_fit
        still honored in set_config and kept coherent in acks).
        Mode 2 = 200px centered square = the physical glass size of a
        1.3" board (math in computeLayoutBox's comment). Dashboard
        radio row grew "1.3 inch compact"; payload sends aspect_mode +
        square_fit for firmware <=1.21 degradation.
  - [x] Temp saver: tiny_sans_bold_32 + degree format, tempColorFor,
        ABSOLUTE panel center (ignores the aspect box on purpose).
  - [x] Splash: tiny_logo.h (RGB565 180x97 from the wordmark PNG,
        34KB flash, regenerate-don't-edit) + "loading..." replaces the
        empty-layout + "waiting for host..." state entirely; the
        stale-with-data banner remains. CI drift guard now covers
        tiny_logo.h.
  - [ ] HARDWARE VERIFY (1.22): rolls on a real panel (smoothness at
        33ms frames, mask strips clean on ring/dial/mist/net); compact
        1.3" behind the actual case cutout; saver centering; splash on
        a fresh boot before the collector connects.

- [x] **0.9.8.1** Roll renderer v2 (FIRMWARE 1.23), from hardware video
      review of 1.22:
  - Bug 1 (mist "blips"): v1 clipped sliding glyphs by painting
    COL_BG rectangles over the overflow strips -- black holes through
    any gradient. v2 clips exactly: snapshot the strip pixels from the
    canvas framebuffer (rollSaveStrip, 22KB static buffer sized for
    bold_128), draw the overflowing glyph, restore (rollRestoreStrip).
    Correct over ANY background.
  - Bug 2 (% wobble): v1 laid glyphs into padded uniform cells;
    mid-roll positions never matched natural print positions. v2
    derives per-character cursors from real font advances (advance =
    width("cc") - width("c"); GFX fonts have no kerning), so an
    unchanged character sits EXACTLY where plain print puts it, and on
    width changes (99 -> 100) survivors glide between old and new
    natural homes (cursor lerp by eased p). Wrappers return the
    readout's LIVE right edge; the dial's %-shoulder follows it.
  - Verified by faithful pixel simulation of the algorithm (PIL, GFX
    baseline semantics) on a gradient: 99%->100% and 45->46 sheets
    show intact gradient, pixel-still unchanged chars, clean band
    clipping. The first sim run itself caught a metrics-anchor bug --
    worth keeping the sim script pattern for future renderer work.
  - [ ] HARDWARE VERIFY (1.23): mist page rolls over the glow (no
        blips), dial % steady through 9->10 and 99->100 boundaries,
        ring/net rolls unchanged.

- [x] **0.9.8.2** Gauge tweens (FIRMWARE 1.24): rings + dial ease
      between values (TWEEN_MS=600, shared smoothstep). One insertion
      point per gauge (the pct at the top of drawRingGauge /
      drawDialGauge); the dial's utilColorFor(pct) follows the tween
      automatically, the ring's color param stays caller-owned
      (switches instantly -- revisit only if it ever looks off on
      hardware). NO cooldown by design: needle leads, odometer
      follows. Mid-flight retarget starts from the current shown
      value (never jumps). tweenEval is the pure host-tested core;
      frame pacing now keys off activeRolls + activeTweens.
  - [ ] HARDWARE VERIFY (1.24): ring + dial sweeps feel right at 33ms
        frames; dial color fade through zone boundaries; splash ->
        first data still snaps (no sweep from zero on page arrival --
        page-arrival snap is by design).

- [x] **0.9.8.3** Gauge/digit lockstep (FIRMWARE 1.25), by user
      request after seeing 1.24's free-running tweens: the tween now
      retargets ONLY when its paired RollSlot's lastRoll timestamp
      changes (the digits fired or the page armed) -- one gate, one
      data snapshot, TWEEN_MS = ROLL_MS. The digits draw after the arc
      within a frame, so a fire is adopted next frame (33ms,
      invisible). String-invariant float wiggle no longer moves the
      arc at all. Lockstep integration test drives the real
      drawValueTextCentered + tweenValue pair against g_fake_millis
      (hold together -> fire together -> planted on wiggle).
  - [ ] HARDWARE VERIFY (1.25): ring + dial sweep starts as the digits
        roll; nothing moves during the hold window; dial color fade
        now also beats with the digits.

- [x] **0.9.8.4** Preview + saver polish (FIRMWARE 1.26):
  - [x] .device-preview svg gets its own border-radius (12px tall /
        8px square) -- the mist preview ended in a hard right angle
        inside the rounded bezel.
  - [x] Saver temp: tiny_sans_bold_64 (2x the page face) + the
        layouts' hold-and-roll via a dedicated 13th roll slot
        (SAVER_ROLL_SLOT, SAVER_OWNER pseudo page id). rollStep
        refactored to rollStepAt(slotIdx, ownerId) with the page
        wrapper preserved; drawValueTextCenteredAt exposed for
        non-page callers. Color still tracks the live sample (same as
        mist's behavior) while digits hold.
  - [x] Mist big number: right pad SY(12)->SY(16), bottom
        SY(10)->SY(18).
  - [x] info-note under every layout style dropdown (hardware
        colors/fonts/animations disclaimer); renders on all enabled
        page cards (disabled cards have no dropdown), verified 3/3 in
        E2E.
  - [ ] HARDWARE VERIFY (1.26): saver number size + roll rhythm; mist
        number position; preview corners on the real dashboard.

## Next up (suggested order)

- [ ] **Hardware round for 0.9.6.1**: flash firmware 1.19, set saver to
      Temperature with brightness ~30%, confirm it dims (and that night
      mode still wins if darker); check the phone General tab; skim the
      Screen tab order.
- [ ] **Hardware round for 0.9.6.0**: unplug the display with the app
      still open (General should show cached state + Warning, not an
      error), fresh-browser check of the HTTP cert gate button, one
      cert revert via the new modal.
- [ ] **Hardware round for 0.9.5.x (remaining)**: enable the PIN on the real
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

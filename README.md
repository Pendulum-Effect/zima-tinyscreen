# Tiny Screen

A tiny external status display for a ZimaBlade / ZimaBoard: a Python script
collects vital stats and streams them over USB to an ESP32-S3, which draws
them on a 1.28" round touch display. Swipe through 5 screens: CPU, RAM,
SSD, network, and temperature. Packaged as a one-click ZimaOS app that also
hosts a browser-based (WebSerial) firmware flasher — no toolchain required
to set up the ESP32-S3.

Hardware target: **Waveshare ESP32-S3-Touch-LCD-1.28** (GC9A01A display,
CST816S touch, 240x240px).

```
zima-tinyscreen/
├── collector/          Python stats collector (runs on the ZimaBlade/ZimaBoard)
├── firmware/            ESP32-S3 firmware (PlatformIO, C++)
├── webflasher/          WebSerial flashing page (ESP Web Tools) + built firmware
├── app/                 ZimaOS/Docker packaging (serves webflasher on :8989)
└── README.md
```

## 1. Build the firmware

```bash
cd firmware
pip install platformio        # if you don't have it
pio run
```

This produces:
```
.pio/build/esp32-s3-tinyscreen/bootloader.bin
.pio/build/esp32-s3-tinyscreen/partitions.bin
.pio/build/esp32-s3-tinyscreen/firmware.bin
```

Copy those three files into `webflasher/firmware/` (create the folder) —
that's what `webflasher/manifest.json` points at.

```bash
mkdir -p ../webflasher/firmware
cp .pio/build/esp32-s3-tinyscreen/{bootloader,partitions,firmware}.bin ../webflasher/firmware/
```

You can also flash directly from PlatformIO during development:
```bash
pio run -t upload -t monitor
```

## 2. Flash from the browser (optional, once packaged)

Open `webflasher/index.html` (served by the app on port 8989 once deployed,
or locally via `python3 -m http.server` from inside `webflasher/`) in
Chrome or Edge, plug in the ESP32-S3, and click **Connect & Flash**. This
uses [ESP Web Tools](https://esphome.github.io/esp-web-tools/) under the
hood via WebSerial.

## 3. Run the collector

On the ZimaBlade/ZimaBoard itself:

```bash
cd collector
pip install -r requirements.txt
python3 stats_collector.py --port /dev/ttyACM0   # omit --port to auto-detect
```

Notes:
- **CPU wattage** is read from Intel RAPL (`/sys/class/powercap/intel-rapl:0`),
  which is what ZimaBoard/ZimaBlade's Celeron N-series SoCs expose. If your
  kernel/permissions don't expose it, this reports `-1` (unknown) — the
  firmware just shows whatever is sent, so you can swap in another power
  source (e.g. a smart PDU/UPS API) if you have one.
- **CPU temperature** uses `psutil.sensors_temperatures()`, picking the
  package/CPU sensor if labeled; falls back to the first sensor found.
- Run as root or with a udev rule granting access to the RAPL energy
  counters and serial device if you hit permission errors.

## 4. Package as a ZimaOS app

### Option A: Publish to Docker Hub via GitHub Actions (no Docker install anywhere)

This project includes `.github/workflows/docker-build-push.yml`, which
builds the image on GitHub's own servers and pushes it to Docker Hub. You
never install Docker locally — you just need free GitHub and Docker Hub
accounts.

1. **Create a GitHub repo** and push this project to it (via GitHub's web
   "upload files" UI if you don't want to use git locally, or `git push`
   if you do).
2. **Create a Docker Hub access token**: Docker Hub → your avatar →
   **Account Settings → Personal access tokens → Generate new token**.
   Give it Read/Write access and copy the token (you won't see it again).
3. **Add two secrets to the GitHub repo**: repo → **Settings → Secrets and
   variables → Actions → New repository secret**:
   - `DOCKERHUB_USERNAME` = your Docker Hub username
   - `DOCKERHUB_TOKEN` = the access token from step 2
4. **Push to the `main` branch** (or go to the repo's **Actions** tab and
   run the workflow manually). GitHub builds the image and pushes
   `YOUR_DOCKERHUB_USERNAME/tinyscreen-dashboard:latest` to Docker Hub —
   watch progress under the Actions tab.
5. This makes the image **public** on Docker Hub (free tier doesn't
   include private repos on most plans) — fine here since there's nothing
   sensitive in it, but worth knowing.
6. Once the workflow finishes, entirely from the ZimaOS web UI:
   - Edit `app/docker-compose.customapp.yml` and replace
     `YOUR_DOCKERHUB_USERNAME` with your actual Docker Hub username.
   - **App Store → + → Install a Custom App → Docker Compose tab** → paste
     the file's contents → **Install**.
   - ZimaOS pulls the image itself. It'll show up in your app list with
     normal start/stop/logs controls, at `http://<zima-ip>:8989`.

To ship an update later: push new code to `main`, the workflow rebuilds and
re-pushes the same tag automatically, then hit **Update** on the app in
ZimaOS (or reinstall) to pull the new image.

### Option B: Build locally on the ZimaOS box over SSH

If you'd rather not publish anything publicly, or don't want to set up
GitHub Actions:

1. **Enable SSH / open the Web Terminal** on ZimaOS (Settings → Advanced →
   SSH, or use the built-in Web Terminal app).
2. **Copy this whole project** onto the ZimaOS device, e.g.:
   ```bash
   scp -r zima-tinyscreen your-user@<zima-ip>:/DATA/AppData/tinyscreen-src
   ssh your-user@<zima-ip>
   ```
3. **Build and tag the image locally on the box:**
   ```bash
   cd /DATA/AppData/tinyscreen-src
   docker build -t tinyscreen-dashboard:latest -f app/Dockerfile .
   ```
4. Open the ZimaOS web UI → App Store → **+** → **Install a Custom App** →
   Docker Compose tab, and paste a version of
   `app/docker-compose.customapp.yml` with the `image:` line changed back
   to `tinyscreen-dashboard:latest` (no Docker Hub username) — since the
   image already exists locally, Compose won't try to pull it.
5. Click **Install**.

If the ESP32-S3 shows up on a specific device node instead of the whole USB
bus, edit the `devices:` line before pasting (e.g. swap
`/dev/bus/usb:/dev/bus/usb` for `/dev/ttyACM0:/dev/ttyACM0` and drop
`privileged: true`) — narrower and preferred if it works reliably across
replugs on your setup.

### Local dev: plain `docker compose up`

```bash
cd app
docker compose build
docker compose up -d
```




The container runs two things:
- the Flask app on port **8989**, serving the flasher UI and firmware files
- the stats collector in the background, auto-retrying if the ESP32-S3 gets
  unplugged/replugged or reflashed

## Protocol

The collector sends one line of JSON per second over USB serial (115200
baud):

```json
{"cpu_name":"Intel(R) Celeron(R) N5105","cpu_pct":12.3,"cpu_temp_c":45.2,"cpu_watts":6.1,"ram_total_gb":16.0,"ram_pct":34.5,"ssd_total_gb":512.0,"ssd_pct":61.2,"net_rx_mbps":12.4,"net_tx_mbps":3.1}
```

The firmware parses each line with ArduinoJson and keeps the latest values
in memory; screens redraw on a timer independent of serial arrival, and
show a "waiting for host / no data" hint if nothing's arrived recently.

## Customizing the look

`firmware/src/main.cpp` has a small color palette at the top
(`COL_BG`, `COL_TEAL`, etc.) and one `drawRingGauge()` helper used by most
screens — tweak colors, fonts (swap in an Adafruit GFX font), or layout
there. If you have reference screenshots/mockups for the exact visual style
you want, send them over and I can match the layout more precisely.

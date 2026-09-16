# TrainMate Raspberry Pi vision agent

This agent runs on a Raspberry Pi 5 with a Camera Module 3 Wide. It performs
pose detection locally. It sends a short-lived annotated JPEG preview for the
authenticated owner to view in Vision Lab, and saves one optional progress
frame with the score each minute. It never records or uploads a continuous
video file.

## Install on the Pi

```bash
sudo apt update
sudo apt install -y python3-picamera2 python3-venv
python3 -m venv --system-site-packages ~/trainmate-vision-venv
source ~/trainmate-vision-venv/bin/activate
pip install -r requirements.txt
```

Copy this `raspberry_pi` directory to the Pi. On the Django host, create a
device once and copy the printed token directly to the Pi:

```bash
python manage.py create_vision_device --name mixon-pi --owner Mixon
```

Run the agent on the Pi. Replace the host address with the LAN address of the
Mac that runs Django; do not expose this HTTP endpoint to the public internet.

```bash
export TRAINMATE_VISION_URL="http://MAC_LAN_IP:8000/api/vision/analyses/"
export TRAINMATE_VISION_TOKEN="paste-the-one-time-token-here"
export TRAINMATE_ACTIVITY="yoga"
# Optional: the defaults are 0.2 seconds (five frames per second) for the
# live browser preview and
# 60 seconds between saved progress checks.
export TRAINMATE_LIVE_FRAME_INTERVAL_SECONDS="0.2"
export TRAINMATE_SNAPSHOT_INTERVAL_SECONDS="60"
export TRAINMATE_SAVE_PROGRESS_SNAPSHOTS="1"
# For a Pi Desktop session with a physically connected touch display:
export TRAINMATE_LOCAL_PREVIEW="qtgl"
# Set these to the display's usable size if needed.
export TRAINMATE_PREVIEW_WIDTH="800"
export TRAINMATE_PREVIEW_HEIGHT="480"
python vision_agent.py
```

Open `http://127.0.0.1:8000/vision/` while logged in as the device owner. The
preview includes MediaPipe pose landmarks; it is held in temporary Django cache
for 15 seconds and is not stored. A saved progress frame is available only to
the owner in their movement-check history. Set
`TRAINMATE_SAVE_PROGRESS_SNAPSHOTS=0` to keep live preview and pose metrics but
not store progress images.

### Local touchscreen preview

When Raspberry Pi Desktop is running on a physically connected display, set
`TRAINMATE_LOCAL_PREVIEW=qtgl`. Picamera2 opens a GPU-accelerated local camera
window while the same agent continues to provide the browser preview and
minute-by-minute progress records. If the device reports an EGL/OpenGL error,
use `TRAINMATE_LOCAL_PREVIEW=qt` instead; this is a software-rendered fallback
that is appropriate for a 960×540 preview on Raspberry Pi 5. Run the agent from
a terminal opened on the Pi Desktop, not an SSH-only terminal, so the preview
can use that display. On Raspberry Pi OS Bookworm the agent automatically
selects Qt's Wayland backend for this preview.
The same MediaPipe pose landmarks are drawn as a transparent overlay on the
local camera image, updated up to 10 times per second. Adjust
`TRAINMATE_LOCAL_OVERLAY_INTERVAL_SECONDS` if needed; the default is `0.1`.

For a production deployment, use HTTPS, a private network or VPN, a dedicated
per-device token rotation process, and a systemd service instead of a terminal
session.

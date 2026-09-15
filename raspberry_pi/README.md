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
# Optional: the defaults are 1 second for the live browser preview and
# 60 seconds between saved progress checks.
export TRAINMATE_LIVE_FRAME_INTERVAL_SECONDS="1"
export TRAINMATE_SNAPSHOT_INTERVAL_SECONDS="60"
export TRAINMATE_SAVE_PROGRESS_SNAPSHOTS="1"
python vision_agent.py
```

Open `http://127.0.0.1:8000/vision/` while logged in as the device owner. The
preview includes MediaPipe pose landmarks; it is held in temporary Django cache
for 15 seconds and is not stored. A saved progress frame is available only to
the owner in their movement-check history. Set
`TRAINMATE_SAVE_PROGRESS_SNAPSHOTS=0` to keep live preview and pose metrics but
not store progress images.

For a production deployment, use HTTPS, a private network or VPN, a dedicated
per-device token rotation process, and a systemd service instead of a terminal
session.

"""Stream a private annotated preview and save minute-by-minute Pi pose checks.

Raw camera frames remain on the Raspberry Pi. A short-lived JPEG is sent for
the owner's authenticated Vision Lab preview; one opt-in progress JPEG and its
pose metadata are saved at the configured interval.
"""

import json
import os
import time
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import cv2
import mediapipe as mp
import numpy as np
from picamera2 import Picamera2, Preview


SERVER_URL = os.environ.get("TRAINMATE_VISION_URL", "")
DEVICE_TOKEN = os.environ.get("TRAINMATE_VISION_TOKEN", "")
ACTIVITY = os.environ.get("TRAINMATE_ACTIVITY", "yoga")
SNAPSHOT_INTERVAL_SECONDS = float(os.environ.get("TRAINMATE_SNAPSHOT_INTERVAL_SECONDS", "60"))
LIVE_FRAME_INTERVAL_SECONDS = float(os.environ.get("TRAINMATE_LIVE_FRAME_INTERVAL_SECONDS", "0.2"))
SAVE_PROGRESS_SNAPSHOTS = os.environ.get(
    "TRAINMATE_SAVE_PROGRESS_SNAPSHOTS", "1",
).lower() in {"1", "true", "yes"}
LOCAL_PREVIEW = os.environ.get("TRAINMATE_LOCAL_PREVIEW", "off").lower()
PREVIEW_WIDTH = int(os.environ.get("TRAINMATE_PREVIEW_WIDTH", "800"))
PREVIEW_HEIGHT = int(os.environ.get("TRAINMATE_PREVIEW_HEIGHT", "480"))
LOCAL_OVERLAY_INTERVAL_SECONDS = float(
    os.environ.get("TRAINMATE_LOCAL_OVERLAY_INTERVAL_SECONDS", "0.1"),
)
LANDMARKS = {
    "left_shoulder": 11, "right_shoulder": 12, "left_hip": 23,
    "right_hip": 24, "left_knee": 25, "right_knee": 26,
    "left_ankle": 27, "right_ankle": 28,
}


def validate_configuration() -> None:
    if not SERVER_URL.startswith(("http://", "https://")) or not SERVER_URL.endswith("analyses/"):
        raise ValueError("TRAINMATE_VISION_URL must end with /api/vision/analyses/.")
    if not DEVICE_TOKEN:
        raise ValueError("Set TRAINMATE_VISION_TOKEN to the device token.")
    if SNAPSHOT_INTERVAL_SECONDS < 10 or LIVE_FRAME_INTERVAL_SECONDS < 0.1:
        raise ValueError("Use a snapshot interval of at least 10 s and live interval of at least 0.1 s.")
    if LOCAL_PREVIEW not in {"off", "qtgl", "qt"}:
        raise ValueError("TRAINMATE_LOCAL_PREVIEW must be 'off', 'qtgl' or 'qt'.")
    if LOCAL_OVERLAY_INTERVAL_SECONDS < 0.05:
        raise ValueError("TRAINMATE_LOCAL_OVERLAY_INTERVAL_SECONDS must be at least 0.05.")


def endpoint(path: str) -> str:
    return f"{SERVER_URL.rsplit('analyses/', 1)[0]}{path.lstrip('/')}"


def pose_payload(result: Any) -> dict[str, Any]:
    if not result.pose_landmarks:
        return {
            "activity": ACTIVITY,
            "pose_score": 0,
            "feedback": "No full-body pose detected. Step back so the camera can see you.",
            "landmarks": {},
        }
    landmarks = result.pose_landmarks.landmark
    selected = {
        name: {
            "x": round(landmarks[index].x, 4),
            "y": round(landmarks[index].y, 4),
            "visibility": round(landmarks[index].visibility, 4),
        }
        for name, index in LANDMARKS.items()
    }
    score = round(sum(item["visibility"] for item in selected.values()) / len(selected) * 100, 2)
    feedback = (
        "Pose captured clearly. Keep your full body inside the frame."
        if score >= 75 else
        "Move back or improve the lighting so your full body is visible."
    )
    return {
        "activity": ACTIVITY,
        "pose_score": score,
        "feedback": feedback,
        "landmarks": selected,
    }


def post(url: str, body: bytes, content_type: str) -> bytes:
    upload = Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {DEVICE_TOKEN}", "Content-Type": content_type},
    )
    with urlopen(upload, timeout=10) as response:
        if response.status not in {200, 201, 204}:
            raise URLError(f"TrainMate returned HTTP {response.status}")
        return response.read()


def post_result(payload: dict[str, Any]) -> int:
    response = post(SERVER_URL, json.dumps(payload).encode(), "application/json")
    analysis_id = int(json.loads(response.decode())["id"])
    print(f"TrainMate accepted analysis {analysis_id}")
    return analysis_id


def post_live_frame(jpeg: bytes) -> None:
    post(endpoint("live-frame/"), jpeg, "image/jpeg")


def post_snapshot(analysis_id: int, jpeg: bytes) -> None:
    post(endpoint(f"analyses/{analysis_id}/snapshot/"), jpeg, "image/jpeg")
    print(f"TrainMate saved progress snapshot for analysis {analysis_id}")


def annotated_jpeg(frame: Any, result: Any) -> bytes:
    annotated = frame.copy()
    if result.pose_landmarks:
        mp.solutions.drawing_utils.draw_landmarks(
            annotated, result.pose_landmarks, mp.solutions.pose.POSE_CONNECTIONS,
        )
    success, encoded = cv2.imencode(
        ".jpg", cv2.cvtColor(annotated, cv2.COLOR_RGB2BGR),
        [int(cv2.IMWRITE_JPEG_QUALITY), 82],
    )
    if not success:
        raise RuntimeError("Could not encode a camera frame as JPEG.")
    return encoded.tobytes()


def local_pose_overlay(result: Any, width: int, height: int) -> np.ndarray:
    """Build a transparent skeleton layer for the local Picamera2 preview."""
    overlay = np.zeros((height, width, 4), dtype=np.uint8)
    if not result.pose_landmarks:
        return overlay
    landmarks = result.pose_landmarks.landmark
    for start, end in mp.solutions.pose.POSE_CONNECTIONS:
        first, second = landmarks[start], landmarks[end]
        if min(first.visibility, second.visibility) < 0.35:
            continue
        cv2.line(
            overlay, (int(first.x * width), int(first.y * height)),
            (int(second.x * width), int(second.y * height)),
            (80, 235, 115, 230), 3,
        )
    for landmark in landmarks:
        if landmark.visibility >= 0.35:
            cv2.circle(
                overlay, (int(landmark.x * width), int(landmark.y * height)),
                5, (255, 245, 120, 255), -1,
            )
    return overlay


def main() -> None:
    validate_configuration()
    camera = Picamera2()
    frame_width, frame_height = 960, 540
    camera.configure(camera.create_preview_configuration(
        main={"size": (frame_width, frame_height), "format": "RGB888"},
        buffer_count=4,
    ))
    if LOCAL_PREVIEW != "off":
        # Raspberry Pi OS Bookworm's Desktop uses Wayland; explicitly selecting
        # it avoids an X11/xcb conflict introduced by the venv's OpenCV package.
        os.environ.setdefault("QT_QPA_PLATFORM", "wayland")
        camera.start_preview(
            Preview.QTGL if LOCAL_PREVIEW == "qtgl" else Preview.QT,
            x=0, y=0, width=PREVIEW_WIDTH, height=PREVIEW_HEIGHT,
        )
        print(f"Local touchscreen camera preview started ({LOCAL_PREVIEW}).")
    camera.start()
    pose = mp.solutions.pose.Pose(
        static_image_mode=False, model_complexity=0, enable_segmentation=False,
        min_detection_confidence=0.6, min_tracking_confidence=0.6,
    )
    next_live_frame = next_snapshot = next_overlay = 0.0
    try:
        while True:
            frame = camera.capture_array()
            result = pose.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            now = time.monotonic()
            if LOCAL_PREVIEW != "off" and now >= next_overlay:
                camera.set_overlay(local_pose_overlay(result, frame_width, frame_height))
                next_overlay = now + LOCAL_OVERLAY_INTERVAL_SECONDS
            jpeg: bytes | None = None
            if now >= next_live_frame or now >= next_snapshot:
                jpeg = annotated_jpeg(frame, result)
            if now >= next_live_frame:
                try:
                    post_live_frame(jpeg or b"")
                except URLError as error:
                    print(f"Could not send live frame: {error}")
                next_live_frame = now + LIVE_FRAME_INTERVAL_SECONDS
            if now >= next_snapshot:
                try:
                    analysis_id = post_result(pose_payload(result))
                    if SAVE_PROGRESS_SNAPSHOTS:
                        post_snapshot(analysis_id, jpeg or b"")
                except (URLError, KeyError, ValueError) as error:
                    print(f"Could not save movement check: {error}")
                next_snapshot = now + SNAPSHOT_INTERVAL_SECONDS
            time.sleep(0.02)
    finally:
        pose.close()
        camera.stop()
        if LOCAL_PREVIEW != "off":
            camera.stop_preview()


if __name__ == "__main__":
    main()

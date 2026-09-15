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
from picamera2 import Picamera2


SERVER_URL = os.environ.get("TRAINMATE_VISION_URL", "")
DEVICE_TOKEN = os.environ.get("TRAINMATE_VISION_TOKEN", "")
ACTIVITY = os.environ.get("TRAINMATE_ACTIVITY", "yoga")
SNAPSHOT_INTERVAL_SECONDS = float(os.environ.get("TRAINMATE_SNAPSHOT_INTERVAL_SECONDS", "60"))
LIVE_FRAME_INTERVAL_SECONDS = float(os.environ.get("TRAINMATE_LIVE_FRAME_INTERVAL_SECONDS", "1"))
SAVE_PROGRESS_SNAPSHOTS = os.environ.get(
    "TRAINMATE_SAVE_PROGRESS_SNAPSHOTS", "1",
).lower() in {"1", "true", "yes"}
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
    if SNAPSHOT_INTERVAL_SECONDS < 10 or LIVE_FRAME_INTERVAL_SECONDS < 0.5:
        raise ValueError("Use a snapshot interval of at least 10 s and live interval of at least 0.5 s.")


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


def main() -> None:
    validate_configuration()
    camera = Picamera2()
    camera.configure(camera.create_preview_configuration(
        main={"size": (960, 540), "format": "RGB888"},
    ))
    camera.start()
    pose = mp.solutions.pose.Pose(
        static_image_mode=False, model_complexity=0, enable_segmentation=False,
        min_detection_confidence=0.6, min_tracking_confidence=0.6,
    )
    next_live_frame = next_snapshot = 0.0
    try:
        while True:
            frame = camera.capture_array()
            result = pose.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            now = time.monotonic()
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


if __name__ == "__main__":
    main()

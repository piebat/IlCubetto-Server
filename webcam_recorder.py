"""Webcam circular buffer that saves the last N seconds of video whenever a
rotation error is detected, so the tester's hand movement around the
mistake can be reviewed afterwards.

Runs in its own daemon thread, started by app.py alongside the MQTT thread.
It listens on the same `broadcaster` the browsers' SSE connections use, and
reacts to "progress" events whose `last_live_result.kind` is one of
"wrong_move", "lost_reading", "released" (see session_recorder.py) -- the
three outcomes that `/` already shows live in the "last-result" panel.

Clips are saved next to the active tester's own logs (logs/<user_id>/clips/)
when a session is active, or under logs/clips/ otherwise, as
clip_<timestamp>_<kind>.mp4.

Needs opencv-python (`cv2`) and a camera accessible to the process (index 0
by default, overridable via CUBETTO_CAMERA_INDEX). If the camera cannot be
opened, the thread logs a warning and the rest of the app keeps working
without video capture.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime

import cv2

import current_session
from broadcaster import broadcaster
from session_recorder import LOG_DIR

log = logging.getLogger("webcam_recorder")

CAMERA_INDEX = int(os.environ.get("CUBETTO_CAMERA_INDEX", "0"))
BUFFER_SECONDS = float(os.environ.get("CUBETTO_CLIP_BUFFER_SECONDS", "10"))
TARGET_FPS = float(os.environ.get("CUBETTO_CLIP_FPS", "20"))

TRIGGER_KINDS = {"wrong_move", "lost_reading", "released"}

FOURCC = cv2.VideoWriter_fourcc(*"mp4v")  # pylint: disable=no-member (native cv2 binding, exists at runtime)

_SUBTITLE_FONT = cv2.FONT_HERSHEY_SIMPLEX
_SUBTITLE_SCALE = 0.6
_SUBTITLE_THICKNESS = 1
_SUBTITLE_MARGIN = 10


def _burn_subtitle(frame, text: str):
    """Returns a copy of `frame` with `text` drawn as a single line near the
    bottom, on a semi-opaque black bar, so it reads over any footage. The
    font scale shrinks if needed so the line always fits the frame width
    (webcam resolutions can be much narrower than a long diagnostic line)."""
    frame = frame.copy()
    h, w = frame.shape[:2]
    max_text_w = w - 2 * _SUBTITLE_MARGIN
    scale = _SUBTITLE_SCALE
    (text_w, text_h), baseline = cv2.getTextSize(
        text, _SUBTITLE_FONT, scale, _SUBTITLE_THICKNESS)
    if text_w > max_text_w > 0:
        scale *= max_text_w / text_w
        (text_w, text_h), baseline = cv2.getTextSize(
            text, _SUBTITLE_FONT, scale, _SUBTITLE_THICKNESS)
    bar_h = text_h + baseline + 2 * _SUBTITLE_MARGIN
    bar_top = max(0, h - bar_h)

    overlay = frame.copy()
    cv2.rectangle(overlay, (0, bar_top), (w, h), (0, 0, 0), thickness=-1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, dst=frame)

    text_x = max(0, (w - text_w) // 2)
    text_y = h - _SUBTITLE_MARGIN - baseline
    cv2.putText(frame, text, (text_x, text_y), _SUBTITLE_FONT, scale,
                (255, 255, 255), _SUBTITLE_THICKNESS, cv2.LINE_AA)
    return frame


class WebcamRecorder:
    """Owns the capture device and a ring buffer of the most recent frames.
    Thread-safe: frames are appended by the capture loop while a save can be
    triggered concurrently from the broadcaster-listening loop (started as a
    second thread by `start()`)."""

    MAX_CONSECUTIVE_FAILURES = 30  # ~3s of failed reads at 0.1s backoff

    def __init__(self, camera_index=CAMERA_INDEX, buffer_seconds=BUFFER_SECONDS,
                 target_fps=TARGET_FPS):
        self.camera_index = camera_index
        self.target_fps = target_fps
        self._maxlen = max(1, int(buffer_seconds * target_fps))
        self._buffer = deque(maxlen=self._maxlen)  # (timestamp, frame) pairs
        self._lock = threading.Lock()
        self._cap = None
        self._frame_size = None

    def _open(self) -> bool:
        cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            log.warning("Could not open camera index %s", self.camera_index)
            return False
        self._cap = cap
        return True

    def capture_loop(self):
        """Pulls frames continuously, at ~target_fps, into the ring buffer.
        Call in its own thread; never returns (retries the camera on
        failure rather than giving up, so a camera plugged in later still
        gets picked up).

        A handful of consecutive failed reads right after opening the
        device is normal (e.g. macOS/AVFoundation needs a short warm-up
        before the first real frame) and is NOT treated as a broken camera;
        only MAX_CONSECUTIVE_FAILURES in a row, at any point, triggers a
        reopen."""
        period = 1.0 / self.target_fps
        consecutive_failures = 0
        while True:
            if self._cap is None and not self._open():
                time.sleep(5.0)
                continue
            ok, frame = self._cap.read()
            if not ok:
                consecutive_failures += 1
                if consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                    log.warning("Camera read failed %d times in a row, reopening",
                                consecutive_failures)
                    self._cap.release()
                    self._cap = None
                    consecutive_failures = 0
                    time.sleep(1.0)
                else:
                    time.sleep(0.1)
                continue
            consecutive_failures = 0
            self._frame_size = (frame.shape[1], frame.shape[0])
            with self._lock:
                self._buffer.append((time.time(), frame))
            time.sleep(period)

    def save_clip(self, out_path: str, subtitle: str | None = None) -> bool:
        """Writes the buffered frames (oldest to newest) as an mp4. Returns
        False if there is nothing to save yet (e.g. camera still warming up
        or unavailable).

        If `subtitle` is given, it is burned into every frame (not a
        separate .srt track) as a single line near the bottom, on a
        semi-opaque bar so it stays legible against any footage."""
        with self._lock:
            frames = [f for _, f in self._buffer]
        if not frames or self._frame_size is None:
            log.warning("No buffered frames to save for %s", out_path)
            return False

        if subtitle:
            frames = [_burn_subtitle(frame, subtitle) for frame in frames]

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        writer = cv2.VideoWriter(out_path, FOURCC, self.target_fps, self._frame_size)
        try:
            for frame in frames:
                writer.write(frame)
        finally:
            writer.release()
        log.info("Saved %d-frame clip to %s", len(frames), out_path)
        return True


def _clip_dir() -> str:
    recorder = current_session.get()
    if recorder is not None:
        return os.path.join(recorder.user_dir, "clips")
    return os.path.join(LOG_DIR, "clips")


def _subtitle_for(live_result: dict) -> str:
    """One-line summary burned into the saved clip, built from the same
    `live_result` dict session_recorder.py already reports (see its
    `_live_result`/`record` for the "kind"/"step"/"expected"/"detected"
    fields it can contain)."""
    kind = live_result.get("kind")
    step = live_result.get("step")
    step_part = f"step {step}" if step is not None else "step ?"
    if kind == "wrong_move":
        return (f"wrong move -- {step_part} -- "
                f"expected {live_result.get('expected')}, detected {live_result.get('detected')}")
    if kind == "lost_reading":
        return f"reading lost -- {step_part} -- recalibrating"
    if kind == "released":
        return f"cube released -- {step_part} -- recalibrating"
    return f"{kind} -- {step_part}"


def _listen_loop(cam: WebcamRecorder):
    """Watches the broadcaster for the same "progress" events the dashboard
    receives, and saves a clip whenever a rotation error is signalled."""
    q = broadcaster.listen()
    try:
        while True:
            data = q.get()
            if data.get("type") != "progress":
                continue
            live_result = data.get("last_live_result")
            kind = live_result.get("kind") if live_result else None
            if kind not in TRIGGER_KINDS:
                continue
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            out_path = os.path.join(_clip_dir(), f"clip_{stamp}_{kind}.mp4")
            cam.save_clip(out_path, subtitle=_subtitle_for(live_result))
    finally:
        broadcaster.unlisten(q)


def start_webcam_recorder_thread():
    """Starts the capture loop and the trigger-listening loop as daemon
    threads. Safe to call even if no camera is attached: failures are
    logged, not raised, so the rest of the app is unaffected."""
    cam = WebcamRecorder()
    threading.Thread(target=cam.capture_loop, daemon=True).start()
    threading.Thread(target=_listen_loop, args=(cam,), daemon=True).start()
    return cam

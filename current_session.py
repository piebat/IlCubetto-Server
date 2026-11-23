"""Holds the SessionRecorder for the tester currently being tested, if any.
A thin, thread-safe indirection so app.py's routes and mqtt_client.py's
event handler always see the same active session without importing a fixed
module-level singleton."""
import threading

_lock = threading.Lock()
_current = None


def get():
    with _lock:
        return _current


def set(recorder):
    global _current
    with _lock:
        _current = recorder

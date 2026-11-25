"""Estimates the offset between the ESP32's clock (whatever `millis()`-based
value it stamps its own publishes with, as the optional "t0" field -- see
mqtt_client._parse_t0) and the server's `time.time()`, so a firmware
timestamp can be converted to server wall-clock time and compared with the
server-side t1/t2 timestamps.

The firmware clock is not NTP-synced to the server, so the two cannot be
compared directly. Standard NTP-style trick: the server publishes a ping
with its own send time, the firmware echoes the ping id back as fast as it
can on the pong topic, and the server measures the round trip. Assuming the
publish delay is roughly symmetric in both directions, the firmware clock
read "now" at the midpoint of the round trip, i.e.:

    offset = t0_firmware_at_pong_echo - (t_sent + rtt / 2)

`t0_firmware_at_pong_echo` is not actually available (the firmware only
echoes the ping id, not its own clock), so the offset is instead estimated
the other way around, from pings where the firmware DOES tag its pong with
its own clock reading (optional "t0" on the pong payload too, same field
as on touch events). Where absent, only the RTT is tracked, which is
enough to report an uncertainty bound even without an offset estimate.

Keeps a rolling median of the last N measurements rather than the latest
one, since individual pings can be delayed by unrelated traffic.
"""
import collections
import json
import threading
import time

_MAX_SAMPLES = 20


class ClockSync:
    def __init__(self, max_samples: int = _MAX_SAMPLES):
        self._lock = threading.Lock()
        self._rtts = collections.deque(maxlen=max_samples)
        self._offsets = collections.deque(maxlen=max_samples)
        self._pending = {}  # ping_id -> t_sent

    def next_ping(self) -> dict:
        """Payload for a new ping, to publish on the ping topic."""
        with self._lock:
            ping_id = int(time.time() * 1000) & 0xFFFFFFFF
            self._pending[ping_id] = time.time()
        return {"ping_id": ping_id, "t_sent": self._pending[ping_id]}

    def on_pong(self, payload: bytes):
        """Call with the raw payload received on the pong topic:
        {"ping_id": <id>} or {"ping_id": <id>, "t0": <firmware clock ms>}."""
        t_recv = time.time()
        try:
            obj = json.loads(payload.decode("utf-8", errors="replace"))
            ping_id = int(obj["ping_id"])
        except Exception:
            return

        with self._lock:
            t_sent = self._pending.pop(ping_id, None)
            if t_sent is None:
                return
            rtt = t_recv - t_sent
            self._rtts.append(rtt)

            t0 = obj.get("t0")
            if t0 is not None:
                try:
                    # the firmware clock at echo time, assumed (by the
                    # symmetric-delay assumption) to equal server time at
                    # the midpoint of the round trip.
                    midpoint = t_sent + rtt / 2
                    self._offsets.append(midpoint - float(t0) / 1000.0)
                except (TypeError, ValueError):
                    pass

    def rtt_ms(self):
        """Current median round-trip time, in milliseconds, or None if no
        ping has completed yet."""
        with self._lock:
            if not self._rtts:
                return None
            return _median(self._rtts) * 1000.0

    def offset_seconds(self):
        """server_time ~= firmware_time_seconds + offset. None if no pong
        has carried a firmware clock reading yet (RTT-only mode)."""
        with self._lock:
            if not self._offsets:
                return None
            return _median(self._offsets)

    def to_server_time(self, firmware_t0_ms: float):
        """Converts a firmware "t0" (ms, its own clock) to server
        `time.time()` time, or None if no offset estimate is available yet."""
        offset = self.offset_seconds()
        if offset is None or firmware_t0_ms is None:
            return None
        return firmware_t0_ms / 1000.0 + offset


def _median(values):
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


clock_sync = ClockSync()

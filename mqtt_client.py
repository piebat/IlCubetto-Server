"""MQTT client (over WebSocket) that reads the 6-bit bitmask from the cube's
topic, runs it through the CubeTracker and publishes every event on the
broadcaster shared with the Flask app.

NOTE: not tested live in this environment (no network access to the broker
during development) -- it needs to be verified locally. If the websocket
connection fails, the first thing to suspect is BROKER_WS_PATH (paho-mqtt
default "/mqtt": if your broker (e.g. Mosquitto with websockets) uses a
different path, change it below).
"""
import json
import logging
import os
import threading
import time

import paho.mqtt.client as mqtt

import current_session
from cube_algorithm import bitmask_to_faces, CubeTracker
from broadcaster import broadcaster
from session_recorder import LOG_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mqtt_client")

# besides stdout, the log also goes to a file: without this handler the
# output only lives in the terminal scrollback and is lost on exit.
_fh = logging.FileHandler(os.path.join(LOG_DIR, "cubetto.log"), encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.getLogger().addHandler(_fh)

# --- broker configuration ---------------------------------------------------
BROKER_HOST = "REDACTED_BROKER_HOST"
BROKER_PORT = 7080
BROKER_WS_PATH = "/mqtt"      # change here if the broker uses another path
BROKER_CA_CERT = "REDACTED_CA_CERT_PATH"
BROKER_USERNAME = "REDACTED_USERNAME"
BROKER_PASSWORD = "REDACTED_PASSWORD"
TOPIC = "cubetto/touch"
VIBRATION_TOPIC = "cubetto/vibrations"   # haptic feedback towards the cube
VIBRATION_PAYLOAD = "S"                   # published after every valid reading
# ----------------------------------------------------------------------------

tracker = CubeTracker()


def calibrate_tracker(expected_move: str, previous_state: tuple) -> bool:
    """Calibrates the real tracker identically to the active session's
    shadow tracker, so both agree on the cube's internal orientation. Passed
    to SessionRecorder as its on_calibrate callback."""
    return tracker.calibrate(expected_move, previous_state)


def _parse_bitmask(payload: bytes):
    """Interprets the payload as an integer 0-63 (6-bit bitmask).
    Supports plain text ("9"), hexadecimal ("0x09") or binary ("0b001001").
    Returns None if it cannot be interpreted (and publishes the raw payload
    for debugging)."""
    text = payload.decode("utf-8", errors="replace").strip()
    try:
        return int(text, 0) if text.lower().startswith(("0x", "0b")) else int(text)
    except ValueError:
        try:
            # fallback: it might be JSON such as {"bitmask": 9}
            obj = json.loads(text)
            if isinstance(obj, dict) and "bitmask" in obj:
                return int(obj["bitmask"])
            if isinstance(obj, int):
                return obj
        except Exception:
            pass
    return None


def _on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0 or reason_code == "Success":
        log.info("Connected to the MQTT broker, subscribing to '%s'", TOPIC)
        client.subscribe(TOPIC)
        broadcaster.publish({"type": "status", "status": "connected"})
    else:
        log.error("Connection failed: %s", reason_code)
        broadcaster.publish({"type": "status", "status": f"connection error: {reason_code}"})


def _on_disconnect(client, userdata, *args):
    log.warning("Disconnected from the broker, retrying automatically...")
    broadcaster.publish({"type": "status", "status": "disconnected, reconnecting"})


def _on_message(client, userdata, msg):
    bitmask = _parse_bitmask(msg.payload)
    recorder = current_session.get()
    if bitmask is None:
        log.warning("Uninterpretable payload: %r", msg.payload)
        err = {"type": "raw_error", "timestamp": time.time(), "payload": repr(msg.payload)}
        if recorder is not None:
            recorder.record(err)
        broadcaster.publish(err)
        return

    faces = bitmask_to_faces(bitmask)
    ev = tracker.update(faces)

    if ev.ambiguous:
        # A step is never skipped on purpose during the protocol, so an
        # ambiguous reading means the sensors missed an intermediate reading,
        # not that several moves were performed. It is reported as an INVALID
        # reading: the step is not recorded and the operator repeats the move.
        #
        # The tracker is deliberately NOT reset here. Real sessions contain a
        # burst of invalid readings between one grip and the next (fingers
        # lifting off), so a reset would be followed by a re-lock on an
        # arbitrary reading mid-gesture, picking the hidden-bit twin at
        # random and scrambling the polarity from then on. Keeping the
        # tracker's own estimate is strictly better than re-guessing it.
        log.warning("Ambiguous reading discarded (%s): repeat the move", ev.alt_moves)
        ev.ok = False
        ev.reason = "ambiguous reading (a reading was missed) -- repeat the move"
        ev.move = None
        ev.move_desc = None

    if ev.ok and not ev.ambiguous:  # only if the reading is valid and unambiguous
        # valid reading (pinch+1 recognised) -> haptic feedback to the cube,
        # confirming the completion of the step just read.
        try:
            client.publish(VIBRATION_TOPIC, VIBRATION_PAYLOAD)
        except Exception as exc:
            log.warning("Failed to publish haptic feedback: %s", exc)

    data = {
        "type": "event",
        "timestamp": time.time(),
        "bitmask": bitmask,
        "bitmask_bin": format(bitmask, "06b"),
        "faces": sorted(faces),
        "ok": ev.ok,
        "reason": ev.reason,
        "state": ev.state,
        "move": ev.move,
        "move_desc": ev.move_desc,
        "alt_moves": ev.alt_moves,
        "distance": ev.distance,
        "ambiguous": ev.ambiguous,
    }
    log.info("evt: %s", data)
    if recorder is not None:
        # write to file + fill in the test protocol
        recorder.record(data)
        broadcaster.publish(data)
        # protocol progress towards the test panel
        broadcaster.publish({"type": "progress", **recorder.progress()})
    else:
        broadcaster.publish(data)


def start_mqtt_thread():
    def run():
        client = mqtt.Client(client_id="", transport="websockets", protocol=mqtt.MQTTv311)
        client.ws_set_options(path=BROKER_WS_PATH)
        client.tls_set(ca_certs=BROKER_CA_CERT)
        client.username_pw_set(BROKER_USERNAME, BROKER_PASSWORD)
        client.on_connect = _on_connect
        client.on_disconnect = _on_disconnect
        client.on_message = _on_message

        while True:
            try:
                log.info("Connecting to wss://%s:%s%s ...", BROKER_HOST, BROKER_PORT, BROKER_WS_PATH)
                client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
                client.loop_forever(retry_first_connection=True)
            except Exception as exc:
                log.error("Connection error: %s -- retrying in 3s", exc)
                broadcaster.publish({"type": "status", "status": f"error: {exc}"})
                time.sleep(3)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t

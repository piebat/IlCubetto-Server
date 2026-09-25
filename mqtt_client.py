"""Client MQTT (via WebSocket) che legge il bitmask a 6 bit dal topic del
cubo, lo fa passare per il CubeTracker e pubblica ogni evento sul
broadcaster condiviso con l'app Flask.

NOTA: non testato dal vivo in questo ambiente (nessun accesso di rete al
broker durante lo sviluppo) -- va verificato in locale. Se la connessione
websocket fallisce, il primo sospetto e' BROKER_WS_PATH (default paho-mqtt
"/mqtt": se il vostro broker (es. Mosquitto con websockets) usa un path
diverso, cambiatelo qui sotto).
"""
import json
import logging
import threading
import time

import paho.mqtt.client as mqtt

from cube_algorithm import bitmask_to_faces, CubeTracker
from broadcaster import broadcaster

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mqtt_client")

# --- configurazione broker -------------------------------------------------
BROKER_HOST = "REDACTED_BROKER_HOST"
BROKER_PORT = 7080
BROKER_WS_PATH = "/mqtt"      # cambiare qui se il broker usa un altro path
TOPIC = "cubetto/touch"
# ----------------------------------------------------------------------------

tracker = CubeTracker()


def _parse_bitmask(payload: bytes):
    """Interpreta il payload come intero 0-63 (bitmask a 6 bit).
    Supporta testo semplice ("9"), esadecimale ("0x09") o binario ("0b001001").
    Ritorna None se non interpretabile (e pubblica il payload grezzo per debug)."""
    text = payload.decode("utf-8", errors="replace").strip()
    try:
        return int(text, 0) if text.lower().startswith(("0x", "0b")) else int(text)
    except ValueError:
        try:
            # fallback: magari e' un JSON tipo {"bitmask": 9}
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
        log.info("Connesso al broker MQTT, sottoscrizione a '%s'", TOPIC)
        client.subscribe(TOPIC)
        broadcaster.publish({"type": "status", "status": "connesso"})
    else:
        log.error("Connessione fallita: %s", reason_code)
        broadcaster.publish({"type": "status", "status": f"errore connessione: {reason_code}"})


def _on_disconnect(client, userdata, *args):
    log.warning("Disconnesso dal broker, riprovo automaticamente...")
    broadcaster.publish({"type": "status", "status": "disconnesso, riconnessione in corso"})


def _on_message(client, userdata, msg):
    bitmask = _parse_bitmask(msg.payload)
    if bitmask is None:
        log.warning("Payload non interpretabile: %r", msg.payload)
        broadcaster.publish({"type": "raw_error", "payload": repr(msg.payload)})
        return

    faces = bitmask_to_faces(bitmask)
    ev = tracker.update(faces)

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
        "distance": ev.distance,
        "ambiguous": ev.ambiguous,
    }
    log.info("evt: %s", data)
    broadcaster.publish(data)


def start_mqtt_thread():
    def run():
        client = mqtt.Client(client_id="", transport="websockets", protocol=mqtt.MQTTv311)
        client.ws_set_options(path=BROKER_WS_PATH)
        client.on_connect = _on_connect
        client.on_disconnect = _on_disconnect
        client.on_message = _on_message

        while True:
            try:
                log.info("Connessione a ws://%s:%s%s ...", BROKER_HOST, BROKER_PORT, BROKER_WS_PATH)
                client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
                client.loop_forever(retry_first_connection=True)
            except Exception as exc:
                log.error("Errore di connessione: %s -- ritento tra 3s", exc)
                broadcaster.publish({"type": "status", "status": f"errore: {exc}"})
                time.sleep(3)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t

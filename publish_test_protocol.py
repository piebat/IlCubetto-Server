"""Publishes, over MQTT, a full realistic simulation of all 10 test-session
slots (120 combinations total, see combination_plan.py) so the live Flask
app/dashboard can be exercised end to end without a physical cube or human
tester.

For each of the 10 slots this replays exactly what a real tester's hands
would produce: an arbitrary anchor reading, the tip_U+ calibration move,
however many elementary moves are needed to reach the slot's required
starting orientation (via the same BFS shortest-path logic
session_recorder.py uses), then the 12 planned moves of the slot in
sequence. Between slots a fresh arbitrary anchor is used again, exactly as
if a new tester had picked up the cube, so the whole run also exercises
calibration repeatedly rather than only once.

This talks to the SAME broker/topic as mqtt_client.py (imported from there,
not duplicated) -- so it is meant to run against a live app.py instance
subscribed to 'cubetto/touch', with /setup -> /session/new having just been
used to start a session for each slot in turn (the script does not drive
the Flask app itself, only the MQTT side the cube would).

Run:  python3 publish_test_protocol.py [--delay 0.3] [--slots 0-9]

See also validate_algorithm.py, which checks the same 120 combinations
directly against cube_algorithm.py (no MQTT, no server) -- the two together
cover both the algorithm in isolation and its behaviour as actually driven
by the running server.
"""
import argparse
import sys
import time

import paho.mqtt.client as mqtt

import cube_algorithm as ca
from combination_plan import N_SLOTS, build_protocol_rows
from mqtt_client import (BROKER_CA_CERT, BROKER_HOST, BROKER_PASSWORD,
                          BROKER_PORT, BROKER_USERNAME, BROKER_WS_PATH, TOPIC)

AXIS_FACES = {'A': (0, 3), 'B': (1, 2), 'C': (4, 5)}
CALIBRATION_MOVE = "tip_U+"

# any valid pinch+1 triple not tied to a specific orientation -- just needs
# to be a legal reading to anchor the very first calibration attempt.
ARBITRARY_ANCHOR_FACES = {0, 3, 1}


def faces_for_state(axis: str, face: int) -> set[int]:
    return set(AXIS_FACES[axis]) | {face}


def bitmask_for_faces(faces: set[int]) -> int:
    return sum(1 << f for f in faces)


class GroundTruthCube:
    """Drives a real CubeTracker the same way a physical cube's sensor
    readings would, so every bitmask this script publishes corresponds to
    an actual elementary move from the previous one -- never an impossible
    teleport."""

    def __init__(self):
        self.tracker = ca.CubeTracker()

    def read(self, faces: set[int]) -> ca.Event:
        return self.tracker.update(faces)

    @property
    def id(self):
        return self.tracker._id


def publish_faces(client, faces: set[int], label: str, delay: float):
    bitmask = bitmask_for_faces(faces)
    client.publish(TOPIC, str(bitmask))
    print(f"  -> published bitmask={bitmask:06b} ({bitmask:2d})  faces={sorted(faces)}   [{label}]")
    time.sleep(delay)


def run_slot(client, slot: int, delay: float):
    rows = build_protocol_rows(slot)
    cube = GroundTruthCube()

    print(f"\n=== slot {slot} ===")

    # 1) arbitrary anchor + calibration move (tip_U+), exactly like a fresh
    #    tester picking up the cube at session start.
    cube.read(ARBITRARY_ANCHOR_FACES)
    publish_faces(client, ARBITRARY_ANCHOR_FACES, "anchor", delay)

    gid = cube.id
    _, nstate = ca._NEIGHBORS[gid][CALIBRATION_MOVE]
    cube.read(faces_for_state(*nstate))
    publish_faces(client, faces_for_state(*nstate), f"calibration ({CALIBRATION_MOVE})", delay)

    # 2) walk one elementary move at a time to the slot's required start
    #    (the same BFS shortest path session_recorder.py's position_hint
    #    uses), publishing every intermediate reading so nothing is skipped
    #    (a skipped reading would be read back as an ambiguous multi-jump).
    target_id = rows[0]["source_id"]
    while cube.id != target_id:
        move = ca._ALL_PAIRS_FIRSTSTEP[cube.id].get(target_id)
        _, nstate = ca._NEIGHBORS[cube.id][move]
        cube.read(faces_for_state(*nstate))
        publish_faces(client, faces_for_state(*nstate), f"positioning ({move})", delay)

    # 3) the 12 planned moves, in sequence (chained: no repositioning needed
    #    between them, by construction of combination_plan.py).
    for row in rows:
        move = row["move"]
        _, nstate = ca._NEIGHBORS[cube.id][move]
        cube.read(faces_for_state(*nstate))
        publish_faces(client, faces_for_state(*nstate), f"step {row['step']}: {move}", delay)


def parse_slot_range(spec: str) -> list[int]:
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(spec)]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--delay", type=float, default=0.3,
                     help="seconds between published readings (default 0.3)")
    ap.add_argument("--slots", type=str, default=f"0-{N_SLOTS - 1}",
                     help="slot or slot range to publish, e.g. '0-9' or '3' (default: all)")
    args = ap.parse_args()

    slots = parse_slot_range(args.slots)
    for s in slots:
        if not 0 <= s < N_SLOTS:
            print(f"slot out of range: {s} (must be 0-{N_SLOTS - 1})", file=sys.stderr)
            return 1

    client = mqtt.Client(client_id="", transport="websockets", protocol=mqtt.MQTTv311)
    client.ws_set_options(path=BROKER_WS_PATH)
    client.tls_set(ca_certs=BROKER_CA_CERT)
    client.username_pw_set(BROKER_USERNAME, BROKER_PASSWORD)

    print(f"Connecting to wss://{BROKER_HOST}:{BROKER_PORT}{BROKER_WS_PATH} ...")
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    client.loop_start()
    time.sleep(1)  # let the connection establish before publishing

    try:
        for slot in slots:
            run_slot(client, slot, args.delay)
    finally:
        client.loop_stop()
        client.disconnect()

    print(f"\nPublished {len(slots)} slot(s) x 12 moves = {len(slots) * 12} test moves "
          f"(plus calibration/positioning readings).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

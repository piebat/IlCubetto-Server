"""Publishes, over MQTT, a full realistic simulation of test sessions (each
one all 16 symbols of one of the N_ORDERS precomputed orders, see
symbol_plan.py) so the live Flask app/dashboard can be exercised end to end
without a physical cube or human tester.

For each order this replays exactly what a real tester's hands would
produce: an arbitrary anchor reading, the tip_U+ calibration move, then the
16 symbols (2 moves each) in sequence, chained -- no repositioning, since a
symbol just starts wherever the cube was left by the previous one. Between
orders a fresh arbitrary anchor is used again, exactly as if a new tester
had picked up the cube, so the whole run also exercises calibration
repeatedly rather than only once.

This talks to the SAME broker/topic as mqtt_client.py (imported from there,
not duplicated) -- so it is meant to run against a live app.py instance
subscribed to 'cubetto/touch', with /setup -> /session/new having just been
used to start a session for each order in turn (the script does not drive
the Flask app itself, only the MQTT side the cube would).

Run:  python3 publish_test_protocol.py [--delay 0.3] [--orders 0-11]

See also validate_algorithm.py, which checks the cube_algorithm.py
transitions directly (no MQTT, no server) -- the two together cover both
the algorithm in isolation and its behaviour as actually driven by the
running server.
"""
import argparse
import sys
import time

import paho.mqtt.client as mqtt

import cube_algorithm as ca
from symbol_plan import N_ORDERS, build_protocol_rows
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


def run_order(client, order_index: int, delay: float):
    rows = build_protocol_rows(order_index)
    cube = GroundTruthCube()

    print(f"\n=== order {order_index} ===")

    # 1) arbitrary anchor + calibration move (tip_U+), exactly like a fresh
    #    tester picking up the cube at session start.
    cube.read(ARBITRARY_ANCHOR_FACES)
    publish_faces(client, ARBITRARY_ANCHOR_FACES, "anchor", delay)

    gid = cube.id
    _, nstate = ca._NEIGHBORS[gid][CALIBRATION_MOVE]
    cube.read(faces_for_state(*nstate))
    publish_faces(client, faces_for_state(*nstate), f"calibration ({CALIBRATION_MOVE})", delay)

    # 2) the 16 symbols, in sequence (chained: each one just starts wherever
    #    the previous one left the cube -- no repositioning phase).
    for row in rows:
        for which, move in (("move1", row["move1"]), ("move2", row["move2"])):
            _, nstate = ca._NEIGHBORS[cube.id][move]
            cube.read(faces_for_state(*nstate))
            publish_faces(client, faces_for_state(*nstate),
                          f"step {row['step']} {which}: {move}", delay)


def parse_order_range(spec: str) -> list[int]:
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(spec)]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--delay", type=float, default=0.3,
                     help="seconds between published readings (default 0.3)")
    ap.add_argument("--orders", type=str, default=f"0-{N_ORDERS - 1}",
                     help="order or order range to publish, e.g. '0-11' or '3' (default: all)")
    args = ap.parse_args()

    orders = parse_order_range(args.orders)
    for o in orders:
        if not 0 <= o < N_ORDERS:
            print(f"order out of range: {o} (must be 0-{N_ORDERS - 1})", file=sys.stderr)
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
        for order_index in orders:
            run_order(client, order_index, args.delay)
    finally:
        client.loop_stop()
        client.disconnect()

    print(f"\nPublished {len(orders)} order(s) x 16 symbols = {len(orders) * 16} test symbols "
          f"(plus calibration readings).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

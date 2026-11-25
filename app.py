"""Flask app for verifying cube rotation recognition in real time. It reads
data from the MQTT broker (topic 'cubetto/tuoch') and shows it on a live
dashboard via Server-Sent Events (SSE).

Local start-up:
    pip install -r requirements.txt
    python app.py
then open http://127.0.0.1:5001
"""
import json
import os
from datetime import datetime

from flask import Flask, Response, jsonify, redirect, render_template, request, stream_with_context, url_for

import current_session
import cube_algorithm as ca
import user_registry
from broadcaster import broadcaster
from symbol_plan import build_protocol_rows
from generate_graph import build_graph
from mqtt_client import calibrate_tracker, start_mqtt_thread
from session_recorder import LOG_DIR, SessionRecorder

app = Flask(__name__)

_mqtt_thread_started = False


def _ensure_mqtt_started():
    global _mqtt_thread_started
    if not _mqtt_thread_started:
        start_mqtt_thread()
        _mqtt_thread_started = True


@app.route("/")
def index():
    _ensure_mqtt_started()
    recorder = current_session.get()
    if recorder is None:
        return redirect(url_for("setup"))
    return render_template("index.html", meta=recorder.meta)


@app.route("/setup")
def setup():
    round_, order_index, user_id = user_registry.peek_next()
    return render_template("setup.html", round=round_, slot=order_index, user_id=user_id)


@app.route("/session/new", methods=["POST"])
def session_new():
    age = request.form.get("age", "").strip()
    gender = request.form.get("gender", "").strip()
    notes = request.form.get("notes", "").strip()

    round_, order_index, user_id = user_registry.allocate_user()
    user_dir = os.path.join(LOG_DIR, user_id)
    os.makedirs(user_dir, exist_ok=True)

    meta = {
        "user_id": user_id, "round": round_, "order_index": order_index,
        "age": age, "gender": gender, "notes": notes,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    rows = build_protocol_rows(order_index)
    recorder = SessionRecorder(user_dir, rows, on_calibrate=calibrate_tracker, meta=meta)
    with open(recorder.meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)

    current_session.set(recorder)
    return redirect(url_for("index"))


@app.route("/graph")
def graph_page():
    """Page with the interactive 3D visualisation of graph G."""
    _ensure_mqtt_started()
    return render_template("graph.html")


@app.route("/graph-data")
def graph_data():
    """Graph G = (V, E) as JSON, in the format expected by 3d-force-graph:
    {"nodes": [{id, ...}], "links": [{source, target, move}]}.

    The 5 outgoing edges of each node may include pairs on the same pair of
    nodes (e.g. spin+ and spin- towards the same neighbour) and self-loops:
    each edge is given a `curvature_idx` index, incrementing per ordered
    (source,target) pair, so the frontend can separate them visually by
    curving them differently instead of overlapping them.
    """
    nodes, edges = build_graph()

    # fixed order of the observable states -> group/colour per node
    state_index = {s: i for i, s in enumerate(ca.ALL_OBSERVABLE_STATES)}

    out_nodes = [
        {
            "id": n["id"],
            "axis": n["axis"],
            "face": n["face"],
            "hidden_bit": n["hidden_bit"],
            "state": f"{n['axis']},{n['face']}",
            "label": f"{n['axis']},{n['face']} #{n['hidden_bit']}",
            "group": state_index[(n["axis"], n["face"])],
        }
        for n in nodes
    ]

    pair_counts: dict[tuple[int, int], int] = {}
    out_links = []
    for e in edges:
        pair = (e["source"], e["target"])
        idx = pair_counts.get(pair, 0)
        pair_counts[pair] = idx + 1
        out_links.append({
            "source": e["source"],
            "target": e["target"],
            "move": e["move"],
            "move_desc": ca.MOVE_DESCRIPTIONS[e["move"]],
            "curvature_idx": idx,
            "self_loop": e["source"] == e["target"],
        })

    return jsonify({
        "nodes": out_nodes,
        "links": out_links,
        "move_descriptions": ca.MOVE_DESCRIPTIONS,
    })


@app.route("/beacon", methods=["POST"])
def beacon():
    """Records when the browser actually rendered an event (t2), for the
    classify->display leg of the latency measurement (see
    latency_report.py). Matched to its event afterwards by t1 (the server
    receipt/classify timestamp, unique per event), not by session cursor:
    the beacon can arrive after the tester has already moved on."""
    recorder = current_session.get()
    if recorder is None:
        return jsonify({"error": "no active session"}), 409
    body = request.get_json(silent=True) or {}
    try:
        t1 = float(body["t1"])
        t2 = float(body["t2"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "t1 and t2 (seconds) are required"}), 400
    recorder.record_display(t1, t2)
    return jsonify({"ok": True})


@app.route("/protocol-progress")
def protocol_progress():
    """Progress of the test protocol being filled in."""
    recorder = current_session.get()
    if recorder is None:
        return jsonify({"error": "no active session"}), 409
    return jsonify(recorder.progress())


@app.route("/protocol-control/<action>", methods=["POST"])
def protocol_control(action):
    """Test panel commands: pause, resume, redo step, reset.

    The result is also republished on the stream, so any other dashboard tabs
    that are open stay in sync.
    """
    recorder = current_session.get()
    if recorder is None:
        return jsonify({"error": "no active session"}), 409

    actions = {
        "pause": lambda: recorder.set_paused(True),
        "resume": lambda: recorder.set_paused(False),
        "redo": recorder.redo_step,
        "reset": recorder.reset,
    }
    if action not in actions:
        return jsonify({"error": f"unknown action: {action}"}), 400

    prog = actions[action]()
    broadcaster.publish({"type": "progress", **prog})
    return jsonify(prog)


@app.route("/stream")
def stream():
    _ensure_mqtt_started()

    def gen():
        q = broadcaster.listen()
        try:
            yield "retry: 2000\n\n"
            while True:
                data = q.get()
                yield f"data: {json.dumps(data)}\n\n"
        finally:
            broadcaster.unlisten(q)

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    _ensure_mqtt_started()
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)

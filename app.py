"""App Flask per verificare in tempo reale il riconoscimento della rotazione
del cubo. Legge i dati dal broker MQTT (topic 'cubetto/tuoch') e li mostra
in una dashboard live via Server-Sent Events (SSE).

Avvio locale:
    pip install -r requirements.txt
    python app.py
poi apri http://127.0.0.1:5001
(la porta 5000 su macOS e' usata da AirPlay Receiver; override con PORT=...)
"""
import json
import os

from flask import Flask, Response, render_template, stream_with_context

from broadcaster import broadcaster
from mqtt_client import start_mqtt_thread

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
    return render_template("index.html")


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
    # Porta 5000 su macOS e' occupata da "AirPlay Receiver": usiamo 5001.
    port = int(os.environ.get("PORT", 5001))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)

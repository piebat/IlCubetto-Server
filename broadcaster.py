"""Semplice pub-sub in-process per inoltrare gli eventi dal client MQTT
(thread separato) a tutte le connessioni SSE dei browser."""
import queue
import threading


class Broadcaster:
    def __init__(self):
        self._listeners = []
        self._lock = threading.Lock()

    def listen(self):
        q = queue.Queue(maxsize=200)
        with self._lock:
            self._listeners.append(q)
        return q

    def unlisten(self, q):
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def publish(self, data: dict):
        with self._lock:
            listeners = list(self._listeners)
        for q in listeners:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass


broadcaster = Broadcaster()

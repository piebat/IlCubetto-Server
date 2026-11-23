"""Test session recording for statistical analysis.

Each tester gets a fresh SessionRecorder (created by app.py's /session/new
route and installed via current_session.set()), writing into its own
directory logs/<user_id>/:

1. ``session_<timestamp>.jsonl`` -- one JSON line per MQTT event received
   (including invalid/ambiguous readings). This is the complete raw log:
   nothing is discarded, so post-hoc analysis can also count reading
   errors.

2. ``protocol.csv`` -- the 12 combinations assigned to this tester's slot
   (see combination_plan.py), filled in step by step: every recognised
   *valid* move fills the next still-empty row (detected_move,
   detected_axis, detected_index_face, match). Rewritten in full at every
   step: 12 rows are few, and a complete rewrite prevents an interruption
   from leaving a half-written row.

3. ``meta.json`` -- user_id, round, slot, age, gender, notes, started_at,
   and (once the 12th step is filled in) ended_at.

Before any move of the protocol can be judged, the cube's internal
orientation must be pinned to a KNOWN value -- CubeTracker locks onto an
arbitrary one of the two "hidden-bit twin" orientations on its very first
reading, and the sensors alone cannot tell which. The recorder therefore
starts (and re-enters, after any ambiguous/missed-reading jump) a
calibration phase: the tester is asked to perform a fixed move ('tip_U+',
"turn the cube forward") whose outcome, combined with the state observed
right before it, lets CubeTracker.calibrate() resolve the ambiguity.

That calibration move itself displaces the cube to wherever 'tip_U+'
happens to take it from its (arbitrary) pre-calibration orientation --
generally NOT the starting orientation the current protocol row requires.
So calibration is followed by a positioning phase: the tester is guided,
one elementary move at a time (the recorder's `position_hint`, via
cube_algorithm's precomputed shortest paths), to the row's required start,
verified by the sensor rather than trusted. Only once positioned does the
recorder arm the row and score the next recognised move against it.
"""
import csv
import json
import logging
import os
import threading
from datetime import datetime

log = logging.getLogger("session_recorder")

import cube_algorithm as ca

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")

CALIBRATION_MOVE = "tip_U+"

# columns filled in by the recorder (the others belong to the protocol)
FILLED_COLS = ["detected_move", "detected_axis", "detected_index_face", "match"]


def _cell(row, key):
    """String form of a protocol cell, treating a legitimately falsy value
    (face 0) the same as any other -- `value or ""` would collapse 0 to ""."""
    value = row.get(key)
    return "" if value is None else str(value).strip()


PROTOCOL_FIELDNAMES = [
    "step", "move", "start_axis", "start_face", "expected_axis",
    "expected_index_face", "instruction",
    "detected_move", "detected_axis", "detected_index_face", "match",
]


class SessionRecorder:
    """Writes the raw log and fills in this tester's protocol. Thread-safe:
    events arrive from the MQTT thread while Flask serves requests."""

    def __init__(self, user_dir, rows, on_calibrate=None, meta=None):
        self.user_dir = user_dir
        self.meta_path = os.path.join(user_dir, "meta.json")
        self.meta = meta or {}
        self.protocol_path = os.path.join(user_dir, "protocol.csv")
        self._lock = threading.Lock()
        self._rows = rows
        self._fieldnames = PROTOCOL_FIELDNAMES
        self._cursor = 0         # index of the next row to fill in
        self._event_seq = 0

        # calibration state machine -- see module docstring.
        self._phase = "calibrating" if rows else "done"
        self._calib_prev_state = None
        # called with (expected_move, previous_state) whenever the shadow
        # tracker is calibrated, so the caller can calibrate the REAL
        # tracker (owned by mqtt_client.py) identically and keep both in
        # lockstep (they are deterministic functions of the same readings).
        self._on_calibrate = on_calibrate

        # while paused, events still go to the raw log but do not consume
        # protocol steps or calibration progress: this allows repositioning
        # the cube without corrupting the sequence.
        self._paused = False

        # "shadow" tracker: same algorithm (CubeTracker) as the real tracker
        # in mqtt_client.py, fed with the same raw readings (data['faces']).
        # Being deterministic on the same input (and calibrated the same
        # way, via _on_calibrate), it always matches the real tracker's
        # internal id exactly. It is used to compute the "true" expected
        # state from where the cube REALLY was, instead of assuming a fixed
        # starting id like the precomputed expected_axis/expected_index_face
        # columns in the CSV do.
        self._shadow = ca.CubeTracker()
        # ambiguous readings discarded so far (sensor faults, not steps)
        self._ambiguous_count = 0

        os.makedirs(self.user_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = os.path.join(self.user_dir, f"session_{stamp}.jsonl")
        # opened in append mode with line buffering: every event hits the
        # disk immediately, without waiting for the process to exit.
        self._log_fh = open(self.log_path, "a", buffering=1, encoding="utf-8")

        self._write_protocol()

    # --- protocol ------------------------------------------------------

    def _write_protocol(self):
        tmp = self.protocol_path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=self._fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self._rows)
        os.replace(tmp, self.protocol_path)   # atomic replacement

    # --- event intake ----------------------------------------------------

    def record(self, data: dict):
        """Called for every event published by the MQTT client."""
        if data.get("type") != "event":
            self._write_log({**data, "seq": None})
            return

        with self._lock:
            self._event_seq += 1
            seq = self._event_seq

            faces = data.get("faces")
            shadow_ev = self._shadow.update(set(faces) if isinstance(faces, list) else set())

            step = None
            match = None
            calib_result = None
            if not self._paused:
                if self._phase == "calibrating":
                    calib_result = self._advance_calibration(shadow_ev)
                elif self._phase == "positioning":
                    self._advance_positioning(shadow_ev)
                elif self._phase == "running":
                    if shadow_ev.ambiguous:
                        # An ambiguous jump means confidence in the tracked
                        # orientation is lost, not just that one reading was
                        # noisy: re-pin it (then reposition) before the
                        # current step can be retried.
                        self._ambiguous_count += 1
                        self._phase = "calibrating"
                        self._calib_prev_state = None
                        log.warning("ambiguous reading discarded (step %s): recalibrate",
                                    self._rows[self._cursor].get("step")
                                    if self._cursor < len(self._rows) else "-")
                    else:
                        usable = (bool(data.get("ok")) and data.get("move")
                                  and self._cursor < len(self._rows))
                        if usable:
                            step, match = self._fill_row(data)

            self._write_log({
                **data,
                "seq": seq,
                "protocol_step": step,
                "protocol_match": match,
                "phase": self._phase,
                "calibration": calib_result,
                "paused": self._paused,
                "recorded_at": datetime.now().isoformat(timespec="milliseconds"),
            })

    def _advance_calibration(self, shadow_ev) -> str:
        """One step of the 2-reading calibration dance. Returns a short tag
        for the raw log: 'invalid' | 'waiting_prev' | 'pinned' | 'retry'."""
        if not shadow_ev.ok or shadow_ev.ambiguous:
            # a bad reading cannot anchor or complete a calibration attempt
            return "invalid"

        if self._calib_prev_state is None:
            self._calib_prev_state = shadow_ev.state
            return "waiting_prev"

        prev = self._calib_prev_state
        pinned = self._shadow.calibrate(CALIBRATION_MOVE, prev)
        if pinned and self._on_calibrate:
            self._on_calibrate(CALIBRATION_MOVE, prev)

        if pinned:
            self._phase = "positioning" if self._cursor < len(self._rows) else "done"
            self._calib_prev_state = None
            return "pinned"

        # not consistent with the forward-turn move: retry from here
        self._calib_prev_state = shadow_ev.state
        return "retry"

    def _advance_positioning(self, shadow_ev):
        """The calibration move itself displaces the cube to an orientation
        that generally does NOT match the current row's required starting
        state (see module docstring): the tester must manually reposition
        it there before the timed move is armed. This phase only reads the
        sensor to confirm arrival (or recompute the next recommended move,
        see `_next_position_hint`) -- it never fills in a row."""
        if not shadow_ev.ok:
            return
        if shadow_ev.ambiguous:
            self._phase = "calibrating"
            self._calib_prev_state = None
            return

        if self._at_required_start(shadow_ev.state):
            self._phase = "running"

    def _next_position_hint(self):
        """The single next elementary move recommended to approach the
        current row's required starting orientation from here, found via
        cube_algorithm's precomputed shortest paths. None once positioned
        (or if the shadow isn't calibrated yet)."""
        if self._cursor >= len(self._rows) or self._shadow._id is None:
            return None
        target_id = self._rows[self._cursor].get("source_id")
        if target_id is None or self._shadow._id == target_id:
            return None
        return ca._ALL_PAIRS_FIRSTSTEP[self._shadow._id].get(target_id)

    def _at_required_start(self, state) -> bool:
        row = self._rows[self._cursor]
        target = (_cell(row, "start_axis"), _cell(row, "start_face"))
        axis, face = state if state else (None, None)
        return (str(axis), str(face)) == target

    def _fill_row(self, data: dict):
        """Fills in the current protocol row from this event. Returns
        (step, match).

        Reached only from `running`, entered only once `positioning` has
        confirmed the cube sits at the row's required starting state -- so
        a MISMATCH here means the move itself was wrong, not that the cube
        started from an unexpected orientation."""
        row = self._rows[self._cursor]
        state = data.get("state") or (None, None)
        axis, face = (state[0], state[1]) if isinstance(state, (list, tuple)) and len(state) == 2 else (None, None)

        move = data.get("move") or ""
        row["detected_move"] = move
        row["detected_axis"] = "" if axis is None else str(axis)
        row["detected_index_face"] = "" if face is None else str(face)

        planned_move = _cell(row, "move")
        state_now = (str(axis), str(face))

        expected = (_cell(row, "expected_axis"), _cell(row, "expected_index_face"))
        ok = (move == planned_move) and (state_now == expected)

        row["match"] = "OK" if ok else "MISMATCH"

        step = row.get("step")
        self._cursor += 1
        if self._cursor >= len(self._rows):
            self._phase = "done"
            self._write_meta_ended()
        else:
            # The move just scored already leaves the cube at `expected`,
            # which usually (thanks to combination_plan's Eulerian chaining)
            # IS the next row's required start -- checked immediately
            # against the state we already have, rather than waiting for a
            # fresh reading that may never come if the tester does nothing.
            self._phase = ("running" if self._at_required_start(self._shadow.state)
                           else "positioning")
        self._write_protocol()
        return step, row["match"]

    def _write_meta_ended(self):
        if not self.meta:
            return
        self.meta["ended_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            with open(self.meta_path, "w", encoding="utf-8") as fh:
                json.dump(self.meta, fh, indent=2)
        except Exception:
            log.exception("failed to write ended_at to %s", self.meta_path)

    # --- raw log -----------------------------------------------------------

    def _write_log(self, obj):
        try:
            self._log_fh.write(json.dumps(obj, default=str) + "\n")
        except Exception:
            pass   # logging must never bring down the MQTT thread

    # --- session controls --------------------------------------------------

    def set_paused(self, paused: bool):
        """While paused, logging continues but no step/calibration is
        consumed."""
        with self._lock:
            self._paused = bool(paused)
            self._write_log({"type": "control", "action": "pause" if paused else "resume",
                             "cursor": self._cursor,
                             "recorded_at": datetime.now().isoformat(timespec="milliseconds")})
            return self._progress_locked()

    def redo_step(self):
        """Undoes the last recorded step: the row is emptied and the next
        move fills it in again. Useful when a manipulation went wrong.

        Note: the shadow tracker (`_shadow`) is NOT rolled back, because it
        mirrors the real tracker, which has advanced on the physical cube
        anyway -- undoing only the protocol row, leaving the shadow tracker
        where it is, is the correct behaviour."""
        with self._lock:
            if self._cursor == 0:
                return self._progress_locked()
            self._cursor -= 1
            row = self._rows[self._cursor]
            for col in FILLED_COLS:
                row[col] = ""
            self._phase = "positioning"
            self._write_protocol()
            self._write_log({"type": "control", "action": "redo_step",
                             "step": row.get("step"), "cursor": self._cursor,
                             "recorded_at": datetime.now().isoformat(timespec="milliseconds")})
            return self._progress_locked()

    def reset(self):
        """Clears all filled-in columns and restarts from step 1, including
        a fresh calibration. The raw log is NOT deleted: the complete trace
        of the session remains, with a reset marker."""
        with self._lock:
            for row in self._rows:
                for col in FILLED_COLS:
                    row[col] = ""
            self._cursor = 0
            self._paused = False
            self._ambiguous_count = 0
            self._phase = "calibrating" if self._rows else "done"
            self._calib_prev_state = None
            # The shadow tracker is NOT cleared: the cube has not moved, and
            # clearing it would lose the current position. Calibration
            # starting over re-pins it correctly regardless.
            self._write_protocol()
            self._write_log({"type": "control", "action": "reset",
                             "recorded_at": datetime.now().isoformat(timespec="milliseconds")})
            return self._progress_locked()

    # --- state for the dashboard -------------------------------------------

    def progress(self):
        with self._lock:
            return self._progress_locked()

    def _progress_locked(self):
        done = self._cursor
        total = len(self._rows)
        ok = sum(1 for r in self._rows[:done] if r.get("match") == "OK")
        return {
            "done": done,
            "total": total,
            "ok": ok,
            "mismatch": done - ok,
            "paused": self._paused,
            "phase": self._phase,
            "recalibrating": self._phase == "calibrating" and done > 0,
            "calibration_move": CALIBRATION_MOVE,
            "shadow_state": self._shadow.state,
            "position_hint": self._next_position_hint() if self._phase == "positioning" else None,
            "log_file": os.path.basename(self.log_path),
            "next_step": self._rows[done] if done < total else None,
            "last_step": self._rows[done - 1] if done > 0 else None,
            "discarded": self._ambiguous_count,
        }

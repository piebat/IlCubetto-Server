"""Test session recording for statistical analysis.

Each tester gets a fresh SessionRecorder (created by app.py's /session/new
route and installed via current_session.set()), writing into its own
directory logs/<user_id>/:

1. ``session_<timestamp>.jsonl`` -- one JSON line per MQTT event received
   (including invalid/ambiguous readings). This is the complete raw log:
   nothing is discarded, so post-hoc analysis can also count reading
   errors.

2. ``protocol.csv`` -- the 16 symbols assigned to this tester's session
   order (see symbol_plan.py), filled in step by step: every recognised
   *valid* move fills the next still-open slot of the current row
   (detected_move1, then detected_move2, then match is computed). Rewritten
   in full at every step: 16 rows are few, and a complete rewrite prevents
   an interruption from leaving a half-written row.

3. ``meta.json`` -- user_id, round, order_index, age, gender, notes,
   started_at, and (once the 16th row is filled in) ended_at.

A Symbol is an ordered pair of two consecutive elementary rotations (see
symbol_plan.py). Symbols are chained: each one simply starts wherever the
cube was left by the previous one -- there is no required starting
orientation to reposition to, unlike the single-move protocol this
replaces.

Before any move can be judged, the cube's internal orientation must be
pinned to a KNOWN value -- CubeTracker locks onto an arbitrary one of the
two "hidden-bit twin" orientations on its very first reading, and the
sensors alone cannot tell which. The recorder therefore starts (and
re-enters, after any ambiguous/missed-reading jump) a calibration phase: the
tester is asked to perform a fixed move ('tip_U+', "turn the cube forward")
whose outcome, combined with the state observed right before it, lets
CubeTracker.calibrate() resolve the ambiguity.

If an ambiguous reading occurs mid-symbol (a missed step), or the cube is
released/dropped (every finger comes off it, so it could have been moved
without being observed), the current symbol's partially-recorded moves are
discarded and the tester is asked to repeat the calibration rotation; once
recalibrated, the SAME symbol (not the next one) is retried.
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
FILLED_COLS = ["detected_move1", "detected_move2", "match"]

PROTOCOL_FIELDNAMES = [
    "step", "symbol", "move1", "move2", "instruction",
    "detected_move1", "detected_move2", "match",
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
        self._cursor = 0         # index of the row (symbol) being filled in
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
        # protocol steps or calibration progress.
        self._paused = False

        # "shadow" tracker: same algorithm (CubeTracker) as the real tracker
        # in mqtt_client.py, fed with the same raw readings (data['faces']).
        # Being deterministic on the same input (and calibrated the same
        # way, via _on_calibrate), it always matches the real tracker's
        # internal id exactly.
        self._shadow = ca.CubeTracker()
        # ambiguous readings discarded so far (sensor faults, not steps)
        self._ambiguous_count = 0
        # most recent per-event outcome (see `_advance_symbol`/`record`):
        # {"kind": "ok"|"wrong_move"|"lost_reading", ...}, for live feedback
        self._last_live_result = None

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

            faces_set = set(faces) if isinstance((faces := data.get("faces")), list) else set()
            shadow_ev = self._shadow.update(faces_set)
            released = len(faces_set) == 0

            step = None
            match = None
            calib_result = None
            live_result = None  # per-event feedback, distinct from `match` (set only when a symbol completes)
            if not self._paused:
                if self._phase == "calibrating":
                    calib_result = self._advance_calibration(shadow_ev)
                elif self._phase == "running":
                    if shadow_ev.ambiguous or released:
                        # Either the sensors missed an intermediate reading
                        # (an ambiguous jump), or every finger came off the
                        # cube (a release/drop): in both cases confidence in
                        # the tracked orientation is lost -- the cube could
                        # have been moved while nothing was observing it.
                        # Discard whatever was recorded so far for the
                        # CURRENT symbol, recalibrate, then retry the same
                        # symbol -- it is not advanced past. This is a sensor
                        # fault, not a wrong move: the tester is not at fault
                        # and nothing can be said about which move they made.
                        self._ambiguous_count += 1
                        self._discard_current_symbol()
                        self._phase = "calibrating"
                        self._calib_prev_state = None
                        current_step = (self._rows[self._cursor].get("step")
                                         if self._cursor < len(self._rows) else None)
                        live_result = {"kind": "released" if released else "lost_reading",
                                        "step": current_step}
                        log.warning("%s (step %s): recalibrate",
                                    "cube released" if released else "ambiguous reading discarded",
                                    self._rows[self._cursor].get("step")
                                    if self._cursor < len(self._rows) else "-")
                    else:
                        usable = (bool(data.get("ok")) and data.get("move")
                                  and self._cursor < len(self._rows))
                        if usable:
                            step, match, live_result = self._advance_symbol(data)

            self._write_log({
                **data,
                "seq": seq,
                "protocol_step": step,
                "protocol_match": match,
                "live_result": live_result,
                "phase": self._phase,
                "calibration": calib_result,
                "paused": self._paused,
                "recorded_at": datetime.now().isoformat(timespec="milliseconds"),
            })
            if live_result is not None:
                self._last_live_result = live_result

    def record_display(self, t1: float, t2: float):
        """Called from the /beacon route with t1 (server receipt/classify
        time, as sent down in the event) and t2 (when the browser rendered
        it). Written as its own log line rather than patched into the
        original event's line: the event is already on disk by the time the
        browser round-trips the beacon, and jsonl is append-only."""
        with self._lock:
            self._write_log({
                "type": "display",
                "t1": t1,
                "t2": t2,
                "classify_to_display_ms": (t2 - t1) * 1000.0,
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
            self._phase = "running" if self._cursor < len(self._rows) else "done"
            self._calib_prev_state = None
            return "pinned"

        # not consistent with the forward-turn move: retry from here
        self._calib_prev_state = shadow_ev.state
        return "retry"

    def _discard_current_symbol(self):
        """Clears whatever moves have already been recorded for the symbol
        currently in progress, without advancing the cursor past it."""
        if self._cursor < len(self._rows):
            row = self._rows[self._cursor]
            for col in FILLED_COLS:
                row[col] = ""
            self._write_protocol()

    def _advance_symbol(self, data: dict):
        """Fills in the next open slot (move1, then move2) of the current
        row from this event. Once both are filled, scores the row and moves
        the cursor to the next symbol. Returns (step, match, live_result):
        match is None until the second move of the pair completes the row;
        live_result reports, for THIS event, whether the move just detected
        was the one expected -- distinct from `match`, which only speaks for
        the whole two-move symbol, and from the "lost_reading" sensor-fault
        case (handled by the caller before this is reached), since here the
        tracker is calibrated and the move is known with certainty: a wrong
        move is a mis-performed rotation, not an ambiguous reading."""
        row = self._rows[self._cursor]
        move = data.get("move") or ""
        row_step = row.get("step")

        def _live_result(detected, expected):
            if detected == expected:
                return {"kind": "ok", "step": row_step}
            return {"kind": "wrong_move", "expected": expected, "detected": detected, "step": row_step}

        if not row["detected_move1"]:
            row["detected_move1"] = move
            self._write_protocol()
            return row.get("step"), None, _live_result(move, row["move1"])

        row["detected_move2"] = move
        ok = (row["detected_move1"] == row["move1"]) and (row["detected_move2"] == row["move2"])
        row["match"] = "OK" if ok else "MISMATCH"

        step = row.get("step")
        match = row["match"]
        live_result = _live_result(move, row["move2"])
        self._cursor += 1
        if self._cursor >= len(self._rows):
            self._phase = "done"
            self._write_meta_ended()
        self._write_protocol()
        return step, match, live_result

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
        """Undoes the last recorded symbol: the row is emptied and the next
        pair of moves fills it in again. Useful when a manipulation went
        wrong.

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
            self._last_live_result = None
            self._write_protocol()
            self._write_log({"type": "control", "action": "redo_step",
                             "step": row.get("step"), "cursor": self._cursor,
                             "recorded_at": datetime.now().isoformat(timespec="milliseconds")})
            return self._progress_locked()

    def recalibrate(self):
        """Tester- or operator-requested calibration, e.g. after setting the
        cube down deliberately or if the live display looks wrong, without
        waiting for a sensor fault to trigger it automatically.

        Unlike `reset()`, the protocol cursor does not move: only the
        CURRENT symbol's partially-recorded moves are discarded (they were
        read under the calibration now being redone and would no longer be
        trustworthy), then calibration restarts and the same symbol is
        retried once it completes -- the same recovery as an automatic
        ambiguous-reading or cube-release event, just operator-triggered.
        A no-op while already calibrating."""
        with self._lock:
            if self._phase != "running":
                return self._progress_locked()
            self._discard_current_symbol()
            self._phase = "calibrating"
            self._calib_prev_state = None
            self._last_live_result = None
            self._write_log({"type": "control", "action": "recalibrate",
                             "step": self._rows[self._cursor].get("step")
                                     if self._cursor < len(self._rows) else None,
                             "cursor": self._cursor,
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
            self._last_live_result = None
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
        current = self._rows[done] if done < total else None
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
            "awaiting_move2": bool(current and current.get("detected_move1")),
            "log_file": os.path.basename(self.log_path),
            "next_step": current,
            "last_step": self._rows[done - 1] if done > 0 else None,
            "discarded": self._ambiguous_count,
            "last_live_result": self._last_live_result,
        }

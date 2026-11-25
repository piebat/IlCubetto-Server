"""Reports the median and 95th percentile latency across the three legs of
the pipeline, over the 120-rotation session logs (logs/<user_id>/session_*.jsonl):

  publish_to_classify_ms  -- ESP32 publish (t0, corrected for clock offset
                              via clock_sync.py's ping/pong) to server
                              classification (t1). None for a run/session
                              where the firmware never sent "t0", or where
                              no ping/pong pair had completed yet.
  classify_to_display_ms  -- server classification (t1) to the browser
                              actually rendering it (t2), from the /beacon
                              POSTs recorded as "display" lines and matched
                              to their "event" line by t1.
  end_to_end_ms           -- publish_to_classify_ms + classify_to_display_ms
                              (only where both legs are known).

Only "event" lines with ok=True are counted (ambiguous/invalid readings
were never classified as a move, so they have no meaningful end-to-end
latency); "display" lines are matched to them by t1 and are not counted on
their own.

Usage:
    python3 latency_report.py [logs/<user_id>/session_*.jsonl ...]
    python3 latency_report.py                      # all sessions under logs/
"""
import glob
import json
import sys

from session_recorder import LOG_DIR


def _median(values):
    s = sorted(values)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _percentile(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def _load(paths):
    """Reads every line of every log, matches each valid "event" to its
    "display" beacon by t1, and returns the three latency lists."""
    display_by_t1 = {}
    events = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "display" and obj.get("t1") is not None:
                    display_by_t1[obj["t1"]] = obj["classify_to_display_ms"]
                elif obj.get("type") == "event" and obj.get("ok"):
                    events.append(obj)

    publish_to_classify, classify_to_display, end_to_end = [], [], []
    for ev in events:
        p2c = ev.get("publish_to_classify_ms")
        c2d = display_by_t1.get(ev.get("t1"))
        if p2c is not None:
            publish_to_classify.append(p2c)
        if c2d is not None:
            classify_to_display.append(c2d)
        if p2c is not None and c2d is not None:
            end_to_end.append(p2c + c2d)

    return len(events), publish_to_classify, classify_to_display, end_to_end


def _report_leg(name, values, total_events):
    print(f"\n{name}: {len(values)}/{total_events} events with a measurement")
    if not values:
        print("  (no samples -- see the module docstring for why a leg can be empty)")
        return
    print(f"  median: {_median(values):7.1f} ms")
    print(f"  p95:    {_percentile(values, 95):7.1f} ms")


def main(argv):
    paths = argv or sorted(glob.glob(f"{LOG_DIR}/*/session_*.jsonl"))
    if not paths:
        sys.exit(f"No session logs found under {LOG_DIR}")

    total_events, p2c, c2d, e2e = _load(paths)
    print(f"{len(paths)} log file(s), {total_events} valid (ok=True) events")
    _report_leg("publish -> classify (firmware to server)", p2c, total_events)
    _report_leg("classify -> display (server to browser)", c2d, total_events)
    _report_leg("end-to-end (publish -> display)", e2e, total_events)


if __name__ == "__main__":
    main(sys.argv[1:])

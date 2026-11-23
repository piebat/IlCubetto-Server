"""Assigns auto-incremental user ids, cycling through 10 slots per round so
that every complete round covers all 120 combinations (see
combination_plan.py) exactly once across 10 testers. A new round starts
automatically once slot 9 has been handed out, so the protocol can be
re-run with later cohorts without manual reset.

Persisted in logs/registry.json: {"next_round": 1, "next_slot": 0}.
"""
import json
import os
import threading

from combination_plan import N_SLOTS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
REGISTRY_PATH = os.path.join(LOG_DIR, "registry.json")

_lock = threading.Lock()


def _read():
    if not os.path.exists(REGISTRY_PATH):
        return {"next_round": 1, "next_slot": 0}
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return {"next_round": int(data["next_round"]), "next_slot": int(data["next_slot"])}
    except Exception:
        return {"next_round": 1, "next_slot": 0}


def _write(state):
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, REGISTRY_PATH)


def user_id_for(round_: int, slot: int) -> str:
    return f"user_r{round_}_{slot:02d}"


def peek_next():
    """Round/slot/user_id that the NEXT allocate_user() call will hand out,
    without consuming it."""
    with _lock:
        state = _read()
    return state["next_round"], state["next_slot"], user_id_for(state["next_round"], state["next_slot"])


def allocate_user():
    """Returns (round, slot, user_id) and advances the registry so the next
    call hands out the following slot (wrapping to a new round after 10)."""
    with _lock:
        state = _read()
        round_, slot = state["next_round"], state["next_slot"]

        next_slot = slot + 1
        next_round = round_
        if next_slot >= N_SLOTS:
            next_slot = 0
            next_round = round_ + 1
        _write({"next_round": next_round, "next_slot": next_slot})

    return round_, slot, user_id_for(round_, slot)

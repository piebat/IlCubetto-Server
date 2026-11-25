"""Assigns auto-incremental user ids, cycling through the N_ORDERS precomputed
symbol orderings (see symbol_plan.py) so that across any N_ORDERS consecutive
testers, every one of them gets a different order of the 16 symbols. A new
round starts automatically once the last order has been handed out, so the
protocol can be re-run with later cohorts without manual reset.

Persisted in logs/registry.json: {"next_round": 1, "next_order": 0}.
"""
import json
import os
import threading

from symbol_plan import N_ORDERS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
REGISTRY_PATH = os.path.join(LOG_DIR, "registry.json")

_lock = threading.Lock()


def _read():
    if not os.path.exists(REGISTRY_PATH):
        return {"next_round": 1, "next_order": 0}
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return {"next_round": int(data["next_round"]), "next_order": int(data["next_order"])}
    except Exception:
        return {"next_round": 1, "next_order": 0}


def _write(state):
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, REGISTRY_PATH)


def user_id_for(round_: int, order_index: int) -> str:
    return f"user_r{round_}_{order_index:02d}"


def peek_next():
    """Round/order_index/user_id that the NEXT allocate_user() call will hand
    out, without consuming it."""
    with _lock:
        state = _read()
    return state["next_round"], state["next_order"], user_id_for(state["next_round"], state["next_order"])


def allocate_user():
    """Returns (round, order_index, user_id) and advances the registry so the
    next call hands out the following order (wrapping to a new round after
    N_ORDERS)."""
    with _lock:
        state = _read()
        round_, order_index = state["next_round"], state["next_order"]

        next_order = order_index + 1
        next_round = round_
        if next_order >= N_ORDERS:
            next_order = 0
            next_round = round_ + 1
        _write({"next_round": next_round, "next_order": next_order})

    return round_, order_index, user_id_for(round_, order_index)

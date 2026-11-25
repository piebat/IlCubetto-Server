"""Defines a "Symbol" as an ordered pair of two consecutive elementary
rotations, and the fixed pool of per-session symbol orderings.

Two consecutive moves in a symbol must be different, and the 4 pairs where
the second move cancels the first (spin+/spin-, spin-/spin+, tip_U+/tip_U-,
tip_U-/tip_U+) are excluded, since they return the cube to its starting
orientation and could easily be produced by accident. That leaves
5 x 4 - 4 = 16 symbols.

Every session starts with a tip_U+ calibration rotation, then the 16 symbols
are presented one at a time in a random order, chained (each symbol just
starts wherever the previous one left the cube -- there is no fixed
"required start state" the way single-move combinations used to have one).
A fixed pool of >=12 distinct permutations of the 16 symbols is precomputed
up front (deterministic seed, so it is reproducible run to run) and handed
out round-robin by user_registry, so that across any 12 consecutive sessions
every participant gets a different order.
"""
import random

import cube_algorithm as ca

MOVE_ORDER = ['spin+', 'spin-', 'tip_H', 'tip_U+', 'tip_U-']
CANCELLING_PAIRS = {('spin+', 'spin-'), ('spin-', 'spin+'),
                    ('tip_U+', 'tip_U-'), ('tip_U-', 'tip_U+')}

N_ORDERS = 12          # at least 12 sessions must all be different
ORDER_SEED = 20261125  # fixed seed: the pool is reproducible across runs


def _build_symbols() -> list[tuple[str, str]]:
    """The 16 symbols: ordered pairs of distinct, non-cancelling moves."""
    symbols = []
    for m1 in MOVE_ORDER:
        for m2 in MOVE_ORDER:
            if m1 == m2:
                continue
            if (m1, m2) in CANCELLING_PAIRS:
                continue
            symbols.append((m1, m2))
    return symbols


SYMBOLS = _build_symbols()
assert len(SYMBOLS) == 16


def _build_orders() -> list[list[tuple[str, str]]]:
    """N_ORDERS distinct random permutations of the 16 symbols, as different
    from each other as a deterministic shuffle allows."""
    rng = random.Random(ORDER_SEED)
    seen = set()
    orders = []
    while len(orders) < N_ORDERS:
        order = SYMBOLS[:]
        rng.shuffle(order)
        key = tuple(order)
        if key in seen:
            continue
        seen.add(key)
        orders.append(order)
    return orders


ORDERS = _build_orders()


def symbol_instruction(move1: str, move2: str) -> str:
    return f"{ca.MOVE_DESCRIPTIONS[move1]}, then {ca.MOVE_DESCRIPTIONS[move2]}"


def build_protocol_rows(order_index: int) -> list[dict]:
    """CSV rows for the session's 16 symbols, in the order assigned to
    `order_index` (0-based, wraps modulo N_ORDERS). The last columns are
    filled in by SessionRecorder as the session progresses."""
    order = ORDERS[order_index % N_ORDERS]
    rows = []
    for i, (move1, move2) in enumerate(order, start=1):
        rows.append({
            "step": i,
            "move1": move1,
            "move2": move2,
            "symbol": f"{move1} + {move2}",
            "instruction": symbol_instruction(move1, move2),
            "detected_move1": "",
            "detected_move2": "",
            "match": "",
        })
    return rows

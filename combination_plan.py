"""Splits the 120 (starting orientation, move) combinations of the cube's
state graph into 10 chunks of 12, one per tester.

The 120 combinations are exactly the edges of the graph already built in
generate_graph.py: 24 internal orientations x 5 elementary moves, a
balanced digraph (in-degree == out-degree == 5 at every node) that is
strongly connected -- so by Euler's theorem it has an Eulerian circuit
visiting every edge exactly once. That circuit is computed once (Hierholzer's
algorithm, deterministic edge order -> same circuit every run) and cut into
10 consecutive chunks of 12 edges.

This matters physically: within a chunk, edge i's target orientation is
edge i+1's source orientation, so the 12 moves of a slot can be performed as
one continuous sequence on the real cube -- it never needs to be
manually repositioned to an unrelated "expected" starting orientation
between steps, which is not something a tester can do without external
tools telling them the cube's exact current internal id.
"""
from collections import defaultdict

import cube_algorithm as ca
from generate_graph import build_graph

MOVE_ORDER = ['spin+', 'spin-', 'tip_H', 'tip_U+', 'tip_U-']
N_SLOTS = 10
SLOT_SIZE = 12

_move_rank = {m: i for i, m in enumerate(MOVE_ORDER)}


def _eulerian_circuit(edges, start=0):
    """Hierholzer's algorithm. `edges`: list of {source, target, move}.
    Returns the list of edges (same dicts) in circuit order, deterministic
    given a fixed edge order per node (sorted by move name)."""
    adj = defaultdict(list)
    for e in edges:
        adj[e["source"]].append(e)
    for node in adj:
        adj[node].sort(key=lambda e: _move_rank[e["move"]])
    # consume from the end of each list (cheap pop), so reverse once up front
    for node in adj:
        adj[node].reverse()

    stack = [(start, None)]  # (node, incoming edge used to reach it)
    circuit_edges = []
    while stack:
        node, _ = stack[-1]
        if adj[node]:
            e = adj[node].pop()
            stack.append((e["target"], e))
        else:
            _, used_edge = stack.pop()
            if used_edge is not None:
                circuit_edges.append(used_edge)
    circuit_edges.reverse()
    return circuit_edges


def _build_blocks():
    _, edges = build_graph()
    assert len(edges) == N_SLOTS * SLOT_SIZE
    circuit = _eulerian_circuit(edges, start=0)
    assert len(circuit) == len(edges), \
        f"Eulerian circuit covers {len(circuit)}/{len(edges)} edges -- graph not connected as expected"
    return [circuit[i * SLOT_SIZE:(i + 1) * SLOT_SIZE] for i in range(N_SLOTS)]


_BLOCKS = _build_blocks()


def slot_combinations(slot: int) -> list[dict]:
    """The 12 edges assigned to this slot (0-9), in the order they must be
    performed (each edge's target == the next edge's source), each enriched
    with the human-readable instruction and the observable start/expected
    state."""
    if not 0 <= slot < N_SLOTS:
        raise ValueError(f"slot out of range: {slot}")
    out = []
    for e in _BLOCKS[slot]:
        source, target, move = e["source"], e["target"], e["move"]
        start_axis, start_face = ca._OBSERVE_OF[source]
        exp_axis, exp_face = ca._OBSERVE_OF[target]
        out.append({
            "source": source,
            "target": target,
            "move": move,
            "instruction": ca.MOVE_DESCRIPTIONS[move],
            "start_axis": start_axis,
            "start_face": start_face,
            "expected_axis": exp_axis,
            "expected_index_face": exp_face,
        })
    return out


def build_protocol_rows(slot: int) -> list[dict]:
    """CSV rows for this slot's 12 combinations. The last 4 columns are
    filled in by SessionRecorder as the session progresses.

    Each row also carries `source_id`/`target_id` (the exact internal
    orientation ids, not just the observable axis/face): an observable
    state alone cannot disambiguate between its two hidden-bit twins, but
    SessionRecorder needs the exact id to compute the shortest positioning
    path (BFS over cube_algorithm's precomputed neighbours) towards it.
    These are not CSV columns -- SessionRecorder's DictWriter is given
    extrasaction="ignore" so these in-memory-only keys are dropped on
    write instead of raising."""
    rows = []
    for i, c in enumerate(slot_combinations(slot), start=1):
        rows.append({
            "step": i,
            "move": c["move"],
            "start_axis": c["start_axis"],
            "start_face": c["start_face"],
            "expected_axis": c["expected_axis"],
            "expected_index_face": c["expected_index_face"],
            "instruction": c["instruction"],
            "detected_move": "",
            "detected_axis": "",
            "detected_index_face": "",
            "match": "",
            "source_id": c["source"],
            "target_id": c["target"],
        })
    return rows

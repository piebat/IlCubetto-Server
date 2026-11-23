"""
Generates the graph G = (V, E) described in the "State Graph Construction"
section of the paper: V = the cube's 24 proper orientations, E = the 5
elementary moves leaving each one (spin+, spin-, tip_U+, tip_U-, tip_H) ->
5-regular directed graph on 24 nodes.

It reuses the structures already precomputed and verified in
cube_algorithm.py (no geometry is recomputed here), and merely exports them
in standard formats:

  - graph_nodes.csv   id,axis,face,hidden_bit
  - graph_edges.csv   source,target,move
  - graph.dot         Graphviz format (open with `dot -Tpng graph.dot -o graph.png`)
  - graph.png         if the `dot` binary is available on the system (optional)
  - graph.png (fallback) drawn with matplotlib if `dot` is unavailable, and in
    any case always generated as "graph_matplotlib.png" for comparison

Usage:
    python generate_graph.py [output_folder]
"""
from __future__ import annotations

import csv
import math
import shutil
import subprocess
import sys
from pathlib import Path

import cube_algorithm as ca

MOVE_COLORS = {
    'spin+': '#4f8cff', 'spin-': '#4f8cff',
    'tip_U+': '#f2b84b', 'tip_U-': '#f2b84b',
    'tip_H': '#34c77b',
}
MOVE_STYLES = {
    'spin+': 'solid', 'spin-': 'dashed',
    'tip_U+': 'solid', 'tip_U-': 'dashed',
    'tip_H': 'solid',
}


def build_graph():
    """Returns (nodes, edges).
    nodes: list of dicts {id, axis, face, hidden_bit} (hidden_bit = 0 or 1,
           distinguishing the two internal ids compatible with the same
           observable state).
    edges: list of dicts {source, target, move}.
    """
    nodes = []
    hidden_bit_of = {}
    for state, ids in ca._STATES_FOR_OBS.items():
        for bit, node_id in enumerate(ids):
            hidden_bit_of[node_id] = bit

    for node_id in range(24):
        axis, face = ca._OBSERVE_OF[node_id]
        nodes.append({
            "id": node_id,
            "axis": axis,
            "face": face,
            "hidden_bit": hidden_bit_of[node_id],
        })

    edges = []
    for node_id in range(24):
        for move, (neighbor_id, _) in ca._NEIGHBORS[node_id].items():
            edges.append({"source": node_id, "target": neighbor_id, "move": move})

    return nodes, edges


def export_csv(nodes, edges, out_dir: Path):
    with open(out_dir / "graph_nodes.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "axis", "face", "hidden_bit"])
        w.writeheader()
        w.writerows(nodes)

    with open(out_dir / "graph_edges.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["source", "target", "move"])
        w.writeheader()
        w.writerows(edges)


def export_dot(nodes, edges, out_dir: Path) -> Path:
    # note: the "neato" layout SIGSEGVs some Graphviz builds on this graph
    # (multi-edges + many labels) -- the default `dot` layout is used, which
    # has been verified to be stable.
    lines = ["digraph CubeStateGraph {", "  overlap=false;",
             "  splines=true;", '  node [shape=circle, style=filled, fontsize=11];']
    for n in nodes:
        label = f"{n['axis']},{n['face']}\\n#{n['hidden_bit']}"
        fill = "#dbe7ff" if n["hidden_bit"] == 0 else "#ffe6c7"
        lines.append(f'  n{n["id"]} [label="{label}", fillcolor="{fill}"];')
    for e in edges:
        color = MOVE_COLORS[e["move"]]
        style = MOVE_STYLES[e["move"]]
        lines.append(
            f'  n{e["source"]} -> n{e["target"]} '
            f'[label="{e["move"]}", color="{color}", style={style}, fontsize=8];'
        )
    lines.append("}")
    dot_path = out_dir / "graph.dot"
    dot_path.write_text("\n".join(lines))
    return dot_path


def try_render_with_graphviz(dot_path: Path, out_dir: Path) -> bool:
    dot_bin = shutil.which("dot")
    if not dot_bin:
        return False
    png_path = out_dir / "graph.png"
    try:
        subprocess.run([dot_bin, "-Tpng", str(dot_path), "-o", str(png_path)],
                        check=True, capture_output=True)
        return True
    except Exception as exc:
        print(f"Graphviz rendering failed ({exc}); using the matplotlib fallback only.")
        return False


def render_with_matplotlib(nodes, edges, out_dir: Path):
    """Manual layout (no networkx dependency): the 12 observable states on an
    outer circle, the 2 hidden ids of each on two nearby radii."""
    import matplotlib.pyplot as plt

    states_order = ca.ALL_OBSERVABLE_STATES  # 12 states, fixed order
    angle_of_state = {s: 2 * math.pi * i / 12 for i, s in enumerate(states_order)}
    radius = {0: 1.4, 1: 2.2}  # two radii for the two hidden bits

    pos = {}
    for n in nodes:
        state = (n["axis"], n["face"])
        angle = angle_of_state[state]
        r = radius[n["hidden_bit"]]
        pos[n["id"]] = (r * math.cos(angle), r * math.sin(angle))

    fig, ax = plt.subplots(figsize=(7, 7))
    for e in edges:
        x1, y1 = pos[e["source"]]
        x2, y2 = pos[e["target"]]
        ax.annotate(
            "", xy=(x2, y2), xytext=(x1, y1),
            arrowprops=dict(arrowstyle="-|>", color=MOVE_COLORS[e["move"]],
                             linestyle=MOVE_STYLES[e["move"]], lw=1.1,
                             shrinkA=14, shrinkB=14, alpha=0.75,
                             connectionstyle="arc3,rad=0.08"),
        )

    for n in nodes:
        x, y = pos[n["id"]]
        color = "#dbe7ff" if n["hidden_bit"] == 0 else "#ffe6c7"
        ax.scatter([x], [y], s=650, color=color, edgecolors="#333", zorder=3)
        ax.text(x, y, f"{n['axis']},{n['face']}\n#{n['hidden_bit']}",
                ha="center", va="center", fontsize=8, zorder=4)

    legend_handles = [
        plt.Line2D([0], [0], color=c, lw=2,
                    linestyle=MOVE_STYLES[m], label=m)
        for m, c in {"spin+": MOVE_COLORS["spin+"], "spin-": None,
                      "tip_U+": MOVE_COLORS["tip_U+"], "tip_U-": None,
                      "tip_H": MOVE_COLORS["tip_H"]}.items()
        if c is not None or m in ("spin-", "tip_U-")
    ]
    # explicit, ordered legend
    legend_handles = [
        plt.Line2D([0], [0], color=MOVE_COLORS['spin+'], lw=2, linestyle='solid', label='spin+'),
        plt.Line2D([0], [0], color=MOVE_COLORS['spin-'], lw=2, linestyle='dashed', label='spin-'),
        plt.Line2D([0], [0], color=MOVE_COLORS['tip_U+'], lw=2, linestyle='solid', label='tip_U+'),
        plt.Line2D([0], [0], color=MOVE_COLORS['tip_U-'], lw=2, linestyle='dashed', label='tip_U-'),
        plt.Line2D([0], [0], color=MOVE_COLORS['tip_H'], lw=2, linestyle='solid', label='tip_H'),
    ]
    ax.legend(handles=legend_handles, loc="upper right", fontsize=10)
    # ax.set_title("Cube state graph (24 nodes, 5-regular)\n"
    #               "outer/inner ring = the two hidden ids for each observable state (P,f)",
    #               fontsize=12)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(out_dir / "graph_matplotlib.png", dpi=150)
    plt.close(fig)


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)

    nodes, edges = build_graph()
    assert len(nodes) == 24
    assert len(edges) == 24 * 5

    export_csv(nodes, edges, out_dir)
    dot_path = export_dot(nodes, edges, out_dir)
    rendered = try_render_with_graphviz(dot_path, out_dir)
    render_with_matplotlib(nodes, edges, out_dir)

    print(f"Nodes: {len(nodes)}  Edges: {len(edges)}")
    print(f"Written: graph_nodes.csv, graph_edges.csv, graph.dot, "
          f"graph_matplotlib.png" + (", graph.png (via graphviz)" if rendered else ""))
    if not rendered:
        print("Note: 'dot' binary (Graphviz) not found -- graph.png via graphviz "
              "was not generated; use graph_matplotlib.png or install graphviz and "
              "run: dot -Tpng graph.dot -o graph.png")


if __name__ == "__main__":
    main()

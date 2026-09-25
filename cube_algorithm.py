"""
Algoritmo di riconoscimento della rotazione del cubo (presa a 3 dita: pinza su
una coppia di facce opposte + indice su una faccia dello stesso anello).

Geometria derivata dal dato di chiralita' fornito: il vertice dove si
incontrano le facce 0,1,4 e' ORARIO visto da fuori.

Frame di riferimento (fisso rispetto alla MANO, non al cubo): l'asse U e' la
linea pollice-medio (pinza), l'asse V e' la direzione dell'indice, l'asse W
e' la direzione perpendicolare (nessun dito la tocca). Tenuti distinti dagli
assi x,y,z del cubo (FACE_VEC piu' sotto), che sono fissi al corpo del cubo
e non alla mano.

Stato osservabile dai sensori: (P, f)
  P = asse pinzato correntemente ('A'=0-3, 'B'=1-2, 'C'=4-5)
  f = faccia toccata dall'indice (una delle 4 facce non in P)
12 stati osservabili in totale.

Stato interno tracciato dall'algoritmo: uno dei 24 orientamenti propri del
cubo (matrice di rotazione 3x3 intera). Serve perche' dal solo stato
osservabile (P,f) la previsione delle mosse e' ambigua (esiste un bit
"nascosto" — quale delle due facce della pinza sta a +x — che non e'
misurabile dai sensori ma influenza l'esito di spin/ribaltamenti). Tracciando
un orientamento specifico e coerente nel tempo (inizializzato in modo
arbitrario, la posizione iniziale e' indifferente) l'ambiguita' sparisce:
verificato che ogni stato ha esattamente 5 mosse elementari a 90 gradi
distinte:
  spin+  / spin-   : rotazione attorno all'asse di pinza (U)      -> P invariato, f cambia
  tip_U+ / tip_U-   : rotazione attorno all'asse libero (W)        -> P e f cambiano entrambi
  tip_H              : rotazione attorno all'asse dell'indice (V)  -> f invariato, P cambia
                        (le due direzioni +90/-90 producono lo stesso esito osservabile)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from collections import deque
import time

import numpy as np

# ---------------------------------------------------------------------------
# Geometria del cubo (derivata dalla chiralita' 0-1-4 = oraria vista da fuori)
# ---------------------------------------------------------------------------
FACE_VEC = {
    0: (1, 0, 0), 3: (-1, 0, 0),   # asse A
    1: (0, 1, 0), 2: (0, -1, 0),   # asse B
    4: (0, 0, -1), 5: (0, 0, 1),   # asse C
}
VEC_FACE = {v: k for k, v in FACE_VEC.items()}
AXIS_OF = {0: 'A', 3: 'A', 1: 'B', 2: 'B', 4: 'C', 5: 'C'}
PAIR = {0: 3, 3: 0, 1: 2, 2: 1, 4: 5, 5: 4}


def _rot_u(s):
    return np.array([[1, 0, 0], [0, 0, -s], [0, s, 0]])


def _rot_v(s):
    return np.array([[0, 0, s], [0, 1, 0], [-s, 0, 0]])


def _rot_w(s):
    return np.array([[0, -s, 0], [s, 0, 0], [0, 0, 1]])


# le 6 rotazioni elementari (attorno agli assi mano u,v,w); v+ e v- collassano
# nello stesso esito osservabile (verificato) quindi le raggruppiamo sotto
# un'unica etichetta 'tip_H'.
_RAW_ROTATIONS = {
    'spin+': _rot_u(1), 'spin-': _rot_u(-1),
    'tip_H': _rot_v(1),          # v- darebbe lo stesso risultato osservabile
    'tip_U+': _rot_w(1), 'tip_U-': _rot_w(-1),
}

MOVE_DESCRIPTIONS = {
    'spin+': "spin attorno all'asse di pinza (verso +)",
    'spin-': "spin attorno all'asse di pinza (verso -)",
    'tip_H': "ribaltamento sull'asse dell'indice (la faccia indice non cambia)",
    'tip_U+': "ribaltamento sull'asse libero (verso +)",
    'tip_U-': "ribaltamento sull'asse libero (verso -)",
}


def _face_at(M, pos):
    body_vec = tuple(int(round(x)) for x in (M.T @ np.array(pos)))
    return VEC_FACE[body_vec]


def observe(M):
    """Dato un orientamento (matrice 3x3), ritorna lo stato osservabile (P, f)."""
    fx_plus = _face_at(M, (1, 0, 0))
    fx_minus = _face_at(M, (-1, 0, 0))
    assert PAIR[fx_plus] == fx_minus
    return AXIS_OF[fx_plus], _face_at(M, (0, 1, 0))


def _generate_all_orientations():
    seen = {}
    start = np.eye(3, dtype=int)
    seen[tuple(map(tuple, start))] = start
    changed = True
    while changed:
        changed = False
        for M in list(seen.values()):
            for R in _RAW_ROTATIONS.values():
                M2 = R @ M
                key = tuple(map(tuple, M2))
                if key not in seen:
                    seen[key] = M2
                    changed = True
    return list(seen.values())


# ---------------------------------------------------------------------------
# Precomputazione (avviene una sola volta, all'import del modulo)
# ---------------------------------------------------------------------------
_ORIENTATIONS = _generate_all_orientations()          # 24 matrici
_ORIENT_KEY = lambda M: tuple(map(tuple, M))
_ID_OF = {_ORIENT_KEY(M): i for i, M in enumerate(_ORIENTATIONS)}
_OBSERVE_OF = [observe(M) for M in _ORIENTATIONS]      # id -> (P,f)

# per ciascun id, i suoi 5 vicini: move_label -> (neighbor_id, neighbor_state)
_NEIGHBORS = []
for M in _ORIENTATIONS:
    nb = {}
    for label, R in _RAW_ROTATIONS.items():
        M2 = R @ M
        nid = _ID_OF[_ORIENT_KEY(M2)]
        nb[label] = (nid, _OBSERVE_OF[nid])
    _NEIGHBORS.append(nb)

# mappa stato osservabile -> lista di id compatibili (1 o 2 elementi)
_STATES_FOR_OBS = {}
for i, s in enumerate(_OBSERVE_OF):
    _STATES_FOR_OBS.setdefault(s, []).append(i)

ALL_OBSERVABLE_STATES = sorted(_STATES_FOR_OBS.keys())


def _bfs_shortest(start_id):
    """BFS da un id verso tutti gli altri: ritorna dict id -> (distanza, primo_passo)."""
    dist = {start_id: 0}
    first_step = {start_id: None}
    q = deque([start_id])
    while q:
        cur = q.popleft()
        for label, (nid, _) in _NEIGHBORS[cur].items():
            if nid not in dist:
                dist[nid] = dist[cur] + 1
                first_step[nid] = label if cur == start_id else first_step[cur]
                q.append(nid)
    return dist, first_step


_ALL_PAIRS_DIST = [{} for _ in range(24)]
_ALL_PAIRS_FIRSTSTEP = [{} for _ in range(24)]
for i in range(24):
    d, fs = _bfs_shortest(i)
    _ALL_PAIRS_DIST[i] = d
    _ALL_PAIRS_FIRSTSTEP[i] = fs


def bitmask_to_faces(bitmask: int) -> set[int]:
    """bit i (LSB=0) attivo -> faccia i toccata."""
    return {i for i in range(6) if (bitmask >> i) & 1}


def faces_to_state(touched: set[int]):
    """Valida 3 facce toccate (coppia opposta + 1) e ritorna (P, f), oppure None se non valido."""
    if len(touched) != 3:
        return None
    pairs_touched = [f for f in touched if PAIR[f] in touched]
    if len(pairs_touched) != 2:
        return None
    p_faces = set(pairs_touched)
    others = touched - p_faces
    if len(others) != 1:
        return None
    f = next(iter(others))
    p_face = next(iter(p_faces))
    return AXIS_OF[p_face], f


@dataclass
class Event:
    ok: bool
    reason: str = ""
    state: tuple | None = None
    move: str | None = None
    move_desc: str | None = None
    distance: int = 0
    ambiguous: bool = False
    raw_faces: set = field(default_factory=set)


class CubeTracker:
    """Mantiene lo stato interno (uno dei 24 orientamenti) e classifica le
    transizioni ad ogni nuova lettura valida."""

    def __init__(self):
        self._id = None  # id dell'orientamento interno corrente (0..23)

    @property
    def state(self):
        return None if self._id is None else _OBSERVE_OF[self._id]

    def reset(self):
        self._id = None

    def update(self, touched_faces: set[int]) -> Event:
        s_new = faces_to_state(touched_faces)
        if s_new is None:
            return Event(ok=False, reason="lettura non valida (non e' pinza+1)", raw_faces=touched_faces)

        if self._id is None:
            # prima lettura: nessun riferimento -> l'orientamento nascosto e'
            # scelto arbitrariamente (indifferente, come la posizione iniziale)
            self._id = _STATES_FOR_OBS[s_new][0]
            return Event(ok=True, reason="stato iniziale", state=s_new, raw_faces=touched_faces)

        # 1) prova match diretto fra i 5 vicini elementari
        for label, (nid, nstate) in _NEIGHBORS[self._id].items():
            if nstate == s_new:
                self._id = nid
                return Event(ok=True, state=s_new, move=label,
                             move_desc=MOVE_DESCRIPTIONS[label], distance=1,
                             raw_faces=touched_faces)

        if s_new == _OBSERVE_OF[self._id]:
            return Event(ok=True, state=s_new, move=None, move_desc="nessun movimento",
                         distance=0, raw_faces=touched_faces)

        # 2) piu' di una rotazione elementare tra due letture: BFS precalcolato
        candidates = _STATES_FOR_OBS[s_new]
        best_dist = None
        best_targets = []
        for cid in candidates:
            d = _ALL_PAIRS_DIST[self._id].get(cid)
            if d is None:
                continue
            if best_dist is None or d < best_dist:
                best_dist = d
                best_targets = [cid]
            elif d == best_dist:
                best_targets.append(cid)

        ambiguous = len(best_targets) > 1
        chosen = best_targets[0]
        first_move = _ALL_PAIRS_FIRSTSTEP[self._id].get(chosen)
        self._id = chosen
        return Event(ok=True, state=s_new, move=first_move,
                     move_desc=f"salto multiplo ({best_dist} mosse, primo passo: "
                               f"{MOVE_DESCRIPTIONS.get(first_move, '?')})",
                     distance=best_dist or 0, ambiguous=ambiguous, raw_faces=touched_faces)

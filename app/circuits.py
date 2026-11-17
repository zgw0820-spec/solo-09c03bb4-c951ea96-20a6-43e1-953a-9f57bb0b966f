"""Power-circuit investigation clue derivation.

A clue means: on one power circuit, alarms from **at least two different
cabinets** occurred within any 60-second window. Repeated alarms from a single
cabinet never make a clue on their own.

Everything here is a pure function over committed rows. The rows themselves
carry the circuit ownership snapshotted at submission time
(``alarms.cabinet_no`` / ``alarms.circuit_no``), and the append-only
``circuit_changes`` audit table decides which clues have been retired as
history, so a rewiring never rewrites old alarm evidence.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

# A common circuit is only suspicious when two *different* cabinets on it
# raised overheat alarms within this sliding window.
CLUE_WINDOW_SECONDS = 60.0

# SQLite REAL timestamps are floats; absorb only sub-microsecond rounding so an
# exact 60.0-second gap is still treated as "within the window".
_EPSILON = 1e-6


def build_clusters(
    alarm_rows: list[dict[str, Any]], window_seconds: float = CLUE_WINDOW_SECONDS
) -> list[list[dict[str, Any]]]:
    """Split one circuit's time-ordered alarms into proximity clusters.

    A cluster breaks whenever two consecutive alarms are more than
    ``window_seconds`` apart. A cluster qualifies as a clue only when it
    contains at least two distinct cabinets:

    * every qualifying cluster therefore contains a pair of different
      cabinets inside one 60-second window;
    * a burst of repeated alarms from one single cabinet is filtered out and
      can never inflate a clue;
    * chaining (A at 0s, B at 40s, A at 80s, ...) stays in one cluster, and
      each adjacent pair is itself within the window — exactly the "any
      60-second window" rule.
    """
    ordered = sorted(alarm_rows, key=lambda row: (row["created_real"], row["alarm_no"]))
    clusters: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for row in ordered:
        if current and row["created_real"] - current[-1]["created_real"] > window_seconds + _EPSILON:
            clusters.append(current)
            current = []
        current.append(row)
    if current:
        clusters.append(current)
    return [
        cluster
        for cluster in clusters
        if len({row["cabinet_no"] for row in cluster}) >= 2
    ]


def is_retired(cluster: list[dict[str, Any]], rewired_at: dict[str, list[float]]) -> bool:
    """A clue is historical when any participating cabinet was rewired at or
    after one of its alarms was raised.

    Rewiring timestamps come from the append-only audit table, so the verdict
    is derived purely from committed state and never mutates old evidence.
    """
    for row in cluster:
        for changed_real in rewired_at.get(row["cabinet_no"], ()):
            if changed_real >= row["created_real"]:
                return True
    return False


def render_cluster(circuit_no: str, cluster: list[dict[str, Any]], window_seconds: float) -> dict[str, Any]:
    """Shape a qualifying cluster into the API response body."""
    by_cabinet: dict[str, list[str]] = defaultdict(list)
    for row in cluster:
        by_cabinet[row["cabinet_no"]].append(row["alarm_no"])
    return {
        "circuit_no": circuit_no,
        "window_seconds": window_seconds,
        "window_started_at": cluster[0]["created_at"],
        "window_ended_at": cluster[-1]["created_at"],
        "cabinet_count": len(by_cabinet),
        "alarm_count": len(cluster),
        # Per-cabinet alarm numbers: the operator's per-cabinet investigation
        # list, with repeats preserved as separate alarm numbers.
        "cabinets": [
            {
                "cabinet_no": cabinet_no,
                "alarm_nos": alarm_nos,
                "alarm_count": len(alarm_nos),
            }
            for cabinet_no, alarm_nos in sorted(by_cabinet.items())
        ],
        "alarm_nos": [row["alarm_no"] for row in cluster],
    }

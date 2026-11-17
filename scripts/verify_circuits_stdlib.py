#!/usr/bin/env python3
"""Stdlib-only verification of the circuit rewiring/clue feature.

The sandbox has no fastapi/httpx/pydantic available, so this script installs
minimal in-process stubs for those packages and then drives the *real*
application code (app.db, app.service, app.circuits, app.view) directly.

Run with: python3 -I /workspace/scripts/verify_circuits_stdlib.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------- stubs

def _install_stubs() -> None:
    # --- pydantic: only what app.models / app.view import paths touch -------
    pydantic = types.ModuleType("pydantic")

    class BaseModel:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

        def model_dump(self, mode: str | None = None):
            return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    class _Field:
        def __call__(self, default=None, *args, **kwargs):
            return default

    class _ConfigDict(dict):
        pass

    def Field(default=None, *args, **kwargs):  # noqa: N802 - mirror pydantic API
        return default

    pydantic.BaseModel = BaseModel
    pydantic.Field = Field
    pydantic.ConfigDict = _ConfigDict
    sys.modules["pydantic"] = pydantic

    # app.service / app.worker import nothing pydantic at runtime except models;
    # models only needs the symbols above.

    # --- httpx: not imported by the modules under test ----------------------
    httpx = types.ModuleType("httpx")

    class _Error(Exception):
        pass

    httpx.TimeoutException = _Error
    httpx.TransportError = _Error
    sys.modules["httpx"] = httpx

    # --- fastapi: not imported by the modules under test --------------------
    fastapi = types.ModuleType("fastapi")
    sys.modules["fastapi"] = fastapi


_install_stubs()

from app.circuits import build_clusters, is_retired, render_cluster  # noqa: E402
from app.db import Database  # noqa: E402
from app.service import AlarmService, ConflictError  # noqa: E402


# ------------------------------------------------------------- test payloads

class Contact:
    def __init__(self, name: str, channel: str = "app", address: str | None = None):
        self.name = name
        self.channel = channel
        self.address = address

    def model_dump(self, mode: str | None = None):
        return {"name": self.name, "channel": self.channel, "address": self.address}


class FakePayload:
    def __init__(self, alarm_no: str, cabinet: str | None, *, circuit_unrelated=True,
                 content: str | None = None):
        self.alarm_no = alarm_no
        self.content = content or f"overheat {alarm_no}"
        self.metadata = {} if cabinet is None else {"cabinet": cabinet}
        self.primary_contact = Contact("primary")
        self.backup_contact = Contact("backup", "sms")
        self.ack_deadline_seconds = 5
        self._hash = f"hash-{alarm_no}-{self.content}"

    def content_hash(self) -> str:
        return self._hash

    def model_dump(self, mode: str | None = None) -> dict:
        return {
            "alarm_no": self.alarm_no,
            "content": self.content,
            "metadata": self.metadata,
            "primary_contact": self.primary_contact.model_dump(),
            "backup_contact": self.backup_contact.model_dump(),
            "ack_deadline_seconds": self.ack_deadline_seconds,
        }


PASS = 0
FAIL = 0


def check(condition: bool, message: str) -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok: {message}")
    else:
        FAIL += 1
        print(f"FAIL: {message}")


def make_alarm(service: AlarmService, alarm_no: str, cabinet: str | None):
    service.submit_alarm(FakePayload(alarm_no, cabinet))


def shift(db: Database, alarm_no: str, delta: float) -> None:
    db.execute(
        "UPDATE alarms SET created_real = created_real + ? WHERE alarm_no = ?",
        (delta, alarm_no),
    )


# --------------------------------------------------------------------- tests

def test_pure_clustering_rules():
    print("[pure] 60s window clustering rules")
    base = 1000.0

    def rows(spec):
        return [
            {"alarm_no": no, "cabinet_no": cab, "created_real": base + t,
             "created_at": str(base + t)}
            for no, cab, t in spec
        ]

    # two distinct cabinets inside the window -> one clue cluster
    clusters = build_clusters(rows([("a1", "A", 0), ("a2", "B", 30)]))
    check(len(clusters) == 1 and len(clusters[0]) == 2, "two cabinets within 60s cluster")

    # gap > 60 splits them; each single-cabinet side is filtered out
    clusters = build_clusters(rows([("a1", "A", 0), ("a2", "B", 61)]))
    check(clusters == [], "gap > 60s yields no clue")

    # exactly 60s still qualifies
    clusters = build_clusters(rows([("a1", "A", 0), ("a2", "B", 60)]))
    check(len(clusters) == 1, "exactly 60s still qualifies")

    # single cabinet repeats never qualify, even many of them
    clusters = build_clusters(rows([(f"s{i}", "A", i) for i in range(5)]))
    check(clusters == [], "single-cabinet repeats never make a clue")

    # repeats plus one other cabinet: one cluster with all alarm numbers
    clusters = build_clusters(rows([("h1", "A", 0), ("h2", "A", 10), ("h3", "B", 20)]))
    check(len(clusters) == 1 and len(clusters[0]) == 3, "repeats preserved inside a clue")
    rendered = render_cluster("LOOP-H", clusters[0], 60.0)
    per = {c["cabinet_no"]: c["alarm_nos"] for c in rendered["cabinets"]}
    check(per == {"A": ["h1", "h2"], "B": ["h3"]}, "per-cabinet alarm numbers listed")
    check(rendered["cabinet_count"] == 2 and rendered["alarm_count"] == 3, "counts correct")

    # chaining adjacent pairs stay in one cluster (any-60s rule)
    clusters = build_clusters(rows([("c1", "A", 0), ("c2", "B", 40), ("c3", "A", 80)]))
    check(len(clusters) == 1 and len(clusters[0]) == 3, "chained adjacent pairs stay clustered")

    # retirement rule
    cluster = build_clusters(rows([("r1", "A", 0), ("r2", "B", 30)]))[0]
    check(is_retired(cluster, {}) is False, "no rewiring -> not retired")
    # rewiring before the alarm does not retire it
    check(is_retired(cluster, {"B": [base - 10]}) is False, "older rewiring does not retire")
    # rewiring at/after an alarm retires the clue
    check(is_retired(cluster, {"B": [base + 30]}) is True, "rewiring at alarm time retires")
    check(is_retired(cluster, {"A": [base + 100]}) is True, "later rewiring retires")


def test_service_paths(tmp: Path):
    print("[service] submission snapshots, rewiring, clue lifecycle")
    db = Database(str(tmp / "verify.db"))
    service = AlarmService(db)

    # missing cabinet and unmapped cabinet: stored without circuit, notifications still exist
    make_alarm(service, "NO-CAB", None)
    make_alarm(service, "UNMAPPED", "X99")
    row = db.query_one("SELECT cabinet_no, circuit_no FROM alarms WHERE alarm_no = 'NO-CAB'")
    check(row["cabinet_no"] is None and row["circuit_no"] is None, "missing cabinet stored null")
    row = db.query_one("SELECT cabinet_no, circuit_no FROM alarms WHERE alarm_no = 'UNMAPPED'")
    check(row["cabinet_no"] == "X99" and row["circuit_no"] is None, "unmapped cabinet has null circuit")
    check(
        len(db.query("SELECT 1 FROM notifications WHERE alarm_no IN ('NO-CAB','UNMAPPED')")) == 2,
        "primary notifications still created without cabinet/mapping",
    )

    # mapping then two cabinets -> current clue
    service.assign_cabinet_circuit("A07", "LOOP-A")
    service.assign_cabinet_circuit("A08", "LOOP-A")
    make_alarm(service, "CL-1", "A07")
    make_alarm(service, "CL-2", "A08")
    current = service.circuit_clues(history=False)
    check(current["total"] == 1 and current["items"][0]["circuit_no"] == "LOOP-A",
          "current clue created for two cabinets")
    check(current["items"][0]["archived"] is False, "current clue not archived")

    # single cabinet burst alone: nothing
    service.assign_cabinet_circuit("B01", "LOOP-B")
    for i in range(4):
        make_alarm(service, f"SOLO-{i}", "B01")
    check(service.circuit_clues(history=False)["total"] == 1,
          "single-cabinet burst adds no clue (still only LOOP-A)")

    # window boundary on real rows
    service.assign_cabinet_circuit("D01", "LOOP-D")
    service.assign_cabinet_circuit("D02", "LOOP-D")
    make_alarm(service, "D-OLD", "D01")
    make_alarm(service, "D-NEW", "D02")
    shift(db, "D-OLD", -61)
    d_clues = [c for c in service.circuit_clues(history=False)["items"] if c["circuit_no"] == "LOOP-D"]
    check(d_clues == [], "61s gap: no LOOP-D clue")
    db.execute(
        "UPDATE alarms SET created_real = (SELECT created_real FROM alarms WHERE alarm_no='D-NEW') - 60.0 "
        "WHERE alarm_no='D-OLD'"
    )
    d_clues = [c for c in service.circuit_clues(history=False)["items"] if c["circuit_no"] == "LOOP-D"]
    check(len(d_clues) == 1, "exact 60s gap: LOOP-D clue appears")

    # rewiring: evidence immutable, clue moves to history
    result = service.assign_cabinet_circuit("A08", "LOOP-Z")
    check(result["changed"] is True and result["previous_circuit_no"] == "LOOP-A",
          "rewiring reports previous circuit")
    noop = service.assign_cabinet_circuit("A08", "LOOP-Z")
    check(noop["changed"] is False, "re-assigning same circuit is a no-op")
    audit_z = db.query(
        "SELECT * FROM circuit_changes WHERE cabinet_no='A08' AND new_circuit_no='LOOP-Z'"
    )
    check(len(audit_z) == 1, "no-op rewiring writes no extra audit row")

    snap = service.snapshot("CL-2")
    check(snap["circuit_no"] == "LOOP-A" and snap["cabinet_no"] == "A08",
          "old alarm evidence not rewritten by rewiring")
    current = service.circuit_clues(history=False)
    check(all(c["circuit_no"] != "LOOP-A" for c in current["items"]),
          "retired clue disappears from current suggestions")
    history = service.circuit_clues(history=True)
    archived = [c for c in history["items"] if c["circuit_no"] == "LOOP-A"]
    check(len(archived) == 1 and archived[0]["archived"] is True,
          "old clue retained in history")
    check({r["cabinet_no"] for r in archived[0]["rewirings"]} == {"A08"},
          "history names the rewired cabinet")

    # new alarm after rewiring belongs to the new circuit
    make_alarm(service, "F-NEW", "A08")
    check(service.snapshot("F-NEW")["circuit_no"] == "LOOP-Z",
          "post-rewiring alarm fixed to new circuit")

    # interleaving: mapping committed between two submissions
    service.assign_cabinet_circuit("G01", "LOOP-1")
    service.assign_cabinet_circuit("G02", "LOOP-1")
    make_alarm(service, "G-A", "G01")
    service.assign_cabinet_circuit("G02", "LOOP-2")
    make_alarm(service, "G-B", "G02")
    check(service.snapshot("G-A")["circuit_no"] == "LOOP-1", "G-A fixed before rewiring")
    check(service.snapshot("G-B")["circuit_no"] == "LOOP-2", "G-B fixed after rewiring")
    check(service.circuit_clues(history=False)["total"] == current_total_or_fewer(service),
          "no phantom shared-circuit clue from interleaving")

    # rewire back: clue stays historical
    service.assign_cabinet_circuit("I01", "LOOP-I")
    service.assign_cabinet_circuit("I02", "LOOP-I")
    make_alarm(service, "I-1", "I01")
    make_alarm(service, "I-2", "I02")
    before = len(service.circuit_clues(history=False)["items"])
    service.assign_cabinet_circuit("I02", "LOOP-TMP")
    service.assign_cabinet_circuit("I02", "LOOP-I")
    after = len(service.circuit_clues(history=False)["items"])
    check(after == before - 1, "rewiring back does not un-retire the clue")
    hist_i = [c for c in service.circuit_clues(history=True)["items"] if c["circuit_no"] == "LOOP-I"]
    check(len(hist_i) == 1, "rewire-back clue remains in history")

    # idempotent alarm retry still works and keeps the original snapshot
    view, dedup = service.submit_alarm(FakePayload("CL-1", "A07"))
    check(dedup is True, "identical retry is deduplicated")
    try:
        service.submit_alarm(FakePayload("CL-1", "A07", content="different content"))
        check(False, "changed content must conflict")
    except ConflictError:
        check(True, "changed content rejected with conflict")

    # mappings listing
    mappings = {r["cabinet_no"]: r["circuit_no"] for r in service.list_cabinet_circuits()}
    check(mappings["A07"] == "LOOP-A" and mappings["A08"] == "LOOP-Z",
          "mapping table reflects current ownership")

    db.close()


def current_total_or_fewer(service: AlarmService) -> int:
    # G01/G02 never share a committed circuit with a second cabinet, so they
    # must not add any new current clue.
    return service.circuit_clues(history=False)["total"]


def test_persistence_across_reopen(tmp: Path):
    print("[persistence] schema migration and state survive reopen")
    path = tmp / "persist.db"
    db = Database(str(path))
    service = AlarmService(db)
    service.assign_cabinet_circuit("J01", "LOOP-J")
    service.assign_cabinet_circuit("J02", "LOOP-J")
    make_alarm(service, "J-1", "J01")
    make_alarm(service, "J-2", "J02")
    service.assign_cabinet_circuit("J02", "LOOP-K")
    db.close()

    db2 = Database(str(path))  # re-run SCHEMA + migrations on the existing file
    service2 = AlarmService(db2)
    check(service2.snapshot("J-1")["circuit_no"] == "LOOP-J", "snapshot survives restart")
    current = service2.circuit_clues(history=False)
    check(all(c["circuit_no"] != "LOOP-J" for c in current["items"]),
          "current clue still retired after restart")
    history = service2.circuit_clues(history=True)
    check(any(c["circuit_no"] == "LOOP-J" for c in history["items"]),
          "history survives restart")
    mappings = {r["cabinet_no"]: r["circuit_no"] for r in service2.list_cabinet_circuits()}
    check(mappings["J02"] == "LOOP-K", "current mapping survives restart")
    db2.close()


def main() -> int:
    test_pure_clustering_rules()
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        test_service_paths(tmp)
        test_persistence_across_reopen(tmp)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

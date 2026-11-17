"""Power-circuit rewiring and investigation-clue semantics."""

from __future__ import annotations

from tests.conftest import ApiHarness
from tests.helpers import _status


async def assign(api: ApiHarness, cabinet: str, circuit: str):
    response = await api.client.put(f"/cabinets/{cabinet}/circuit", json={"circuit_no": circuit})
    assert response.status_code == 200, response.text
    return response.json()


async def clues(api: ApiHarness, path: str = "/circuit-clues", **params) -> dict:
    response = await api.client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def cabinet_payload(api: ApiHarness, alarm_no: str, cabinet: str, *, metadata=None, deadline: int = 5) -> dict:
    payload = api.alarm_payload(alarm_no, deadline=deadline)
    if metadata is not None:
        payload["metadata"] = metadata
    else:
        payload["metadata"] = {"cabinet": cabinet}
    return payload


async def test_missing_cabinet_or_mapping_still_notifies_and_has_no_circuit(api: ApiHarness):
    # No cabinet at all.
    no_cab = await api.client.post(
        "/alarms", json=cabinet_payload(api, "C-NO-CAB", "", metadata={"temperature_c": 80})
    )
    assert no_cab.status_code == 201
    body = no_cab.json()
    assert body["cabinet_no"] is None and body["circuit_no"] is None
    assert any("cabinet" in w for w in body["warnings"])

    # Cabinet present but no mapping yet.
    unmapped = await api.client.post("/alarms", json=cabinet_payload(api, "C-UNMAPPED", "X99"))
    assert unmapped.status_code == 201
    body = unmapped.json()
    assert body["cabinet_no"] == "X99" and body["circuit_no"] is None
    assert any("回路映射" in w for w in body["warnings"])

    # Primary notifications still flow for both.
    assert await api.wait_until(lambda: _status(api, "C-NO-CAB", "awaiting_confirmation"))
    assert await api.wait_until(lambda: _status(api, "C-UNMAPPED", "awaiting_confirmation"))
    assert len(api.fake.role_notifications("primary")) == 2

    listing = await clues(api)
    assert listing["total"] == 0


async def test_two_distinct_cabinets_within_window_create_a_clue(api: ApiHarness):
    await assign(api, "A07", "LOOP-A")
    await assign(api, "A08", "LOOP-A")
    await api.client.post("/alarms", json=cabinet_payload(api, "CL-A1", "A07"))
    await api.client.post("/alarms", json=cabinet_payload(api, "CL-A2", "A08"))

    listing = await clues(api)
    assert listing["total"] == 1
    clue = listing["items"][0]
    assert clue["circuit_no"] == "LOOP-A"
    assert clue["cabinet_count"] == 2
    assert clue["alarm_count"] == 2
    assert clue["archived"] is False
    cabinets = {c["cabinet_no"]: c["alarm_nos"] for c in clue["cabinets"]}
    assert cabinets == {"A07": ["CL-A1"], "A08": ["CL-A2"]}
    assert set(clue["alarm_nos"]) == {"CL-A1", "CL-A2"}


async def test_single_cabinet_repeats_never_make_a_clue(api: ApiHarness):
    await assign(api, "B01", "LOOP-B")
    for index in range(4):
        await api.client.post(
            "/alarms", json=cabinet_payload(api, f"SOLO-{index}", "B01", deadline=5)
        )
    listing = await clues(api)
    assert listing["total"] == 0
    assert listing["history"] is False


async def test_sixty_second_window_boundary(api: ApiHarness):
    await assign(api, "D01", "LOOP-D")
    await assign(api, "D02", "LOOP-D")
    db = api.app.state.db
    await api.client.post("/alarms", json=cabinet_payload(api, "D-OLD", "D01"))
    await api.client.post("/alarms", json=cabinet_payload(api, "D-NEW", "D02"))

    # Push the first alarm 61 s into the past: gap > 60 s, no clue.
    db.execute(
        "UPDATE alarms SET created_real = created_real - 61 WHERE alarm_no = ?", ("D-OLD",)
    )
    listing = await clues(api)
    assert listing["total"] == 0

    # Exactly 60 s apart still counts (the rule breaks only when strictly greater).
    db.execute(
        "UPDATE alarms SET created_real = (SELECT created_real FROM alarms WHERE alarm_no = 'D-NEW') - 60 "
        "WHERE alarm_no = 'D-OLD'"
    )
    listing = await clues(api)
    assert listing["total"] == 1


async def test_rewiring_keeps_old_evidence_and_retires_the_clue_to_history(api: ApiHarness):
    await assign(api, "E01", "LOOP-E")
    await assign(api, "E02", "LOOP-E")
    db = api.app.state.db
    # First assignments are not rewirings: the audit table stays empty.
    assert db.query("SELECT COUNT(*) AS n FROM circuit_changes")[0]["n"] == 0

    await api.client.post("/alarms", json=cabinet_payload(api, "E-ALM-1", "E01"))
    await api.client.post("/alarms", json=cabinet_payload(api, "E-ALM-2", "E02"))
    before = await clues(api)
    assert before["total"] == 1

    # Rewire E02 away to a different loop.
    result = await assign(api, "E02", "LOOP-Z")
    assert result["changed"] is True
    assert result["previous_circuit_no"] == "LOOP-E"
    audit = db.query("SELECT * FROM circuit_changes WHERE cabinet_no='E02'")
    assert len(audit) == 1 and audit[0]["old_circuit_no"] == "LOOP-E"

    # Idempotent no-op: assigning the same circuit again changes nothing.
    again = await assign(api, "E02", "LOOP-Z")
    assert again["changed"] is False
    assert again["changed_at"] is None
    assert len(db.query("SELECT * FROM circuit_changes WHERE cabinet_no='E02'")) == 1

    # Old alarm evidence is untouched: the stored circuit is still LOOP-E.
    old_alarm = (await api.client.get("/alarms/E-ALM-2")).json()
    assert old_alarm["circuit_no"] == "LOOP-E"
    assert old_alarm["cabinet_no"] == "E02"

    current = await clues(api)
    assert current["total"] == 0
    history = await clues(api, "/circuit-clues/history")
    assert history["total"] == 1
    archived = history["items"][0]
    assert archived["circuit_no"] == "LOOP-E"
    assert archived["archived"] is True
    assert archived["archive_reason"]
    assert {r["cabinet_no"] for r in archived["rewirings"]} == {"E02"}
    assert set(archived["alarm_nos"]) == {"E-ALM-1", "E-ALM-2"}

    # The current mapping table reflects the new ownership.
    mappings = (await api.client.get("/cabinets/circuit-mappings")).json()
    lookup = {row["cabinet_no"]: row["circuit_no"] for row in mappings}
    assert lookup["E01"] == "LOOP-E"
    assert lookup["E02"] == "LOOP-Z"


async def test_new_alarm_after_rewiring_uses_the_new_circuit(api: ApiHarness):
    await assign(api, "F01", "LOOP-F")
    await assign(api, "F02", "LOOP-F")
    await api.client.post("/alarms", json=cabinet_payload(api, "F-OLD-1", "F01"))
    await api.client.post("/alarms", json=cabinet_payload(api, "F-OLD-2", "F02"))
    await assign(api, "F02", "LOOP-G")

    # A fresh F02 alarm belongs to LOOP-G; LOOP-F now has only one cabinet.
    await api.client.post("/alarms", json=cabinet_payload(api, "F-NEW", "F02"))
    fresh = (await api.client.get("/alarms/F-NEW")).json()
    assert fresh["circuit_no"] == "LOOP-G"

    current = await clues(api)
    assert all(item["circuit_no"] != "LOOP-F" for item in current["items"])
    history = await clues(api, "/circuit-clues/history")
    assert any(item["circuit_no"] == "LOOP-F" for item in history["items"])


async def test_interleaved_submissions_and_rewiring_use_one_committed_state(api: ApiHarness):
    """Alarm ownership is always the mapping committed at that moment, and the
    clue query reads a single consistent snapshot."""
    await assign(api, "G01", "LOOP-1")
    await assign(api, "G02", "LOOP-1")
    await api.client.post("/alarms", json=cabinet_payload(api, "G-A", "G01"))
    await assign(api, "G02", "LOOP-2")
    await api.client.post("/alarms", json=cabinet_payload(api, "G-B", "G02"))

    view_a = (await api.client.get("/alarms/G-A")).json()
    view_b = (await api.client.get("/alarms/G-B")).json()
    assert view_a["circuit_no"] == "LOOP-1"
    assert view_b["circuit_no"] == "LOOP-2"

    # No current clue: the two alarms never shared a circuit in any committed state.
    current = await clues(api)
    assert current["total"] == 0
    # And nothing to archive either: no qualifying LOOP-1 cluster survives.
    history = await clues(api, "/circuit-clues/history")
    assert history["total"] == 0


async def test_clue_with_repeats_still_lists_every_alarm_number_per_cabinet(api: ApiHarness):
    await assign(api, "H01", "LOOP-H")
    await assign(api, "H02", "LOOP-H")
    await api.client.post("/alarms", json=cabinet_payload(api, "H-1", "H01"))
    await api.client.post("/alarms", json=cabinet_payload(api, "H-2", "H01"))  # repeat, same cabinet
    await api.client.post("/alarms", json=cabinet_payload(api, "H-3", "H02"))

    listing = await clues(api)
    assert listing["total"] == 1
    clue = listing["items"][0]
    assert clue["cabinet_count"] == 2
    assert clue["alarm_count"] == 3
    per_cabinet = {c["cabinet_no"]: c["alarm_nos"] for c in clue["cabinets"]}
    assert per_cabinet["H01"] == ["H-1", "H-2"]
    assert per_cabinet["H02"] == ["H-3"]


async def test_rewired_back_clue_stays_historical(api: ApiHarness):
    """Once a clue has been retired by a rewiring, rewiring back does not
    un-retire it: the audit trail keeps the intermediate change."""
    await assign(api, "I01", "LOOP-I")
    await assign(api, "I02", "LOOP-I")
    await api.client.post("/alarms", json=cabinet_payload(api, "I-1", "I01"))
    await api.client.post("/alarms", json=cabinet_payload(api, "I-2", "I02"))
    assert (await clues(api))["total"] == 1

    await assign(api, "I02", "LOOP-TMP")
    await assign(api, "I02", "LOOP-I")

    assert (await clues(api))["total"] == 0
    history = await clues(api, "/circuit-clues/history")
    assert history["total"] == 1
    assert history["items"][0]["circuit_no"] == "LOOP-I"


async def test_endpoints_are_in_openapi(api: ApiHarness):
    schema = (await api.client.get("/openapi.json")).json()
    paths = schema["paths"]
    assert "/cabinets/{cabinet_no}/circuit" in paths
    assert "/cabinets/circuit-mappings" in paths
    assert "/circuit-clues" in paths
    assert "/circuit-clues/history" in paths
    # Path ordering: static segment must not be swallowed by the parameter route.
    mappings = await api.client.get("/cabinets/circuit-mappings")
    assert mappings.status_code == 200

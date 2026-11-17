"""Domain logic for alarm intake, confirmation and escalation.

Design invariants
-----------------
1. Submissions are idempotent by ``alarm_no`` + content fingerprint. An exact
   retry returns the existing alarm; a differing body is rejected with 409.
2. A notification is persisted (with its full POST payload) *before* any HTTP
   call. The notification number is the gateway idempotency key, so re-posting
   after a crash can never page twice.
3. The escalation clock starts only when the gateway has confirmed that the
   primary notification was accepted. An unproven outcome is recorded as
   ``unknown`` and re-checked through the gateway lookup endpoint; it is never
   treated as delivered and never starts the timer.
4. Confirmation and escalation are serialised by SQLite write transactions and
   arbitrated by conditional UPDATEs. If the confirmation wins, no backup
   notification is created and a queued-but-undispatched backup is cancelled.
   If escalation wins, the confirmation is recorded but creates no new
   delivery; an already in-flight backup is reconciled and reported truthfully.
5. At most one backup notification exists per alarm (unique index), so a
   deadline can only ever enqueue a single backup delivery.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from .circuits import CLUE_WINDOW_SECONDS, build_clusters, is_retired, render_cluster
from .db import Database, iso_from_real, parse_ts, utc_now
from .view import build_alarm_view


class ConflictError(Exception):
    """Same alarm number, different content."""


class NotFoundError(Exception):
    pass


class InvalidStateError(Exception):
    pass


def _cabinet_from_metadata(metadata: Any) -> str | None:
    """Extract ``metadata.cabinet``.

    Only string or integer cabinet ids are recognised as mappable. A missing
    cabinet (or an unusable type) yields None: such alarms still go through the
    normal primary/backup notification path, they simply cannot be attached to a
    circuit for clue derivation.
    """
    if not isinstance(metadata, dict):
        return None
    cabinet = metadata.get("cabinet")
    if isinstance(cabinet, str):
        cabinet = cabinet.strip()
        return cabinet or None
    if isinstance(cabinet, int) and not isinstance(cabinet, bool):
        return str(cabinet)
    return None


def _new_notification_no(alarm_no: str, role: str) -> str:
    # The random suffix makes the number globally unique. It is generated once
    # and persisted, so every retry of this notification uses the same number.
    return f"N-{alarm_no}-{role}-{secrets.token_hex(6)}"


class AlarmService:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _event(conn, alarm_no: str, kind: str, detail: str | None = None) -> None:
        # Same collapse rule as notification history: a repeated unknown-receipt
        # event on consecutive ticks is recorded once. A different kind in
        # between starts a new entry, so state changes remain fully ordered.
        previous = conn.execute(
            "SELECT kind, detail FROM events WHERE alarm_no = ? ORDER BY id DESC LIMIT 1",
            (alarm_no,),
        ).fetchone()
        if previous is not None and previous["kind"] == kind and previous["detail"] == detail:
            conn.execute(
                "UPDATE events SET at = ? WHERE id = "
                "(SELECT id FROM events WHERE alarm_no = ? ORDER BY id DESC LIMIT 1)",
                (utc_now(), alarm_no),
            )
            return
        conn.execute(
            "INSERT INTO events (alarm_no, at, kind, detail) VALUES (?, ?, ?, ?)",
            (alarm_no, utc_now(), kind, detail),
        )

    @staticmethod
    def _append_history(history_json: str | None, kind: str, detail: str | None) -> str:
        try:
            history = json.loads(history_json) if history_json else []
        except ValueError:
            history = []
        # Collapse consecutive identical entries: an unresolved notification is
        # re-checked on every tick, and the history must not grow without bound.
        entry = {"at": utc_now(), "kind": kind, "detail": detail}
        if history and history[-1].get("kind") == kind and history[-1].get("detail") == detail:
            history[-1]["at"] = entry["at"]
        else:
            history.append(entry)
        return json.dumps(history, ensure_ascii=False)

    @staticmethod
    def _notification_row(
        *,
        alarm_no: str,
        role: str,
        contact: dict[str, Any],
        content_hash: str,
        payload: dict[str, Any],
        created_by: str,
    ) -> dict[str, Any]:
        now = utc_now()
        now_real = parse_ts(now)
        return {
            "alarm_no": alarm_no,
            "notification_no": _new_notification_no(alarm_no, role),
            "role": role,
            "contact_json": json.dumps(contact, ensure_ascii=False, sort_keys=True),
            "content_hash": content_hash,
            "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "created_at": now,
            "created_real": now_real,
            "state": "pending",
            "attempts": 0,
            "created_by": created_by,
            "history_json": json.dumps(
                [{"at": now, "kind": "enqueued", "detail": f"created by {created_by}"}],
                ensure_ascii=False,
            ),
            "next_attempt_at": now_real,
        }

    def _insert_notification(self, conn, row: dict[str, Any]) -> None:
        columns = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        conn.execute(
            f"INSERT INTO notifications ({columns}) VALUES ({placeholders})",
            tuple(row.values()),
        )

    @staticmethod
    def _deadline_seconds(conn, alarm_no: str) -> int:
        row = conn.execute(
            "SELECT ack_deadline_seconds FROM alarms WHERE alarm_no = ?", (alarm_no,)
        ).fetchone()
        return int(row["ack_deadline_seconds"])

    # ------------------------------------------------------------------ intake

    def submit_alarm(self, payload) -> tuple[dict[str, Any], bool]:
        """Return (alarm view, deduplicated)."""
        content_hash = payload.content_hash()
        now = utc_now()
        now_real = parse_ts(now)
        cabinet_no = _cabinet_from_metadata(payload.metadata)

        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM alarms WHERE alarm_no = ?", (payload.alarm_no,)
            ).fetchone()
            if existing is not None:
                if existing["content_hash"] != content_hash:
                    raise ConflictError(
                        f"alarm {payload.alarm_no!r} already exists with different content"
                    )
                return self._snapshot(conn, payload.alarm_no, now_real), True

            # Ownership is resolved and snapshotted in the same transaction that
            # creates the alarm, so the evidence always reflects a committed
            # mapping state. A later rewiring never rewrites these two columns.
            circuit_no = None
            if cabinet_no is not None:
                mapping = conn.execute(
                    "SELECT circuit_no FROM cabinet_circuits WHERE cabinet_no = ?",
                    (cabinet_no,),
                ).fetchone()
                if mapping is not None:
                    circuit_no = mapping["circuit_no"]

            conn.execute(
                """
                INSERT INTO alarms (
                    alarm_no, content_hash, payload_json, primary_contact,
                    backup_contact, ack_deadline_seconds, created_at, created_real,
                    cabinet_no, circuit_no
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.alarm_no,
                    content_hash,
                    json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
                    json.dumps(payload.primary_contact.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
                    json.dumps(payload.backup_contact.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
                    payload.ack_deadline_seconds,
                    now,
                    now_real,
                    cabinet_no,
                    circuit_no,
                ),
            )

            primary = self._notification_row(
                alarm_no=payload.alarm_no,
                role="primary",
                contact=payload.primary_contact.model_dump(mode="json"),
                content_hash=content_hash,
                payload={
                    "content": payload.content,
                    "metadata": payload.metadata,
                    "ack_deadline_seconds": payload.ack_deadline_seconds,
                },
                created_by="initial",
            )
            self._insert_notification(conn, primary)
            self._event(
                conn,
                payload.alarm_no,
                "submitted",
                f"primary notification {primary['notification_no']}",
            )
            if cabinet_no is None:
                self._event(
                    conn,
                    payload.alarm_no,
                    "cabinet_missing",
                    "metadata.cabinet missing or unusable; normal notification only, no circuit clue",
                )
            elif circuit_no is None:
                self._event(
                    conn,
                    payload.alarm_no,
                    "cabinet_unmapped",
                    f"cabinet {cabinet_no} has no circuit mapping; normal notification only, no circuit clue",
                )
            else:
                self._event(
                    conn,
                    payload.alarm_no,
                    "circuit_snapshotted",
                    f"cabinet {cabinet_no} fixed to circuit {circuit_no} at submission",
                )
            return self._snapshot(conn, payload.alarm_no, now_real), False

    # ----------------------------------------------------------------- confirm

    def confirm(self, alarm_no: str, confirmed_by: str, note: str | None) -> dict[str, Any]:
        """Confirm an alarm and atomically prevent any new backup delivery."""
        now = utc_now()
        now_real = parse_ts(now)
        with self.db.transaction() as conn:
            alarm = conn.execute("SELECT * FROM alarms WHERE alarm_no = ?", (alarm_no,)).fetchone()
            if alarm is None:
                raise NotFoundError(alarm_no)

            if alarm["confirmed_at"] is not None:
                # Idempotent repeat confirmation: report the stored result.
                return self._snapshot(conn, alarm_no, now_real)

            updated = conn.execute(
                """
                UPDATE alarms SET confirmed_at = ?, confirmed_real = ?, confirmed_by = ?
                 WHERE alarm_no = ? AND confirmed_at IS NULL AND escalated_at IS NULL
                """,
                (now, now_real, confirmed_by, alarm_no),
            ).rowcount

            if updated:
                self._event(conn, alarm_no, "confirmed", f"by {confirmed_by}" + (f": {note}" if note else ""))
                return self._snapshot(conn, alarm_no, now_real)

            # Escalation already committed. Record the late confirmation but
            # create no new notification. A backup that is still only queued
            # (never handed to the gateway) is cancelled; one that is already
            # in flight is left for reconciliation and shown truthfully.
            conn.execute(
                "UPDATE alarms SET confirmed_at = ?, confirmed_real = ?, confirmed_by = ? "
                " WHERE alarm_no = ? AND confirmed_at IS NULL",
                (now, now_real, confirmed_by, alarm_no),
            )
            backup_row = conn.execute(
                "SELECT id, state, history_json FROM notifications "
                " WHERE alarm_no = ? AND role = 'backup'",
                (alarm_no,),
            ).fetchone()
            cancelled = False
            if backup_row is not None and backup_row["state"] == "pending":
                conn.execute(
                    """
                    UPDATE notifications
                       SET state = 'cancelled',
                           detail = 'cancelled before dispatch: alarm confirmed after escalation',
                           history_json = ?, next_attempt_at = NULL, lease_until = NULL
                     WHERE id = ? AND state = 'pending'
                    """,
                    (
                        self._append_history(
                            backup_row["history_json"],
                            "cancelled",
                            "alarm confirmed after escalation, before dispatch",
                        ),
                        backup_row["id"],
                    ),
                )
                cancelled = True
            self._event(
                conn,
                alarm_no,
                "confirmed_after_escalation",
                f"by {confirmed_by} after the escalation deadline"
                + (
                    "; queued backup cancelled before dispatch"
                    if cancelled
                    else "; backup already dispatched, cannot be recalled"
                    if backup_row is not None
                    else ""
                ),
            )
            return self._snapshot(conn, alarm_no, now_real)

    # ---------------------------------------------------------------- deadline

    def due_escalations(self, now_real: float) -> list[str]:
        rows = self.db.query(
            """
            SELECT alarm_no FROM alarms
             WHERE primary_ack_deadline_real IS NOT NULL
               AND primary_ack_deadline_real <= ?
               AND confirmed_at IS NULL
               AND escalated_at IS NULL
            """,
            (now_real,),
        )
        return [row["alarm_no"] for row in rows]

    def escalate(self, alarm_no: str, now_real: float) -> bool:
        """Create the single backup notification for an overdue alarm.

        Returns True when this call enqueued the backup. The conditional UPDATE
        guarantees exactly one enqueue even with concurrent workers, and loses
        cleanly to a simultaneous confirmation.
        """
        now = utc_now()
        with self.db.transaction() as conn:
            alarm = conn.execute("SELECT * FROM alarms WHERE alarm_no = ?", (alarm_no,)).fetchone()
            if alarm is None:
                return False
            if alarm["confirmed_at"] is not None or alarm["escalated_at"] is not None:
                return False
            if alarm["primary_ack_deadline_real"] is None or alarm["primary_ack_deadline_real"] > now_real:
                return False

            updated = conn.execute(
                "UPDATE alarms SET escalated_at = ?, escalated_real = ? "
                " WHERE alarm_no = ? AND confirmed_at IS NULL AND escalated_at IS NULL",
                (now, now_real, alarm_no),
            ).rowcount
            if not updated:
                return False

            existing = conn.execute(
                "SELECT 1 FROM notifications WHERE alarm_no = ? AND role = 'backup'", (alarm_no,)
            ).fetchone()
            if existing is not None:
                return True

            backup_contact = json.loads(alarm["backup_contact"])
            payload = json.loads(alarm["payload_json"])
            backup = self._notification_row(
                alarm_no=alarm_no,
                role="backup",
                contact=backup_contact,
                content_hash=alarm["content_hash"],
                payload={
                    "content": payload["content"],
                    "metadata": payload.get("metadata", {}),
                    "ack_deadline_seconds": payload["ack_deadline_seconds"],
                    "escalation_reason": "primary_not_confirmed",
                },
                created_by="escalation",
            )
            self._insert_notification(conn, backup)
            self._event(
                conn,
                alarm_no,
                "escalated",
                f"backup notification {backup['notification_no']} queued after deadline",
            )
            return True

    # ------------------------------------------------------------ notification

    def notifications_needing_action(self, now_real: float) -> list[dict[str, Any]]:
        """Pending posts, unknown receipts, and expired in-flight leases."""
        rows = self.db.query(
            """
            SELECT * FROM notifications
             WHERE (state = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                OR (state = 'unknown' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                OR (state = 'in_flight' AND (lease_until IS NULL OR lease_until <= ?))
             ORDER BY created_real, id
            """,
            (now_real, now_real, now_real),
        )
        return [dict(row) for row in rows]

    def claim_notification(
        self,
        notification_id: int,
        now_real: float,
        lease_seconds: float,
        *,
        counts_as_post_attempt: bool = False,
    ) -> bool:
        """Move a notification to in_flight. Must happen outside any HTTP wait.

        ``attempts`` counts gateway POSTs only; claiming a row to *look up* an
        unresolved receipt is not a delivery attempt.
        """
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT state, lease_until FROM notifications WHERE id = ?", (notification_id,)
            ).fetchone()
            if row is None:
                return False
            claimable = row["state"] in {"pending", "unknown"} or (
                row["state"] == "in_flight" and (row["lease_until"] is None or row["lease_until"] <= now_real)
            )
            if not claimable:
                return False
            conn.execute(
                "UPDATE notifications SET state = 'in_flight', lease_until = ?, last_attempt_at = ?, "
                " attempts = attempts + ? WHERE id = ?",
                (
                    now_real + lease_seconds,
                    utc_now(),
                    1 if counts_as_post_attempt else 0,
                    notification_id,
                ),
            )
            return True

    def note_post_attempt(self, notification_id: int) -> None:
        """Count a real gateway POST. Committed before the call, so even a crash
        in the middle of the request leaves an audit trail."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE notifications SET attempts = attempts + 1, last_attempt_at = ? WHERE id = ?",
                (utc_now(), notification_id),
            )

    def mark_notification(
        self,
        notification_id: int,
        state: str,
        gateway_status: str | None,
        detail: str | None,
        *,
        accepted_real: float | None = None,
        retry_at: float | None = None,
        lookup_miss: bool = False,
    ) -> dict[str, Any] | None:
        """Persist an outcome. A confirmed primary acceptance starts the clock
        exactly once, atomically with the state change."""
        now = utc_now()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM notifications WHERE id = ?", (notification_id,)
            ).fetchone()
            if row is None:
                return None

            history = self._append_history(
                row["history_json"],
                state,
                f"gateway_status={gateway_status}" + (f"; {detail}" if detail else ""),
            )
            accepted_at = row["accepted_at"]
            accepted_real_value = row["accepted_real"]
            if state == "accepted" and accepted_at is None:
                accepted_real_value = accepted_real if accepted_real is not None else parse_ts(now)
                accepted_at = iso_from_real(accepted_real_value)

            next_attempt_at = None
            if state in {"pending", "unknown", "in_flight"}:
                next_attempt_at = retry_at if retry_at is not None else parse_ts(now)

            if lookup_miss:
                lookup_misses = row["lookup_misses"] + 1
            elif state in {"accepted", "pending", "failed"}:
                # A fresh post or a resolution resets the miss counter.
                lookup_misses = 0
            else:
                lookup_misses = row["lookup_misses"]

            conn.execute(
                """
                UPDATE notifications
                   SET state = ?, gateway_status = ?, detail = ?, history_json = ?,
                       next_attempt_at = ?, accepted_at = ?, accepted_real = ?,
                       lease_until = NULL, lookup_misses = ?
                 WHERE id = ?
                """,
                (
                    state,
                    gateway_status,
                    detail,
                    history,
                    next_attempt_at,
                    accepted_at,
                    accepted_real_value,
                    lookup_misses,
                    notification_id,
                ),
            )

            if state == "accepted":
                if row["role"] == "primary":
                    deadline_seconds = self._deadline_seconds(conn, row["alarm_no"])
                    accepted_real_value = accepted_real_value or parse_ts(now)
                    conn.execute(
                        """
                        UPDATE alarms
                           SET primary_accepted_at = COALESCE(primary_accepted_at, ?),
                               primary_accepted_real = COALESCE(primary_accepted_real, ?),
                               primary_ack_deadline_at = COALESCE(primary_ack_deadline_at, ?),
                               primary_ack_deadline_real = COALESCE(primary_ack_deadline_real, ?)
                         WHERE alarm_no = ?
                        """,
                        (
                            iso_from_real(accepted_real_value),
                            accepted_real_value,
                            iso_from_real(accepted_real_value + deadline_seconds),
                            accepted_real_value + deadline_seconds,
                            row["alarm_no"],
                        ),
                    )
                    self._event(
                        conn,
                        row["alarm_no"],
                        "primary_accepted",
                        f"{row['notification_no']} accepted; {deadline_seconds}s confirmation clock started",
                    )
                else:
                    self._event(
                        conn,
                        row["alarm_no"],
                        "backup_accepted",
                        f"{row['notification_no']} accepted",
                    )
            elif state == "failed":
                self._event(
                    conn,
                    row["alarm_no"],
                    f"{row['role']}_rejected",
                    f"{row['notification_no']}: {detail}",
                )
            elif state == "unknown":
                self._event(
                    conn,
                    row["alarm_no"],
                    f"{row['role']}_receipt_unknown",
                    f"{row['notification_no']}: {detail} (no escalation timer change)",
                )

            return dict(
                conn.execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)).fetchone()
            )

    # ------------------------------------------------------------- snapshots
    #
    # The following section was the snapshot boundary; circuit ownership and
    # clue APIs are defined just above it.

    # ------------------------------------------------- cabinet rewiring/clues

    def assign_cabinet_circuit(self, cabinet_no: str, circuit_no: str) -> dict[str, Any]:
        """Assign (or rewire) a cabinet onto a power circuit.

        Only the current mapping and the append-only audit table change. Alarm
        rows keep their snapshotted ``circuit_no`` and their notification
        evidence forever, so an old clue involving this cabinet is retired as
        history rather than rewritten. A no-op assignment to the same circuit
        changes nothing and records no audit row.
        """
        now = utc_now()
        now_real = parse_ts(now)
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT circuit_no FROM cabinet_circuits WHERE cabinet_no = ?",
                (cabinet_no,),
            ).fetchone()
            old_circuit = existing["circuit_no"] if existing is not None else None
            changed = old_circuit != circuit_no

            if changed:
                conn.execute(
                    """
                    INSERT INTO cabinet_circuits (cabinet_no, circuit_no, assigned_at, assigned_real)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(cabinet_no) DO UPDATE SET
                        circuit_no = excluded.circuit_no,
                        assigned_at = excluded.assigned_at,
                        assigned_real = excluded.assigned_real
                    """,
                    (cabinet_no, circuit_no, now, now_real),
                )
                if old_circuit is not None:
                    # The first assignment is not a rewiring: only moves between
                    # two already-known circuits enter the audit trail that
                    # retires clues.
                    conn.execute(
                        """
                        INSERT INTO circuit_changes
                            (cabinet_no, old_circuit_no, new_circuit_no, changed_at, changed_real)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (cabinet_no, old_circuit, circuit_no, now, now_real),
                    )
            return {
                "cabinet_no": cabinet_no,
                "circuit_no": circuit_no,
                "previous_circuit_no": old_circuit,
                "changed": changed,
                "changed_at": now if changed else None,
            }

    def list_cabinet_circuits(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT cabinet_no, circuit_no, assigned_at, assigned_real "
            "FROM cabinet_circuits ORDER BY cabinet_no"
        )
        return [dict(row) for row in rows]

    def circuit_clues(
        self,
        *,
        history: bool,
        window_seconds: float = CLUE_WINDOW_SECONDS,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return current investigation clues (or retired historical clues).

        The whole derivation runs inside one read-only deferred transaction, so
        the alarm ownership and the rewiring audit rows always come from the
        same committed state even when submissions and rewirings interleave.
        """
        with self.db.read_transaction() as conn:
            alarm_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT alarm_no, cabinet_no, circuit_no, created_at, created_real "
                    "FROM alarms "
                    "WHERE circuit_no IS NOT NULL AND cabinet_no IS NOT NULL "
                    "ORDER BY circuit_no, created_real, alarm_no"
                ).fetchall()
            ]
            change_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT cabinet_no, changed_real FROM circuit_changes"
                ).fetchall()
            ]

        rewired_at: dict[str, list[float]] = {}
        for row in change_rows:
            rewired_at.setdefault(row["cabinet_no"], []).append(row["changed_real"])

        by_circuit: dict[str, list[dict[str, Any]]] = {}
        for row in alarm_rows:
            by_circuit.setdefault(row["circuit_no"], []).append(row)

        clues: list[dict[str, Any]] = []
        for circuit_no in sorted(by_circuit):
            for cluster in build_clusters(by_circuit[circuit_no], window_seconds):
                retired = is_retired(cluster, rewired_at)
                if retired != history:
                    continue
                rendered = render_cluster(circuit_no, cluster, window_seconds)
                if retired:
                    # Name the earliest rewiring that retired the clue, once per
                    # cabinet (a cabinet may contribute several alarms).
                    reasons: dict[str, str] = {}
                    for row in cluster:
                        later = [t for t in rewired_at.get(row["cabinet_no"], ()) if t >= row["created_real"]]
                        if later:
                            stamp = iso_from_real(min(later))
                            previous = reasons.get(row["cabinet_no"])
                            if previous is None or stamp < previous:
                                reasons[row["cabinet_no"]] = stamp
                    rendered["archived"] = True
                    rendered["archive_reason"] = "cabinet rewired after the contributing alarm(s)"
                    rendered["rewirings"] = [
                        {"cabinet_no": cabinet, "rewired_at": stamp}
                        for cabinet, stamp in sorted(reasons.items(), key=lambda item: item[1])
                    ]
                else:
                    rendered["archived"] = False
                clues.append(rendered)

        clues.sort(key=lambda item: (item["window_started_at"], item["circuit_no"]), reverse=True)
        items = clues[:limit]
        return {
            "window_seconds": window_seconds,
            "history": history,
            "items": items,
            "total": len(items),
            "generated_at": utc_now(),
        }

    # --------------------------------------------------------------- snapshot

    def snapshot(self, alarm_no: str, now_real: float | None = None) -> dict[str, Any]:
        return self._snapshot(None, alarm_no, now_real or parse_ts(utc_now()))

    def _snapshot(self, conn, alarm_no: str, now_real: float) -> dict[str, Any]:
        def q(sql: str, params: tuple) -> list[Any]:
            if conn is not None:
                return list(conn.execute(sql, params).fetchall())
            return self.db.query(sql, params)

        alarm = (q("SELECT * FROM alarms WHERE alarm_no = ?", (alarm_no,)) or [None])[0]
        if alarm is None:
            raise NotFoundError(alarm_no)

        notifications = q(
            "SELECT * FROM notifications WHERE alarm_no = ? ORDER BY id", (alarm_no,)
        )
        events = q(
            "SELECT at, kind, detail FROM events WHERE alarm_no = ? ORDER BY id", (alarm_no,)
        )
        return build_alarm_view(
            dict(alarm),
            [dict(n) for n in notifications],
            [dict(e) for e in events],
            now_real=now_real,
        )

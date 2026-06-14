from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path.home() / "var" / "lib" / "fleet" / "state.db"

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS pause_journal (
    service_name TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    intent_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    applied      BOOLEAN NOT NULL DEFAULT 0,
    prev_state   TEXT NOT NULL,
    resumed_at   TIMESTAMP
);
"""


class PauseJournal:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or _DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path),
            isolation_level="DEFERRED",
        )
        self._conn.row_factory = sqlite3.Row
        # WAL mode for concurrent readers
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    # ── lifecycle ────────────────────────────────────────────

    def _init_db(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── writes ───────────────────────────────────────────────

    def record_intent(
        self,
        service_name: str,
        source: str,
        prev_state_json: str,
    ) -> None:
        # UPSERT: if a row already exists (e.g. re-pause after partial
        # failure) overwrite it with fresh intent
        self._conn.execute(
            """\
            INSERT INTO pause_journal (service_name, source, prev_state, intent_at, applied, resumed_at)
            VALUES (?, ?, ?, ?, 0, NULL)
            ON CONFLICT(service_name) DO UPDATE SET
                source     = excluded.source,
                prev_state = excluded.prev_state,
                intent_at  = excluded.intent_at,
                applied    = 0,
                resumed_at = NULL
            """,
            (
                service_name,
                source,
                prev_state_json,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()
        log.debug("recorded pause intent: %s (source=%s)", service_name, source)

    def mark_applied(self, service_name: str) -> None:
        self._conn.execute(
            "UPDATE pause_journal SET applied = 1 WHERE service_name = ?",
            (service_name,),
        )
        self._conn.commit()
        log.debug("marked applied: %s", service_name)

    def mark_resumed(self, service_name: str) -> None:
        self._conn.execute(
            "UPDATE pause_journal SET resumed_at = ? WHERE service_name = ?",
            (datetime.now(timezone.utc).isoformat(), service_name),
        )
        self._conn.commit()
        log.debug("marked resumed: %s", service_name)

    # ── reads ────────────────────────────────────────────────

    def get_paused_services(self) -> list[dict]:
        # Services with applied=True that have NOT been resumed
        rows = self._conn.execute(
            """\
            SELECT service_name, source, intent_at, prev_state
            FROM pause_journal
            WHERE applied = 1 AND resumed_at IS NULL
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stale_intents(self) -> list[dict]:
        # Intents recorded but never applied — crash recovery candidates
        rows = self._conn.execute(
            """\
            SELECT service_name, source, intent_at, prev_state
            FROM pause_journal
            WHERE applied = 0 AND resumed_at IS NULL
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def get_prev_state(self, service_name: str) -> dict | None:
        row = self._conn.execute(
            "SELECT prev_state FROM pause_journal WHERE service_name = ?",
            (service_name,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["prev_state"])

    # ── reconciliation ───────────────────────────────────────

    def reconcile(self, service_name: str, object_has_label: bool) -> None:
        # Reconciliation rules:
        #   journal=paused  + label=present  → consistent, no-op
        #   journal=paused  + label=absent   → external resume, mark resumed in journal
        #   journal=absent  + label=present  → external tool paused it, record in journal
        #   journal=absent  + label=absent   → consistent, no-op
        row = self._conn.execute(
            """\
            SELECT service_name, applied, resumed_at
            FROM pause_journal
            WHERE service_name = ?
            """,
            (service_name,),
        ).fetchone()

        journal_says_paused = (
            row is not None
            and row["applied"] == 1
            and row["resumed_at"] is None
        )

        if journal_says_paused and not object_has_label:
            # External resume detected — update journal to match reality
            log.info(
                "reconcile: %s was resumed externally, updating journal",
                service_name,
            )
            self.mark_resumed(service_name)

        elif not journal_says_paused and object_has_label:
            # External pause detected — record in journal so we track it
            log.info(
                "reconcile: %s was paused externally, recording in journal",
                service_name,
            )
            self.record_intent(service_name, "unknown", json.dumps({}))
            self.mark_applied(service_name)

        # Other two cases are already consistent — no action needed

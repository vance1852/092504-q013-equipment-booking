"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equipment (
    equipment_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    capability_version TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'deactivated')),
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attachments (
    attachment_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    capability_version TEXT NOT NULL,
    compatible_equipment_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'deactivated')),
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calibration_certificates (
    certificate_id TEXT PRIMARY KEY,
    resource_type TEXT NOT NULL CHECK(resource_type IN ('equipment', 'attachment')),
    resource_id TEXT NOT NULL,
    issuer TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS open_windows (
    window_id TEXT PRIMARY KEY,
    resource_type TEXT NOT NULL CHECK(resource_type IN ('equipment', 'attachment')),
    resource_id TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS changeover_rules (
    rule_id TEXT PRIMARY KEY,
    equipment_id TEXT NOT NULL REFERENCES equipment(equipment_id),
    from_goal TEXT NOT NULL,
    to_goal TEXT NOT NULL,
    minutes INTEGER NOT NULL CHECK(minutes >= 0),
    created_at TEXT NOT NULL,
    UNIQUE(equipment_id, from_goal, to_goal)
);
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    goal TEXT NOT NULL,
    applicant_id TEXT NOT NULL REFERENCES actors(actor_id),
    equipment_id TEXT NOT NULL REFERENCES equipment(equipment_id),
    equipment_version INTEGER NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('confirmed', 'reschedule_pending', 'rescheduled', 'cancelled', 'terminated')),
    calibration_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reservation_attachments (
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    attachment_id TEXT NOT NULL REFERENCES attachments(attachment_id),
    attachment_version INTEGER NOT NULL,
    PRIMARY KEY (reservation_id, attachment_id)
);
CREATE TABLE IF NOT EXISTS maintenance_blocks (
    block_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    resource_type TEXT NOT NULL CHECK(resource_type IN ('equipment', 'attachment')),
    resource_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('planned', 'emergency')),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reschedule_queue (
    entry_id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    block_id TEXT NOT NULL REFERENCES maintenance_blocks(block_id),
    priority_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'resolved', 'dropped')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_risks (
    risk_id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    block_id TEXT NOT NULL REFERENCES maintenance_blocks(block_id),
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('awaiting_decision', 'resolved_continue', 'resolved_terminated')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_overrides (
    override_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reservations_equipment ON reservations(equipment_id, status);
CREATE INDEX IF NOT EXISTS idx_reservation_attachments_attachment ON reservation_attachments(attachment_id);
CREATE INDEX IF NOT EXISTS idx_maintenance_blocks_resource ON maintenance_blocks(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_open_windows_resource ON open_windows(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_calibration_resource ON calibration_certificates(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_reschedule_queue_site ON reschedule_queue(site_id, status);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()

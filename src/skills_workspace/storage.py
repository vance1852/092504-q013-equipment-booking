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
CREATE TABLE IF NOT EXISTS booking_equipment (
    equipment_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    capability_version INTEGER NOT NULL CHECK(capability_version >= 1),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS booking_accessories (
    accessory_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    capability_version INTEGER NOT NULL CHECK(capability_version >= 1),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS booking_equipment_accessories (
    equipment_id TEXT NOT NULL REFERENCES booking_equipment(equipment_id),
    accessory_id TEXT NOT NULL REFERENCES booking_accessories(accessory_id),
    PRIMARY KEY (equipment_id, accessory_id)
);
CREATE TABLE IF NOT EXISTS booking_certificates (
    certificate_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL CHECK(target_type IN ('equipment', 'accessory')),
    target_id TEXT NOT NULL,
    certificate_no TEXT NOT NULL,
    basis TEXT NOT NULL,
    calibrated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS booking_open_windows (
    window_id TEXT PRIMARY KEY,
    equipment_id TEXT NOT NULL REFERENCES booking_equipment(equipment_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS booking_transition_rules (
    rule_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    equipment_id TEXT NOT NULL,
    from_setup TEXT NOT NULL,
    to_setup TEXT NOT NULL,
    minutes INTEGER NOT NULL CHECK(minutes >= 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(equipment_id, from_setup, to_setup)
);
CREATE TABLE IF NOT EXISTS booking_inquiries (
    inquiry_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    applicant_actor_id TEXT NOT NULL,
    objective TEXT NOT NULL,
    setup TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL CHECK(duration_minutes >= 1),
    earliest_start TEXT NOT NULL,
    latest_end TEXT NOT NULL,
    priority_tier INTEGER NOT NULL CHECK(priority_tier BETWEEN 1 AND 3),
    capabilities_json TEXT NOT NULL,
    accessory_requirements_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS booking_reservations (
    reservation_id TEXT PRIMARY KEY,
    inquiry_id TEXT,
    site_id TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    accessory_ids_json TEXT NOT NULL,
    setup TEXT NOT NULL,
    objective TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    priority_tier INTEGER NOT NULL,
    applicant_actor_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('confirmed', 'displaced', 'rebooked', 'cancelled', 'terminated')),
    equipment_version INTEGER NOT NULL,
    accessory_versions_json TEXT NOT NULL,
    equipment_certificate_id TEXT,
    accessory_certificates_json TEXT NOT NULL,
    terminated_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS booking_reservation_slots (
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    reservation_id TEXT NOT NULL,
    UNIQUE(resource_type, resource_id, slot)
);
CREATE INDEX IF NOT EXISTS idx_booking_slots_reservation
    ON booking_reservation_slots(reservation_id);
CREATE INDEX IF NOT EXISTS idx_booking_slots_lookup
    ON booking_reservation_slots(resource_type, resource_id, slot);
CREATE TABLE IF NOT EXISTS booking_blockades (
    blockade_id TEXT PRIMARY KEY,
    equipment_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('planned', 'emergency')),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'lifted')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    lifted_at TEXT,
    lifted_by TEXT
);
CREATE TABLE IF NOT EXISTS booking_reschedule_queue (
    queue_id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL UNIQUE,
    site_id TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    accessory_ids_json TEXT NOT NULL,
    setup TEXT NOT NULL,
    objective TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    priority_tier INTEGER NOT NULL,
    applicant_actor_id TEXT NOT NULL,
    original_starts_at TEXT NOT NULL,
    original_ends_at TEXT NOT NULL,
    cause_kind TEXT NOT NULL CHECK(cause_kind IN ('planned', 'emergency')),
    blockade_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('waiting', 'rebooked', 'cancelled')),
    new_reservation_id TEXT,
    enqueued_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS booking_usage_risks (
    risk_id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    blockade_id TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'continued', 'terminated', 'rescheduled')),
    noted_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(reservation_id, blockade_id)
);
CREATE TABLE IF NOT EXISTS booking_manual_overrides (
    override_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_booking_reservations_time
    ON booking_reservations(starts_at, ends_at);
CREATE INDEX IF NOT EXISTS idx_booking_certificates_target
    ON booking_certificates(target_type, target_id);
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

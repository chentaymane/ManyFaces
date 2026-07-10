"""SQLite persistence for profiles.

Metadata is stored as JSON in a single column so the schema never has to change as
the Profile model grows. The persistent browser data (cookies, storage) lives on
disk per-profile under PROFILES_DIR, not in this DB.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from . import config
from .models import Profile, ProfileUpdate


_SCHEMA = """
    CREATE TABLE IF NOT EXISTS profiles (
        id         TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        data       TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
"""


def _connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    # Idempotent: guarantees the schema exists no matter how the app was started.
    conn.execute(_SCHEMA)
    return conn


def init() -> None:
    with _connect() as conn:
        conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(profile: Profile) -> Profile:
    now = _now()
    profile.created_at = now
    profile.updated_at = now
    with _connect() as conn:
        conn.execute(
            "INSERT INTO profiles (id, name, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (profile.id, profile.name, profile.model_dump_json(), now, now),
        )
        conn.commit()
    return profile


def list_all() -> list[Profile]:
    with _connect() as conn:
        rows = conn.execute("SELECT data FROM profiles ORDER BY created_at DESC").fetchall()
    return [Profile.model_validate_json(r["data"]) for r in rows]


def get(profile_id: str) -> Optional[Profile]:
    with _connect() as conn:
        row = conn.execute("SELECT data FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    return Profile.model_validate_json(row["data"]) if row else None


def update(profile_id: str, patch: ProfileUpdate) -> Optional[Profile]:
    profile = get(profile_id)
    if profile is None:
        return None
    data = profile.model_dump()
    for key, value in patch.model_dump(exclude_unset=True).items():
        data[key] = value
    profile = Profile.model_validate(data)
    profile.updated_at = _now()
    with _connect() as conn:
        conn.execute(
            "UPDATE profiles SET name = ?, data = ?, updated_at = ? WHERE id = ?",
            (profile.name, profile.model_dump_json(), profile.updated_at, profile_id),
        )
        conn.commit()
    return profile


def delete(profile_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        conn.commit()
        return cur.rowcount > 0

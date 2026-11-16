"""Transactional SQLite persistence for workspaces, batches, and publish locks.

The database stores only small application metadata. Media, checkpoints, and
other large binaries are resolved through :mod:`backend.config` into Google
Drive and are never copied into this SQLite file.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend import config

SCHEMA_VERSION = 1
EPISODE_STATUSES = frozenset(
    {
        "draft",
        "queued",
        "approved",
        "rendering",
        "ready",
        "scheduled",
        "publishing",
        "published",
        "failed",
        "archived",
    }
)
VARIANT_TYPES = frozenset({"hook", "title"})
ALLOWED_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"queued", "approved", "archived"}),
    "queued": frozenset({"draft", "approved", "rendering", "failed", "archived"}),
    "approved": frozenset({"queued", "rendering", "ready", "scheduled", "failed", "archived"}),
    "rendering": frozenset({"ready", "failed", "archived"}),
    "ready": frozenset({"approved", "scheduled", "publishing", "failed", "archived"}),
    "scheduled": frozenset({"ready", "publishing", "failed", "archived"}),
    "publishing": frozenset({"published", "failed"}),
    "published": frozenset({"archived"}),
    "failed": frozenset({"queued", "approved", "rendering", "archived"}),
    "archived": frozenset(),
}


class DatabaseError(RuntimeError):
    """Base class for expected persistence errors."""


class NotFoundError(DatabaseError):
    """Raised when a requested database record does not exist."""


class ConflictError(DatabaseError):
    """Raised when a unique record conflicts with existing data."""


class IdempotencyConflictError(ConflictError):
    """An idempotency key was reused for a different upload payload."""


@dataclass(frozen=True)
class UploadLockLease:
    """Outcome of acquiring a per-channel idempotency lock.

    ``acquired`` is true only for the caller that may perform the upload.
    ``reason`` is one of ``acquired``, ``already_completed``, or
    ``in_progress``. A completed result can be replayed from ``result`` and
    ``remote_id`` without sending another upload.
    """

    acquired: bool
    reason: str
    lock_token: str | None = None
    remote_id: str | None = None
    result: Any = None
    expires_at: str | None = None
    attempts: int = 0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _stamp(value: datetime | None = None) -> str:
    current = value or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json_dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Value is not JSON serializable: {exc}") from exc


def _json_load(value: str | None, *, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized.casefold()).strip("-")
    return slug[:80].rstrip("-")


def _required_text(value: Any, field: str, *, maximum: int = 20_000) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} cannot be empty")
    if len(cleaned) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return cleaned


def _optional_text(value: Any, field: str, *, maximum: int = 100_000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return cleaned


def _parse_scheduled_for(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("scheduled_for must be an ISO-8601 datetime") from exc
    else:
        raise ValueError("scheduled_for must be an ISO-8601 string or datetime")
    if dt.tzinfo is None:
        raise ValueError("scheduled_for must include a timezone offset")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class DatabaseManager:
    """Thread-safe SQLite manager with one short-lived connection per operation."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        chosen_path = db_path if db_path is not None else config.DATABASE_PATH
        chosen_text = str(chosen_path)
        self._memory = chosen_text == ":memory:"
        self._uri = self._memory
        if self._memory:
            self._dsn = f"file:daemoncut-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self.path: Path | None = None
            self._keeper = sqlite3.connect(
                self._dsn, uri=True, isolation_level=None, check_same_thread=False
            )
            self._configure_connection(self._keeper)
        else:
            self.path = Path(chosen_path).expanduser().resolve(strict=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._dsn = str(self.path)
            self._keeper = None
        self._lock = threading.RLock()
        self._initialized = False
        self.initialize()

    def _configure_connection(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._dsn,
            timeout=5.0,
            isolation_level=None,
            uri=self._uri,
            check_same_thread=False,
        )
        self._configure_connection(conn)
        return conn

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        """Create the versioned schema and enable WAL for file-backed databases."""
        with self._lock:
            if self._initialized:
                return
            conn = self._connect()
            try:
                if not self._memory:
                    conn.execute("PRAGMA journal_mode = WAL")
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if version > SCHEMA_VERSION:
                    raise DatabaseError(
                        f"Database schema version {version} is newer than supported "
                        f"version {SCHEMA_VERSION}. Update DaemonCut before opening this DB."
                    )
                if version == 0:
                    conn.executescript(
                        """
                        BEGIN IMMEDIATE;

                        CREATE TABLE IF NOT EXISTS workspaces (
                            workspace_id TEXT PRIMARY KEY,
                            name TEXT NOT NULL CHECK(length(trim(name)) > 0),
                            slug TEXT NOT NULL UNIQUE,
                            timezone TEXT NOT NULL DEFAULT 'Asia/Karachi',
                            default_platform TEXT,
                            settings_json TEXT NOT NULL DEFAULT '{}',
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );

                        CREATE TABLE IF NOT EXISTS channels (
                            channel_id TEXT PRIMARY KEY,
                            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
                            platform TEXT NOT NULL CHECK(length(trim(platform)) > 0),
                            external_channel_id TEXT NOT NULL CHECK(length(trim(external_channel_id)) > 0),
                            display_name TEXT NOT NULL,
                            credentials_ref TEXT,
                            settings_json TEXT NOT NULL DEFAULT '{}',
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            UNIQUE(workspace_id, platform, external_channel_id)
                        );

                        CREATE TABLE IF NOT EXISTS batches (
                            batch_id TEXT PRIMARY KEY,
                            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
                            batch_date TEXT NOT NULL,
                            target_count INTEGER NOT NULL DEFAULT 7 CHECK(target_count = 7),
                            status TEXT NOT NULL DEFAULT 'queued'
                                CHECK(status IN ('queued', 'in_progress', 'completed', 'cancelled')),
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            UNIQUE(workspace_id, batch_date)
                        );

                        CREATE TABLE IF NOT EXISTS episodes (
                            episode_id TEXT PRIMARY KEY,
                            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
                            batch_id TEXT REFERENCES batches(batch_id) ON DELETE SET NULL,
                            episode_number INTEGER CHECK(episode_number IS NULL OR episode_number BETWEEN 1 AND 7),
                            topic TEXT NOT NULL,
                            title TEXT NOT NULL,
                            hook TEXT NOT NULL DEFAULT '',
                            script TEXT NOT NULL DEFAULT '',
                            status TEXT NOT NULL DEFAULT 'draft'
                                CHECK(status IN ('draft', 'queued', 'approved', 'rendering', 'ready', 'scheduled', 'publishing', 'published', 'failed', 'archived')),
                            scheduled_for TEXT,
                            metadata_json TEXT NOT NULL DEFAULT '{}',
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            CHECK((batch_id IS NULL AND episode_number IS NULL) OR
                                  (batch_id IS NOT NULL AND episode_number IS NOT NULL)),
                            UNIQUE(batch_id, episode_number)
                        );

                        CREATE TABLE IF NOT EXISTS content_variants (
                            variant_id TEXT PRIMARY KEY,
                            episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
                            variant_type TEXT NOT NULL CHECK(variant_type IN ('hook', 'title')),
                            variant_label TEXT NOT NULL CHECK(variant_label IN ('A', 'B', 'C')),
                            text_value TEXT NOT NULL CHECK(length(trim(text_value)) > 0),
                            is_winner INTEGER NOT NULL DEFAULT 0 CHECK(is_winner IN (0, 1)),
                            metrics_json TEXT NOT NULL DEFAULT '{}',
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            UNIQUE(episode_id, variant_type, variant_label)
                        );

                        CREATE UNIQUE INDEX IF NOT EXISTS one_winning_variant_per_type
                            ON content_variants(episode_id, variant_type) WHERE is_winner = 1;

                        CREATE TABLE IF NOT EXISTS upload_locks (
                            lock_id TEXT PRIMARY KEY,
                            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
                            platform TEXT NOT NULL CHECK(length(trim(platform)) > 0),
                            external_channel_id TEXT NOT NULL CHECK(length(trim(external_channel_id)) > 0),
                            idempotency_key TEXT NOT NULL CHECK(length(trim(idempotency_key)) > 0),
                            payload_hash TEXT NOT NULL CHECK(length(trim(payload_hash)) > 0),
                            status TEXT NOT NULL CHECK(status IN ('in_progress', 'completed', 'failed')),
                            lock_token TEXT NOT NULL UNIQUE,
                            lease_expires_at TEXT NOT NULL,
                            attempt_count INTEGER NOT NULL DEFAULT 1 CHECK(attempt_count >= 1),
                            episode_id TEXT REFERENCES episodes(episode_id) ON DELETE SET NULL,
                            remote_id TEXT,
                            result_json TEXT,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            UNIQUE(workspace_id, platform, external_channel_id, idempotency_key)
                        );

                        CREATE INDEX IF NOT EXISTS episodes_queue_idx
                            ON episodes(workspace_id, status, scheduled_for, created_at);
                        CREATE INDEX IF NOT EXISTS batches_date_idx
                            ON batches(workspace_id, batch_date DESC);
                        CREATE INDEX IF NOT EXISTS variants_episode_idx
                            ON content_variants(episode_id, variant_type, variant_label);
                        CREATE INDEX IF NOT EXISTS upload_lock_status_idx
                            ON upload_locks(status, lease_expires_at);

                        PRAGMA user_version = 1;
                        COMMIT;
                        """
                    )
                self._initialized = True
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def __enter__(self) -> "DatabaseManager":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the shared-memory keeper, if this manager owns one."""
        with self._lock:
            if self._keeper is not None:
                self._keeper.close()
                self._keeper = None
                self._initialized = False

    def create_workspace(
        self,
        name: str,
        *,
        timezone_name: str = "Asia/Karachi",
        default_platform: str | None = None,
        settings: Mapping[str, Any] | None = None,
        slug: str | None = None,
    ) -> dict[str, Any]:
        name = _required_text(name, "name", maximum=120)
        timezone_name = _required_text(timezone_name, "timezone_name", maximum=80)
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone {timezone_name!r}") from exc
        if settings is not None and not isinstance(settings, Mapping):
            raise ValueError("settings must be a mapping")
        chosen_slug = _required_text(slug or _slugify(name) or f"workspace-{uuid.uuid4().hex[:8]}", "slug", maximum=80)
        chosen_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", chosen_slug.casefold()).strip("-_")
        if not chosen_slug:
            raise ValueError("slug must contain at least one letter or number")
        platform = _optional_text(default_platform, "default_platform", maximum=40).casefold() or None
        now = _stamp()
        workspace_id = str(uuid.uuid4())
        try:
            with self._transaction(immediate=True) as conn:
                conn.execute(
                    """INSERT INTO workspaces
                       (workspace_id, name, slug, timezone, default_platform, settings_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        workspace_id,
                        name,
                        chosen_slug,
                        timezone_name,
                        platform,
                        _json_dump(dict(settings or {})),
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Workspace slug {chosen_slug!r} already exists") from exc
        return self.get_workspace(workspace_id)

    def get_workspace(self, workspace_id_or_slug: str) -> dict[str, Any]:
        identifier = _required_text(workspace_id_or_slug, "workspace_id_or_slug", maximum=100)
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ? OR slug = ?",
                (identifier, identifier),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Workspace {identifier!r} was not found")
            result = dict(row)
            result["settings"] = _json_load(result.pop("settings_json"), default={})
            result["channels"] = self._channels_for(conn, result["workspace_id"])
            return result

    def list_workspaces(self) -> list[dict[str, Any]]:
        with self._reader() as conn:
            rows = conn.execute("SELECT * FROM workspaces ORDER BY name COLLATE NOCASE").fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                workspace = dict(row)
                workspace["settings"] = _json_load(workspace.pop("settings_json"), default={})
                workspace["channels"] = self._channels_for(conn, workspace["workspace_id"])
                result.append(workspace)
            return result

    def update_workspace(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        timezone_name: str | None = None,
        default_platform: str | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.get_workspace(workspace_id)
        new_name = _required_text(name, "name", maximum=120) if name is not None else current["name"]
        new_timezone = timezone_name if timezone_name is not None else current["timezone"]
        new_timezone = _required_text(new_timezone, "timezone_name", maximum=80)
        try:
            ZoneInfo(new_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone {new_timezone!r}") from exc
        if settings is not None and not isinstance(settings, Mapping):
            raise ValueError("settings must be a mapping")
        new_settings = dict(settings) if settings is not None else current["settings"]
        new_platform = (
            _optional_text(default_platform, "default_platform", maximum=40).casefold() or None
            if default_platform is not None
            else current["default_platform"]
        )
        now = _stamp()
        with self._transaction(immediate=True) as conn:
            updated = conn.execute(
                """UPDATE workspaces SET name = ?, timezone = ?, default_platform = ?,
                       settings_json = ?, updated_at = ? WHERE workspace_id = ?""",
                (new_name, new_timezone, new_platform, _json_dump(new_settings), now, current["workspace_id"]),
            ).rowcount
            if not updated:
                raise NotFoundError(f"Workspace {workspace_id!r} was not found")
        return self.get_workspace(current["workspace_id"])

    def add_channel(
        self,
        workspace_id: str,
        platform: str,
        external_channel_id: str,
        *,
        display_name: str = "",
        credentials_ref: str | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        workspace = self.get_workspace(workspace_id)
        platform = _required_text(platform, "platform", maximum=40).casefold()
        if not re.fullmatch(r"[a-z0-9_-]+", platform):
            raise ValueError("platform may contain only letters, numbers, hyphens, and underscores")
        external_channel_id = _required_text(external_channel_id, "external_channel_id", maximum=255)
        display_name = _optional_text(display_name, "display_name", maximum=160)
        credentials_ref = _optional_text(credentials_ref, "credentials_ref", maximum=255) or None
        if credentials_ref and any(marker in credentials_ref.casefold() for marker in ("token=", "secret=", "api_key=")):
            raise ValueError("credentials_ref must be a reference, never a secret value")
        if settings is not None and not isinstance(settings, Mapping):
            raise ValueError("settings must be a mapping")
        now = _stamp()
        channel_id = str(uuid.uuid4())
        try:
            with self._transaction(immediate=True) as conn:
                conn.execute(
                    """INSERT INTO channels
                       (channel_id, workspace_id, platform, external_channel_id, display_name,
                        credentials_ref, settings_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        channel_id,
                        workspace["workspace_id"],
                        platform,
                        external_channel_id,
                        display_name or external_channel_id,
                        credentials_ref,
                        _json_dump(dict(settings or {})),
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("That channel is already registered in this workspace") from exc
        with self._reader() as conn:
            row = conn.execute("SELECT * FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
            assert row is not None
            return self._channel_dict(row)

    @staticmethod
    def _channel_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["settings"] = _json_load(result.pop("settings_json"), default={})
        return result

    @classmethod
    def _channels_for(cls, conn: sqlite3.Connection, workspace_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM channels WHERE workspace_id = ? ORDER BY platform, display_name COLLATE NOCASE",
            (workspace_id,),
        ).fetchall()
        return [cls._channel_dict(row) for row in rows]

    def create_saturday_batch(
        self,
        workspace_id: str,
        episodes: Sequence[Mapping[str, Any]],
        *,
        batch_date: date | str | None = None,
    ) -> dict[str, Any]:
        """Atomically queue exactly seven episodes for a Saturday batch date.

        ``batch_date`` is the Saturday the batch is prepared. When omitted, the
        current Saturday in the workspace timezone is used, or the next one.
        Episode items require a title or topic; scripts may be added later.
        """
        workspace = self.get_workspace(workspace_id)
        if isinstance(episodes, (str, bytes)) or not isinstance(episodes, Sequence):
            raise ValueError("episodes must be a sequence of seven episode mappings")
        if len(episodes) != 7:
            raise ValueError(f"A Saturday batch must contain exactly 7 episodes; received {len(episodes)}")
        if any(not isinstance(item, Mapping) for item in episodes):
            raise ValueError("Each episode must be a mapping")

        if batch_date is None:
            local_today = datetime.now(ZoneInfo(workspace["timezone"])).date()
            batch_day = local_today + timedelta(days=(5 - local_today.weekday()) % 7)
        elif isinstance(batch_date, str):
            try:
                batch_day = date.fromisoformat(batch_date)
            except ValueError as exc:
                raise ValueError("batch_date must be an ISO date") from exc
        elif isinstance(batch_date, datetime):
            raise ValueError("batch_date must be a calendar date, not a datetime")
        elif isinstance(batch_date, date):
            batch_day = batch_date
        else:
            raise ValueError("batch_date must be a date, ISO date string, or None")
        if batch_day.weekday() != 5:
            raise ValueError(f"batch_date {batch_day.isoformat()} is not a Saturday")

        prepared: list[dict[str, Any]] = []
        for index, item in enumerate(episodes, start=1):
            title = _optional_text(item.get("title"), f"episodes[{index}].title", maximum=200)
            topic = _optional_text(item.get("topic"), f"episodes[{index}].topic", maximum=500)
            if not title and not topic:
                raise ValueError(f"episodes[{index}] requires a title or topic")
            if not title:
                title = topic[:200]
            if not topic:
                topic = title
            hook = _optional_text(item.get("hook"), f"episodes[{index}].hook", maximum=2_000)
            script = _optional_text(item.get("script"), f"episodes[{index}].script", maximum=100_000)
            scheduled_for = _parse_scheduled_for(item.get("scheduled_for"))
            metadata = item.get("metadata", {})
            if metadata is None:
                metadata = {}
            if not isinstance(metadata, Mapping):
                raise ValueError(f"episodes[{index}].metadata must be a mapping")
            prepared.append(
                {
                    "episode_id": str(uuid.uuid4()),
                    "episode_number": index,
                    "topic": topic,
                    "title": title,
                    "hook": hook,
                    "script": script,
                    "scheduled_for": scheduled_for,
                    "metadata_json": _json_dump(dict(metadata)),
                }
            )

        now = _stamp()
        batch_id = str(uuid.uuid4())
        try:
            with self._transaction(immediate=True) as conn:
                conn.execute(
                    """INSERT INTO batches
                       (batch_id, workspace_id, batch_date, target_count, status, created_at, updated_at)
                       VALUES (?, ?, ?, 7, 'queued', ?, ?)""",
                    (batch_id, workspace["workspace_id"], batch_day.isoformat(), now, now),
                )
                conn.executemany(
                    """INSERT INTO episodes
                       (episode_id, workspace_id, batch_id, episode_number, topic, title, hook, script,
                        status, scheduled_for, metadata_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
                    [
                        (
                            item["episode_id"],
                            workspace["workspace_id"],
                            batch_id,
                            item["episode_number"],
                            item["topic"],
                            item["title"],
                            item["hook"],
                            item["script"],
                            item["scheduled_for"],
                            item["metadata_json"],
                            now,
                            now,
                        )
                        for item in prepared
                    ],
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                f"A batch already exists for workspace {workspace['slug']!r} on {batch_day.isoformat()}"
            ) from exc
        return self.get_batch(batch_id)

    def create_episode(
        self,
        workspace_id: str,
        *,
        topic: str,
        title: str,
        hook: str = "",
        script: str = "",
        scheduled_for: str | datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
        status: str = "queued",
    ) -> dict[str, Any]:
        """Create one standalone draft/queued episode outside a seven-item batch."""
        workspace = self.get_workspace(workspace_id)
        topic = _required_text(topic, "topic", maximum=500)
        title = _required_text(title, "title", maximum=200)
        hook = _optional_text(hook, "hook", maximum=2_000)
        script = _optional_text(script, "script", maximum=100_000)
        if metadata is not None and not isinstance(metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        normalized_status = _required_text(status, "status", maximum=32).casefold()
        if normalized_status not in {"draft", "queued", "approved", "scheduled"}:
            raise ValueError("standalone episode status must be draft, queued, approved, or scheduled")
        schedule = _parse_scheduled_for(scheduled_for)
        if normalized_status == "scheduled" and schedule is None:
            raise ValueError("scheduled episodes require scheduled_for")
        episode_id = str(uuid.uuid4())
        now = _stamp()
        with self._transaction(immediate=True) as conn:
            conn.execute(
                """INSERT INTO episodes
                   (episode_id, workspace_id, batch_id, episode_number, topic, title, hook, script,
                    status, scheduled_for, metadata_json, created_at, updated_at)
                   VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    episode_id,
                    workspace["workspace_id"],
                    topic,
                    title,
                    hook,
                    script,
                    normalized_status,
                    schedule,
                    _json_dump(dict(metadata or {})),
                    now,
                    now,
                ),
            )
        return self.get_episode(episode_id)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        batch_id = _required_text(batch_id, "batch_id", maximum=100)
        with self._reader() as conn:
            row = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Batch {batch_id!r} was not found")
            result = dict(row)
            result["episodes"] = [
                self._episode_dict(episode)
                for episode in conn.execute(
                    "SELECT * FROM episodes WHERE batch_id = ? ORDER BY episode_number",
                    (batch_id,),
                ).fetchall()
            ]
            return result

    def list_batches(self, workspace_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        workspace = self.get_workspace(workspace_id)
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT * FROM batches WHERE workspace_id = ? ORDER BY batch_date DESC LIMIT ?",
                (workspace["workspace_id"], limit),
            ).fetchall()
            result = []
            for row in rows:
                batch = dict(row)
                batch["episode_count"] = conn.execute(
                    "SELECT count(*) FROM episodes WHERE batch_id = ?", (batch["batch_id"],)
                ).fetchone()[0]
                result.append(batch)
            return result

    @staticmethod
    def _episode_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = _json_load(result.pop("metadata_json"), default={})
        return result

    def get_episode(self, episode_id: str) -> dict[str, Any]:
        episode_id = _required_text(episode_id, "episode_id", maximum=100)
        with self._reader() as conn:
            row = conn.execute("SELECT * FROM episodes WHERE episode_id = ?", (episode_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Episode {episode_id!r} was not found")
            episode = self._episode_dict(row)
            episode["variants"] = self._variants_for(conn, episode_id)
            return episode

    def list_queue(
        self,
        workspace_id: str,
        *,
        statuses: Sequence[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        workspace = self.get_workspace(workspace_id)
        if not 1 <= limit <= 2_000:
            raise ValueError("limit must be between 1 and 2000")
        query = "SELECT * FROM episodes WHERE workspace_id = ?"
        params: list[Any] = [workspace["workspace_id"]]
        if statuses:
            normalized = [str(status).casefold() for status in statuses]
            invalid = set(normalized) - EPISODE_STATUSES
            if invalid:
                raise ValueError(f"Unsupported episode statuses: {sorted(invalid)}")
            query += f" AND status IN ({','.join('?' for _ in normalized)})"
            params.extend(normalized)
        query += " ORDER BY (scheduled_for IS NULL), scheduled_for, created_at, episode_number LIMIT ?"
        params.append(limit)
        with self._reader() as conn:
            return [self._episode_dict(row) for row in conn.execute(query, params).fetchall()]

    def update_episode(
        self,
        episode_id: str,
        *,
        topic: str | None = None,
        title: str | None = None,
        hook: str | None = None,
        script: str | None = None,
        scheduled_for: str | datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.get_episode(episode_id)
        values = {
            "topic": _required_text(topic, "topic", maximum=500) if topic is not None else current["topic"],
            "title": _required_text(title, "title", maximum=200) if title is not None else current["title"],
            "hook": _optional_text(hook, "hook", maximum=2_000) if hook is not None else current["hook"],
            "script": _optional_text(script, "script", maximum=100_000) if script is not None else current["script"],
            "scheduled_for": _parse_scheduled_for(scheduled_for) if scheduled_for is not None else current["scheduled_for"],
        }
        if metadata is not None and not isinstance(metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        metadata_json = _json_dump(dict(metadata) if metadata is not None else current["metadata"])
        with self._transaction(immediate=True) as conn:
            updated = conn.execute(
                """UPDATE episodes SET topic = ?, title = ?, hook = ?, script = ?, scheduled_for = ?,
                       metadata_json = ?, updated_at = ? WHERE episode_id = ?""",
                (
                    values["topic"],
                    values["title"],
                    values["hook"],
                    values["script"],
                    values["scheduled_for"],
                    metadata_json,
                    _stamp(),
                    current["episode_id"],
                ),
            ).rowcount
            if not updated:
                raise NotFoundError(f"Episode {episode_id!r} was not found")
        return self.get_episode(current["episode_id"])

    def set_episode_status(self, episode_id: str, status: str) -> dict[str, Any]:
        status = _required_text(status, "status", maximum=32).casefold()
        if status not in EPISODE_STATUSES:
            raise ValueError(f"Unsupported status {status!r}; expected one of {sorted(EPISODE_STATUSES)}")
        current = self.get_episode(episode_id)
        old_status = current["status"]
        if status != old_status and status not in ALLOWED_STATUS_TRANSITIONS[old_status]:
            raise ConflictError(f"Invalid episode status transition: {old_status} -> {status}")
        with self._transaction(immediate=True) as conn:
            updated = conn.execute(
                "UPDATE episodes SET status = ?, updated_at = ? WHERE episode_id = ?",
                (status, _stamp(), current["episode_id"]),
            ).rowcount
            if not updated:
                raise NotFoundError(f"Episode {episode_id!r} was not found")
        return self.get_episode(current["episode_id"])

    def save_variants(
        self,
        episode_id: str,
        variant_type: str,
        variants: Sequence[str | Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Save two or three A/B/C hook or title alternatives for an episode."""
        episode = self.get_episode(episode_id)
        kind = _required_text(variant_type, "variant_type", maximum=16).casefold()
        if kind not in VARIANT_TYPES:
            raise ValueError("variant_type must be 'hook' or 'title'")
        if isinstance(variants, (str, bytes)) or not isinstance(variants, Sequence):
            raise ValueError("variants must be a sequence of two or three strings/mappings")
        if len(variants) not in {2, 3}:
            raise ValueError("A/B testing requires two variants; three-way tests may add C")
        prepared: list[tuple[str, str, dict[str, Any]]] = []
        used_labels: set[str] = set()
        for index, item in enumerate(variants):
            if isinstance(item, str):
                label, text, metrics = chr(ord("A") + index), item, {}
            elif isinstance(item, Mapping):
                label = str(item.get("label", chr(ord("A") + index))).strip().upper()
                text = item.get("text", item.get("value", ""))
                metrics = item.get("metrics", {}) or {}
                if not isinstance(metrics, Mapping):
                    raise ValueError(f"variants[{index}].metrics must be a mapping")
            else:
                raise ValueError(f"variants[{index}] must be a string or mapping")
            if label not in {"A", "B", "C"} or label in used_labels:
                raise ValueError("Variant labels must be unique values from A, B, and C")
            used_labels.add(label)
            prepared.append(
                (
                    label,
                    _required_text(text, f"variants[{index}].text", maximum=2_000),
                    dict(metrics),
                )
            )
        now = _stamp()
        labels = [entry[0] for entry in prepared]
        label_placeholders = ",".join("?" for _ in labels)
        with self._transaction(immediate=True) as conn:
            # A fresh test replaces the old option set and clears any previous
            # winner so stale A/B/C data cannot survive a two-way rerun.
            conn.execute(
                f"DELETE FROM content_variants WHERE episode_id = ? AND variant_type = ? "
                f"AND variant_label NOT IN ({label_placeholders})",
                (episode["episode_id"], kind, *labels),
            )
            conn.execute(
                "UPDATE content_variants SET is_winner = 0, updated_at = ? WHERE episode_id = ? AND variant_type = ?",
                (now, episode["episode_id"], kind),
            )
            for label, text, metrics in prepared:
                conn.execute(
                    """INSERT INTO content_variants
                       (variant_id, episode_id, variant_type, variant_label, text_value,
                        is_winner, metrics_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
                       ON CONFLICT(episode_id, variant_type, variant_label) DO UPDATE SET
                         text_value = excluded.text_value,
                         metrics_json = excluded.metrics_json,
                         updated_at = excluded.updated_at""",
                    (
                        str(uuid.uuid4()),
                        episode["episode_id"],
                        kind,
                        label,
                        text,
                        _json_dump(metrics),
                        now,
                        now,
                    ),
                )
        return self.get_variants(episode["episode_id"], kind)

    def get_variants(self, episode_id: str, variant_type: str | None = None) -> list[dict[str, Any]]:
        episode = self.get_episode(episode_id)
        kind = variant_type.casefold().strip() if variant_type else None
        if kind is not None and kind not in VARIANT_TYPES:
            raise ValueError("variant_type must be 'hook' or 'title'")
        with self._reader() as conn:
            if kind:
                rows = conn.execute(
                    """SELECT * FROM content_variants WHERE episode_id = ? AND variant_type = ?
                       ORDER BY variant_type, variant_label""",
                    (episode["episode_id"], kind),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM content_variants WHERE episode_id = ? ORDER BY variant_type, variant_label",
                    (episode["episode_id"],),
                ).fetchall()
            return [self._variant_dict(row) for row in rows]

    @staticmethod
    def _variant_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["is_winner"] = bool(result["is_winner"])
        result["metrics"] = _json_load(result.pop("metrics_json"), default={})
        return result

    @classmethod
    def _variants_for(cls, conn: sqlite3.Connection, episode_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM content_variants WHERE episode_id = ? ORDER BY variant_type, variant_label",
            (episode_id,),
        ).fetchall()
        return [cls._variant_dict(row) for row in rows]

    def set_winning_variant(self, episode_id: str, variant_type: str, label: str) -> dict[str, Any]:
        episode = self.get_episode(episode_id)
        kind = _required_text(variant_type, "variant_type", maximum=16).casefold()
        variant_label = _required_text(label, "label", maximum=1).upper()
        if kind not in VARIANT_TYPES or variant_label not in {"A", "B", "C"}:
            raise ValueError("variant_type must be hook/title and label must be A/B/C")
        with self._transaction(immediate=True) as conn:
            exists = conn.execute(
                """SELECT 1 FROM content_variants WHERE episode_id = ? AND variant_type = ?
                   AND variant_label = ?""",
                (episode["episode_id"], kind, variant_label),
            ).fetchone()
            if exists is None:
                raise NotFoundError(f"Variant {kind} {variant_label} was not found for this episode")
            conn.execute(
                "UPDATE content_variants SET is_winner = 0, updated_at = ? WHERE episode_id = ? AND variant_type = ?",
                (_stamp(), episode["episode_id"], kind),
            )
            conn.execute(
                """UPDATE content_variants SET is_winner = 1, updated_at = ?
                   WHERE episode_id = ? AND variant_type = ? AND variant_label = ?""",
                (_stamp(), episode["episode_id"], kind, variant_label),
            )
        selected = next(
            variant
            for variant in self.get_variants(episode["episode_id"], kind)
            if variant["variant_label"] == variant_label
        )
        return selected

    def acquire_upload_lock(
        self,
        workspace_id: str,
        platform: str,
        external_channel_id: str,
        idempotency_key: str,
        payload_hash: str,
        *,
        episode_id: str | None = None,
        lease_seconds: int = 900,
    ) -> UploadLockLease:
        """Atomically claim an upload, preventing duplicate platform posts.

        A live lease is never stolen. Expired or failed leases may be retried,
        but an idempotency key may not be reused with a different payload hash.
        """
        workspace = self.get_workspace(workspace_id)
        platform = _required_text(platform, "platform", maximum=40).casefold()
        external_channel_id = _required_text(external_channel_id, "external_channel_id", maximum=255)
        key = _required_text(idempotency_key, "idempotency_key", maximum=255)
        digest = _required_text(payload_hash, "payload_hash", maximum=256).casefold()
        if not re.fullmatch(r"[a-z0-9:_-]+", digest):
            raise ValueError("payload_hash must be a stable lowercase hex/delimited digest")
        if not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        episode_uuid: str | None = None
        if episode_id is not None:
            episode = self.get_episode(episode_id)
            if episode["workspace_id"] != workspace["workspace_id"]:
                raise ValueError("episode_id belongs to a different workspace")
            episode_uuid = episode["episode_id"]

        now_dt = _utc_now()
        now = _stamp(now_dt)
        expires = _stamp(now_dt + timedelta(seconds=lease_seconds))
        new_token = str(uuid.uuid4())
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                """SELECT * FROM upload_locks WHERE workspace_id = ? AND platform = ?
                   AND external_channel_id = ? AND idempotency_key = ?""",
                (workspace["workspace_id"], platform, external_channel_id, key),
            ).fetchone()
            if row is not None:
                if row["payload_hash"] != digest:
                    raise IdempotencyConflictError(
                        "This idempotency key is already bound to a different payload hash"
                    )
                attempts = int(row["attempt_count"])
                if row["status"] == "completed":
                    return UploadLockLease(
                        acquired=False,
                        reason="already_completed",
                        remote_id=row["remote_id"],
                        result=_json_load(row["result_json"], default=None),
                        expires_at=row["lease_expires_at"],
                        attempts=attempts,
                    )
                if row["status"] == "in_progress" and row["lease_expires_at"] > now:
                    return UploadLockLease(
                        acquired=False,
                        reason="in_progress",
                        expires_at=row["lease_expires_at"],
                        attempts=attempts,
                    )
                conn.execute(
                    """UPDATE upload_locks SET status = 'in_progress', lock_token = ?,
                           lease_expires_at = ?, attempt_count = attempt_count + 1,
                           episode_id = COALESCE(?, episode_id), remote_id = NULL,
                           result_json = NULL, updated_at = ? WHERE lock_id = ?""",
                    (new_token, expires, episode_uuid, now, row["lock_id"]),
                )
                return UploadLockLease(
                    acquired=True,
                    reason="acquired",
                    lock_token=new_token,
                    expires_at=expires,
                    attempts=attempts + 1,
                )

            conn.execute(
                """INSERT INTO upload_locks
                   (lock_id, workspace_id, platform, external_channel_id, idempotency_key,
                    payload_hash, status, lock_token, lease_expires_at, attempt_count,
                    episode_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'in_progress', ?, ?, 1, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    workspace["workspace_id"],
                    platform,
                    external_channel_id,
                    key,
                    digest,
                    new_token,
                    expires,
                    episode_uuid,
                    now,
                    now,
                ),
            )
        return UploadLockLease(
            acquired=True,
            reason="acquired",
            lock_token=new_token,
            expires_at=expires,
            attempts=1,
        )

    def finish_upload_lock(
        self,
        lock_token: str,
        *,
        success: bool,
        remote_id: str | None = None,
        result: Any = None,
    ) -> dict[str, Any]:
        """Complete or fail a lease. Only its current token owner can finish it."""
        token = _required_text(lock_token, "lock_token", maximum=100)
        remote = _optional_text(remote_id, "remote_id", maximum=500) or None
        status = "completed" if success else "failed"
        result_json = _json_dump(result) if result is not None else None
        with self._transaction(immediate=True) as conn:
            updated = conn.execute(
                """UPDATE upload_locks SET status = ?, remote_id = ?, result_json = ?, updated_at = ?
                   WHERE lock_token = ? AND status = 'in_progress'""",
                (status, remote, result_json, _stamp(), token),
            ).rowcount
            if updated != 1:
                raise ConflictError("Upload lease is missing, no longer current, or already finished")
            row = conn.execute("SELECT * FROM upload_locks WHERE lock_token = ?", (token,)).fetchone()
            assert row is not None
            return self._upload_lock_dict(row)

    def extend_upload_lock(self, lock_token: str, *, lease_seconds: int = 900) -> str:
        """Extend a still-current in-progress lease and return its new expiry."""
        token = _required_text(lock_token, "lock_token", maximum=100)
        if not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        now_dt = _utc_now()
        now = _stamp(now_dt)
        expires = _stamp(now_dt + timedelta(seconds=lease_seconds))
        with self._transaction(immediate=True) as conn:
            updated = conn.execute(
                """UPDATE upload_locks SET lease_expires_at = ?, updated_at = ?
                   WHERE lock_token = ? AND status = 'in_progress'""",
                (expires, now, token),
            ).rowcount
            if updated != 1:
                raise ConflictError("Upload lease is missing, no longer current, or already finished")
        return expires

    def get_upload_lock(
        self,
        workspace_id: str,
        platform: str,
        external_channel_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        workspace = self.get_workspace(workspace_id)
        with self._reader() as conn:
            row = conn.execute(
                """SELECT * FROM upload_locks WHERE workspace_id = ? AND platform = ?
                   AND external_channel_id = ? AND idempotency_key = ?""",
                (
                    workspace["workspace_id"],
                    platform.casefold().strip(),
                    external_channel_id.strip(),
                    idempotency_key.strip(),
                ),
            ).fetchone()
            return self._upload_lock_dict(row) if row is not None else None

    @staticmethod
    def _upload_lock_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["result"] = _json_load(result.pop("result_json"), default=None)
        return result

    def stats(self) -> dict[str, int]:
        with self._reader() as conn:
            return {
                table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                for table in ("workspaces", "channels", "batches", "episodes", "content_variants", "upload_locks")
            }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Initialize or inspect the DaemonCut SQLite database")
    parser.add_argument("--db", help="Optional SQLite file path; defaults to .daemoncut/daemoncut.sqlite3")
    parser.add_argument("--check", action="store_true", help="initialize schema and print table counts")
    args = parser.parse_args()
    db = DatabaseManager(args.db)
    if args.check:
        print(json.dumps({"database": str(db.path) if db.path else ":memory:", **db.stats()}, indent=2))
    else:
        print("Database schema is ready. Use --check to inspect counts.")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

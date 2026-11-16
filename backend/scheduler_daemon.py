"""Asia/Karachi upload watchdog, idempotency guard, and scratch cleanup.

The scheduler intentionally delegates platform posting to an injected adapter;
Phase 3 has no OAuth/publisher implementation. Every adapter receives a stable
idempotency key in addition to the local SQLite lease so it can reconcile a
remote request after a crash between posting and saving the result.
"""
from __future__ import annotations

import hashlib
import json
import logging
import socket
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, time as wall_time, timedelta, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from backend import config
from backend.database import ConflictError, DatabaseManager, IdempotencyConflictError
from backend.maintenance import CleanupSummary, clean_transient_data


LOGGER = logging.getLogger("daemoncut.scheduler")
PKT = ZoneInfo("Asia/Karachi")
DEFAULT_PKT_PEAK_TIMES = ((21, 30), (0, 30))


class SchedulerError(RuntimeError):
    """Base class for scheduler and watchdog errors."""


class SchedulerStopped(SchedulerError):
    """Raised when an operation is requested after shutdown."""


@dataclass(frozen=True)
class NetworkWaitResult:
    connected: bool
    attempts: int
    delays_seconds: tuple[float, ...]


@dataclass(frozen=True)
class UploadAttempt:
    episode_id: str
    channel_id: str
    platform: str
    status: str
    idempotency_key: str
    remote_id: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class SchedulerRunResult:
    started_at: str
    finished_at: str
    local_time: str
    is_peak_window: bool
    network: NetworkWaitResult
    cleanup: Mapping[str, Any]
    due_episode_count: int
    attempts: tuple[UploadAttempt, ...]
    shutdown_requested: bool
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


UploadHandler = Callable[..., Any]
NetworkProbe = Callable[[], bool]
Sleep = Callable[[float], None]


def _utc_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _network_probe() -> bool:
    """Check raw Internet reachability without sending application data."""
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=2.0):
            return True
    except OSError:
        return False


def wait_for_network(
    *,
    max_attempts: int = config.BOOT_NETWORK_RETRIES,
    base_delay_seconds: float = config.BOOT_NETWORK_BASE_DELAY_SECONDS,
    max_delay_seconds: float = config.BOOT_NETWORK_MAX_DELAY_SECONDS,
    probe: NetworkProbe = _network_probe,
    sleep: Sleep = time.sleep,
) -> NetworkWaitResult:
    """Retry a network probe with bounded exponential backoff."""
    if isinstance(max_attempts, bool) or not 1 <= int(max_attempts) <= 20:
        raise ValueError("max_attempts must be between 1 and 20")
    if base_delay_seconds <= 0 or max_delay_seconds <= 0 or base_delay_seconds > max_delay_seconds:
        raise ValueError("network backoff delays must be positive and base <= maximum")
    delays: list[float] = []
    for attempt in range(1, int(max_attempts) + 1):
        try:
            if probe():
                return NetworkWaitResult(True, attempt, tuple(delays))
        except Exception as exc:
            LOGGER.info("Network probe failed (%s).", type(exc).__name__)
        if attempt < max_attempts:
            delay = min(float(max_delay_seconds), float(base_delay_seconds) * (2 ** (attempt - 1)))
            delays.append(delay)
            sleep(delay)
    return NetworkWaitResult(False, int(max_attempts), tuple(delays))


def is_pkt_peak_window(
    moment: datetime | None = None,
    *,
    window_minutes: int = config.PKT_PEAK_WINDOW_MINUTES,
    peak_times: tuple[tuple[int, int], ...] = DEFAULT_PKT_PEAK_TIMES,
) -> bool:
    """Return whether ``moment`` is within ±window of 21:30 or 00:30 PKT."""
    if not 1 <= int(window_minutes) <= 120:
        raise ValueError("window_minutes must be between 1 and 120")
    current = moment or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("moment must be timezone-aware")
    local = current.astimezone(PKT)
    for day_offset in (-1, 0, 1):
        target_day = local.date() + timedelta(days=day_offset)
        for hour, minute in peak_times:
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError("peak_times must contain valid 24-hour clock values")
            target = datetime.combine(target_day, wall_time(hour, minute), tzinfo=PKT)
            if abs((local - target).total_seconds()) <= window_minutes * 60:
                return True
    return False


def _parse_schedule(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _hash_payload(episode: Mapping[str, Any]) -> str:
    metadata = episode.get("metadata", {})
    selected_metadata: dict[str, Any] = {}
    if isinstance(metadata, Mapping):
        for key in ("rendered_path", "video_path", "asset_path", "media_id", "version"):
            value = metadata.get(key)
            if isinstance(value, (str, int, float, bool)):
                selected_metadata[key] = value
    payload = {
        "episode_id": str(episode.get("episode_id", "")),
        "topic": str(episode.get("topic", "")),
        "title": str(episode.get("title", "")),
        "hook": str(episode.get("hook", "")),
        "script": str(episode.get("script", "")),
        "media": selected_metadata,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SchedulerDaemon:
    """Run scheduled uploads through SQLite leases and clean local scratch."""

    def __init__(
        self,
        database: DatabaseManager | None = None,
        *,
        upload_handler: UploadHandler | None = None,
        network_probe: NetworkProbe = _network_probe,
        sleep: Sleep = time.sleep,
        clock: Callable[[], datetime] | None = None,
        cleanup: Callable[..., CleanupSummary] = clean_transient_data,
        poll_seconds: int = config.SCHEDULER_POLL_SECONDS,
        lease_seconds: int = 900,
        max_network_attempts: int = config.BOOT_NETWORK_RETRIES,
    ) -> None:
        self.database = database or DatabaseManager()
        self.upload_handler = upload_handler
        self.network_probe = network_probe
        self.sleep = sleep
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.cleanup = cleanup
        if not 5 <= int(poll_seconds) <= 3_600:
            raise ValueError("poll_seconds must be between 5 and 3600")
        if not 1 <= int(lease_seconds) <= 86_400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        self.poll_seconds = int(poll_seconds)
        self.lease_seconds = int(lease_seconds)
        self.max_network_attempts = int(max_network_attempts)
        self.shutdown_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.RLock()
        self._last_result: SchedulerRunResult | None = None
        self._last_error: str | None = None
        self._boot_run_active = False

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def last_result(self) -> SchedulerRunResult | None:
        return self._last_result

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @staticmethod
    def _episode_is_due(episode: Mapping[str, Any], now_utc: datetime) -> bool:
        if str(episode.get("status", "")).casefold() not in {"queued", "approved", "ready", "scheduled", "publishing"}:
            return False
        scheduled = _parse_schedule(episode.get("scheduled_for"))
        return scheduled is not None and scheduled <= now_utc

    def due_episodes(self, *, now: datetime | None = None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Return due scheduled episodes grouped by their workspace metadata."""
        moment = now or self.clock()
        if moment.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        utc_now = moment.astimezone(timezone.utc)
        due: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for workspace in self.database.list_workspaces():
            queue = self.database.list_queue(
                workspace["workspace_id"],
                statuses=("queued", "approved", "ready", "scheduled", "publishing"),
                limit=2_000,
            )
            due.extend((workspace, episode) for episode in queue if self._episode_is_due(episode, utc_now))
        due.sort(key=lambda item: (str(item[1].get("scheduled_for") or ""), item[0]["workspace_id"], item[1]["episode_id"]))
        return due

    def _upload_episode(self, workspace: Mapping[str, Any], episode: Mapping[str, Any]) -> list[UploadAttempt]:
        channels = workspace.get("channels", [])
        if not isinstance(channels, list) or not channels:
            return [UploadAttempt(
                episode_id=str(episode["episode_id"]), channel_id="", platform="", status="waiting_for_channel",
                idempotency_key=f"episode:{episode['episode_id']}", detail="No upload channel is registered in this workspace.",
            )]
        if self.upload_handler is None:
            return [UploadAttempt(
                episode_id=str(episode["episode_id"]), channel_id="", platform="", status="waiting_for_adapter",
                idempotency_key=f"episode:{episode['episode_id']}", detail="No platform publisher is configured; no upload lock was acquired.",
            )]

        attempts: list[UploadAttempt] = []
        payload_hash = _hash_payload(episode)
        idempotency_key = f"episode:{episode['episode_id']}"
        for channel in channels:
            if not isinstance(channel, Mapping):
                continue
            channel_settings = channel.get("settings", {})
            if isinstance(channel_settings, Mapping) and channel_settings.get("autopublish") is False:
                continue
            channel_id = str(channel.get("channel_id", ""))
            platform = str(channel.get("platform", ""))
            external_id = str(channel.get("external_channel_id", ""))
            try:
                lease = self.database.acquire_upload_lock(
                    str(workspace["workspace_id"]), platform, external_id,
                    idempotency_key, payload_hash,
                    episode_id=str(episode["episode_id"]), lease_seconds=self.lease_seconds,
                )
            except IdempotencyConflictError:
                LOGGER.exception("Upload idempotency payload conflict for episode %s.", episode.get("episode_id"))
                attempts.append(UploadAttempt(str(episode["episode_id"]), channel_id, platform, "payload_conflict", idempotency_key, detail="The idempotency key is bound to a different payload."))
                continue
            if not lease.acquired:
                attempts.append(UploadAttempt(
                    str(episode["episode_id"]), channel_id, platform, lease.reason, idempotency_key,
                    remote_id=lease.remote_id,
                    detail="Previously completed upload was not repeated." if lease.reason == "already_completed" else "An unexpired upload lease is already active.",
                ))
                continue
            token = lease.lock_token or ""
            try:
                result = self.upload_handler(
                    dict(episode), dict(channel),
                    idempotency_key=idempotency_key,
                    lock_token=token,
                    database=self.database,
                )
                if isinstance(result, Mapping):
                    remote_id = result.get("remote_id")
                    result_value = dict(result)
                else:
                    remote_id = None
                    result_value = {"value": result} if result is not None else None
                self.database.finish_upload_lock(token, success=True, remote_id=str(remote_id) if remote_id else None, result=result_value)
                attempts.append(UploadAttempt(
                    str(episode["episode_id"]), channel_id, platform, "completed", idempotency_key,
                    remote_id=str(remote_id) if remote_id else None,
                ))
            except Exception as exc:
                try:
                    self.database.finish_upload_lock(token, success=False, result={"error_type": type(exc).__name__})
                except (ConflictError, Exception):
                    LOGGER.exception("Could not release failed upload lease for episode %s.", episode.get("episode_id"))
                LOGGER.exception("Scheduled upload failed for episode %s on %s.", episode.get("episode_id"), platform)
                attempts.append(UploadAttempt(
                    str(episode["episode_id"]), channel_id, platform, "failed", idempotency_key,
                    detail=f"{type(exc).__name__}: {str(exc)[:300]}",
                ))
        if not attempts:
            attempts.append(UploadAttempt(
                str(episode["episode_id"]), "", "", "not_enabled", idempotency_key,
                detail="No channel has autopublish enabled.",
            ))
        return attempts

    def run_once(
        self,
        *,
        wait_for_connectivity: bool = True,
        cleanup_scratch: bool = True,
        now: datetime | None = None,
    ) -> SchedulerRunResult:
        """Clean scratch, wait for network, and process due scheduled episodes."""
        if self.shutdown_event.is_set():
            raise SchedulerStopped("Scheduler shutdown has already been requested")
        started = self.clock()
        if started.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        moment = now or started
        if moment.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        local_time = moment.astimezone(PKT).isoformat(timespec="seconds")
        warnings: list[str] = []
        cleanup_summary: Mapping[str, Any] = {}
        if cleanup_scratch:
            try:
                cleanup_result = self.cleanup(ttl_hours=config.NVME_CACHE_TTL_HOURS, dry_run=False)
                cleanup_summary = asdict(cleanup_result) if hasattr(cleanup_result, "__dataclass_fields__") else dict(cleanup_result)
                if cleanup_summary.get("errors", 0):
                    warnings.append("Scratch cleanup reported one or more errors.")
            except (OSError, ValueError) as exc:
                warnings.append(f"Scratch cleanup could not complete: {type(exc).__name__}")
                LOGGER.exception("Scratch cleanup failed.")
        network = wait_for_network(
            max_attempts=self.max_network_attempts,
            base_delay_seconds=config.BOOT_NETWORK_BASE_DELAY_SECONDS,
            max_delay_seconds=config.BOOT_NETWORK_MAX_DELAY_SECONDS,
            probe=self.network_probe,
            sleep=self.sleep,
        ) if wait_for_connectivity else NetworkWaitResult(True, 0, ())
        due = self.due_episodes(now=moment)
        results: list[UploadAttempt] = []
        if network.connected:
            for workspace, episode in due:
                results.extend(self._upload_episode(workspace, episode))
        elif due:
            warnings.append("Network was unavailable after bounded retries; due uploads were left queued.")
        finished = self.clock()
        boot_idle = self._boot_run_active and not due
        complete = bool(due) and all(item.status in {"completed", "already_completed"} for item in results)
        should_shutdown = bool(self._boot_run_active and (boot_idle or complete))
        result = SchedulerRunResult(
            started_at=_utc_iso(started),
            finished_at=_utc_iso(finished),
            local_time=local_time,
            is_peak_window=is_pkt_peak_window(moment),
            network=network,
            cleanup=cleanup_summary,
            due_episode_count=len(due),
            attempts=tuple(results),
            shutdown_requested=should_shutdown,
            warnings=tuple(warnings),
        )
        self._last_result = result
        self._last_error = None
        LOGGER.info(
            "Scheduler run finished: due=%d attempts=%d peak_window=%s shutdown=%s.",
            len(due), len(results), result.is_peak_window, should_shutdown,
        )
        if should_shutdown:
            self.request_clean_shutdown()
        return result

    def run_boot_watchdog(self) -> SchedulerRunResult:
        """Perform one bounded startup cycle and request shutdown only when idle/completed."""
        self._boot_run_active = True
        try:
            return self.run_once(wait_for_connectivity=True, cleanup_scratch=True)
        finally:
            self._boot_run_active = False

    def request_clean_shutdown(self) -> None:
        """Signal all scheduler loops and the API server's shutdown watcher."""
        LOGGER.info("Controlled shutdown requested by the scheduler.")
        self.shutdown_event.set()
        self._stop_event.set()

    def start_background(self, *, boot_run: bool = False) -> None:
        with self._thread_lock:
            if self.running:
                return
            if self.shutdown_event.is_set():
                raise SchedulerStopped("A stopped scheduler cannot be restarted")
            self._stop_event.clear()
            target = self._run_boot_thread if boot_run else self._run_loop
            self._thread = threading.Thread(target=target, name="DaemonCutScheduler", daemon=True)
            self._thread.start()
            LOGGER.info("Scheduler background thread started (boot_run=%s).", boot_run)

    def _run_boot_thread(self) -> None:
        try:
            self.run_boot_watchdog()
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            LOGGER.exception("Boot watchdog failed.")
        finally:
            self._stop_event.set()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once(wait_for_connectivity=False, cleanup_scratch=False)
            except SchedulerStopped:
                break
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                LOGGER.exception("Scheduler poll failed.")
            self._stop_event.wait(self.poll_seconds)

    def stop_background(self, *, timeout_seconds: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout_seconds))
        LOGGER.info("Scheduler background thread stopped.")

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "shutdown_requested": self.shutdown_event.is_set(),
            "boot_run_active": self._boot_run_active,
            "last_error": self._last_error,
            "last_result": self._last_result.to_dict() if self._last_result else None,
            "timezone": "Asia/Karachi",
            "peak_times": ["21:30", "00:30"],
            "network_retries": self.max_network_attempts,
        }


__all__ = [
    "DEFAULT_PKT_PEAK_TIMES",
    "NetworkWaitResult",
    "SchedulerDaemon",
    "SchedulerError",
    "SchedulerRunResult",
    "SchedulerStopped",
    "UploadAttempt",
    "is_pkt_peak_window",
    "wait_for_network",
]

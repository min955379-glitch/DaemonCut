"""Safe 24-hour NVMe cleanup for disposable local scratch data only.

The cleaner is intentionally incapable of deleting source media, Drive folders,
voice samples, model weights, the SQLite database, or render archives. It only
visits ``.daemoncut/cache`` and ``.daemoncut/tmp`` and never follows symlinks.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from backend import config


@dataclass
class CleanupSummary:
    dry_run: bool
    ttl_hours: int
    files_considered: int = 0
    files_would_remove: int = 0
    files_removed: int = 0
    bytes_would_remove: int = 0
    bytes_removed: int = 0
    directories_would_remove: int = 0
    directories_removed: int = 0
    skipped_symlinks: int = 0
    errors: int = 0
    roots: list[str] | None = None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _approved_roots(roots: Iterable[str | Path] | None) -> list[Path]:
    # Use absolute lexical paths here rather than resolve(): resolve would hide
    # a cache-root symlink before the later safety check can reject it.
    allowed = {config.CACHE_DIR.absolute(), config.TEMP_DIR.absolute()}
    selected = [Path(path).expanduser().absolute() for path in roots] if roots is not None else sorted(allowed)
    runtime = config.RUNTIME_DIR.absolute()
    if runtime.is_symlink():
        raise ValueError("Refusing cleanup because .daemoncut itself is a symlink")
    for path in selected:
        if path not in allowed:
            raise ValueError(f"Refusing to clean unapproved directory: {path}")
        if path == runtime:
            raise ValueError("Refusing to clean the entire runtime directory")
        if not _is_relative_to(path, runtime):
            raise ValueError(f"Transient path is outside the local runtime folder: {path}")
        resolved = path.resolve(strict=False)
        if config.GDRIVE_DAEMONCUT_PATH is not None:
            drive = config.GDRIVE_DAEMONCUT_PATH.resolve(strict=False)
            if _is_relative_to(resolved, drive) or _is_relative_to(drive, resolved):
                raise ValueError("Refusing cleanup because a transient path overlaps Google Drive storage")
    return selected


def clean_transient_data(
    *,
    ttl_hours: int = config.NVME_CACHE_TTL_HOURS,
    now: datetime | None = None,
    dry_run: bool = True,
    roots: Iterable[str | Path] | None = None,
) -> CleanupSummary:
    """Remove only old files beneath the two approved local scratch folders.

    The default is a dry run. A regular file must be older than ``ttl_hours``
    by filesystem modification time. Empty old child folders may be removed;
    the cache/temp root folders themselves are always preserved.
    """
    if not 1 <= int(ttl_hours) <= 720:
        raise ValueError("ttl_hours must be between 1 and 720")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current.timestamp() - int(ttl_hours) * 3600
    selected = _approved_roots(roots)
    summary = CleanupSummary(
        dry_run=bool(dry_run),
        ttl_hours=int(ttl_hours),
        roots=[str(path) for path in selected],
    )

    for root in selected:
        if root.is_symlink():
            summary.skipped_symlinks += 1
            continue
        if not root.exists():
            continue
        if not root.is_dir():
            summary.errors += 1
            continue
        try:
            for current_dir, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
                directory = Path(current_dir)
                for dirname in list(dirnames):
                    if (directory / dirname).is_symlink():
                        summary.skipped_symlinks += 1
                for filename in filenames:
                    file_path = directory / filename
                    try:
                        if file_path.is_symlink():
                            summary.skipped_symlinks += 1
                            continue
                        info = file_path.stat(follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
                            continue
                        summary.files_considered += 1
                        summary.files_would_remove += 1
                        summary.bytes_would_remove += info.st_size
                        if not dry_run:
                            file_path.unlink()
                            summary.files_removed += 1
                            summary.bytes_removed += info.st_size
                    except FileNotFoundError:
                        continue
                    except OSError:
                        summary.errors += 1
                if directory != root:
                    try:
                        info = directory.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode) and info.st_mtime < cutoff and not any(directory.iterdir()):
                            summary.directories_would_remove += 1
                            if not dry_run:
                                directory.rmdir()
                                summary.directories_removed += 1
                    except FileNotFoundError:
                        continue
                    except OSError:
                        summary.errors += 1
        except OSError:
            summary.errors += 1
    return summary


def _main() -> int:
    parser = argparse.ArgumentParser(description="Safely prune old DaemonCut cache and temporary files")
    parser.add_argument("--ttl-hours", type=int, default=config.NVME_CACHE_TTL_HOURS)
    parser.add_argument("--execute", action="store_true", help="perform deletions (without this flag only report)")
    args = parser.parse_args()
    try:
        result = clean_transient_data(ttl_hours=args.ttl_hours, dry_run=not args.execute)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0 if result.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_main())

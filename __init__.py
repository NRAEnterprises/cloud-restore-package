"""
cloud_restore — core backup/restore logic for syncing arbitrary project
directories to and from a cloud service or remote host.

Standalone and file-type-agnostic: it doesn't know about device logs,
JSON schemas, or any specific data format, and it has ZERO dependency
on any other package (including autopath) — it is a complete, correct
tool on its own for any project that just needs "get this directory to
a remote/cloud target, and back again, reliably."

This package composes well with autopath (or anything else) in a
CALLING project, but never depends on it directly: a project that wants
richer local-path handling (fallback chains, collision-safe naming,
deep taxonomy folders, etc.) should prepare its local_path with
autopath — or whatever it likes — BEFORE calling backup()/restore()
here. This package's own local-directory handling
(_ensure_local_dir) is intentionally minimal, just enough to guarantee
restore() always has somewhere to write.

Two interchangeable backends — pick whichever matches what you already
have set up. Both implement the same interface, so calling code never
needs to know which one is actually configured:

  - RsyncBackend  — direct rsync, to a remote host over SSH or to
    another local/mounted path (a NAS mount, a second drive, etc.)

  - RcloneBackend — works with essentially any cloud provider (S3,
    Backblaze B2, Google Drive, Dropbox, and dozens more) via an rclone
    remote you've already set up with `rclone config`. This is the
    natural fit for "a cloud service of my choosing," since rclone is
    the one tool that speaks to nearly all of them the same way.

Safety defaults, on purpose:
  - Backup and restore never DELETE anything on the destination by
    default (rsync without --delete; rclone `copy` not `sync`). A
    silent deletion during what's supposed to be a backup is exactly
    the kind of failure that turns a safety net into a second way to
    lose data. Pass mirror=True explicitly if you actually want the
    destination to exactly match the source, deletions included.
  - Every operation returns a SyncResult with an explicit success flag
    and captured stderr — nothing here silently no-ops. If a backend
    binary isn't installed, or a remote is unreachable, you get a
    clear failure back, not an empty-looking success.
  - Multiple projects can share one backend/remote target without
    colliding, by namespacing every operation under a project_name
    subpath — see backup_project() / restore_project() below.
"""

import shutil
import re
import json
import subprocess
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .compression import (
    compress_directory, decompress_archive, detect_available_compressors,
    recommend_best_available, ARCHIVE_EXTENSIONS,
)


# =======================================================================
# Self-contained local-path helpers
# =======================================================================
#
# This package is deliberately independent of autopath (or anything
# else) — no cross-dependency between core logic packages. It only
# needs a bare-minimum, best-effort local directory creation and a
# name sanitizer for its own project-namespacing, so those are
# implemented inline here rather than imported. If a project using
# BOTH autopath and cloud_restore wants richer local-path handling
# (fallback chains, collision-safe naming, etc.), that composition
# happens in the CALLING project, not inside either core package.

def _ensure_local_dir(path: Path) -> Path:
    """Minimal best-effort directory creation. Not a full fallback
    chain — just enough that restore() always has somewhere to write.
    A calling project that needs more (verified writability, fallback
    paths, etc.) should prepare its local_path with a fuller tool
    (e.g. autopath.ensure_path) BEFORE calling restore(), rather than
    this package reimplementing that logic."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sanitize_name(value: str, fallback: str = "unknown") -> str:
    """Minimal sanitizer for this package's own project-namespacing use
    (turning a project_name into a safe remote subpath segment). Kept
    intentionally small/local rather than imported from elsewhere."""
    value = (value or "").strip()
    value = re.sub(r"[^A-Za-z0-9 _.\-]", "", value)
    value = re.sub(r"\s+", "-", value).strip("-")
    return value or fallback


# =======================================================================
# Shared result type
# =======================================================================

@dataclass
class SyncResult:
    success: bool
    direction: str              # "backup" or "restore"
    local_path: str
    remote_target: str
    command: list = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    dry_run: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _run(cmd: list[str], timeout: float = 3600.0):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


# =======================================================================
# Backend interface — both backends below implement this identically
# =======================================================================

class SyncBackend:
    def available(self) -> bool:
        """True if the underlying tool (rsync/rclone/etc.) is actually
        installed and usable. Always check this before relying on a
        backup/restore result — a missing binary should never be
        mistaken for 'nothing to sync'."""
        raise NotImplementedError

    def backup(self, local_path: Path, remote_subpath: str,
               mirror: bool = False, dry_run: bool = False) -> SyncResult:
        raise NotImplementedError

    def restore(self, remote_subpath: str, local_path: Path,
                mirror: bool = False, dry_run: bool = False) -> SyncResult:
        raise NotImplementedError

    def list_remote(self, remote_subpath: str) -> list[str] | None:
        """Best-effort listing of what's on the remote at this subpath.
        Returns None if the backend can't be reached at all (distinct
        from an empty list, which means reachable-but-nothing-there)."""
        raise NotImplementedError

    def fetch_file(self, remote_subpath: str, filename: str, local_dest: Path) -> bool:
        """Pulls ONE named file from the remote, without syncing
        anything else. Used to cheaply check a small manifest file
        before deciding how to handle the rest of a restore — returns
        False (not an exception) if the file isn't there, since 'this
        file doesn't exist on the remote' is an expected, normal
        outcome here, not an error."""
        raise NotImplementedError


# =======================================================================
# rsync backend
# =======================================================================

class RsyncBackend(SyncBackend):
    """
    remote_root can be:
      - a local/mounted path:  "/mnt/nas/backups"
      - an SSH target:         "user@host:/path/to/backups"
    """

    def __init__(self, remote_root: str, ssh_options: list[str] | None = None,
                 extra_rsync_args: list[str] | None = None):
        self.remote_root = remote_root.rstrip("/")
        self.ssh_options = ssh_options or ["-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
        self.extra_rsync_args = extra_rsync_args or []

    def available(self) -> bool:
        return shutil.which("rsync") is not None

    def _is_ssh_target(self) -> bool:
        # A local path can contain ":" on Windows drive letters, but this
        # project targets Linux/Termux, so ":" is a reliable SSH-target
        # signal here (rsync uses the same convention).
        return ":" in self.remote_root

    def _remote_path(self, remote_subpath: str) -> str:
        remote_subpath = remote_subpath.strip("/")
        return f"{self.remote_root}/{remote_subpath}" if remote_subpath else self.remote_root

    def _base_cmd(self, mirror: bool, dry_run: bool) -> list[str]:
        cmd = ["rsync", "-avz", "--checksum", "--partial"]
        if mirror:
            cmd.append("--delete")
        if dry_run:
            cmd.append("--dry-run")
        if self._is_ssh_target():
            cmd += ["-e", "ssh " + " ".join(self.ssh_options)]
        cmd += self.extra_rsync_args
        return cmd

    def backup(self, local_path: Path, remote_subpath: str,
               mirror: bool = False, dry_run: bool = False) -> SyncResult:
        local_path = Path(local_path)
        remote = self._remote_path(remote_subpath)
        cmd = self._base_cmd(mirror, dry_run) + [f"{local_path}/", f"{remote}/"]
        result = _run(cmd)
        return SyncResult(
            success=bool(result and result.returncode == 0),
            direction="backup", local_path=str(local_path), remote_target=remote,
            command=cmd,
            stdout=result.stdout if result else "",
            stderr=result.stderr if result else "rsync not available or the operation timed out",
            dry_run=dry_run,
        )

    def restore(self, remote_subpath: str, local_path: Path,
                mirror: bool = False, dry_run: bool = False) -> SyncResult:
        local_path = _ensure_local_dir(Path(local_path))
        remote = self._remote_path(remote_subpath)
        cmd = self._base_cmd(mirror, dry_run) + [f"{remote}/", f"{local_path}/"]
        result = _run(cmd)
        return SyncResult(
            success=bool(result and result.returncode == 0),
            direction="restore", local_path=str(local_path), remote_target=remote,
            command=cmd,
            stdout=result.stdout if result else "",
            stderr=result.stderr if result else "rsync not available or the operation timed out",
            dry_run=dry_run,
        )

    def list_remote(self, remote_subpath: str) -> list[str] | None:
        remote = self._remote_path(remote_subpath)
        cmd = ["rsync", "--list-only"]
        if self._is_ssh_target():
            cmd += ["-e", "ssh " + " ".join(self.ssh_options)]
        cmd += [f"{remote}/"]
        result = _run(cmd, timeout=30.0)
        if result is None or result.returncode != 0:
            return None
        return [line.split()[-1] for line in result.stdout.splitlines() if line.strip()]

    def fetch_file(self, remote_subpath: str, filename: str, local_dest: Path) -> bool:
        remote = self._remote_path(remote_subpath)
        cmd = ["rsync", "-az", "--checksum"]
        if self._is_ssh_target():
            cmd += ["-e", "ssh " + " ".join(self.ssh_options)]
        cmd += [f"{remote}/{filename}", str(local_dest)]
        result = _run(cmd, timeout=60.0)
        return bool(result and result.returncode == 0 and Path(local_dest).exists())


# =======================================================================
# rclone backend
# =======================================================================

class RcloneBackend(SyncBackend):
    """
    remote_name is whatever you configured via `rclone config` (e.g.
    "b2", "gdrive", "s3-backup"). remote_root is an optional path/bucket
    prefix under that remote.
    """

    def __init__(self, remote_name: str, remote_root: str = "",
                 extra_rclone_args: list[str] | None = None):
        self.remote_name = remote_name
        self.remote_root = remote_root.strip("/")
        self.extra_rclone_args = extra_rclone_args or []

    def available(self) -> bool:
        return shutil.which("rclone") is not None

    def _remote_path(self, remote_subpath: str) -> str:
        remote_subpath = remote_subpath.strip("/")
        parts = [p for p in (self.remote_root, remote_subpath) if p]
        path = "/".join(parts)
        return f"{self.remote_name}:{path}" if path else f"{self.remote_name}:"

    def _op_name(self, mirror: bool) -> str:
        # "copy" never deletes destination-only files; "sync" does.
        # mirror=True opts explicitly into the destructive one.
        return "sync" if mirror else "copy"

    def backup(self, local_path: Path, remote_subpath: str,
               mirror: bool = False, dry_run: bool = False) -> SyncResult:
        local_path = Path(local_path)
        remote = self._remote_path(remote_subpath)
        cmd = ["rclone", self._op_name(mirror), str(local_path), remote, "--checksum"]
        if dry_run:
            cmd.append("--dry-run")
        cmd += self.extra_rclone_args
        result = _run(cmd)
        return SyncResult(
            success=bool(result and result.returncode == 0),
            direction="backup", local_path=str(local_path), remote_target=remote,
            command=cmd,
            stdout=result.stdout if result else "",
            stderr=result.stderr if result else "rclone not available or the operation timed out",
            dry_run=dry_run,
        )

    def restore(self, remote_subpath: str, local_path: Path,
                mirror: bool = False, dry_run: bool = False) -> SyncResult:
        local_path = _ensure_local_dir(Path(local_path))
        remote = self._remote_path(remote_subpath)
        cmd = ["rclone", self._op_name(mirror), remote, str(local_path), "--checksum"]
        if dry_run:
            cmd.append("--dry-run")
        cmd += self.extra_rclone_args
        result = _run(cmd)
        return SyncResult(
            success=bool(result and result.returncode == 0),
            direction="restore", local_path=str(local_path), remote_target=remote,
            command=cmd,
            stdout=result.stdout if result else "",
            stderr=result.stderr if result else "rclone not available or the operation timed out",
            dry_run=dry_run,
        )

    def list_remote(self, remote_subpath: str) -> list[str] | None:
        remote = self._remote_path(remote_subpath)
        cmd = ["rclone", "lsf", remote]
        result = _run(cmd, timeout=30.0)
        if result is None or result.returncode != 0:
            return None
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def fetch_file(self, remote_subpath: str, filename: str, local_dest: Path) -> bool:
        remote = self._remote_path(remote_subpath)
        cmd = ["rclone", "copyto", f"{remote}/{filename}", str(local_dest)]
        result = _run(cmd, timeout=60.0)
        return bool(result and result.returncode == 0 and Path(local_dest).exists())


# =======================================================================
# Project-level orchestration — the part any project actually calls
# =======================================================================
#
# Namespacing every project under its own sanitized subpath means many
# unrelated projects (this compute-farm tooling, or anything else you
# build later) can share ONE configured backend/remote target without
# their data colliding or overwriting each other.

def backup_project(backend: SyncBackend, project_name: str, local_path: str | Path,
                    mirror: bool = False, dry_run: bool = False) -> SyncResult:
    """
    Backs up local_path to the given backend, namespaced under
    project_name. This is the one call any project needs for its
    backup side, regardless of which backend is actually configured.

    Also pushes a small manifest recording that this backup is
    uncompressed, so restore_project_auto() can tell the two backup
    modes apart later without the caller needing to remember or pass a
    flag — see the manifest section below for why this matters.
    """
    if not backend.available():
        return SyncResult(
            success=False, direction="backup", local_path=str(local_path),
            remote_target="", command=[],
            stderr=f"backend unavailable — required tool is not installed",
            dry_run=dry_run,
        )
    remote_subpath = _sanitize_name(project_name)
    result = backend.backup(Path(local_path), remote_subpath, mirror=mirror, dry_run=dry_run)
    if result.success and not dry_run:
        _push_manifest(backend, remote_subpath, {"compressed": False, "algorithm": None, "archive_name": None})
    return result


def restore_project(backend: SyncBackend, project_name: str, local_path: str | Path,
                     mirror: bool = False, dry_run: bool = False) -> SyncResult:
    """
    Restores project_name's data from the given backend down into
    local_path, ASSUMING an uncompressed backup — use this only when
    you already know that's the case (e.g. you always call
    backup_project(), never the _compressed variant, for this
    project). If you don't want to track that yourself, use
    restore_project_auto() instead, which checks the manifest and
    handles either mode automatically. This also means the manifest
    file written by backup_project() lands in local_path alongside
    your real data here — restore_project_auto() cleans that up;
    this plain version does not, since it doesn't know to look for it.
    """
    if not backend.available():
        return SyncResult(
            success=False, direction="restore", local_path=str(local_path),
            remote_target="", command=[],
            stderr=f"backend unavailable — required tool is not installed",
            dry_run=dry_run,
        )
    remote_subpath = _sanitize_name(project_name)
    return backend.restore(remote_subpath, Path(local_path), mirror=mirror, dry_run=dry_run)


def backup_project_compressed(backend: SyncBackend, project_name: str, local_path: str | Path,
                               algorithm: str | None = None, level: int | None = None,
                               dry_run: bool = False) -> SyncResult:
    """
    Compresses local_path into a single archive (algorithm auto-picked
    via recommend_best_available() if not specified) and syncs THAT one
    file to the backend, instead of syncing the directory tree
    uncompressed. Trades rsync's delta-transfer efficiency for real
    control over storage size — worth it for periodic backup snapshots,
    which is what this function is for; use the plain backup_project()
    above instead if you're syncing something that changes frequently
    and delta efficiency matters more than absolute size.

    Also pushes a manifest recording the algorithm and archive
    filename used, so restore_project_auto() knows exactly how to
    reverse this later without the caller having to specify it again.
    """
    algorithm = algorithm or recommend_best_available()
    if not backend.available():
        return SyncResult(
            success=False, direction="backup", local_path=str(local_path),
            remote_target="", command=[],
            stderr="backend unavailable — required tool is not installed",
            dry_run=dry_run,
        )

    local_path = Path(local_path)
    with tempfile.TemporaryDirectory(prefix="cloud_restore_compress_") as tmp:
        archive_name = f"{_sanitize_name(project_name)}{ARCHIVE_EXTENSIONS[algorithm]}"
        archive_path = Path(tmp) / archive_name
        try:
            compress_directory(local_path, archive_path, algorithm, level=level)
        except (RuntimeError, ValueError) as e:
            return SyncResult(
                success=False, direction="backup", local_path=str(local_path),
                remote_target="", command=[], stderr=str(e), dry_run=dry_run,
            )

        remote_subpath = _sanitize_name(project_name)
        # Sync the archive AND the manifest together — the temp dir now
        # contains both, so one backend.backup() call pushes them as a pair.
        if not dry_run:
            _write_manifest_file(Path(tmp), {
                "compressed": True, "algorithm": algorithm, "archive_name": archive_name,
            })
        return backend.backup(Path(tmp), remote_subpath, dry_run=dry_run)


def restore_project_compressed(backend: SyncBackend, project_name: str, local_path: str | Path,
                                algorithm: str | None = None, dry_run: bool = False) -> SyncResult:
    """
    Pulls the compressed archive down from the backend into a temp
    location, then extracts it into local_path. algorithm is
    auto-detected from the archive's filename if not given.
    """
    if not backend.available():
        return SyncResult(
            success=False, direction="restore", local_path=str(local_path),
            remote_target="", command=[],
            stderr="backend unavailable — required tool is not installed",
            dry_run=dry_run,
        )

    remote_subpath = _sanitize_name(project_name)
    with tempfile.TemporaryDirectory(prefix="cloud_restore_decompress_") as tmp:
        result = backend.restore(remote_subpath, Path(tmp), dry_run=dry_run)
        if not result.success or dry_run:
            return result

        archives = [p for p in Path(tmp).iterdir() if p.is_file() and p.name != _MANIFEST_FILENAME]
        if not archives:
            return SyncResult(
                success=False, direction="restore", local_path=str(local_path),
                remote_target=result.remote_target, command=result.command,
                stderr="restore reported success but no archive file was found in the pulled data",
            )

        try:
            decompress_archive(archives[0], Path(local_path), algorithm=algorithm)
        except (RuntimeError, ValueError) as e:
            return SyncResult(
                success=False, direction="restore", local_path=str(local_path),
                remote_target=result.remote_target, command=result.command, stderr=str(e),
            )

    return result


# =======================================================================
# Manifest — lets restore_project_auto() ask the remote what format a
# backup is in, instead of the caller having to know or remember
# =======================================================================
#
# This is the fix for a real ambiguity: if a project's backups have
# ever been made both compressed and uncompressed at different times
# (or you simply don't want restore code to have to track which mode
# was used), restore can't know which to expect just by looking at a
# project name. The manifest closes that gap at the CORE LOGIC level —
# every project built on this package gets it automatically, not just
# ones that remember to ask the same question themselves.

_MANIFEST_FILENAME = ".cloud_restore_manifest.json"


def _write_manifest_file(directory: Path, data: dict) -> Path:
    payload = dict(data)
    payload["written_at"] = datetime.now(timezone.utc).isoformat()
    manifest_path = directory / _MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return manifest_path


def _push_manifest(backend: SyncBackend, remote_subpath: str, data: dict) -> bool:
    """Syncs just the manifest up as a small, separate backup() call —
    since mirror defaults to False, this ADDS/updates the manifest file
    in the same remote directory without touching (or deleting) the
    real data already synced there in the preceding call."""
    with tempfile.TemporaryDirectory(prefix="cloud_restore_manifest_") as tmp:
        _write_manifest_file(Path(tmp), data)
        result = backend.backup(Path(tmp), remote_subpath, mirror=False, dry_run=False)
        return result.success


def fetch_manifest(backend: SyncBackend, project_name: str) -> dict | None:
    """
    Pulls just the manifest for a project, without downloading anything
    else. Returns None if there's no manifest (legacy backup made
    before this feature existed, or the manifest push failed) — callers
    should treat None as 'assume uncompressed', which matches this
    package's original, pre-manifest behavior exactly.
    """
    remote_subpath = _sanitize_name(project_name)
    with tempfile.TemporaryDirectory(prefix="cloud_restore_manifest_fetch_") as tmp:
        dest = Path(tmp) / _MANIFEST_FILENAME
        if not backend.fetch_file(remote_subpath, _MANIFEST_FILENAME, dest):
            return None
        try:
            return json.loads(dest.read_text())
        except (json.JSONDecodeError, OSError):
            return None


def restore_project_auto(backend: SyncBackend, project_name: str, local_path: str | Path,
                          dry_run: bool = False) -> SyncResult:
    """
    THE recommended restore entry point: checks the remote's manifest
    first (one small file fetch) to find out whether this project's
    backup is compressed and with what algorithm, then does the
    correct kind of restore automatically — no flag, no caller-tracked
    state, no risk of using the wrong mode. Falls back to plain
    uncompressed restore if no manifest is found (a backup made before
    this feature existed, or with an older version of this package).
    """
    if not backend.available():
        return SyncResult(
            success=False, direction="restore", local_path=str(local_path),
            remote_target="", command=[],
            stderr="backend unavailable — required tool is not installed",
            dry_run=dry_run,
        )

    manifest = fetch_manifest(backend, project_name)

    if manifest and manifest.get("compressed"):
        return restore_project_compressed(
            backend, project_name, local_path,
            algorithm=manifest.get("algorithm"), dry_run=dry_run,
        )

    result = restore_project(backend, project_name, local_path, dry_run=dry_run)
    if result.success and not dry_run:
        # The manifest (if this backup ever pushed one as uncompressed)
        # rides along in the same remote directory as the real data and
        # gets pulled down too — remove it from local_path since it's
        # bookkeeping, not part of the actual backed-up content.
        stray_manifest = Path(local_path) / _MANIFEST_FILENAME
        stray_manifest.unlink(missing_ok=True)
    return result


def restore_or_raise(backend: SyncBackend, project_name: str, local_path: str | Path,
                      mirror: bool = False, dry_run: bool = False) -> SyncResult:
    """
    Same as restore_project(), but raises instead of returning a failed
    result. Use this at the start of a rebuild script where a silent
    restore failure would mean everything downstream (rebuild_from_logs,
    etc.) runs against an empty/stale local directory without anyone
    noticing until much later. Fail loud, right here, instead.
    """
    result = restore_project(backend, project_name, local_path, mirror=mirror, dry_run=dry_run)
    if not result.success:
        raise RuntimeError(
            f"restore failed for project '{project_name}' from '{result.remote_target}': "
            f"{result.stderr or 'no error detail available'}"
        )
    return result


def verify_remote_reachable(backend: SyncBackend, project_name: str) -> bool:
    """
    Cheap pre-flight check: is the backend installed AND can it actually
    see this project's remote namespace? Call this before a rebuild
    depends on restore succeeding, so a misconfigured remote is caught
    immediately rather than discovered mid-rebuild.
    """
    if not backend.available():
        return False
    remote_subpath = _sanitize_name(project_name)
    listing = backend.list_remote(remote_subpath)
    return listing is not None


def print_sync_result(result: SyncResult) -> None:
    status = "OK" if result.success else "FAILED"
    label = "DRY RUN — " if result.dry_run else ""
    print(f"{label}{result.direction.upper()} [{status}]")
    print(f"  local:  {result.local_path}")
    print(f"  remote: {result.remote_target}")
    if not result.success and result.stderr:
        print(f"  error:  {result.stderr.strip()[:500]}")

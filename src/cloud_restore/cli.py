"""
Command-line interface for cloud_restore — generic backup/restore for
any local directory to/from a configured rsync or rclone target.

Usage:
    cloud_restore backup <local-path> <project-name> --backend rsync --remote user@host:/backups
    cloud_restore backup <local-path> <project-name> --backend rclone --remote-name b2 --remote-root farm
    cloud_restore restore <project-name> <local-path> --backend rsync --remote user@host:/backups
    cloud_restore list <project-name> --backend rclone --remote-name b2
"""

import sys
import argparse

from . import (
    RsyncBackend, RcloneBackend, backup_project, restore_project, print_sync_result,
    backup_project_compressed, restore_project_compressed, restore_project_auto,
)
from .compression import detect_available_compressors, recommend_best_available


def _build_backend(args):
    if args.backend == "rsync":
        if not args.remote:
            print("--remote is required for the rsync backend", file=sys.stderr)
            sys.exit(2)
        return RsyncBackend(remote_root=args.remote)
    elif args.backend == "rclone":
        if not args.remote_name:
            print("--remote-name is required for the rclone backend", file=sys.stderr)
            sys.exit(2)
        return RcloneBackend(remote_name=args.remote_name, remote_root=args.remote_root or "")
    else:
        print(f"unknown backend: {args.backend}", file=sys.stderr)
        sys.exit(2)


def _add_backend_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend", choices=["rsync", "rclone"], required=True)
    parser.add_argument("--remote", help="rsync only: remote root, e.g. user@host:/path or /mnt/nas/backups")
    parser.add_argument("--remote-name", help="rclone only: remote name from `rclone config`")
    parser.add_argument("--remote-root", help="rclone only: optional path/bucket prefix under the remote")
    parser.add_argument("--mirror", action="store_true", help="Also delete files on the other side that aren't on this side.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--compress", nargs="?", const="auto", default=None,
                         help="Compress into a single archive before syncing. Pass an algorithm "
                              "(gzip, bzip2, xz, zstd, lz4, brotli, 7z) or omit the value to "
                              "auto-pick the strongest one actually installed.")
    parser.add_argument("--level", type=int, default=None, help="Compression level, if --compress is used (meaning varies by algorithm).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cloud_restore",
        description="Generic backup/restore for any local directory, via rsync or rclone.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_backup = sub.add_parser("backup", help="Push a local directory to the remote, namespaced by project name.")
    p_backup.add_argument("local_path")
    p_backup.add_argument("project_name")
    _add_backend_args(p_backup)

    p_restore = sub.add_parser("restore", help="Pull a project's data from the remote into a local directory.")
    p_restore.add_argument("project_name")
    p_restore.add_argument("local_path")
    _add_backend_args(p_restore)

    sub.add_parser("compressors", help="List available compression algorithms and which are actually installed.")

    args = parser.parse_args(argv)

    if args.command == "compressors":
        avail = detect_available_compressors()
        best = recommend_best_available()
        for name, ok in sorted(avail.items()):
            builtin = " (stdlib, always available)" if name in ("gzip", "bzip2", "xz") else ""
            marker = "installed" if ok else "NOT installed"
            print(f"{name:8} {marker}{builtin}")
        print(f"\nauto-selected best available: {best}")
        return 0

    backend = _build_backend(args)
    algorithm = None
    if args.compress is not None:
        algorithm = None if args.compress == "auto" else args.compress

    if args.command == "backup":
        if args.compress is not None:
            result = backup_project_compressed(backend, args.project_name, args.local_path,
                                                 algorithm=algorithm, level=args.level, dry_run=args.dry_run)
        else:
            result = backup_project(backend, args.project_name, args.local_path,
                                     mirror=args.mirror, dry_run=args.dry_run)
    else:
        if args.compress is not None:
            # Explicit override — honor it even though restore_project_auto
            # exists, since the caller is deliberately overriding.
            if args.compress == "auto" or algorithm is None:
                result = restore_project_auto(backend, args.project_name, args.local_path, dry_run=args.dry_run)
            else:
                result = restore_project_compressed(backend, args.project_name, args.local_path,
                                                      algorithm=algorithm, dry_run=args.dry_run)
        else:
            # No --compress flag given at all -> use the manifest-driven
            # auto-detect, which is correct regardless of how the backup
            # was actually made.
            result = restore_project_auto(backend, args.project_name, args.local_path, dry_run=args.dry_run)

    print_sync_result(result)
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
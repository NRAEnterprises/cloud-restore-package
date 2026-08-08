"""
cloud_restore.compression — local archive compression, used as an
optional layer BEFORE handing data to a backup backend.

Why this exists as a separate local step rather than relying on
rsync's or rclone's own built-in compression:

  - rsync (3.2.0+) supports --compress-choice of zlib/zlibx/zstd/lz4/
    none — decent, but capped at those four, and rsync's delta-transfer
    algorithm works best on UNCOMPRESSED data in the first place (its
    whole design is diffing against a previous version; compressing
    beforehand defeats that). Its own --compress flag is meant for
    compressing data ON THE WIRE during a normal delta sync, not for
    minimizing storage size of a backup snapshot.

  - rclone's "compress" remote (gzip/zstd) is still explicitly marked
    EXPERIMENTAL by its own documentation — "things may break and data
    may be lost... don't use this remote in critical applications."
    That's not a reasonable foundation for something described as a
    backup/restore tool.

  - Neither gives you xz, bzip2, brotli, or 7z at all.

So: this module compresses a whole local directory into ONE archive
file, with your choice of algorithm and level, BEFORE it ever reaches
RsyncBackend/RcloneBackend — which then just moves that one file like
any other. You trade rsync's delta-efficiency for real, full control
over compression ratio; for periodic backup snapshots (this package's
actual use case) that trade is usually the right one, since a backup
run isn't a live continuous sync in the first place.

Algorithm coverage and what each needs:

  ALWAYS available, zero external dependency (pure Python stdlib):
    gzip  — via tarfile, fast, decent ratio, universal
    bzip2 — via tarfile, slower, somewhat better ratio than gzip
    xz    — via tarfile/lzma, the strongest ratio of the three, slower

  Available IF the real binary is installed (same upstream binaries on
  Linux and Termux — `pkg install zstd` / `pkg install brotli` /
  `pkg install lz4` / `pkg install p7zip` on Termux gets you the exact
  same tool as `apt install` does on Linux, not a lesser version):
    zstd    — modern, adjustable speed/ratio tradeoff, excellent
              all-round choice, often the best pick available
    lz4     — extremely fast, weakest ratio of the group
    brotli  — excellent on already text-heavy data (source code, JSON,
              logs — much of what this project actually archives)
    7z      — its own container format (not tar-based), typically the
              strongest ratio of all of these at high settings, at
              real time/CPU cost

recommend_best_available() picks the strongest one actually present,
so you get real improvement automatically as soon as you install a
better tool — no code changes needed, on either platform.
"""

import shutil
import subprocess
import tarfile
import lzma
from pathlib import Path


EXTERNAL_BINARIES = {"zstd": "zstd", "lz4": "lz4", "brotli": "brotli", "7z": "7z"}
STDLIB_ALGORITHMS = ("gzip", "bzip2", "xz")
ARCHIVE_EXTENSIONS = {
    "gzip": ".tar.gz", "bzip2": ".tar.bz2", "xz": ".tar.xz",
    "zstd": ".tar.zst", "lz4": ".tar.lz4", "brotli": ".tar.br", "7z": ".7z",
}

# Priority order for auto-selection: strongest realistic all-round
# choice first. zstd and brotli are prioritized over the always-present
# xz because, when actually installed, they generally beat it on the
# speed/ratio tradeoff that matters for repeated backup runs; 7z is
# excluded from AUTO-selection despite often having the best raw ratio,
# because its higher settings are also the slowest by a wide margin —
# pick it explicitly if you want it.
_AUTO_PRIORITY = ["zstd", "brotli", "xz", "bzip2", "lz4", "gzip"]


def detect_available_compressors() -> dict:
    """
    Returns {algorithm: True/False} for every algorithm this module
    knows about. gzip/bzip2/xz are always True (stdlib). The rest
    reflect whether the actual binary is on PATH right now — check
    this before assuming a stronger algorithm is usable, especially on
    a freshly set-up Termux install where you haven't run `pkg install`
    for these yet.
    """
    availability = {name: True for name in STDLIB_ALGORITHMS}
    for name, binary in EXTERNAL_BINARIES.items():
        availability[name] = shutil.which(binary) is not None
    return availability


def recommend_best_available() -> str:
    """Picks the strongest available algorithm automatically, per the
    priority order above. Always returns something usable — falls back
    to gzip (always present) if nothing else is installed."""
    avail = detect_available_compressors()
    for name in _AUTO_PRIORITY:
        if avail.get(name):
            return name
    return "gzip"


def _detect_algorithm_from_extension(path: Path) -> str:
    name = path.name
    for algo, ext in ARCHIVE_EXTENSIONS.items():
        if name.endswith(ext):
            return algo
    raise ValueError(f"could not determine compression algorithm from filename: {path}")


def compress_directory(source_dir: str | Path, archive_path: str | Path,
                        algorithm: str, level: int | None = None) -> Path:
    """
    Compresses the entire source_dir into a single archive at
    archive_path, using the given algorithm. Raises RuntimeError with a
    clear message (naming the missing binary and how to install it) if
    an external-tool algorithm is requested but not installed — never
    silently falls back to a different algorithm than the one you
    asked for.
    """
    source_dir = Path(source_dir).expanduser()
    archive_path = Path(archive_path).expanduser()
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    if algorithm in STDLIB_ALGORITHMS:
        if algorithm == "gzip":
            mode, kwargs = "w:gz", ({"compresslevel": level} if level is not None else {})
        elif algorithm == "bzip2":
            mode, kwargs = "w:bz2", ({"compresslevel": level} if level is not None else {})
        else:  # xz
            mode = "w:xz"
            if level is not None:
                preset = level | (lzma.PRESET_EXTREME if level >= 9 else 0)
                kwargs = {"preset": preset}
            else:
                kwargs = {}
        with tarfile.open(archive_path, mode, **kwargs) as tar:
            tar.add(source_dir, arcname=source_dir.name)
        return archive_path

    if algorithm in EXTERNAL_BINARIES:
        binary = EXTERNAL_BINARIES[algorithm]
        if shutil.which(binary) is None:
            raise RuntimeError(
                f"'{binary}' is not installed. Install it first: "
                f"Termux: `pkg install {binary if algorithm != '7z' else 'p7zip'}`  /  "
                f"Debian/Ubuntu: `sudo apt install {binary if algorithm != '7z' else 'p7zip-full'}`"
            )

        if algorithm == "7z":
            level_flag = [f"-mx={level}"] if level is not None else []
            subprocess.run(
                ["7z", "a"] + level_flag + [str(archive_path), str(source_dir)],
                check=True, capture_output=True,
            )
            return archive_path

        level_flag = []
        if level is not None:
            if algorithm == "zstd":
                level_flag = [f"-{level}"] + (["--ultra"] if level > 19 else [])
            elif algorithm == "lz4":
                level_flag = [f"-{level}"]
            elif algorithm == "brotli":
                level_flag = ["-q", str(level)]

        tar_proc = subprocess.Popen(
            ["tar", "-cf", "-", "-C", str(source_dir.parent), source_dir.name],
            stdout=subprocess.PIPE,
        )
        with archive_path.open("wb") as out_f:
            comp_proc = subprocess.Popen(
                [binary] + level_flag + ["-c"], stdin=tar_proc.stdout, stdout=out_f,
            )
            tar_proc.stdout.close()
            comp_proc.communicate()
        tar_proc.wait()
        if comp_proc.returncode != 0:
            raise RuntimeError(f"{binary} exited with code {comp_proc.returncode} while compressing")
        return archive_path

    raise ValueError(f"unknown algorithm: '{algorithm}' (expected one of {sorted(ARCHIVE_EXTENSIONS)})")


def decompress_archive(archive_path: str | Path, destination_dir: str | Path,
                        algorithm: str | None = None) -> Path:
    """
    Extracts archive_path back into destination_dir (created if
    needed). algorithm is auto-detected from the file extension if not
    given explicitly. Raises RuntimeError naming the missing binary if
    the archive needs an external tool that isn't installed — this is
    exactly the failure you want surfaced loudly during a restore, not
    silently skipped.
    """
    archive_path = Path(archive_path).expanduser()
    destination_dir = Path(destination_dir).expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)

    algorithm = algorithm or _detect_algorithm_from_extension(archive_path)

    if algorithm in STDLIB_ALGORITHMS:
        mode = {"gzip": "r:gz", "bzip2": "r:bz2", "xz": "r:xz"}[algorithm]
        with tarfile.open(archive_path, mode) as tar:
            # filter="data" -- required on modern Python to avoid the
            # path-traversal-unsafe legacy extraction behavior
            # (CVE-2007-4559-class issue); restoring from your own
            # backups should still go through the safe path.
            tar.extractall(destination_dir, filter="data")
        return destination_dir

    if algorithm in EXTERNAL_BINARIES:
        binary = EXTERNAL_BINARIES[algorithm]
        if shutil.which(binary) is None:
            raise RuntimeError(
                f"'{binary}' is not installed, so this archive can't be decompressed here. "
                f"Install it first: Termux: `pkg install {binary if algorithm != '7z' else 'p7zip'}`  /  "
                f"Debian/Ubuntu: `sudo apt install {binary if algorithm != '7z' else 'p7zip-full'}`"
            )

        if algorithm == "7z":
            subprocess.run(
                ["7z", "x", str(archive_path), f"-o{destination_dir}", "-y"],
                check=True, capture_output=True,
            )
            return destination_dir

        decomp_proc = subprocess.Popen([binary, "-dc", str(archive_path)], stdout=subprocess.PIPE)
        tar_proc = subprocess.Popen(["tar", "-xf", "-", "-C", str(destination_dir)], stdin=decomp_proc.stdout)
        decomp_proc.stdout.close()
        tar_proc.communicate()
        if tar_proc.returncode != 0:
            raise RuntimeError(f"extraction failed with exit code {tar_proc.returncode}")
        return destination_dir

    raise ValueError(f"unknown algorithm: '{algorithm}'")

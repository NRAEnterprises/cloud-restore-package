#!/bin/sh
# cloud_restore installer — works on Linux and Termux (Android).
#
# Pure-Python, no compiled extensions, and no dependency on any other
# package (including autopath) — fully standalone.
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/<YOUR_GH_USER>/cloud_restore/main/install.sh | sh

set -eu

REPO_URL="${CLOUD_RESTORE_REPO_URL:-https://github.com/CHANGE_ME/cloud_restore/archive/refs/heads/main.tar.gz}"

have_cmd() {
    command -v "$1" >/dev/null 2>&1
}

download() {
    url="$1"
    output="$2"
    if have_cmd curl; then
        curl -fsSL "$url" -o "$output"
    elif have_cmd wget; then
        wget -qO "$output" "$url"
    else
        echo "curl or wget is required to install cloud_restore." >&2
        exit 1
    fi
}

find_python() {
    for candidate in python3 python; do
        if have_cmd "$candidate"; then
            echo "$candidate"
            return 0
        fi
    done
    echo "No python3 interpreter found on PATH." >&2
    exit 1
}

main() {
    PY="$(find_python)"
    echo "Using interpreter: $($PY --version 2>&1) ($PY)"

    TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cloud-restore-install.XXXXXX")"
    trap 'rm -rf "$TMP_DIR"' EXIT INT TERM

    archive="$TMP_DIR/cloud_restore.tar.gz"
    download "$REPO_URL" "$archive"

    tar -xzf "$archive" -C "$TMP_DIR"
    src_dir="$(find "$TMP_DIR" -maxdepth 1 -type d -name 'cloud_restore-*' | head -n 1)"
    if [ -z "$src_dir" ]; then
        echo "Could not locate extracted package source directory." >&2
        exit 1
    fi

    if "$PY" -m pip install --user "$src_dir" 2>/tmp/cloud-restore-pip-err.log; then
        :
    else
        echo "Standard install failed, retrying with --break-system-packages..." >&2
        "$PY" -m pip install --user --break-system-packages "$src_dir"
    fi

    echo
    echo "cloud_restore installed."
    echo
    echo "This tool needs rsync AND/OR rclone actually installed to do real"
    echo "transfers (it wraps them; it doesn't replace them):"
    echo "  Termux:  pkg install rsync   /   pkg install rclone"
    echo "  Debian/Ubuntu: sudo apt install rsync   /   see rclone.org for rclone"
    echo
    echo "Try it:"
    echo "  cloud_restore --help"
}

main "$@"

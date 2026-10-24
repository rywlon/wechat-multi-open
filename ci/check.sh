#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
bash -n wechat-multi-open.sh
python3 -m unittest discover -s tests -v
python3 -m py_compile autoupdate/manager.py autoupdate/installer.py
if [[ "$(uname -s)" == "Darwin" ]]; then
    CHECK_BUILD_DIR="$(mktemp -d)"
    trap 'rm -rf "$CHECK_BUILD_DIR"' EXIT
    swiftc autoupdate/watcher.swift -o "$CHECK_BUILD_DIR/watcher"
    "$CHECK_BUILD_DIR/watcher" --self-test
    "$CHECK_BUILD_DIR/watcher" --event-test
fi

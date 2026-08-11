#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || {
  echo "Missing .venv. Create it with: python3 -m venv .venv" >&2
  exit 1
}
exec .venv/bin/python run.py

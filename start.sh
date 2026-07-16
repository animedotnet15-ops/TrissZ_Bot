#!/usr/bin/env bash
set -euo pipefail
python -m pip install --disable-pip-version-check --no-cache-dir -q -r requirements.txt
exec python main.py

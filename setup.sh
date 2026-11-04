#!/bin/bash
# One-time setup: creates .venv and installs dependencies.
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
chmod +x zt
echo "Done. Next: ./zt journal config --capital 100000   then   ./zt market"

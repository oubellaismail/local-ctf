#!/usr/bin/env bash
# ctfd-setup.sh — first-time CTFd setup (admin account, API token, team mode,
# controller key, rules page) and optional team import.
#
# Usage: make ctfd-setup                         (setup only)
#        make ctfd-setup TEAMS_CSV=data/teams.csv
#
# Admin password + token are saved to .secrets/ctfd-admin.env.

. "$(dirname "$0")/lib.sh"
load_config
load_secrets
kubectl_ready

export CTFD_URL CTFD_ADMIN_NAME CTFD_ADMIN_EMAIL CONTROLLER_KEY
export CTFD_EVENT_NAME="$EVENT_NAME"
export ENV_FILE="$SECRETS_DIR/ctfd-admin.env"
export PYTHON="$ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON=python3
export TEAMS_CSV="${TEAMS_CSV:-}"

exec bash "$ROOT/scripts/setup-ctfd.sh"

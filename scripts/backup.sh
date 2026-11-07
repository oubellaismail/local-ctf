#!/usr/bin/env bash
# backup.sh — dump the CTFd database to backups/ (plus a copy of .secrets/,
# because flags can't be re-checked without CTF_MASTER_SECRET).
#
# Usage: make backup [NAME=before-round-3]
# Copy backups/ to a USB stick or another laptop after each round.

. "$(dirname "$0")/lib.sh"
load_config
load_secrets
kubectl_ready

NAME="${1:-${NAME:-}}"
STAMP="$(date +%Y-%m-%d_%H-%M-%S)${NAME:+_$NAME}"
DIR="$ROOT/backups/$STAMP"
mkdir -p "$DIR"; chmod 700 "$ROOT/backups" "$DIR"

info "Dumping CTFd database"
kubectl -n ctfd exec mysql-0 -- sh -c \
  'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --no-tablespaces ctfd 2>/dev/null' \
  | gzip > "$DIR/ctfd.sql.gz"
SIZE=$(gzip -dc "$DIR/ctfd.sql.gz" | wc -c)
(( SIZE > 1000 )) || die "Dump looks empty ($SIZE bytes) — is mysql-0 running?"

# Challenges added from the dashboard exist only in the cluster
kubectl -n ctfd get configmap challenge-manifests -o yaml 2>/dev/null \
  | grep -vE '^\s+(resourceVersion|uid|creationTimestamp):' > "$DIR/challenge-manifests.yaml" || true

cp -r "$SECRETS_DIR" "$DIR/secrets"
cp "$CONFIG_FILE" "$DIR/config.env"
ok "Backup: backups/$STAMP ($(du -sh "$DIR" | cut -f1))"

#!/usr/bin/env bash
# restore.sh — restore a CTFd database dump made by backup.sh.
#
# Usage: make restore BACKUP=backups/2026-11-19_18-00-00
#
# The dump must match the CTF_MASTER_SECRET in .secrets/platform.env (flags
# are derived from it). If you restore onto a fresh laptop, copy the
# backup's secrets/ folder to .secrets/ first, then: make secrets && make deploy.

. "$(dirname "$0")/lib.sh"
load_config
load_secrets
kubectl_ready

DIR="${1:-${BACKUP:-}}"
[[ -n "$DIR" && -f "$DIR/ctfd.sql.gz" ]] || die "Usage: make restore BACKUP=backups/<folder>"

if [[ -f "$DIR/secrets/platform.env" ]] && \
   ! diff -q <(grep '^CTF_MASTER_SECRET=' "$DIR/secrets/platform.env") \
             <(grep '^CTF_MASTER_SECRET=' "$SECRETS_DIR/platform.env") >/dev/null; then
  warn "This backup was made with a different CTF_MASTER_SECRET — flags would not validate."
  die "Copy $DIR/secrets/ to .secrets/, run 'make secrets && make deploy', then retry."
fi

read -r -p "Replace the current CTFd database with $DIR? Type 'yes': " CONFIRM
[[ "$CONFIRM" == "yes" ]] || die "Aborted"

info "Taking a safety backup first"
bash "$ROOT/scripts/backup.sh" before-restore

info "Restoring"
kubectl -n ctfd scale deployment/ctfd --replicas=0 >/dev/null
kubectl -n ctfd wait --for=delete pod -l app=ctfd --timeout=120s >/dev/null 2>&1 || true
kubectl -n ctfd exec mysql-0 -- sh -c \
  'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "DROP DATABASE IF EXISTS ctfd; CREATE DATABASE ctfd CHARACTER SET utf8mb4; GRANT ALL ON ctfd.* TO \"ctfd\"@\"%\";" 2>/dev/null'
gzip -dc "$DIR/ctfd.sql.gz" | kubectl -n ctfd exec -i mysql-0 -- sh -c \
  'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" ctfd 2>/dev/null'
kubectl -n ctfd exec redis-0 -- redis-cli FLUSHALL >/dev/null
start_ctfd

if [[ -f "$DIR/secrets/ctfd-admin.env" ]]; then
  cp "$DIR/secrets/ctfd-admin.env" "$SECRETS_DIR/ctfd-admin.env"
  ok "Admin token restored from backup"
fi
ok "Restored $DIR"

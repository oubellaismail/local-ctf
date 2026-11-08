#!/usr/bin/env bash
# new-round.sh — reset the platform between biweekly rounds.
#
#   make new-round                 keep accounts/teams, clear challenges,
#                                  solves, submissions, scores
#   make new-round MODE=full       wipe everything (fresh CTFd, new admin
#                                  token); re-import teams afterwards
#
# Always takes a backup first. Afterwards: put the new round's manifests in
# challenges/manifests/ and run make challenges.

. "$(dirname "$0")/lib.sh"
load_config
load_secrets
kubectl_ready

MODE="${MODE:-keep-teams}"
[[ "$MODE" == "keep-teams" || "$MODE" == "full" ]] || die "MODE must be keep-teams or full"

echo "This will reset the CTF platform (mode: $MODE)."
read -r -p "Type 'reset' to continue: " CONFIRM
[[ "$CONFIRM" == "reset" ]] || die "Aborted"

info "Backing up the finished round"
bash "$ROOT/scripts/backup.sh" end-of-round

info "Removing all challenge instances"
kubectl -n challenges delete deployment,service,secret -l managed-by=ctf-controller --wait=false >/dev/null
ok "challenge resources deleted"

info "Clearing the round's challenge list and uploaded files"
kubectl -n ctfd exec "$(admin_pod)" -- python3 admin_cli.py clear >/dev/null
ok "manifests and download files cleared (images stay in the registry)"

mysql_exec() {
  kubectl -n ctfd exec -i mysql-0 -- sh -c 'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" ctfd 2>/dev/null'
}

if [[ "$MODE" == "keep-teams" ]]; then
  info "Clearing challenges, solves, submissions and scores (accounts kept)"
  # Everything tied to challenges or scores. Users, teams, config, pages and
  # tokens stay. Missing tables (older/newer CTFd) are skipped.
  TABLES="submissions solves awards unlocks flags hints tags files challenge_topics topics
          comments solutions ratings dynamic_challenge challenges notifications tracking
          multi_flag_components multi_flag_solves cheaters"
  {
    echo "SET FOREIGN_KEY_CHECKS=0;"
    for t in $TABLES; do
      echo "SET @t := (SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='ctfd' AND table_name='$t');"
      echo "SET @q := IF(@t > 0, 'TRUNCATE TABLE \`$t\`', 'DO 0');"
      echo "PREPARE s FROM @q; EXECUTE s; DEALLOCATE PREPARE s;"
    done
    echo "DELETE FROM config WHERE \`key\` = 'ctf_challenge_map';"
    echo "SET FOREIGN_KEY_CHECKS=1;"
  } | mysql_exec
  kubectl -n ctfd exec redis-0 -- redis-cli FLUSHALL >/dev/null
  kubectl -n ctfd rollout restart deployment/ctfd >/dev/null
  kubectl -n ctfd rollout status deployment/ctfd --timeout=300s >/dev/null
  ok "CTFd cleared; teams and players kept"
else
  info "Wiping the CTFd database"
  kubectl -n ctfd scale deployment/ctfd --replicas=0 >/dev/null
  kubectl -n ctfd wait --for=delete pod -l app=ctfd --timeout=120s >/dev/null 2>&1 || true
  echo "DROP DATABASE IF EXISTS ctfd; CREATE DATABASE ctfd CHARACTER SET utf8mb4; GRANT ALL ON ctfd.* TO 'ctfd'@'%';" \
    | kubectl -n ctfd exec -i mysql-0 -- sh -c 'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" 2>/dev/null'
  kubectl -n ctfd exec redis-0 -- redis-cli FLUSHALL >/dev/null
  rm -f "$SECRETS_DIR/ctfd-admin.env"
  sync_admin_token_secret
  start_ctfd >/dev/null
  ok "Fresh CTFd. Next: make ctfd-setup TEAMS_CSV=data/teams.csv"
fi

rm -rf "$ROOT/build/static"
echo ""
echo "Next: add the round's challenges in Admin → Platform, or put them in"
echo "challenges/manifests/ and run: make challenges"

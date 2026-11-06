#!/usr/bin/env bash
# push-challenge.sh — build a challenge image and push it to the local registry.
#
# Usage: bash scripts/push-challenge.sh <challenge-dir> [image-name]
#   challenge-dir  directory with a Dockerfile (e.g. challenges/src/web-login)
#   image-name     defaults to the directory name
#
# Manifest image field must then be:  ctf-registry:5000/challenges/<image-name>:latest

. "$(dirname "$0")/lib.sh"
load_config

DIR="${1:?Usage: $0 <challenge-dir> [image-name]}"
[[ -f "$DIR/Dockerfile" ]] || die "No Dockerfile in $DIR"
NAME="${2:-$(basename "$DIR")}"
[[ "$NAME" =~ ^[a-z0-9][a-z0-9._-]*$ ]] || die "Invalid image name '$NAME' (lowercase, digits, . _ -)"

registry_login
info "Building $NAME from $DIR"
docker_cmd build -t "$PUSH_REGISTRY/challenges/$NAME:latest" "$DIR"
docker_cmd push -q "$PUSH_REGISTRY/challenges/$NAME:latest" >/dev/null
ok "Pushed — use in the manifest:  image: $REGISTRY/challenges/$NAME:latest"

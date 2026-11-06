#!/usr/bin/env bash
# build-images.sh — build and push platform images to the local registry.
#
# Usage: bash scripts/build-images.sh [ctfd|controller|all]   (default: all)

. "$(dirname "$0")/lib.sh"
load_config

WHAT="${1:-all}"
registry_login

build_push() {
  local name="$1"; shift
  info "Building $name"
  docker_cmd build -t "$PUSH_REGISTRY/$name:latest" "$@"
  docker_cmd push -q "$PUSH_REGISTRY/$name:latest" >/dev/null
  ok "Pushed $REGISTRY/$name:latest"
}

case "$WHAT" in
  ctfd|all)
    build_push ctfd --build-arg "CTFD_VERSION=$CTFD_VERSION" -f "$ROOT/ctfd/Dockerfile" "$ROOT"
    ;;&
  controller|all)
    build_push controller "$ROOT/controller"
    ;;&
  all)
    # Placeholder static-files image so the deployment starts before any
    # static challenge exists. make static replaces it.
    if [[ ! -d "$ROOT/build/static" ]]; then
      mkdir -p "$ROOT/build/static"
      : > "$ROOT/build/static/.keep"
    fi
    build_push static-files -f "$ROOT/static-files/Dockerfile" "$ROOT/build/static"
    ;;
  ctfd|controller) ;;
  *) die "Unknown target '$WHAT' (ctfd|controller|all)" ;;
esac

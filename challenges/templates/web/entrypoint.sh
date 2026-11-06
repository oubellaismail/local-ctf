#!/bin/sh
set -e
if [ -z "${FLAG}" ]; then
  echo "ERROR: FLAG environment variable is required and must not be empty." >&2
  exit 1
fi
exec "$@"

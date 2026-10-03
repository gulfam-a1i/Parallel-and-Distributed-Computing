#!/usr/bin/env bash
# Starts the GPU worker in the foreground. Set RENDER_TOKEN first or pass --token.
cd "$(dirname "$0")"
exec python3 -m server --host 0.0.0.0 --port 5050 "$@"

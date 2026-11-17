#!/usr/bin/env bash
# Restart the desk with secrets loaded from ~/.desk_secrets/desk.env (0600). Never echoes values.
set -u; cd "$(dirname "$0")"
ENVF="$HOME/.desk_secrets/desk.env"
if [ -f "$ENVF" ]; then set -a; . "$ENVF"; set +a; fi
for k in TG_TOKEN DESK_RPC_URL GH_DESK_TOKEN; do
  if [ -z "${!k:-}" ]; then echo "start.sh: missing $k (not starting)"; exit 1; fi
done
PID=$(pgrep -f "python3 main.py" | head -1)
if [ -n "$PID" ]; then kill "$PID"; for i in $(seq 20); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done; kill -9 "$PID" 2>/dev/null; fi
nohup python3 main.py >> logs/main.out 2>&1 &
echo "desk started pid $! at $(date +%T) (secrets loaded: ok)"

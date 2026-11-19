#!/usr/bin/env bash
# Run the whole platform on your machine without Docker: governor (console) on :8080,
# runtime on :8081, chat app on :8082. Data in ./.local (SQLite). Dev login, dev mode
# (one person may author/approve/publish), mock models unless GOOGLE_AI_API_KEY is set.
#
#   ./scripts/run-local.sh            start (seeds the example agents on first start)
#   ./scripts/run-local.sh --reset    wipe ./.local first
#   GOOGLE_AI_API_KEY=... ./scripts/run-local.sh   use the Gemini API for real answers
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
DATA="$ROOT/.local"
PY="${PYTHON:-python3}"

if [ "${1:-}" = "--reset" ]; then
  rm -f "$DATA"/governor.db* "$DATA"/runtime.db*
fi
mkdir -p "$DATA"
FIRST_RUN=0
[ -f "$DATA/governor.db" ] || FIRST_RUN=1

"$PY" -c "import starlette, uvicorn, httpx, jwt, pydantic, jsonschema" 2>/dev/null || {
  echo "Missing Python packages. Install them with:"
  echo "  $PY -m pip install -e packages/eap_core -e services/governor -e services/runtime -e services/chat"
  exit 1
}

export PYTHONPATH="$ROOT/packages/eap_core:$ROOT/services/governor:$ROOT/services/runtime:$ROOT/services/chat:$ROOT"
export EAP_AUTH_MODES=dev
EMBED="mock-embedding"
[ -n "${GOOGLE_AI_API_KEY:-}" ] && EMBED="google_ai:gemini-embedding-001"

pids=()
stopped=0
cleanup() {
  [ "$stopped" = 1 ] && return; stopped=1
  echo; echo "Stopping..."; kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

DATABASE_URL="sqlite:///$DATA/runtime.db" PORT=8081 EAP_GOVERNOR_URL=http://127.0.0.1:8080 \
  EAP_CALL_CONTEXT_KEY=local-dev-call-context-key-0123456789abcdef \
  "$PY" -c "from eap_runtime.app import main; main()" > "$DATA/runtime.log" 2>&1 &
pids+=($!)
DATABASE_URL="sqlite:///$DATA/governor.db" PORT=8080 EAP_RUNTIME_URL=http://127.0.0.1:8081 \
  EAP_EVENTS_PUSH_URLS=http://127.0.0.1:8081/internal/v1/events EAP_EVENTS_PUSH_DEV_USER=governor \
  EAP_BOOTSTRAP_ADMINS=admin@example.com EAP_RELAXED_RELEASE=true EAP_CHAT_URL=http://localhost:8082/ \
  EAP_WEB_EMBEDDING_MODEL="$EMBED" \
  "$PY" -c "from eap_governor.app import main; main()" > "$DATA/governor.log" 2>&1 &
pids+=($!)
PORT=8082 EAP_GOVERNOR_URL=http://127.0.0.1:8080 EAP_RUNTIME_URL=http://127.0.0.1:8081 \
  EAP_CHAT_DEV_USER=admin@example.com EAP_CHAT_TITLE="Agent Chat (local)" \
  "$PY" -c "from eap_chat.app import main; main()" > "$DATA/chat.log" 2>&1 &
pids+=($!)

for _ in $(seq 1 30); do
  if "$PY" - <<'EOF' 2>/dev/null
import urllib.request
for port in (8080, 8081, 8082):
    urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
EOF
  then break; fi
  sleep 1
done

if [ "$FIRST_RUN" = 1 ]; then
  echo "Seeding example agents..."
  "$PY" examples/seed.py --governor http://127.0.0.1:8080 --runtime http://127.0.0.1:8081 --studio \
    || echo "Seeding failed - see $DATA/*.log"
fi

cat <<EOF

  Console : http://localhost:8080   (top bar: dev user admin@example.com, roles *:platform_admin, click Use)
  Chat app: http://localhost:8082
  Logs    : $DATA/governor.log, runtime.log, chat.log
  Models  : $([ -n "${GOOGLE_AI_API_KEY:-}" ] && echo "Gemini API key set - Catalog > google-ai-flash > Test, then set active" || echo "mock (set GOOGLE_AI_API_KEY for real answers)")

  Press Ctrl+C to stop.
EOF
wait

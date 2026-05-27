#!/usr/bin/env bash
set -euo pipefail

echo "=========================================="
echo "MiniCode dev launcher"
echo "=========================================="
echo

if [[ ! -f "pyproject.toml" ]]; then
  echo "Error: run this script from the MiniCode project root." >&2
  exit 1
fi

find_port() {
  local start="$1"
  local end="$2"
  python - "$start" "$end" <<'PY'
import socket
import sys

start = int(sys.argv[1])
end = int(sys.argv[2])
for port in range(start, end):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            continue
        print(port)
        raise SystemExit(0)
raise SystemExit(1)
PY
}

BACKEND_PORT="$(find_port 8000 8100)"
FRONTEND_PORT="$(find_port 5173 5273)"

export MINICODE_BACKEND_HOST="127.0.0.1"
export MINICODE_BACKEND_PORT="$BACKEND_PORT"
export MINICODE_API_BASE_URL="http://127.0.0.1:$BACKEND_PORT"
export MINICODE_WS_BASE_URL="ws://127.0.0.1:$BACKEND_PORT"
export MINICODE_FRONTEND_URL="http://127.0.0.1:$FRONTEND_PORT"
export VITE_DEV_BACKEND_ORIGIN="$MINICODE_API_BASE_URL"
export VITE_API_BASE_URL="$MINICODE_API_BASE_URL"
export VITE_WS_BASE_URL="$MINICODE_WS_BASE_URL"

echo "1. Starting backend on $MINICODE_API_BASE_URL"
python -m backend > backend.log 2>&1 &
BACKEND_PID=$!
echo "   Backend PID: $BACKEND_PID"
echo "   Backend log: backend.log"

echo
echo "2. Starting frontend on $MINICODE_FRONTEND_URL"
(
  cd frontend
  npm run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT" > ../frontend.log 2>&1
) &
FRONTEND_PID=$!
echo "   Frontend PID: $FRONTEND_PID"
echo "   Frontend log: frontend.log"

echo
echo "=========================================="
echo "MiniCode started"
echo "=========================================="
echo "Backend:  $MINICODE_API_BASE_URL"
echo "Frontend: $MINICODE_FRONTEND_URL"
echo "WebSocket: $MINICODE_WS_BASE_URL"
echo
echo "Stop services:"
echo "  kill $BACKEND_PID $FRONTEND_PID"
echo
echo "Logs:"
echo "  tail -f backend.log"
echo "  tail -f frontend.log"

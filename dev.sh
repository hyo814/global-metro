#!/usr/bin/env bash
# 로컬 개발: Flask(127.0.0.1:5001) + Vite(0.0.0.0:5173). 폰은 같은 와이파이에서 접속.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .cache/stops.db ] || { echo "인덱스가 없습니다. python3 backend/build_index.py 를 먼저 실행하세요."; exit 1; }
[ -d frontend/node_modules ] || (cd frontend && npm install)

(cd backend && exec python3 app.py) &
trap 'kill 0' EXIT

IP=$(ipconfig getifaddr en0 || ipconfig getifaddr en1 || echo localhost)
echo ""
echo "📱 폰(같은 와이파이)에서 열기: http://$IP:5173"
echo ""
cd frontend && npm run dev -- --host 0.0.0.0

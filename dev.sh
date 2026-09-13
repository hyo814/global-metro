#!/usr/bin/env bash
# 로컬 개발: Flask(0.0.0.0:5001). 폰은 같은 와이파이에서 접속.
# 프론트는 static/index.html 한 장이라 별도 빌드 서버가 없다.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .cache/stops.db ] || { echo "인덱스가 없습니다. python3 build_index.py 를 먼저 실행하세요."; exit 1; }

IP=$(ipconfig getifaddr en0 || ipconfig getifaddr en1 || echo localhost)
echo ""
echo "📱 폰(같은 와이파이)에서 열기: http://$IP:5001"
echo ""
exec python3 app.py

#!/usr/bin/env python3
"""전 세계 GTFS 조회 API.

검색은 전역 인덱스(.cache/stops.db) 하나로 끝내고, 시간표는 그 정류장이 속한
피드의 zip 하나만 열어서 뽑는다. 무거운 stop_times를 전역으로 모으지 않는
이유가 이거다 — 정류장 하나는 피드 하나에만 속한다.

    python3 build_index.py     # 먼저 인덱스 구축
    python3 app.py
"""
import socket
import threading
from datetime import datetime
from functools import lru_cache

from flask import Flask, jsonify, request, send_from_directory

from build_index import countries, feed_info, search
from gtfs import Feed
from translate import korean, to_original

app = Flask(__name__, static_folder="static")


# ponytail: 최근 쓴 피드 16개만 열어둔다. 밀려난 Feed의 DuckDB 커넥션은 GC가
# 정리한다. 메모리가 문제되면 명시적으로 close하는 래퍼를 씌울 것.
@lru_cache(maxsize=16)
def feed_for(zip_path):
    return Feed(zip_path)


# DuckDB 커넥션은 스레드 간 공유가 안전하지 않다. 시간표 조회만 직렬화한다.
# 검색(SQLite)과 번역(HTTP)은 잠그지 않아서, 5초짜리 번역이 도는 동안에도
# 화면이 멈추지 않는다.
_feed_lock = threading.Lock()


def with_korean(rows, *fields, use_api=False):
    """지정 필드에 <field>_ko를 붙인다. 기본은 캐시만 — 번역 대기로 응답을 막지 않는다."""
    ko = korean([r[f] for r in rows for f in fields if r.get(f)], use_api=use_api)
    for r in rows:
        for f in fields:
            r[f + "_ko"] = ko.get((r.get(f) or "").strip(), "")
    return rows


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.post("/api/translate")
def translate():
    """화면을 먼저 그린 뒤 UI가 부르는 번역 엔드포인트."""
    texts = (request.get_json(silent=True) or {}).get("texts") or []
    if not isinstance(texts, list):
        return jsonify({"error": "texts는 배열이어야 합니다"}), 400
    return jsonify(korean([str(t) for t in texts][:120]))


@app.get("/api/countries")
def country_list():
    return jsonify(countries())


@app.get("/api/stops")
def stops():
    q = request.args.get("q", "").strip()
    country = request.args.get("country", "").strip().upper()
    if not q:
        return jsonify([])

    rows = search(q, country, limit=25)
    # 한글로 검색하면 인덱스(현지 표기)에 걸리지 않는다. 현지 표기 후보로 바꿔
    # 다시 찾는다. 이게 이 앱을 만든 이유라서 폴백이 아니라 본 경로에 가깝다.
    if not rows:
        guess_cc, cands = to_original(q)
        cands = cands[:4]
        # 사용자가 국가를 고르지 않았으면 모델이 판단한 국가로 좁힌다.
        # 한 문자 체계 안에서만 비교하게 되어 순위가 안정된다.
        scope = country or guess_cc

        def gather(cc):
            # 후보별로 끊지 않고 전부 모은다. 한 후보가 25칸을 다 먹으면
            # 더 정확한 뒤쪽 후보("新宿" 다음의 "新宿駅")가 검색조차 안 된다.
            seen, out = set(), []
            for cand in cands:
                for r in search(cand, cc, limit=25):
                    key = (r["feed_id"], r["stop_id"])
                    if key not in seen:
                        seen.add(key)
                        out.append(r)
            return out

        pool = gather(scope)
        # 폴백은 풀 전체가 빌 때만. 후보별로 걸면 "Osaka"가 일본에서 0건일 때
        # 전 세계로 넓어져 독일의 Osakaallee가 딸려 들어온다.
        if not pool and scope != country:
            pool = gather(country)
        low = {c.casefold() for c in cands}

        def specificity(name):
            """이름이 시작하는 후보 중 가장 긴 것의 길이.

            "오사카"의 후보는 大阪와 大阪駅 둘 다다. 大阪駅ＪＲ高速バスターミナル은
            더 구체적인 大阪駅로 시작하므로 大阪屋ショップ(마트)보다 위여야 한다.
            정차 횟수만 보면 손님 많은 마트가 이긴다.

            국가로 좁힌 뒤라 비교 대상이 같은 문자 체계다. 그래서 글자 수를
            그대로 써도 된다.
            """
            n = name.casefold()
            return max((len(c) for c in cands if n.startswith(c.casefold())),
                       default=0)

        pool.sort(key=lambda r: (r["stop_name"].casefold() not in low,
                                 -specificity(r["stop_name"]),
                                 -r["trips"], len(r["stop_name"])))
        rows = pool[:25]
    return jsonify(with_korean(rows, "stop_name", "agency"))


@app.get("/api/departures")
def departures():
    feed_id = request.args.get("feed_id", "").strip()
    stop_id = request.args.get("stop_id", "").strip()
    if not feed_id or not stop_id:
        return jsonify({"error": "feed_id와 stop_id가 필요합니다"}), 400

    info = feed_info(feed_id)
    if not info:
        return jsonify({"error": f"모르는 피드입니다: {feed_id}"}), 404

    feed = feed_for(info["zip"])
    now = datetime.now(feed.tz)
    with _feed_lock:
        runs = feed.departures(stop_id, now=now)
    return jsonify({
        "agency": info["agency"],
        "timezone": info["timezone"],
        "country": info["country"],
        # 여행자는 다른 시간대에 있을 수 있다. "3분 후"가 어느 시계 기준인지 보여준다.
        "local_time": now.strftime("%H:%M"),
        "departures": with_korean(runs, "route", "headsign"),
    })


def lan_ip():
    """같은 Wi-Fi의 폰에서 접속할 주소. 실제로 나가지는 않는 UDP 소켓으로 알아낸다."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    print(f"\n  폰에서 열기:  http://{lan_ip()}:5001\n"
          f"  같은 Wi-Fi에 있어야 합니다.\n")
    # 0.0.0.0: 같은 네트워크의 다른 기기에서 접속할 수 있게 한다.
    app.run(host="0.0.0.0", port=5001, threaded=True)

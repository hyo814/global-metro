#!/usr/bin/env python3
"""전 세계 GTFS 조회 API.

검색은 전역 인덱스(.cache/stops.db) 하나로 끝내고, 시간표는 그 정류장이 속한
피드의 zip 하나만 열어서 뽑는다. 무거운 stop_times를 전역으로 모으지 않는
이유가 이거다 — 정류장 하나는 피드 하나에만 속한다.

    python3 build_index.py     # 먼저 인덱스 구축
    python3 app.py
"""
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
        seen = set()
        for cand in to_original(q):
            for r in search(cand, country, limit=25):
                key = (r["feed_id"], r["stop_id"])
                if key not in seen:
                    seen.add(key)
                    rows.append(r)
            if len(rows) >= 25:
                break
        rows = rows[:25]
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
    return jsonify({
        "agency": info["agency"],
        "timezone": info["timezone"],
        "country": info["country"],
        # 여행자는 다른 시간대에 있을 수 있다. "3분 후"가 어느 시계 기준인지 보여준다.
        "local_time": now.strftime("%H:%M"),
        "departures": with_korean(feed.departures(stop_id, now=now), "route", "headsign"),
    })


if __name__ == "__main__":
    # ponytail: threaded=False. 피드마다 DuckDB 커넥션 하나를 공유하므로
    # 동시 요청이 위험하다. 다중 사용자가 필요해지면 요청마다 cursor()를 쓸 것.
    app.run(port=5001, threaded=False)

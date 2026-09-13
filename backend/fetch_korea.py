#!/usr/bin/env python3
"""TAGO(국가대중교통정보센터) 버스 정보를 GTFS로 바꾼다.

한국은 Mobility Database에 피드가 0개다. 데이터가 없는 게 아니라 GTFS가
아닐 뿐이다 — TAGO가 공공데이터포털로 전국 버스 정보를 연다.

    data.go.kr 가입 -> "국토교통부_(TAGO)_버스노선정보"와 "버스정류소정보" 활용신청
    .env 에  DATA_GO_KR_KEY=...  추가   (디코딩된 일반 인증키)

    python3 fetch_korea.py --probe          # 응답 형태부터 확인
    python3 fetch_korea.py --cities         # 도시 코드 목록
    python3 fetch_korea.py --city 25        # 그 도시를 GTFS로

한계 두 가지를 먼저 알아야 한다.

1. TAGO는 시각표를 주지 않는다. 첫차·막차·배차간격만 준다. 그래서 GTFS의
   frequencies.txt로 만든다. "몇 분마다 온다"는 정확하지만 "몇 시 몇 분에
   온다"는 없다. 지하철은 서울교통공사가 실제 시간표를 따로 공개하므로
   그쪽은 이 파일이 아니라 별도 변환이 필요하다.

2. 정류장 사이 소요시간도 주지 않는다. 좌표 거리로 추정한다(아래 SPEED).
   도착 예정 시각은 추정값이며, feed_info.txt에도 그렇게 적어 둔다.

검증 안 된 코드다. 엔드포인트와 응답 필드는 공공데이터포털 문서에서 확인했지만
실제로 돌려본 적이 없다. --probe 를 먼저 실행해 가정이 맞는지 보라.
"""
import argparse
import csv
import io
import math
import os
import pathlib
import sys
import zipfile

import requests
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OUT = ROOT / "gtfs"
ROUTE_API = "https://apis.data.go.kr/1613000/BusRouteInfoInqireService"
STOP_API = "https://apis.data.go.kr/1613000/BusSttnInfoInqireService"
KEY = os.getenv("DATA_GO_KR_KEY")

SPEED = 18_000 / 3600      # m/s. 도심 시내버스 표정속도 18km/h로 잡은 추정치
DWELL = 20                 # s. 정류장당 정차 시간
PAGE = 1000                # 한 번에 받을 행 수

# GTFS route_type. TAGO의 routetp는 한국어 분류라 전부 버스(3)로 간다.
BUS = "3"


def die(msg):
    print(f"오류: {msg}", file=sys.stderr)
    sys.exit(1)


def call(base, op, **params):
    """공공데이터포털 호출. 응답 형태가 제멋대로라 여기서 다 흡수한다."""
    params = {"serviceKey": KEY, "_type": "json", "numOfRows": PAGE,
              "pageNo": 1, **params}
    r = requests.get(f"{base}/{op}", params=params, timeout=60)
    r.raise_for_status()
    try:
        body = r.json()
    except ValueError:
        # 인증 실패는 JSON이 아니라 XML 에러로 온다
        die(f"{op}: JSON이 아닌 응답입니다. 키가 잘못됐을 수 있습니다.\n{r.text[:300]}")

    head = (body.get("response") or {}).get("header") or {}
    if head.get("resultCode") not in ("00", "0", None):
        die(f"{op}: {head.get('resultCode')} {head.get('resultMsg')}")

    payload = (body.get("response") or {}).get("body") or {}
    items = payload.get("items")
    if not items:                      # 결과 없음은 "" 로 오기도 한다
        return [], 0
    item = items.get("item") if isinstance(items, dict) else items
    if isinstance(item, dict):         # 한 건이면 리스트가 아니라 dict로 온다
        item = [item]
    return item or [], int(payload.get("totalCount") or 0)


def call_all(base, op, **params):
    """페이지를 끝까지."""
    out, page = [], 1
    while True:
        rows, total = call(base, op, pageNo=page, **params)
        out += rows
        if not rows or len(out) >= total:
            return out
        page += 1


def dist(a, b):
    (la1, lo1), (la2, lo2) = a, b
    dy = (la2 - la1) * 111_000
    dx = (lo2 - lo1) * 111_000 * math.cos(math.radians(la1))
    return math.hypot(dx, dy)


def hhmmss(v):
    """TAGO의 '0530' 또는 '05:30' -> '05:30:00'. 못 읽으면 None."""
    s = str(v or "").strip().replace(":", "")
    if len(s) == 4 and s.isdigit():
        return f"{s[:2]}:{s[2:]}:00"
    return None


def minutes(v):
    try:
        n = int(str(v).strip())
        return n * 60 if 0 < n < 240 else None
    except (TypeError, ValueError):
        return None


def probe():
    """가정이 맞는지 한 번에 확인한다. 도시 -> 노선 -> 정류장 순서로 한 건씩."""
    import json

    print("1) 도시 코드")
    cities, _ = call(ROUTE_API, "getCtyCodeList")
    print(f"   {len(cities)}개 · 예: {cities[:3]}")
    if not cities:
        return print("   도시가 없습니다. 활용신청이 승인됐는지 확인하세요.")

    code = cities[0].get("citycode")
    print(f"\n2) 노선 목록 (citycode={code})")
    routes, total = call(ROUTE_API, "getRouteNoList", cityCode=code, numOfRows=3)
    print(f"   전체 {total}개 · 첫 건 필드: {sorted(routes[0])if routes else '없음'}")
    print(f"   {json.dumps(routes[0] if routes else {}, ensure_ascii=False)[:300]}")
    if not routes:
        return

    rid = routes[0].get("routeid")
    print(f"\n3) 노선 상세 (routeId={rid})")
    info, _ = call(ROUTE_API, "getRouteInfoIem", cityCode=code, routeId=rid)
    print(f"   {json.dumps(info[0] if info else {}, ensure_ascii=False)[:400]}")

    print(f"\n4) 노선의 정류장 순서")
    stops, n = call(ROUTE_API, "getRouteAcctoThrghSttnList",
                    cityCode=code, routeId=rid, numOfRows=3)
    print(f"   전체 {n}개 · 첫 건: {json.dumps(stops[0] if stops else {}, ensure_ascii=False)[:300]}")
    print(f"   필드: {sorted(stops[0]) if stops else '없음'}")

    need = {"nodeid", "nodenm", "gpslati", "gpslong", "nodeord"}
    if stops and not need <= set(stops[0]):
        print(f"\n   ! 예상한 필드와 다릅니다. 없는 것: {sorted(need - set(stops[0]))}")
        print("   fetch_korea.py 의 build_city() 에서 필드명을 맞춰 주세요.")
    else:
        print("\n   예상한 필드가 모두 있습니다. 변환을 실행하세요:")
        print(f"   python3 fetch_korea.py --city {code}")


def build_city(code, name):
    """한 도시의 버스망을 GTFS zip으로."""
    routes = call_all(ROUTE_API, "getRouteNoList", cityCode=code)
    if not routes:
        die(f"citycode {code} 에 노선이 없습니다.")
    print(f"  노선 {len(routes)}개")

    stops, s_rows, t_rows, r_rows, f_rows = {}, [], [], [], []
    for i, r in enumerate(routes, 1):
        rid = str(r.get("routeid") or "").strip()
        if not rid:
            continue
        seq = call_all(ROUTE_API, "getRouteAcctoThrghSttnList",
                       cityCode=code, routeId=rid)
        if len(seq) < 2:
            continue
        seq.sort(key=lambda x: int(x.get("nodeord") or 0))

        info, _ = call(ROUTE_API, "getRouteInfoIem", cityCode=code, routeId=rid)
        info = info[0] if info else {}
        first = hhmmss(info.get("startvehicletime")) or "05:30:00"
        last = hhmmss(info.get("endvehicletime")) or "23:00:00"

        r_rows.append({"route_id": rid, "agency_id": f"tago-{code}",
                       "route_short_name": str(r.get("routeno") or "").strip(),
                       "route_long_name": f"{info.get('startnodenm','')}-{info.get('endnodenm','')}".strip("-"),
                       "route_type": BUS})

        trip_id = f"{rid}-1"
        t_rows.append({"route_id": rid, "service_id": "weekday", "trip_id": trip_id,
                       "trip_headsign": str(seq[-1].get("nodenm") or "").strip()})

        # 소요시간은 좌표 거리로 추정한다. TAGO가 주지 않는 값이다.
        clock, prev = 0, None
        for order, s in enumerate(seq):
            sid = str(s.get("nodeid") or "").strip()
            try:
                la, lo = float(s.get("gpslati")), float(s.get("gpslong"))
            except (TypeError, ValueError):
                continue
            stops[sid] = {"stop_id": sid, "stop_name": str(s.get("nodenm") or "").strip(),
                          "stop_lat": la, "stop_lon": lo}
            if prev is not None:
                clock += int(dist(prev, (la, lo)) / SPEED) + DWELL
            prev = (la, lo)
            hms = f"{clock // 3600:02d}:{clock % 3600 // 60:02d}:{clock % 60:02d}"
            s_rows.append({"trip_id": trip_id, "arrival_time": hms, "departure_time": hms,
                           "stop_id": sid, "stop_sequence": order})

        for svc, field in (("weekday", "intervaltime"), ("sat", "intervalsattime"),
                           ("sun", "intervalsuntime")):
            head = minutes(info.get(field))
            if head:
                f_rows.append({"trip_id": trip_id if svc == "weekday" else f"{rid}-{svc}",
                               "start_time": first, "end_time": last,
                               "headway_secs": head, "exact_times": 0})
                if svc != "weekday":
                    t_rows.append({"route_id": rid, "service_id": svc,
                                   "trip_id": f"{rid}-{svc}",
                                   "trip_headsign": t_rows[0]["trip_headsign"]})
                    for row in [x for x in s_rows if x["trip_id"] == trip_id]:
                        s_rows.append({**row, "trip_id": f"{rid}-{svc}"})
        if i % 50 == 0:
            print(f"    {i}/{len(routes)} · 정류장 {len(stops):,}")

    if not s_rows:
        die("변환할 정차 정보가 없습니다. --probe 로 응답 형태를 확인하세요.")

    cal = [{"service_id": "weekday", "monday": 1, "tuesday": 1, "wednesday": 1,
            "thursday": 1, "friday": 1, "saturday": 0, "sunday": 0,
            "start_date": "20200101", "end_date": "20991231"},
           {"service_id": "sat", **{d: 0 for d in ("monday", "tuesday", "wednesday",
                                                   "thursday", "friday", "sunday")},
            "saturday": 1, "start_date": "20200101", "end_date": "20991231"},
           {"service_id": "sun", **{d: 0 for d in ("monday", "tuesday", "wednesday",
                                                   "thursday", "friday", "saturday")},
            "sunday": 1, "start_date": "20200101", "end_date": "20991231"}]

    files = {
        "agency.txt": [{"agency_id": f"tago-{code}", "agency_name": name,
                        "agency_url": "https://www.tago.go.kr/",
                        "agency_timezone": "Asia/Seoul", "agency_lang": "ko"}],
        "stops.txt": list(stops.values()),
        "routes.txt": r_rows,
        "trips.txt": t_rows,
        "stop_times.txt": s_rows,
        "frequencies.txt": f_rows,
        "calendar.txt": cal,
        "feed_info.txt": [{"feed_publisher_name": f"{name} (TAGO 변환)",
                           "feed_publisher_url": "https://www.data.go.kr/",
                           "feed_lang": "ko",
                           "feed_contact_email": "",
                           # 이 피드를 읽는 사람이 알아야 할 사실
                           "feed_version": f"tago-{code}-추정소요시간-{SPEED*3.6:.0f}kmh"}],
    }

    OUT.mkdir(exist_ok=True)
    path = OUT / f"tago-{code}_{name}.zip"
    tmp = path.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for fname, rows in files.items():
            if not rows:
                continue
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
            z.writestr(fname, buf.getvalue())
    tmp.replace(path)
    print(f"\n완료: {path.name}")
    print(f"  정류장 {len(stops):,} · 노선 {len(r_rows):,} · 정차 {len(s_rows):,} "
          f"· 배차 {len(f_rows):,}")
    print("  소요시간은 좌표 거리로 추정한 값입니다(TAGO가 주지 않음).")
    print("이제 인덱스를 다시 만드세요: python3 build_index.py")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="응답 형태만 확인")
    ap.add_argument("--cities", action="store_true", help="도시 코드 목록")
    ap.add_argument("--city", help="이 도시 코드를 GTFS로 변환")
    args = ap.parse_args()

    if not KEY:
        die(".env 에 DATA_GO_KR_KEY 가 없습니다. data.go.kr 에서 발급하세요.")
    if args.probe:
        return probe()
    if args.cities:
        rows, _ = call(ROUTE_API, "getCtyCodeList")
        for r in rows:
            print(f"  {r.get('citycode'):>6}  {r.get('cityname')}")
        return
    if not args.city:
        die("--probe / --cities / --city 중 하나가 필요합니다.")

    rows, _ = call(ROUTE_API, "getCtyCodeList")
    name = next((r.get("cityname") for r in rows
                 if str(r.get("citycode")) == str(args.city)), args.city)
    print(f"{name} (코드 {args.city}) 변환")
    build_city(args.city, name)


if __name__ == "__main__":
    main()

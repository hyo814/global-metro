#!/usr/bin/env python3
"""GTFS 피드 하나를 열어 정류장 검색과 다음 출발 시각을 계산한다.

핵심은 웹 프레임워크가 아니라 여기 있는 시간 계산이다. 실제로 받아본 피드를
세어보고 확인한 지뢰 4개를 처리한다:

  1. departure_time은 24시를 넘는다. "25:30:00"은 다음날 01:30이다.
     자정 기준 초 단위 정수로만 다룬다. HH:MM:SS로 파싱하면 깨진다.
  2. 오늘 운행하는지는 stop_times만 봐선 모른다. calendar.txt(요일 반복) +
     calendar_dates.txt(예외일)를 합쳐야 나온다. 받아둔 피드의 37%는
     calendar.txt가 아예 없고 calendar_dates.txt만 있다.
  3. 시각은 agency_timezone 기준이다. 서버 로컬 시간이 아니다.
  4. 피드의 16%는 frequencies.txt를 쓴다. 그 trip의 stop_times는 패턴일 뿐이고
     실제 출발은 headway 간격으로 생성해야 한다.

그리고 자정 직후에는 "어제 service_id의 25:xx 출발"이 지금 출발이다.
그래서 오늘과 어제 두 날을 모두 조회한다.

자체 점검:  python3 gtfs.py
"""
import pathlib
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import duckdb

ROOT = pathlib.Path(__file__).resolve().parent.parent

CACHE = ROOT / ".cache" / "gtfs"
TABLES = ("agency", "stops", "stop_times", "trips", "routes",
          "calendar", "calendar_dates", "frequencies")
DOW = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# 파일이 없을 때도 SQL이 동일하게 돌도록 빈 테이블을 같은 스키마로 만든다
EMPTY = {
    "calendar": ("service_id VARCHAR, start_date VARCHAR, end_date VARCHAR, "
                 + ", ".join(f"{d} VARCHAR" for d in DOW)),
    "calendar_dates": "service_id VARCHAR, date VARCHAR, exception_type VARCHAR",
    "frequencies": ("trip_id VARCHAR, start_time VARCHAR, end_time VARCHAR, "
                    "headway_secs VARCHAR"),
}


def secs(t):
    """'25:30:00' -> 91800. 빈 값이면 None."""
    if not t or not t.strip():
        return None
    h, m, s = (int(x) for x in t.strip().split(":"))
    return h * 3600 + m * 60 + s


def hhmm(sec):
    """자정 기준 초를 시계 표시로. 86400을 넘으면 다음날로 접는다."""
    sec %= 86400
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}"


def _hex(v):
    """GTFS는 #를 뺀 6자리 hex로 준다. 이상한 값이면 None."""
    v = (v or "").strip().lstrip("#")
    return f"#{v}" if len(v) == 6 and all(c in "0123456789abcdefABCDEF" for c in v) else None


def _sql_secs(col):
    """SQL 안에서 HH:MM:SS를 초로. split_part라 24시 초과도 그대로 통과한다."""
    return (f"CAST(split_part({col}, ':', 1) AS BIGINT) * 3600 + "
            f"CAST(split_part({col}, ':', 2) AS BIGINT) * 60 + "
            f"CAST(split_part({col}, ':', 3) AS BIGINT)")


class Feed:
    """GTFS zip 하나. 압축을 한 번 풀어두고 DuckDB 뷰로 읽는다."""

    def __init__(self, zip_path):
        self.path = pathlib.Path(zip_path)
        self.dir = CACHE / self.path.stem
        self._extract()
        self.con = duckdb.connect()
        self._views()
        self.stop_cols = {r[0] for r in self.con.execute("DESCRIBE stops").fetchall()}
        self.route_cols = {r[0] for r in self.con.execute("DESCRIBE routes").fetchall()}
        row = self.con.execute("SELECT agency_timezone FROM agency "
                               "WHERE agency_timezone IS NOT NULL LIMIT 1").fetchone()
        self.tz = ZoneInfo(row[0]) if row else ZoneInfo("UTC")

    def _extract(self):
        if self.dir.exists():
            return
        tmp = self.dir.with_name(self.dir.name + ".part")
        with zipfile.ZipFile(self.path) as z:
            have = set(z.namelist())
            z.extractall(tmp, [f"{t}.txt" for t in TABLES if f"{t}.txt" in have])
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        tmp.rename(self.dir)

    def _views(self):
        for t in TABLES:
            f = self.dir / f"{t}.txt"
            if f.exists() and f.stat().st_size > 0:
                # all_varchar: stop_id "007"이 숫자 7로 추론되면 조인이 조용히 깨진다
                self.con.execute(
                    f"CREATE VIEW {t} AS SELECT * FROM read_csv('{f}', header=true, "
                    f"all_varchar=true, ignore_errors=true)")
            elif t in EMPTY:
                self.con.execute(f"CREATE TABLE {t} ({EMPTY[t]})")
            else:
                raise FileNotFoundError(f"{self.path.name}에 {t}.txt가 없습니다")

    def search_stops(self, q, limit=20):
        lt = "location_type" if "location_type" in self.stop_cols else "NULL"
        rows = self.con.execute(f"""
            SELECT stop_id, stop_name, stop_lat, stop_lon, {lt} FROM stops
            WHERE stop_name IS NOT NULL AND lower(stop_name) LIKE lower(?)
            ORDER BY length(stop_name), stop_name LIMIT ?
        """, [f"%{q}%", limit]).fetchall()
        return [{"stop_id": r[0], "stop_name": r[1], "lat": r[2], "lon": r[3],
                 "is_station": r[4] == "1"} for r in rows]

    def stop_group(self, stop_id):
        """역을 고르면 출발은 자식 승강장에 걸려 있다. 지뢰 5.

        location_type=1인 역은 stop_times가 0행이고, 실제 정차는
        parent_station으로 그 역을 가리키는 승강장들에 들어 있다.
        """
        if "parent_station" not in self.stop_cols:
            return [stop_id]
        kids = self.con.execute(
            "SELECT stop_id FROM stops WHERE parent_station = ?", [stop_id]).fetchall()
        return [stop_id] + [r[0] for r in kids]

    def services_on(self, day):
        """그 날 운행하는 service_id. calendar가 없으면 calendar_dates만으로 나온다."""
        ymd = day.strftime("%Y%m%d")
        rows = self.con.execute(f"""
            WITH base AS (
                SELECT service_id FROM calendar
                WHERE {DOW[day.weekday()]} = '1'
                  AND start_date <= ? AND end_date >= ?
            ), removed AS (
                SELECT service_id FROM calendar_dates
                WHERE date = ? AND exception_type = '2' AND service_id IS NOT NULL
            ), added AS (
                SELECT service_id FROM calendar_dates
                WHERE date = ? AND exception_type = '1'
            )
            SELECT service_id FROM base
            WHERE service_id NOT IN (SELECT service_id FROM removed)
            UNION SELECT service_id FROM added
        """, [ymd, ymd, ymd, ymd]).fetchall()
        return [r[0] for r in rows]

    def departures(self, stop_id, now=None, limit=20, lookahead=7):
        """지금 이후 출발 목록.

        어제 service의 자정 넘긴 편성(25:xx = 오늘 새벽)부터 본다. 오늘 막차가
        끊겼으면 내일, 모레로 넘어가며 첫차를 찾는다. 주 1~2회만 다니는 노선이
        있어서 기본 7일까지 본다.
        """
        now = now or datetime.now(self.tz)
        now_secs = now.hour * 3600 + now.minute * 60 + now.second
        # 인덱스가 같은 이름의 인접 정류장을 묶어서 넘겨준다("a,b,c").
        # 양방향 정류장은 한 안내판에 모아 보여주는 게 맞다.
        ids = stop_id if isinstance(stop_id, list) else str(stop_id).split(",")
        group = list(dict.fromkeys(
            g for one in ids if one.strip() for g in self.stop_group(one.strip())))

        out = []
        # offset: -1=어제(자정 넘긴 편성), 0=오늘, 1=내일 …
        # ponytail: 하루당 쿼리 1회. 보통 2회에서 끝나고, 막차가 끊긴 정류장일 때만
        # 첫차를 찾을 때까지 더 돈다. 느려지면 그때 날짜별 캐싱을 볼 것.
        for offset in range(-1, lookahead + 1):
            day = now.date() + timedelta(days=offset)
            services = self.services_on(day)
            if not services:
                continue
            shift = -offset * 86400        # 그 날 자정을 오늘 자정 기준으로 옮긴다
            for secs_val, short, long_, head, color, text, trip in self._at_stop(
                    group, services, now_secs + shift, limit):
                out.append({
                    "trip_id": trip,
                    "secs": secs_val - shift,
                    "time": hhmm(secs_val),
                    "in_min": (secs_val - shift - now_secs) // 60,
                    "route": (short or long_ or "").strip(),
                    "headsign": (head or "").strip(),
                    "day_offset": offset,
                    # 운영사가 지정한 노선 색. 86%의 피드가 가지고 있다.
                    "color": _hex(color),
                    "text_color": _hex(text) or "#ffffff",
                })
            if len(out) >= limit and offset >= 0:
                break
        out.sort(key=lambda d: d["secs"])
        out = out[:limit]
        self._fill_headsigns(out)
        return out

    def _fill_headsigns(self, runs):
        """행선지가 비면 그 편성의 종점 이름으로 채운다.

        받아둔 피드의 13%는 trip_headsign이 통째로 비어 있다(공백만 든 경우 포함).
        행선지가 없으면 시각만 있는 표가 되어 쓸 수 없다. 종점은 stop_times의
        stop_sequence 최댓값이라 언제나 구할 수 있다.
        """
        need = sorted({r["trip_id"] for r in runs
                       if not r["headsign"] and r.get("trip_id")})
        if not need:
            return
        rows = self.con.execute("""
            SELECT trip_id, stop_name FROM (
                SELECT st.trip_id, s.stop_name,
                       row_number() OVER (PARTITION BY st.trip_id
                           ORDER BY CAST(st.stop_sequence AS INTEGER) DESC) AS rn
                FROM stop_times st
                JOIN stops s ON trim(s.stop_id) = trim(st.stop_id)
                WHERE list_contains(?::VARCHAR[], trim(st.trip_id))
            ) WHERE rn = 1
        """, [need]).fetchall()
        last = {t: n for t, n in rows}
        for r in runs:
            if not r["headsign"]:
                r["headsign"] = (last.get(r["trip_id"]) or "").strip()

    def _at_stop(self, stop_ids, services, threshold, limit):
        # ponytail: stop_times 전체를 스캔한다. 한 피드(평균 15만행)라 수십 ms.
        # 큰 피드가 느려지면 그 피드만 stop_id로 정렬한 Parquet로 캐싱할 것.
        rc = "r.route_color" if "route_color" in self.route_cols else "NULL"
        rt = "r.route_text_color" if "route_text_color" in self.route_cols else "NULL"
        return self.con.execute(f"""
            WITH st AS (
                SELECT trip_id, stop_id, {_sql_secs('departure_time')} AS secs
                FROM stop_times
                WHERE departure_time IS NOT NULL AND trim(departure_time) <> ''
            ), trip AS (
                SELECT trip_id, trip_headsign, route_id FROM trips
                WHERE list_contains(?::VARCHAR[], service_id)
            ), fr AS (
                SELECT trip_id, {_sql_secs('start_time')} AS s0,
                       {_sql_secs('end_time')} AS s1, CAST(headway_secs AS BIGINT) AS hw
                FROM frequencies
                WHERE headway_secs IS NOT NULL AND CAST(headway_secs AS BIGINT) > 0
            ), sched AS (
                SELECT st.secs, trip.trip_headsign, trip.route_id, st.trip_id
                FROM st JOIN trip USING (trip_id)
                WHERE list_contains(?::VARCHAR[], st.stop_id)
                  AND st.trip_id NOT IN (SELECT trip_id FROM fr)
            ), gen AS (
                -- frequencies 기반: stop_times는 패턴이므로 headway 간격으로 펼친다
                SELECT fr.s0 + g.i * fr.hw + (st.secs - t0.s0) AS secs,
                       trip.trip_headsign, trip.route_id, fr.trip_id
                FROM fr
                JOIN st ON st.trip_id = fr.trip_id
                       AND list_contains(?::VARCHAR[], st.stop_id)
                JOIN (SELECT trip_id, min(secs) AS s0 FROM st GROUP BY trip_id) t0
                     ON t0.trip_id = fr.trip_id
                JOIN trip ON trip.trip_id = fr.trip_id
                CROSS JOIN generate_series(
                     0, CAST((fr.s1 - fr.s0) / fr.hw AS BIGINT)) AS g(i)
            )
            SELECT d.secs, r.route_short_name, r.route_long_name, d.trip_headsign,
                   {rc}, {rt}, d.trip_id
            FROM (SELECT * FROM sched UNION ALL SELECT * FROM gen) d
            -- trim: 고정폭 공백을 붙여 내보내는 피드가 있다. Renfe Cercanías는
            -- 패딩 때문에 이 조인이 137,715건 중 95건만 맞았다.
            LEFT JOIN routes r ON trim(r.route_id) = trim(d.route_id)
            WHERE d.secs >= ?
            ORDER BY d.secs LIMIT ?
        """, [services, stop_ids, stop_ids, threshold, limit]).fetchall()


def demo():
    """실제 피드로 지뢰 4개를 검증한다."""
    assert secs("25:30:00") == 91800, "24시 초과 파싱 실패"
    assert secs("00:00:00") == 0 and secs("") is None
    assert hhmm(91800) == "01:30", "자정 넘김 표시 실패"

    f = Feed("gtfs/mdb-2254_Bay_Area_Transportation_Authority.zip")
    assert str(f.tz) == "America/New_York", f"타임존: {f.tz}"          # 지뢰 3
    today = datetime.now(f.tz).date()
    assert f.services_on(today), "오늘 운행 service가 없음"             # 지뢰 2

    hits = f.search_stops("a", limit=5)
    assert hits and hits[0]["stop_name"], "정류장 검색 실패"
    deps = f.departures(hits[0]["stop_id"], limit=5)
    assert deps == sorted(deps, key=lambda d: d["secs"]), "정렬 안 됨"

    # calendar.txt가 없는 피드도 운행일이 나와야 한다 (전체의 37%)
    g = Feed("gtfs/tld-4777_Athens_Clarke_County_Transit.zip")
    assert not (g.dir / "calendar.txt").exists(), "이 피드는 calendar.txt가 없어야 함"
    assert g.services_on(today), "calendar_dates만으로 운행일을 못 구함"  # 지뢰 2

    # 24시 초과 시각을 가진 피드 (지뢰 1)
    a = Feed("gtfs/mdb-1029_Auckland_Transport.zip")
    over = a.con.execute(f"""
        SELECT stop_id, {_sql_secs('departure_time')} AS s FROM stop_times
        WHERE {_sql_secs('departure_time')} >= 86400 LIMIT 1""").fetchone()
    assert over and over[1] >= 86400, "24시 초과 행을 못 찾음"
    assert hhmm(over[1]) < "12:00", "24시 초과가 새벽으로 안 접힘"

    # 역(location_type=1)을 고르면 자식 승강장의 출발이 나와야 한다 (지뢰 5)
    station = a.con.execute("""SELECT stop_id FROM stops WHERE location_type = '1'
        AND stop_id IN (SELECT parent_station FROM stops WHERE parent_station IS NOT NULL)
        LIMIT 1""").fetchone()[0]
    assert a.con.execute("SELECT count(*) FROM stop_times WHERE stop_id = ?",
                         [station]).fetchone()[0] == 0, "역에 직접 걸린 정차가 있음"
    assert len(a.stop_group(station)) > 1, "자식 승강장을 못 찾음"
    day_a = next(d for d in (datetime.now(a.tz).date() + timedelta(days=i)
                             for i in range(14)) if a.services_on(d))
    noon = datetime(day_a.year, day_a.month, day_a.day, 12, 0, tzinfo=a.tz)
    assert a.departures(station, now=noon, limit=5), "역 조회가 비어 있음"

    # frequencies 기반 피드: stop_times는 패턴이고 headway로 펼쳐져야 한다 (지뢰 4)
    b = Feed("gtfs/mdb-1985_Aeroexpreso.zip")
    day = next(d for d in (datetime.now(b.tz).date() + timedelta(days=i)
                           for i in range(400)) if b.services_on(d))
    probe = datetime(day.year, day.month, day.day, 0, 0, tzinfo=b.tz)
    stop = "03701CUZ"
    deps = b.departures(stop, now=probe, limit=200)
    raw = b.con.execute("SELECT count(*) FROM stop_times WHERE stop_id = ?",
                        [stop]).fetchone()[0]
    assert len(deps) > raw, f"패턴이 안 펼쳐짐: 출발 {len(deps)} <= stop_times {raw}"
    gaps = {deps[i + 1]["secs"] - deps[i]["secs"] for i in range(len(deps) - 1)}
    assert 1800 in gaps, f"headway 1800초가 안 보임: {sorted(gaps)}"
    print(f"  frequencies: stop_times {raw}행 -> 출발 {len(deps)}개, 간격 {sorted(gaps)}")

    # 막차가 끊긴 시각에 조회하면 다음 운행일 첫차가 나와야 한다
    late = datetime(day_a.year, day_a.month, day_a.day, 23, 58, tzinfo=a.tz)
    nxt = a.departures(station, now=late, limit=3)
    assert nxt, "막차 이후에 아무것도 안 나옴"
    assert any(x["day_offset"] > 0 for x in nxt), f"다음날로 안 넘어감: {nxt[:1]}"
    assert nxt[0]["in_min"] > 0 and nxt == sorted(nxt, key=lambda d: d["secs"])
    print(f"  막차 이후 23:58 조회 -> {nxt[0]['time']} "
          f"(+{nxt[0]['day_offset']}일, {nxt[0]['in_min']}분 후)")

    print("자체 점검 통과")


if __name__ == "__main__":
    demo()

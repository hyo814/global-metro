#!/usr/bin/env python3
"""전 세계 피드의 stops.txt를 검색 인덱스 하나로 합친다.

무거운 stop_times(431M행, 28GB)는 넣지 않는다. 정류장 하나는 피드 하나에만
속하므로, 검색으로 정류장을 찾은 뒤 그 피드의 zip만 열어서 시간표를 뽑으면 된다.
그래서 여기 들어가는 건 stops뿐이고 5.7M행 수준이다.

    python3 build_index.py          # gtfs/*.zip 전부
    python3 build_index.py --limit 50

검색은 unicode61 토크나이저 + 접두 질의를 쓴다. trigram은 두 글자 CJK("新宿")를
못 잡아서 탈락시켰다.
"""
import argparse
import collections
import csv
import io
import json
import math
import pathlib
import sqlite3
import sys
import time
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent

OUT = ROOT / ".cache" / "stops.db"
META = ROOT / ".cache" / "feeds.json"
GTFS = ROOT / "gtfs"


def feed_countries():
    """feed_id -> 국가코드. API에서 한 번 받아 캐싱한다. 실패해도 진행한다."""
    if META.exists():
        return json.loads(META.read_text())
    try:
        from fetch_gtfs import get_access_token, fetch_feeds
        token = get_access_token()
        out = {}
        for f in fetch_feeds(token):
            locs = f.get("locations") or [{}]
            out[f.get("id", "")] = locs[0].get("country_code") or ""
        META.parent.mkdir(parents=True, exist_ok=True)
        META.write_text(json.dumps(out))
        return out
    except Exception as e:
        print(f"  국가 정보 생략 ({type(e).__name__}: {e})", file=sys.stderr)
        return {}


def _num(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _dist(a, b):
    """두 (lat, lon) 사이 대략 거리(m). 좌표가 없으면 None."""
    try:
        la1, lo1 = float(a[0]), float(a[1])
        la2, lo2 = float(b[0]), float(b[1])
    except (TypeError, ValueError):
        return None
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def cluster(stops, radius=500):
    """이름이 같은 정류장들을 거리로 묶는다.

    인덱스 행의 48%가 (피드, 이름) 중복이다. 대부분은 같은 교차로의 양방향
    정류장이거나 한 역의 여러 승강장이라 검색 결과에서는 한 곳으로 보여야 한다.
    다만 5%는 이름만 같고 수 km 떨어진 다른 장소라 그건 나눈다.
    """
    out = []
    for s in stops:
        for c in out:
            d = _dist((s[1], s[2]), (c[0][1], c[0][2]))
            if d is not None and d <= radius:
                c.append(s)
                break
        else:
            out.append([s])
    return out


def rows(z, name):
    if name not in z.namelist():
        return
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, "utf-8-sig", errors="replace"))


def build(zips, country):
    tmp = OUT.with_suffix(".part")
    tmp.unlink(missing_ok=True)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE feeds (feed_id TEXT PRIMARY KEY, zip TEXT, agency TEXT,
                            timezone TEXT, country TEXT, n_stops INTEGER,
                            la1 REAL, la2 REAL, lo1 REAL, lo2 REAL);
        -- 한 테이블에 다 넣는다. 조인도 동기화도 없다.
        CREATE VIRTUAL TABLE stops USING fts5(
            name, feed_id UNINDEXED, stop_id UNINDEXED,
            lat UNINDEXED, lon UNINDEXED, is_station UNINDEXED,
            trips UNINDEXED,
            tokenize='unicode61');
    """)

    n_stop = n_feed = skipped = 0
    t0 = time.time()
    for i, p in enumerate(zips, 1):
        feed_id = p.stem.split("_")[0]
        try:
            z = zipfile.ZipFile(p)
        except zipfile.BadZipFile:
            skipped += 1
            continue

        ag = next(rows(z, "agency.txt"), {}) or {}

        # 정류장별 정차 횟수. 이게 "얼마나 중요한 정류장인가"의 진짜 신호다.
        # 망 규모는 거꾸로 나올 때가 있다 — 大田原市(268개 망)의 新宿은 하루 10대,
        # 東京都交通局(141개 망)의 新宿은 1,534대다.
        calls = collections.Counter()
        if "stop_times.txt" in z.namelist():
            with z.open("stop_times.txt") as f:
                rd = csv.reader(io.TextIOWrapper(f, "utf-8-sig", errors="replace"))
                head = next(rd, [])
                if "stop_id" in head:
                    i = head.index("stop_id")
                    for row in rd:
                        if len(row) > i:
                            calls[row[i]] += 1

        by_name = {}
        for r in rows(z, "stops.txt"):
            nm = (r.get("stop_name") or "").strip()
            if nm:
                by_name.setdefault(nm, []).append(
                    (r.get("stop_id") or "", r.get("stop_lat") or "",
                     r.get("stop_lon") or "",
                     "1" if r.get("location_type") == "1" else "0"))

        batch = []
        for nm, group in by_name.items():
            for c in cluster(group):
                # 묶인 정류장의 stop_id를 모두 들고 간다. 시간표는 전부 합쳐 보여준다.
                batch.append((nm, feed_id, ",".join(x[0] for x in c),
                              c[0][1], c[0][2],
                              "1" if any(x[3] == "1" for x in c) else "0",
                              sum(calls[x[0]] for x in c)))
        if not batch:
            skipped += 1
            continue

        # 망 규모는 "얼마나 중요한 정류장인가"의 싼 대용치다. 정류장 5천 개짜리
        # 도쿄 교통국의 '新宿'과 마을버스의 '新宿'을 같은 순위로 두면 안 된다.
        las = [float(x[3]) for x in batch if _num(x[3])]
        los = [float(x[4]) for x in batch if _num(x[4])]
        con.execute("INSERT OR REPLACE INTO feeds VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (feed_id, str(p), ag.get("agency_name") or "",
                     ag.get("agency_timezone") or "", country.get(feed_id, ""),
                     len(batch),
                     min(las) if las else None, max(las) if las else None,
                     min(los) if los else None, max(los) if los else None))
        con.executemany("INSERT INTO stops VALUES (?,?,?,?,?,?,?)", batch)
        n_stop += len(batch)
        n_feed += 1

        if i % 200 == 0:
            con.commit()
            print(f"  {i}/{len(zips)} 피드 · 정류장 {n_stop:,}개 · {time.time()-t0:.0f}초")

    con.commit()
    print("  최적화 중…")
    con.execute("INSERT INTO stops(stops) VALUES('optimize')")
    con.commit()
    con.close()
    tmp.replace(OUT)
    return n_feed, n_stop, skipped, time.time() - t0


def _abs(zip_path):
    """예전 인덱스는 상대경로를 담고 있다. 실행 위치와 무관하게 열리도록 푼다."""
    q = pathlib.Path(zip_path)
    return str(q if q.is_absolute() else ROOT / q)


def _con():
    if not OUT.exists():
        raise FileNotFoundError("인덱스가 없습니다. python3 build_index.py 를 먼저 실행하세요.")
    return sqlite3.connect(f"file:{OUT}?mode=ro", uri=True)


def countries():
    """인덱스에 있는 국가와 피드 수."""
    return [{"code": r[0], "feeds": r[1]} for r in _con().execute(
        "SELECT country, count(*) FROM feeds WHERE country <> '' "
        "GROUP BY country ORDER BY 2 DESC")]


def search(q, country="", limit=20):
    """접두 질의. 특수문자가 FTS5 구문을 깨지 않게 통째로 인용한다."""
    if not q.strip():
        return []
    fts = '"' + q.strip().replace('"', '""') + '"*'
    rows = _con().execute("""
        SELECT s.name, s.feed_id, s.stop_id, s.lat, s.lon, s.is_station,
               f.agency, f.country, s.trips
        FROM stops s LEFT JOIN feeds f ON f.feed_id = s.feed_id
        WHERE s.stops MATCH ? AND (? = '' OR f.country = ?)
        ORDER BY (s.name = ?) DESC,               -- 정확히 일치하는 이름이 먼저
                 CAST(s.trips AS INTEGER) DESC,   -- 차가 많이 서는 곳이 먼저
                 rank, length(s.name)
        LIMIT ?
    """, (fts, country, country, q.strip(), limit)).fetchall()
    return [{"stop_name": r[0], "feed_id": r[1], "stop_id": r[2],
             "lat": r[3], "lon": r[4], "is_station": r[5] == "1",
             "agency": r[6] or "", "country": r[7] or "",
             "trips": int(r[8] or 0)} for r in rows]


def stop_by_id(feed_id, stop_id):
    """search()와 같은 모양의 dict 하나. URL 복원용."""
    r = _con().execute("""
        SELECT s.name, s.feed_id, s.stop_id, s.lat, s.lon, s.is_station,
               f.agency, f.country, s.trips
        FROM stops s LEFT JOIN feeds f ON f.feed_id = s.feed_id
        WHERE s.feed_id = ? AND s.stop_id = ? LIMIT 1
    """, (feed_id, stop_id)).fetchone()
    if r is None:
        return None
    return {"stop_name": r[0], "feed_id": r[1], "stop_id": r[2],
            "lat": r[3], "lon": r[4], "is_station": r[5] == "1",
            "agency": r[6] or "", "country": r[7] or "", "trips": int(r[8] or 0)}


CELL = 0.1          # 도. 약 11km 격자


def ensure_geo():
    """피드별 좌표 범위와 격자 색인을 채운다. 없으면 한 번만 만든다.

    좌표 범위만으로는 부족하다. 프랑스 국가 피드는 39.1~60.2N이라 사각형이
    헬싱키까지 덮는다. 실제로 그 지역에 정류장이 있는지를 보려면 격자가 필요하다.
    """
    con = sqlite3.connect(OUT)
    cols = {r[1] for r in con.execute("PRAGMA table_info(feeds)")}
    for c in ("la1", "la2", "lo1", "lo2"):
        if c not in cols:
            con.execute(f"ALTER TABLE feeds ADD COLUMN {c} REAL")
    con.execute("""CREATE TABLE IF NOT EXISTS cells
                   (cx INTEGER, cy INTEGER, feed_id TEXT, n INTEGER,
                    PRIMARY KEY (cx, cy, feed_id))""")
    con.execute("CREATE INDEX IF NOT EXISTS cells_xy ON cells (cx, cy)")

    done = con.execute("SELECT count(*) FROM cells").fetchone()[0]
    if done and not con.execute(
            "SELECT count(*) FROM feeds WHERE la1 IS NULL").fetchone()[0]:
        con.close()
        return

    # 같은 연결에서 SELECT를 돌면서 쓰면 잠긴다. 읽기는 따로 연다.
    rd = sqlite3.connect(f"file:{OUT}?mode=ro", uri=True)
    box, cells = {}, {}
    for feed_id, la, lo in rd.execute("SELECT feed_id, lat, lon FROM stops"):
        try:
            la, lo = float(la), float(lo)
        except (TypeError, ValueError):
            continue
        b = box.get(feed_id)
        if b is None:
            box[feed_id] = [la, la, lo, lo]
        else:
            b[0] = min(b[0], la); b[1] = max(b[1], la)
            b[2] = min(b[2], lo); b[3] = max(b[3], lo)
        key = (int(la // CELL), int(lo // CELL), feed_id)
        cells[key] = cells.get(key, 0) + 1
    rd.close()

    con.executemany("UPDATE feeds SET la1=?, la2=?, lo1=?, lo2=? WHERE feed_id=?",
                    [(*v, k) for k, v in box.items()])
    con.execute("DELETE FROM cells")
    con.executemany("INSERT INTO cells VALUES (?,?,?,?)",
                    [(cx, cy, fid, n) for (cx, cy, fid), n in cells.items()])
    con.commit()
    con.close()


def feeds_covering(la1, la2, lo1, lo2, must=(), limit=12, max_stops=120_000):
    """이 구역에 실제로 정류장이 있는 피드들.

    must에 준 피드는 무조건 넣는다. 출발지와 도착지가 속한 피드는 예산과
    무관하게 필요하기 때문이다. 나머지는 그 구역의 정류장이 많은 순으로
    예산까지 채우되, 큰 피드 하나 때문에 멈추지 않고 건너뛴다.
    """
    con = _con()
    rows = con.execute("""
        SELECT c.feed_id, sum(c.n) AS here, f.zip, f.agency, coalesce(f.n_stops, 0)
        FROM cells c JOIN feeds f ON f.feed_id = c.feed_id
        WHERE c.cx BETWEEN ? AND ? AND c.cy BETWEEN ? AND ?
        GROUP BY c.feed_id ORDER BY here DESC
    """, (int(la1 // CELL), int(la2 // CELL),
          int(lo1 // CELL), int(lo2 // CELL))).fetchall()

    picked, total, seen = [], 0, set()
    for want in (True, False):
        for feed_id, here, zp, agency, n in rows:
            if feed_id in seen or (feed_id in must) != want:
                continue
            if not want and (len(picked) >= limit or total + n > max_stops):
                continue          # 큰 것 하나 때문에 뒤의 작은 것까지 버리지 않는다
            seen.add(feed_id)
            picked.append({"feed_id": feed_id, "zip": _abs(zp), "agency": agency,
                           "n_stops": n, "here": here})
            total += n
    return picked


def feed_info(feed_id):
    """feed_id -> zip 경로와 운영사 정보. 없으면 None."""
    r = _con().execute(
        "SELECT zip, agency, timezone, country FROM feeds WHERE feed_id = ?",
        (feed_id,)).fetchone()
    return None if r is None else {"zip": _abs(r[0]), "agency": r[1],
                                   "timezone": r[2], "country": r[3]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="피드 수 제한 (시험용)")
    args = ap.parse_args()

    zips = sorted(GTFS.glob("*.zip"))[: args.limit]
    if not zips:
        sys.exit("gtfs/ 에 zip이 없습니다.")
    print(f"피드 {len(zips)}개로 인덱스를 만듭니다.")

    n_feed, n_stop, skipped, secs = build(zips, feed_countries())
    mb = OUT.stat().st_size / 1e6
    print(f"\n완료: 피드 {n_feed:,} · 정류장 {n_stop:,} · 건너뜀 {skipped} "
          f"· {secs:.0f}초 · {mb:,.0f} MB")
    print(f"저장: {OUT.resolve()}")


if __name__ == "__main__":
    main()

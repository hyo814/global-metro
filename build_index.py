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
import csv
import io
import json
import pathlib
import sqlite3
import sys
import time
import zipfile

OUT = pathlib.Path(".cache/stops.db")
META = pathlib.Path(".cache/feeds.json")
GTFS = pathlib.Path("gtfs")


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
                            timezone TEXT, country TEXT);
        -- 한 테이블에 다 넣는다. 조인도 동기화도 없다.
        CREATE VIRTUAL TABLE stops USING fts5(
            name, feed_id UNINDEXED, stop_id UNINDEXED,
            lat UNINDEXED, lon UNINDEXED, is_station UNINDEXED,
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
        batch = [(r.get("stop_name") or "", feed_id, r.get("stop_id") or "",
                  r.get("stop_lat") or "", r.get("stop_lon") or "",
                  "1" if r.get("location_type") == "1" else "0")
                 for r in rows(z, "stops.txt") if (r.get("stop_name") or "").strip()]
        if not batch:
            skipped += 1
            continue

        con.execute("INSERT OR REPLACE INTO feeds VALUES (?,?,?,?,?)",
                    (feed_id, str(p), ag.get("agency_name") or "",
                     ag.get("agency_timezone") or "", country.get(feed_id, "")))
        con.executemany("INSERT INTO stops VALUES (?,?,?,?,?,?)", batch)
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
               f.agency, f.country
        FROM stops s LEFT JOIN feeds f ON f.feed_id = s.feed_id
        WHERE s.stops MATCH ? AND (? = '' OR f.country = ?)
        ORDER BY rank, length(s.name) LIMIT ?
    """, (fts, country, country, limit)).fetchall()
    return [{"stop_name": r[0], "feed_id": r[1], "stop_id": r[2],
             "lat": r[3], "lon": r[4], "is_station": r[5] == "1",
             "agency": r[6] or "", "country": r[7] or ""} for r in rows]


def feed_info(feed_id):
    """feed_id -> zip 경로와 운영사 정보. 없으면 None."""
    r = _con().execute(
        "SELECT zip, agency, timezone, country FROM feeds WHERE feed_id = ?",
        (feed_id,)).fetchone()
    return None if r is None else {"zip": r[0], "agency": r[1],
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

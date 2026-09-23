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
import unicodedata
import time
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent

OUT = ROOT / ".cache" / "stops.db"
META = ROOT / ".cache" / "feeds.json"
GTFS = ROOT / "gtfs"


def feed_countries():
    """feed_id -> 확정 국가코드. 후보가 여럿이면 ""(나중에 좌표로 정한다).

    국경을 걸치는 피드는 locations에 이웃 나라가 섞여 들어온다. 355,497개짜리
    "Public Transport Germany" 피드의 첫 location이 FR이라, 첫 번째만 쓰면
    독일 전국 피드가 프랑스가 된다. 실제로 그렇게 되어 있었다.
    """
    if META.exists():
        raw = json.loads(META.read_text())
        return {k: (v[0] if len(v) == 1 else "") for k, v in raw.items()}
    try:
        from fetch_gtfs import get_access_token, fetch_feeds
        token = get_access_token()
        raw = {}
        for f in fetch_feeds(token):
            cc = sorted({l.get("country_code") for l in (f.get("locations") or [])
                         if l.get("country_code")})
            raw[f.get("id", "")] = cc
        META.parent.mkdir(parents=True, exist_ok=True)
        META.write_text(json.dumps(raw, ensure_ascii=False))
        return {k: (v[0] if len(v) == 1 else "") for k, v in raw.items()}
    except Exception as e:
        print(f"  국가 정보 생략 ({type(e).__name__}: {e})", file=sys.stderr)
        return {}


def _num(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def haversine(a, b):
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
            d = haversine((s[1], s[2]), (c[0][1], c[0][2]))
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
        -- norm이 검색 대상, name은 보여주기용. 악센트와 대소문자를 접어 두면
        -- Σύνταγμα로 ΣΥΝΤΑΓΜΑ를 찾을 수 있다.
        CREATE VIRTUAL TABLE stops USING fts5(
            norm, name UNINDEXED, feed_id UNINDEXED, stop_id UNINDEXED,
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
                batch.append((fold(nm), nm, feed_id, ",".join(x[0] for x in c),
                              c[0][1], c[0][2],
                              "1" if any(x[3] == "1" for x in c) else "0",
                              sum(calls[x[0]] for x in c)))
        if not batch:
            skipped += 1
            continue

        # 망 규모는 "얼마나 중요한 정류장인가"의 싼 대용치다. 정류장 5천 개짜리
        # 도쿄 교통국의 '新宿'과 마을버스의 '新宿'을 같은 순위로 두면 안 된다.
        las = [float(x[4]) for x in batch if _num(x[4])]
        los = [float(x[5]) for x in batch if _num(x[5])]
        con.execute("INSERT OR REPLACE INTO feeds VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (feed_id, str(p), ag.get("agency_name") or "",
                     ag.get("agency_timezone") or "", country.get(feed_id, ""),
                     len(batch),
                     min(las) if las else None, max(las) if las else None,
                     min(los) if los else None, max(los) if los else None))
        con.executemany("INSERT INTO stops VALUES (?,?,?,?,?,?,?,?)", batch)
        n_stop += len(batch)
        n_feed += 1

        if i % 200 == 0:
            con.commit()
            print(f"  {i}/{len(zips)} 피드 · 정류장 {n_stop:,}개 · {time.time()-t0:.0f}초")

    con.commit()
    _vote_countries(con)
    print("  최적화 중…")
    con.execute("INSERT INTO stops(stops) VALUES('optimize')")
    con.commit()
    con.close()
    tmp.replace(OUT)
    return n_feed, n_stop, skipped, time.time() - t0


def _abs(zip_path):
    """인덱스는 만든 기계의 경로를 담고 있다(예전 것은 상대경로, 지금은 맥의
    절대경로). 그 경로에 없으면 이 기계의 gtfs/에서 같은 이름을 찾는다 —
    맥에서 만든 인덱스를 서버에 그대로 올려도 열리게."""
    q = pathlib.Path(zip_path)
    return str(q if q.is_absolute() and q.exists() else GTFS / q.name)


def _con():
    if not OUT.exists():
        raise FileNotFoundError("인덱스가 없습니다. python3 build_index.py 를 먼저 실행하세요.")
    return sqlite3.connect(f"file:{OUT}?mode=ro", uri=True)


def countries():
    """인덱스에 있는 국가와 피드 수."""
    return [{"code": r[0], "feeds": r[1]} for r in _con().execute(
        "SELECT country, count(*) FROM feeds WHERE country <> '' "
        "GROUP BY country ORDER BY 2 DESC")]


# IANA 시간대 이름은 지명을 그대로 담고 있어서, 한 나라만 가리키는 것이 많다.
# 그 나라에 "확실한" 피드가 하나도 없으면 좌표 투표가 성립하지 않는다. 태국은
# 확정 피드가 0개라 방콕 정류장이 말레이시아로 넘어갔다. 그때 쓰는 최후 보정.
TZ_COUNTRY = {
    "Asia/Bangkok": "TH", "Asia/Singapore": "SG", "Asia/Hong_Kong": "HK",
    "Asia/Seoul": "KR", "Asia/Tokyo": "JP", "Asia/Jakarta": "ID",
    "Asia/Kolkata": "IN", "Asia/Jerusalem": "IL", "Asia/Manila": "PH",
    "Asia/Taipei": "TW", "Asia/Kuala_Lumpur": "MY", "Asia/Ho_Chi_Minh": "VN",
    "Asia/Dubai": "AE", "Asia/Tbilisi": "GE", "Asia/Yerevan": "AM",
    "Europe/Athens": "GR", "Europe/Kyiv": "UA", "Europe/Kiev": "UA",
    "Europe/Lisbon": "PT", "Europe/Madrid": "ES", "Europe/Rome": "IT",
    "Europe/Warsaw": "PL", "Europe/Prague": "CZ", "Europe/Vienna": "AT",
    "Europe/Zurich": "CH", "Europe/Brussels": "BE", "Europe/Amsterdam": "NL",
    "Europe/Copenhagen": "DK", "Europe/Stockholm": "SE", "Europe/Oslo": "NO",
    "Europe/Helsinki": "FI", "Europe/Dublin": "IE", "Europe/Istanbul": "TR",
    "Pacific/Auckland": "NZ", "Africa/Nairobi": "KE", "Africa/Cairo": "EG",
}


def fix_by_timezone(con):
    """시간대가 한 나라만 가리키는데 표기가 다르면 바로잡는다."""
    fixed = []
    for fid, tz, cc in con.execute(
            "SELECT feed_id, timezone, country FROM feeds WHERE timezone <> ''"):
        want = TZ_COUNTRY.get(tz)
        if want and want != cc:
            fixed.append((want, fid))
    if fixed:
        con.executemany("UPDATE feeds SET country=? WHERE feed_id=?", fixed)
        con.commit()
    return len(fixed)


def _vote_countries(con):
    """국가가 정해지지 않은 피드를 좌표로 정한다.

    국가가 하나뿐인 피드(전체의 95%)로 "격자 -> 국가" 지도를 만들고, 남은
    피드는 자기 정류장이 어느 칸에 있는지로 투표한다. 외부 자료 없이
    가진 데이터만으로 국경을 근사한다.
    """
    grid, unknown = {}, []
    cc_of = dict(con.execute("SELECT feed_id, country FROM feeds").fetchall())
    for fid, cc in cc_of.items():
        if not cc:
            unknown.append(fid)
    if not unknown:
        return

    rd = sqlite3.connect(f"file:{OUT}?mode=ro", uri=True)
    mine = {}
    for fid, la, lo in rd.execute("SELECT feed_id, lat, lon FROM stops"):
        try:
            cell = (int(float(la) // CELL), int(float(lo) // CELL))
        except (TypeError, ValueError):
            continue
        cc = cc_of.get(fid) or ""
        if cc:
            g = grid.setdefault(cell, {})
            g[cc] = g.get(cc, 0) + 1
        else:
            m = mine.setdefault(fid, {})
            m[cell] = m.get(cell, 0) + 1
    rd.close()

    fixed = []
    for fid, cells in mine.items():
        votes = {}
        for cell, n in cells.items():
            for cc, w in grid.get(cell, {}).items():
                votes[cc] = votes.get(cc, 0) + n
        if votes:
            fixed.append((max(votes, key=votes.get), fid))
    if fixed:
        con.executemany("UPDATE feeds SET country=? WHERE feed_id=?", fixed)
        con.commit()
    print(f"  국가 미정 {len(unknown)}개 중 {len(fixed)}개를 좌표로 판정")
    n = fix_by_timezone(con)
    if n:
        print(f"  시간대와 어긋난 국가 표기 {n}개 바로잡음")


def search(q, country="", limit=20, near=None):
    """접두 질의. 특수문자가 FTS5 구문을 깨지 않게 통째로 인용한다.

    near=(위도, 경도)를 주면 그 지점에서 가까운 순으로 다시 세운다. 도착지를
    고를 때 출발지 근처가 먼저 와야 한다 — 같은 나라 안에도 같은 이름의
    정류장이 여럿이다.
    """
    if not q.strip():
        return []
    key = fold(q.strip())
    fts = '"' + key.replace('"', '""') + '"*'
    rows = _con().execute("""
        SELECT s.name, s.feed_id, s.stop_id, s.lat, s.lon, s.is_station,
               f.agency, f.country, s.trips
        FROM stops s LEFT JOIN feeds f ON f.feed_id = s.feed_id
        WHERE s.stops MATCH ? AND (? = '' OR f.country = ?)
        ORDER BY (s.norm = ?) DESC,               -- 정확히 일치하는 이름이 먼저
                 CAST(s.trips AS INTEGER) DESC,   -- 차가 많이 서는 곳이 먼저
                 rank, length(s.name)
        LIMIT ?
    """, (fts, country, country, key, limit * 4 if near else limit)).fetchall()
    out = [{"stop_name": r[0], "feed_id": r[1], "stop_id": r[2],
            "lat": r[3], "lon": r[4], "is_station": r[5] == "1",
            "agency": r[6] or "", "country": r[7] or "",
            "trips": int(r[8] or 0)} for r in rows]
    if near:
        # 거리만으로 세우면 중요도를 잃는다. 도쿄에서 渋谷를 찾을 때 출발지에
        # 조금 더 가깝다는 이유로 渋谷区役所前이 渋谷駅前을 이겨선 안 된다.
        # 2km 구간으로 묶고, 같은 구간 안에서는 차가 많이 서는 곳이 먼저다.
        def key(x):
            d = haversine((x["lat"], x["lon"]), near)
            return (int((d if d is not None else 1e9) // 2000), -x["trips"])
        out.sort(key=key)
        out = out[:limit]
    return out


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

# 유니코드가 합자로 안 쪼개는 글자들. ł·ø·ß 같은 것은 악센트가 아니라
# 별개 문자라서 정규화로는 안 없어진다.
SPECIAL = str.maketrans({
    "\u0142": "l", "\u0141": "L", "\u0111": "d", "\u0110": "D",
    "\u00f8": "o", "\u00d8": "O", "\u00e6": "ae", "\u00c6": "AE",
    "\u0153": "oe", "\u0152": "OE", "\u00df": "ss", "\u0131": "i",
    "\u00f0": "d", "\u00d0": "D", "\u00fe": "th", "\u00de": "TH",
})


def fold(s):
    """검색용 정규화. 악센트를 벗기고 대소문자를 없앤다.

    FTS5의 unicode61은 remove_diacritics 2를 줘도 그리스어 악센트를 못 지운다
    (Σύνταγμα가 ΣΥΝΤΑΓΜΑ에 안 걸린다). 그래서 직접 접는다. 베트남어·체코어·
    폴란드어도 같은 문제다.
    """
    s = (s or "").translate(SPECIAL)
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


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

#!/usr/bin/env python3
"""GTFS 경로 탐색. "어디에서 어디까지"에 답한다.

RAPTOR(Round-bAsed Public Transit Optimized Router)를 쓴다. 라운드 k는
"k번 타고 갈 때의 최단 도착 시각"이고, 라운드를 늘리면 환승이 늘어난다.
그래서 환승 횟수별 최적해가 한 번의 탐색으로 같이 나온다.

핵심은 trip이 아니라 패턴 단위로 도는 것이다. 헬싱키는 trip이 385,428개지만
정차 순서로 묶으면 패턴이 1,167개뿐이라, 스캔 대상이 300배 줄어든다.

transfers.txt는 대부분의 피드에서 거의 비어 있다(헬싱키는 4행). 그래서 환승
도보 연결은 좌표로 직접 만든다.

자체 점검:  python3 router.py
"""
import heapq
import math
import time
from collections import defaultdict

from gtfs import Feed, hhmm, secs

WALK_SPEED = 1.2       # m/s. 실측 보행 속도의 보수적인 값
WALK_BUILD = 1500      # m. 여기까지 미리 이어두고, 실제 한도는 탐색 때 고른다
WALK_DEFAULT = 500     # m. 기본 도보 한도
CHANGE_TIME = 60       # s. 같은 정류장에서 갈아탈 때의 최소 여유
ROUNDS = 5             # 최대 환승 4회
SLOWER_OK = 45 * 60    # s. 환승이 적어도 이보다 늦게 닿으면 안 보여준다
MAX_EXPAND = 300       # frequencies 한 trip을 펼칠 최대 개수
INF = 1 << 30


class Timetable:
    """하루치 시각표를 RAPTOR가 훑기 좋은 배열로 펼쳐둔 것."""

    def __init__(self, feeds, day):
        """feeds: [(feed_id, Feed)]. 여러 피드를 하나의 망으로 본다.

        도쿄에서 버스와 지하철이 다른 피드에 있다. 사용자에게 "운영사를 맞춰
        주세요"라고 할 일이 아니라 여기서 이어야 한다. 정류장 번호는
        "피드id:정류장id"로 붙여 충돌을 막는다.
        """
        if not isinstance(feeds, list):
            feeds = [("", feeds)]                  # 피드 하나만 준 경우
        self.feeds = feeds
        self.feed = feeds[0][1]                    # 시간대 등 대표값
        self.day = day
        t0 = time.time()

        self.stop_ids, self.idx = [], {}
        self.pat_stops, self.pat_arr, self.pat_dep, self.pat_meta = [], [], [], []
        self.by_stop = defaultdict(list)
        self.n_trips = self.n_generated = 0
        self.owner = {}                            # 정류장 -> 피드id

        for fid, feed in feeds:
            self._load_feed(fid, feed, day)
        if not self.pat_stops:
            raise ValueError(f"{day}에 운행하는 노선이 없습니다.")
        self._footpaths()
        self.build_secs = time.time() - t0

    def _load_feed(self, fid, feed, day):
        services = feed.services_on(day)
        if not services:
            return

        rows = feed.con.execute("""
            SELECT st.trip_id,
                   trim(st.stop_id) AS stop_id,
                   CAST(st.stop_sequence AS INTEGER) AS seq,
                   coalesce(nullif(trim(st.arrival_time), ''),
                            nullif(trim(st.departure_time), '')) AS arr,
                   coalesce(nullif(trim(st.departure_time), ''),
                            nullif(trim(st.arrival_time), '')) AS dep,
                   t.route_id, t.trip_headsign
            FROM stop_times st
            JOIN trips t ON t.trip_id = st.trip_id
            WHERE list_contains(?::VARCHAR[], t.service_id)
              AND coalesce(nullif(trim(st.departure_time), ''),
                           nullif(trim(st.arrival_time), '')) IS NOT NULL
            ORDER BY st.trip_id, seq
        """, [services]).fetchall()

        trips = defaultdict(list)
        meta = {}
        for trip_id, stop_id, seq, arr, dep, route_id, head in rows:
            trips[trip_id].append((f"{fid}:{stop_id}", secs(arr), secs(dep)))
            meta[trip_id] = (route_id, (head or "").strip())

        self.n_generated += self._expand_frequencies(feed, trips, meta)

        for legs in trips.values():
            for stop_id, _, _ in legs:
                if stop_id not in self.idx:
                    self.idx[stop_id] = len(self.stop_ids)
                    self.stop_ids.append(stop_id)
                    self.owner[stop_id] = fid

        # 정차 순서가 같은 trip을 한 패턴으로 묶는다
        groups = defaultdict(list)
        for trip_id, legs in trips.items():
            key = (meta[trip_id][0], tuple(s for s, _, _ in legs))
            groups[key].append((legs[0][2], trip_id, legs))

        for (route_id, stop_seq), members in groups.items():
            members.sort(key=lambda m: m[0])       # 첫 정류장 출발 시각 순
            p = len(self.pat_stops)
            self.pat_stops.append([self.idx[s] for s in stop_seq])
            self.pat_arr.append([[a for _, a, _ in legs] for _, _, legs in members])
            self.pat_dep.append([[d for _, _, d in legs] for _, _, legs in members])
            self.pat_meta.append((route_id, [meta[tid][1] for _, tid, _ in members]))
            for pos, s in enumerate(self.pat_stops[p]):
                self.by_stop[s].append((p, pos))

        self.n_trips += len(trips)

    def _expand_frequencies(self, feed, trips, meta):
        """frequencies.txt를 쓰는 trip을 실제 운행으로 펼친다.

        이런 trip의 stop_times는 시각표가 아니라 정차 간격만 담은 패턴이다.
        RAPTOR는 구체적인 운행이 있어야 탈 차를 고를 수 있어서, 배차간격만큼
        복제해 실제 시각을 만들어 넣는다. 피드의 16%가 이 형식이고,
        한국 시내버스도 TAGO가 시각표 대신 배차간격을 주므로 이 경로를 탄다.
        """
        try:
            freqs = feed.con.execute("""
                SELECT trim(trip_id), start_time, end_time,
                       CAST(nullif(trim(headway_secs), '') AS INTEGER)
                FROM frequencies""").fetchall()
        except Exception:
            return 0                      # frequencies.txt가 없는 피드가 대부분이다

        made = 0
        for trip_id, t_start, t_end, headway in freqs:
            base = trips.get(trip_id)
            if not base or not headway or headway <= 0:
                continue
            s0, s1 = secs(t_start), secs(t_end)
            if s0 is None or s1 is None or s1 <= s0:
                continue
            n = (s1 - s0) // headway + 1
            if n > MAX_EXPAND:
                # 배차 1분짜리를 하루치 펼치면 수천 개가 된다. 그 정도면
                # 어차피 항상 차가 있으니 표본만 만들어도 답이 같다.
                headway = max(headway, (s1 - s0) // MAX_EXPAND)
                n = (s1 - s0) // headway + 1
            first = base[0][2]            # 패턴의 기준 시각
            del trips[trip_id]
            route_id, head = meta.pop(trip_id)
            for i in range(n):
                shift = s0 + i * headway - first
                new_id = f"{trip_id}#{i}"
                trips[new_id] = [(sid, (a + shift) if a is not None else None,
                                  (d + shift) if d is not None else None)
                                 for sid, a, d in base]
                meta[new_id] = (route_id, head)
                made += 1
        return made

    def _footpaths(self):
        """좌표로 도보 환승을 만든다. 격자에 넣고 이웃 칸만 본다.

        WALK_BUILD까지 넉넉히 이어두고 거리를 함께 담는다. 실제 허용 거리는
        탐색할 때 고른다 — "좀 걸어도 환승 없이" 같은 선택이 가능하도록.
        """
        pos = {}
        for fid, feed in self.feeds:
            for sid, la, lo in feed.con.execute("""
                    SELECT trim(stop_id), stop_lat, stop_lon FROM stops
                    WHERE stop_lat IS NOT NULL AND stop_lon IS NOT NULL""").fetchall():
                i = self.idx.get(f"{fid}:{sid}")
                if i is None:
                    continue
                try:
                    pos[i] = (float(la), float(lo))
                except (TypeError, ValueError):
                    pass

        cell = WALK_BUILD / 111_000          # 위도 1도 ≒ 111km
        grid = defaultdict(list)
        for i, (la, lo) in pos.items():
            grid[(int(la / cell), int(lo / cell))].append(i)

        self.foot = defaultdict(list)
        for (gx, gy), members in grid.items():
            near = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                    for j in grid.get((gx + dx, gy + dy), ())]
            for i in members:
                la1, lo1 = pos[i]
                for j in near:
                    if i == j:
                        continue
                    la2, lo2 = pos[j]
                    dy = (la2 - la1) * 111_000
                    dx = (lo2 - lo1) * 111_000 * math.cos(math.radians(la1))
                    d = math.hypot(dx, dy)
                    if d <= WALK_BUILD:
                        self.foot[i].append((j, int(d / WALK_SPEED) + CHANGE_TIME, int(d)))

        # transfers.txt가 있으면 그쪽 값을 우선한다(운영사가 아는 실제 환승 시간)
        for fid, feed in self.feeds:
            try:
                rows = feed.con.execute("""
                    SELECT trim(from_stop_id), trim(to_stop_id),
                           CAST(nullif(trim(min_transfer_time), '') AS INTEGER)
                    FROM transfers""").fetchall()
            except Exception:
                continue               # transfers.txt가 없는 피드가 대부분이다
            for a, b, wait in rows:
                i = self.idx.get(f"{fid}:{a}")
                j = self.idx.get(f"{fid}:{b}")
                if i is None or j is None or i == j:
                    continue
                keep = [f for f in self.foot[i] if f[0] != j]
                keep.append((j, wait or CHANGE_TIME, 0))
                self.foot[i] = keep

    def plain(self, i):
        """정류장 번호 -> (피드id, 원래 stop_id)."""
        fid, _, sid = self.stop_ids[i].partition(":")
        return fid, sid

    def earliest_trip(self, p, pos, after):
        """위치 pos에서 after 이후에 출발하는 가장 이른 trip 번호. 없으면 None."""
        deps = self.pat_dep[p]
        lo, hi = 0, len(deps)
        while lo < hi:                      # 첫 정류장 출발 순으로 정렬돼 있다
            mid = (lo + hi) // 2
            if deps[mid][pos] < after:
                lo = mid + 1
            else:
                hi = mid
        return lo if lo < len(deps) else None


def _seed(tt, stop_ids, t0, walk_max):
    """출발 정류장들과, 거기서 걸어갈 수 있는 정류장들의 최초 도착 시각."""
    start = {}
    for sid in stop_ids:
        i = tt.idx.get(sid.strip())
        if i is not None:
            start[i] = t0
    if not start:
        return {}, {}
    reach, par = dict(start), {}
    for i, t in start.items():
        for j, w, m in tt.foot.get(i, ()):
            if m <= walk_max and t + w < reach.get(j, INF):
                reach[j] = t + w
                par[j] = ("walk", i, w)
    return reach, par


def search(tt, from_ids, to_ids, depart, rounds=ROUNDS, walk_max=WALK_DEFAULT):
    """RAPTOR. 환승 횟수별 최단 도착을 한 번에 구한다."""
    n = len(tt.stop_ids)
    targets = {tt.idx[s.strip()] for s in to_ids if s.strip() in tt.idx}
    if not targets:
        return []

    reach, seed_par = _seed(tt, from_ids, depart, walk_max)
    if not reach:
        return []

    best = [INF] * n
    arr = [[INF] * n for _ in range(rounds + 1)]
    parent = [[None] * n for _ in range(rounds + 1)]
    for i, t in reach.items():
        best[i] = arr[0][i] = t
        parent[0][i] = seed_par.get(i)
    marked = set(reach)

    for k in range(1, rounds + 1):
        arr[k] = arr[k - 1][:]
        parent[k] = parent[k - 1][:]

        # 표시된 정류장을 지나는 패턴을 모으되, 가장 앞선 위치부터 훑는다
        queue = {}
        for s in marked:
            for p, pos in tt.by_stop.get(s, ()):
                if pos < queue.get(p, 1 << 30):
                    queue[p] = pos
        marked = set()
        goal = min((best[t] for t in targets), default=INF)

        for p, start_pos in queue.items():
            stops = tt.pat_stops[p]
            arrs, deps = tt.pat_arr[p], tt.pat_dep[p]
            trip = board = None
            for pos in range(start_pos, len(stops)):
                s = stops[pos]
                if trip is not None:
                    a = arrs[trip][pos]
                    if a < min(best[s], goal):     # 목적지보다 늦으면 볼 필요 없다
                        best[s] = arr[k][s] = a
                        parent[k][s] = ("ride", p, trip, board, pos)
                        marked.add(s)
                        if s in targets:
                            goal = min(goal, a)
                # 여기서 더 이른 차를 탈 수 있나
                ready = arr[k - 1][s]
                if ready >= INF:
                    continue
                if k > 1 and parent[k - 1][s] and parent[k - 1][s][0] == "ride":
                    ready += CHANGE_TIME            # 차에서 내렸으면 갈아탈 여유가 필요
                if trip is None or ready <= deps[trip][pos]:
                    t2 = tt.earliest_trip(p, pos, ready)
                    if t2 is not None and t2 != trip:
                        trip, board = t2, pos

        # 내린 곳에서 걸어서 닿는 곳.
        # 걸어서 닿은 곳에서 또 걷지 않는다. 라운드를 넘나들며 도보가 연쇄되면
        # "91분 경로에 도보 109분" 같은 답이 나온다(실제로 나왔다).
        for s in list(marked):
            if parent[k][s] and parent[k][s][0] == "walk":
                continue
            for j, w, m in tt.foot.get(s, ()):
                if m > walk_max:
                    continue
                t = arr[k][s] + w
                if t < best[j]:
                    best[j] = arr[k][j] = t
                    parent[k][j] = ("walk", s, w)
                    marked.add(j)
        if not marked:
            break

    # 환승 횟수별로 하나씩. 더 갈아타는데 더 늦으면 버린다
    out, seen_best = [], INF
    for k in range(rounds + 1):
        cand = [(arr[k][t], t) for t in targets if arr[k][t] < INF]
        if not cand:
            continue
        at, stop = min(cand)
        if at >= seen_best:
            continue
        seen_best = at
        out.append(_journey(tt, parent, k, stop, depart))

    if not out:
        return []
    # 환승이 적다는 이유만으로 몇 시간 뒤에 출발하는 차를 올릴 수는 없다.
    # 실제로 헬싱키에서 "환승 2회 941분 / 환승 3회 85분"이 같이 나왔다.
    soonest = min(j["arrive"] for j in out)
    out = [j for j in out if j["arrive"] <= soonest + SLOWER_OK]
    out.sort(key=lambda j: (j["arrive"], j["transfers"]))
    return out


def _journey(tt, parent, k, stop, depart):
    """부모 포인터를 거꾸로 따라가 구간 목록을 만든다."""
    legs, cur, kk = [], stop, k
    while True:
        p = parent[kk][cur]
        if p is None:
            break
        if p[0] == "walk":
            _, src, w = p
            legs.append({"mode": "walk", "from": tt.stop_ids[src],
                         "to": tt.stop_ids[cur], "secs": w})
            cur = src
        else:
            _, pat, trip, board, alight = p
            route_id, heads = tt.pat_meta[pat]
            legs.append({
                "mode": "ride", "route_id": route_id,
                "headsign": heads[trip] if trip < len(heads) else "",
                "from": tt.stop_ids[tt.pat_stops[pat][board]],
                "to": tt.stop_ids[tt.pat_stops[pat][alight]],
                "depart": tt.pat_dep[pat][trip][board],
                "arrive": tt.pat_arr[pat][trip][alight],
                "stops": alight - board,
            })
            cur = tt.pat_stops[pat][board]
            kk -= 1
    legs.reverse()

    # 이어진 도보는 한 구간으로 합친다. 라운드가 진행되며 앞서 붙인 도보 라벨이
    # 나중 라운드에서 덮인 라벨을 가리켜, 재구성 때 도보가 연달아 나올 때가 있다.
    # A→B→C를 걷는 것은 결국 한 번 걷는 것이므로 합쳐서 보여준다.
    merged = []
    for leg in legs:
        if merged and leg["mode"] == "walk" and merged[-1]["mode"] == "walk":
            merged[-1] = {**merged[-1], "to": leg["to"],
                          "secs": merged[-1]["secs"] + leg["secs"]}
        else:
            merged.append(leg)
    legs = merged

    rides = [l for l in legs if l["mode"] == "ride"]

    # 마지막 승차 뒤의 도보도 도착 시각에 들어가야 한다. 안 넣었더니
    # "총 91분인데 도보 109분" 같은 값이 나왔다.
    clock = depart
    for leg in legs:
        clock = (clock + leg["secs"]) if leg["mode"] == "walk" else leg["arrive"]

    return {"legs": legs, "transfers": max(0, len(rides) - 1),
            "depart": depart,                                  # 집을 나서는 시각
            "board": rides[0]["depart"] if rides else depart,  # 첫 차 출발
            "arrive": clock, "duration": clock - depart}


def demo():
    """실제 헬싱키 피드로 검증한다. 붙어 있는 구간과 환승 구간을 모두 본다."""
    import random
    from datetime import datetime

    f = Feed("../gtfs/mdb-865_Helsingin_seudun_liikenne__HSL.zip")
    tt = Timetable([("hsl", f)], datetime.now(f.tz).date())
    assert tt.pat_stops, "패턴이 비었음"
    assert tt.foot, "도보 환승이 만들어지지 않았음"
    print(f"  시각표: trip {tt.n_trips:,} · 패턴 {len(tt.pat_stops):,} · "
          f"정류장 {len(tt.stop_ids):,} · {tt.build_secs:.1f}초")

    random.seed(5)
    checked = rides_total = slow = 0
    for _ in range(60):
        a, b = random.sample(tt.stop_ids, 2)
        for j in search(tt, [a], [b], 9 * 3600):
            checked += 1
            # 앞 구간에 도착하기 전에 다음 차를 탈 수는 없다
            clock = None
            for leg in j["legs"]:
                if leg["mode"] == "walk":
                    if clock is not None:
                        clock += leg["secs"]
                else:
                    if clock is not None:
                        assert leg["depart"] >= clock, (
                            f"시간 역전: {hhmm(clock)} 도착인데 {hhmm(leg['depart'])} 승차")
                    assert leg["arrive"] >= leg["depart"], "구간 내 시간 역전"
                    clock = leg["arrive"]
                    rides_total += 1
            assert j["arrive"] >= j["depart"], "도착이 출발보다 이르다"
            walked = sum(l["secs"] for l in j["legs"] if l["mode"] == "walk")
            assert walked <= j["duration"], (
                f"도보 {walked // 60}분이 총 소요 {j['duration'] // 60}분보다 크다")
            run = cur = 0
            for l in j["legs"]:
                cur = cur + 1 if l["mode"] == "walk" else 0
                run = max(run, cur)
            assert run <= 1, "도보 구간이 연달아 있다"
            if j["duration"] > 4 * 3600:
                slow += 1

    assert checked > 10, f"검증한 경로가 너무 적음: {checked}"
    print(f"  경로 {checked}개 · 승차 구간 {rides_total}개 검증 · 4시간 초과 {slow}개")

    # 같은 구간에 여러 후보가 나오면 서로 SLOWER_OK 안에 있어야 한다.
    # 긴 소요시간 자체는 정상이다 — 하루 두 대만 다니는 정류장이 실제로 있다.
    random.seed(11)
    spread_checked = 0
    for _ in range(60):
        a, b = random.sample(tt.stop_ids, 2)
        js = search(tt, [a], [b], 9 * 3600)
        if len(js) < 2:
            continue
        spread_checked += 1
        assert js[-1]["arrive"] - js[0]["arrive"] <= SLOWER_OK, (
            f"후보 간 도착 차이가 너무 큼: "
            f"{[j['duration'] // 60 for j in js]}분")
    print(f"  후보가 둘 이상인 구간 {spread_checked}개의 도착 편차 확인")

    # 환승이 실제로 일어나는지
    random.seed(5)
    with_transfer = 0
    for _ in range(40):
        a, b = random.sample(tt.stop_ids, 2)
        if any(j["transfers"] >= 1 for j in search(tt, [a], [b], 9 * 3600)):
            with_transfer += 1
    assert with_transfer > 0, "환승 경로가 하나도 안 나왔다"
    print(f"  환승 포함 경로가 나온 구간: {with_transfer}/40")

    _check_frequencies()
    print("자체 점검 통과")


def _check_frequencies():
    """frequencies 전개를 출발 안내판과 대조한다.

    안내판은 SQL로, 라우터는 파이썬으로 같은 일을 따로 구현했다. 두 결과가
    어긋나면 둘 중 하나가 틀린 것이므로, 이 대조가 가장 강한 검사다.
    """
    from datetime import datetime, timedelta

    f = Feed("../gtfs/mdb-1985_Aeroexpreso.zip")
    day = next(d for d in (datetime.now(f.tz).date() + timedelta(days=i)
                           for i in range(400)) if f.services_on(d))
    tt = Timetable([("x", f)], day)
    assert tt.n_generated > 0, "frequencies가 전개되지 않았다"

    stop = "03701CUZ"
    probe = datetime(day.year, day.month, day.day, 0, 0, tzinfo=f.tz)
    board = {d["secs"] for d in f.departures(stop, now=probe, limit=500)
             if d["day_offset"] == 0}
    i = tt.idx[f"x:{stop}"]
    router = {tt.pat_dep[p][t][pos]
              for p, pos in tt.by_stop[i]
              for t in range(len(tt.pat_dep[p]))}
    assert board == router, (
        f"안내판과 라우터가 어긋남: 안내판만 {len(board - router)}개, "
        f"라우터만 {len(router - board)}개")
    print(f"  frequencies: {tt.n_generated}개 전개 · 안내판과 {len(board)}개 전부 일치")


if __name__ == "__main__":
    demo()

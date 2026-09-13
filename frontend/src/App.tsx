import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "./api";
import type { Board, Stop } from "./api";
import BoardPanel, { Empty } from "./components/Board";

const regionName = (() => {
  let f: Intl.DisplayNames | undefined;
  try {
    f = new Intl.DisplayNames(["ko"], { type: "region" });
  } catch {
    /* 구형 브라우저는 코드 그대로 */
  }
  return (c: string) => {
    try {
      return f?.of(c) ?? c;
    } catch {
      return c;
    }
  };
})();

/** URL이 상태다. 새로고침해도, 링크를 보내도 그대로 열린다. */
function readUrl() {
  const p = new URLSearchParams(location.search);
  return {
    q: p.get("q") ?? "",
    country: p.get("country") ?? "",
    feed: p.get("feed") ?? "",
    stop: p.get("stop") ?? "",
  };
}

function writeUrl(patch: Record<string, string>, push = false) {
  const p = new URLSearchParams(location.search);
  for (const [k, v] of Object.entries(patch)) v ? p.set(k, v) : p.delete(k);
  const url = p.toString() ? `?${p}` : location.pathname;
  push ? history.pushState({}, "", url) : history.replaceState({}, "", url);
}

export default function App() {
  const start = useRef(readUrl()).current;
  const [q, setQ] = useState(start.q);
  const [country, setCountry] = useState(start.country);
  const [places, setPlaces] = useState<{ code: string; feeds: number }[]>([]);
  const [hits, setHits] = useState<Stop[]>([]);
  const [busy, setBusy] = useState(false);
  const [picked, setPicked] = useState<Stop | null>(null);
  const [board, setBoard] = useState<Board | null>(null);
  const run = useRef(0); // 늦게 온 응답이 최신 결과를 덮지 않도록

  useEffect(() => {
    api.countries().then(setPlaces).catch(() => setPlaces([]));
  }, []);

  const search = useCallback(
    async (text: string, cc: string, restore?: { feed: string; stop: string }) => {
      const mine = ++run.current;
      if (!text.trim()) {
        setHits([]);
        setPicked(null);
        return;
      }
      setBusy(true);
      let rows: Stop[] = [];
      try {
        rows = await api.searchStops(text, cc);
      } catch {
        rows = [];
      }
      if (mine !== run.current) return;
      setHits(rows);
      setBusy(false);

      if (restore) {
        const found = rows.find((r) => r.feed_id === restore.feed && r.stop_id === restore.stop);
        if (found) void open(found, false);
      }

      // 번역은 화면을 그린 뒤에 채운다
      const missing = rows.filter((r) => !r.stop_name_ko).map((r) => r.stop_name);
      const got = await api.translate(missing);
      if (mine !== run.current || !Object.keys(got).length) return;
      setHits((prev) =>
        prev.map((r) => (got[r.stop_name] ? { ...r, stop_name_ko: got[r.stop_name] } : r)),
      );
    },
    [],
  );

  const open = useCallback(async (stop: Stop, push = true) => {
    setPicked(stop);
    setBoard(null);
    writeUrl({ feed: stop.feed_id, stop: stop.stop_id }, push);
    let data: Board;
    try {
      data = await api.board(stop.feed_id, stop.stop_id);
    } catch {
      data = { agency: "", timezone: "", country: "", local_time: "", departures: [],
               error: "시간표를 불러오지 못했습니다." };
    }
    setBoard(data);

    const missing = data.departures.flatMap((r) => [r.headsign, r.route]);
    const got = await api.translate(missing);
    if (!Object.keys(got).length) return;
    setBoard((prev) =>
      prev === data
        ? {
            ...prev,
            departures: prev.departures.map((r) => ({
              ...r,
              headsign_ko: got[r.headsign] ?? r.headsign_ko,
              route_ko: got[r.route] ?? r.route_ko,
            })),
          }
        : prev,
    );
  }, []);

  // 첫 진입: URL에 있던 검색과 정류장을 되살린다
  useEffect(() => {
    if (start.q) void search(start.q, start.country, { feed: start.feed, stop: start.stop });
  }, [search, start]);

  // 타이핑이 멈추면 검색
  useEffect(() => {
    writeUrl({ q, country });
    if (q === start.q && !hits.length && !q) return;
    const t = setTimeout(() => void search(q, country), 230);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, country]);

  return (
    <div className="app">
      <aside className="rail">
        <div className="brand">
          <b>어디로 가는 차</b>
          <span>전 세계 대중교통 출발 안내</span>
        </div>

        <div className="find">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="정류장 이름 (한국어로도 됩니다)"
            aria-label="정류장 검색"
            autoComplete="off"
            autoFocus
          />
          <select value={country} onChange={(e) => setCountry(e.target.value)} aria-label="국가">
            <option value="">모든 나라</option>
            {places.map((c) => (
              <option key={c.code} value={c.code}>
                {regionName(c.code)} ({c.feeds})
              </option>
            ))}
          </select>
        </div>

        <ul className="hits">
          {busy && <li className="note">찾는 중…</li>}
          {!busy && q.trim() && !hits.length && (
            <li className="note">그 이름으로는 찾지 못했습니다. 철자를 바꾸거나 국가를 넓혀 보세요.</li>
          )}
          {hits.map((r) => (
            <li
              key={`${r.feed_id}/${r.stop_id}`}
              className="hit"
              tabIndex={0}
              role="button"
              aria-current={picked === r}
              onClick={() => void open(r)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  void open(r);
                }
              }}
            >
              <span className="cc">{r.country || "·"}</span>
              <span className="what">
                <b>
                  {r.stop_name_ko || r.stop_name}
                  {r.is_station && <i className="tag">역</i>}
                </b>
                {r.stop_name_ko && <em>{r.stop_name}</em>}
                <i className="who">{r.agency_ko || r.agency}</i>
              </span>
            </li>
          ))}
        </ul>
      </aside>

      <main className="board">
        {picked ? <BoardPanel stop={picked} data={board} /> : <Empty onPick={setQ} />}
      </main>
    </div>
  );
}

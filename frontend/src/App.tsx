import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "./api";
import type { Board, Route, Stop } from "./api";
import BoardPanel, { Empty } from "./components/Board";
import Splash from "./components/Splash";
import JourneyPanel from "./components/Journey";

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

type Slot = "from" | "to";
const shown = (s: Stop) => s.stop_name_ko || s.stop_name;
const pack = (s: Stop) => `${s.feed_id}|${s.stop_id}`;

/** URL이 상태다. 새로고침해도, 링크를 보내도 그대로 열린다. */
function readUrl() {
  const p = new URLSearchParams(location.search);
  return { from: p.get("from") ?? "", to: p.get("to") ?? "", country: p.get("country") ?? "" };
}
function writeUrl(patch: Record<string, string>) {
  const p = new URLSearchParams(location.search);
  for (const [k, v] of Object.entries(patch)) v ? p.set(k, v) : p.delete(k);
  history.replaceState({}, "", p.toString() ? `?${p}` : location.pathname);
}

export default function App() {
  const start = useRef(readUrl()).current;
  const [country, setCountry] = useState(start.country);
  const [places, setPlaces] = useState<{ code: string; feeds: number }[]>([]);
  const [text, setText] = useState<Record<Slot, string>>({ from: "", to: "" });
  const [stop, setStop] = useState<Record<Slot, Stop | null>>({ from: null, to: null });
  const [active, setActive] = useState<Slot>("from");
  const [hits, setHits] = useState<Stop[]>([]);
  const [busy, setBusy] = useState(false);
  const [filling, setFilling] = useState(false);   // 한국어 이름 채우는 중
  const [board, setBoard] = useState<Board | null>(null);
  const [route, setRoute] = useState<Route | null>(null);
  const [walk, setWalk] = useState(500);   // m. 걸어도 되는 거리
  const run = useRef(0); // 늦게 온 응답이 최신 결과를 덮지 않도록

  const [booted, setBooted] = useState(false);
  useEffect(() => {
    const t0 = Date.now();
    // 국가 목록이 오면 들어간다. 너무 빨리 오면 깜빡여 보이므로 잠깐만 잡아둔다.
    api.countries().then(setPlaces).catch(() => setPlaces([])).finally(() => {
      setTimeout(() => setBooted(true), Math.max(0, 650 - (Date.now() - t0)));
    });
    // API가 멎어도 여기서 막히면 안 된다
    const hard = setTimeout(() => setBooted(true), 5000);
    return () => clearTimeout(hard);
  }, []);

  // URL에 담긴 출발·도착지를 되살린다
  useEffect(() => {
    (["from", "to"] as Slot[]).forEach((k) => {
      const raw = start[k];
      if (!raw.includes("|")) return;
      const [feed, id] = raw.split("|");
      api.stopById(feed, id).then((s) => {
        setStop((p) => ({ ...p, [k]: s }));
        setText((p) => ({ ...p, [k]: shown(s) }));
      }).catch(() => undefined);
    });
  }, [start]);

  const search = useCallback(async (q: string, cc: string, near?: Stop | null) => {
    const mine = ++run.current;
    if (!q.trim()) {
      setHits([]);
      return;
    }
    setBusy(true);
    let rows: Stop[] = [];
    try {
      rows = await api.searchStops(q, cc, near);
    } catch {
      rows = [];
    }
    if (mine !== run.current) return;
    setHits(rows);
    setBusy(false);

    // 이름이 같으면 운영사가 유일한 구별 수단이다. 그것도 한글이어야 한다.
    // 한 번에 부른다. 지연은 개수가 아니라 호출 수에 비례한다 — 10개와
    // 25개가 둘 다 5~8초라서, 나누면 대기가 두 배가 된다(실측).
    setFilling(true);
    const got = await api.translate(
      rows.flatMap((r) => [r.stop_name_ko ? "" : r.stop_name,
                           r.agency_ko ? "" : r.agency]),
    );
    if (mine !== run.current) return;
    setFilling(false);
    if (!Object.keys(got).length) return;
    setHits((prev) =>
      prev.map((r) => ({
        ...r,
        stop_name_ko: got[r.stop_name] || r.stop_name_ko,
        agency_ko: got[r.agency] || r.agency_ko,
      })),
    );
  }, []);

  // 타이핑이 멈추면 활성 칸으로 검색
  const q = text[active];
  useEffect(() => {
    if (stop[active] && shown(stop[active]!) === q) return; // 고른 직후엔 다시 찾지 않는다
    // 반대쪽이 정해져 있으면 그 근처를 먼저 보여준다. 도착지를 고를 때
    // 출발지에서 먼 같은 이름 정류장이 위에 올 이유가 없다.
    const other = stop[active === "from" ? "to" : "from"];
    const t = setTimeout(() => void search(q, country, other), 230);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, country, active]);

  useEffect(() => {
    writeUrl({
      country,
      from: stop.from ? pack(stop.from) : "",
      to: stop.to ? pack(stop.to) : "",
    });
  }, [country, stop]);

  // 한쪽만 골랐으면 출발 안내판, 둘 다 골랐으면 길찾기
  useEffect(() => {
    const { from, to } = stop;
    setBoard(null);
    setRoute(null);
    if (from && to) {
      let alive = true;
      api.findRoute(from, to, walk)
        .then((r) => alive && setRoute(r))
        .catch(() => alive && setRoute({ agency: "", timezone: "", local_time: "",
          feeds: 0, walk, plans: [], error: "길을 불러오지 못했습니다." }));
      return () => { alive = false; };
    }
    const one = from ?? to;
    if (!one) return;
    let alive = true;
    api.board(one.feed_id, one.stop_id)
      .then(async (d) => {
        if (!alive) return;
        setBoard(d);
        const got = await api.translate(d.departures.flatMap((r) => [r.headsign, r.route]));
        if (!alive || !Object.keys(got).length) return;
        setBoard((prev) =>
          prev && prev === d
            ? { ...prev, departures: prev.departures.map((r) => ({
                ...r,
                headsign_ko: got[r.headsign] ?? r.headsign_ko,
                route_ko: got[r.route] ?? r.route_ko,
              })) }
            : prev,
        );
      })
      .catch(() => alive && setBoard({ agency: "", timezone: "", country: "",
        local_time: "", departures: [], error: "시간표를 불러오지 못했습니다." }));
    return () => { alive = false; };
  }, [stop, walk]);

  const choose = (s: Stop) => {
    setStop((p) => ({ ...p, [active]: s }));
    setText((p) => ({ ...p, [active]: shown(s) }));
    setHits([]);
    // 한 쪽이 정해지면 나라도 거기로 맞춘다. 일본에서 출발해 호주로 가는
    // 대중교통 경로는 없으므로 선택지에 남겨둘 이유가 없다.
    if (!country && s.country) setCountry(s.country);
    if (active === "from" && !stop.to) setActive("to");
  };

  const clear = (k: Slot) => {
    setStop((p) => ({ ...p, [k]: null }));
    setText((p) => ({ ...p, [k]: "" }));
    setActive(k);
  };

  // 방금 고른 정류장의 이름이 칸에 남아 있는 것뿐이다. 못 찾은 게 아니다.
  const chosenHere = !!stop[active] && shown(stop[active]!) === q;
  const fields: [Slot, string][] = [["from", "출발"], ["to", "도착"]];
  const both = stop.from && stop.to;
  const walks: [number, string][] = [[350, "적게"], [500, "보통"], [1200, "많이"]];

  return (
    <>
      <Splash done={booted} />
      <div className="app">
      <aside className="rail">
        <div className="brand">
          <b>어디로</b>
          <span>전 세계 대중교통 출발 안내</span>
        </div>

        <div className="find">
          {fields.map(([k, tag]) => (
            <label key={k} className={`field${active === k ? " on" : ""}`}>
              <span className="tag">{tag}</span>
              <input
                value={text[k]}
                onChange={(e) => setText((p) => ({ ...p, [k]: e.target.value }))}
                onFocus={() => setActive(k)}
                placeholder={k === "from" ? "정류장 이름" : "비우면 출발 안내판"}
                aria-label={tag}
                autoComplete="off"
                autoFocus={k === "from"}
              />
              {stop[k] && (
                <button type="button" onClick={() => clear(k)} aria-label={`${tag} 지우기`}>
                  ×
                </button>
              )}
            </label>
          ))}
          {both && (
            <div className="walkpick" role="group" aria-label="걸어도 되는 거리">
              <span>걷기</span>
              {walks.map(([m, tag]) => (
                <button key={m} type="button" aria-pressed={walk === m}
                        onClick={() => setWalk(m)}>{tag}</button>
              ))}
            </div>
          )}
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
          {!busy && filling && hits.some((r) => !r.stop_name_ko) && (
            <li className="note quiet">한국어 이름을 불러오는 중…</li>
          )}
          {!busy && q.trim() && !hits.length && !chosenHere && (
            <li className="note">그 이름으로는 찾지 못했습니다. 철자를 바꾸거나 국가를 넓혀 보세요.</li>
          )}
          {hits.map((r) => (
            <li
              key={`${r.feed_id}/${r.stop_id}`}
              className="hit"
              tabIndex={0}
              role="button"
              onClick={() => choose(r)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  choose(r);
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
        {both ? (
          <JourneyPanel from={stop.from!} to={stop.to!} data={route} />
        ) : stop.from || stop.to ? (
          <BoardPanel stop={(stop.from ?? stop.to)!} data={board} />
        ) : (
          <Empty onPick={(s) => { setActive("from"); setText((p) => ({ ...p, from: s })); }} />
        )}
      </main>
      </div>
    </>
  );
}

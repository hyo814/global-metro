import type { Board, Run, Stop } from "../api";

const dayLabel: Record<number, string> = { 1: "내일", 2: "모레" };

function wait(r: Run) {
  if (r.day_offset === -1) return "자정 넘김";
  if (r.day_offset > 0) return dayLabel[r.day_offset] ?? `${r.day_offset}일 후`;
  if (r.in_min <= 0) return "곧";
  if (r.in_min < 60) return `${r.in_min}분`;
  return `${Math.floor(r.in_min / 60)}시간 ${r.in_min % 60}분`;
}

/** 색은 운영사가 자기 피드에 넣어둔 노선 색이다. 없으면 테두리만. */
function chip(r: Run) {
  return r.color
    ? { background: r.color, color: r.text_color, borderColor: r.color }
    : undefined;
}

const samples: [string, string][] = [
  ["신주쿠", "新宿"],
  ["바르셀로나", "Barcelona-Sants"],
  ["샤를드골", "Charles de Gaulle"],
];

export function Empty({ onPick }: { onPick: (q: string) => void }) {
  return (
    <div className="empty">
      <h1>
        한국어로 치면
        <br />
        현지 표기로 찾습니다
      </h1>
      <p>
        68개 나라 368만 개 정류장. 현지 표기를 몰라도 됩니다. 결과는 한국어와
        현지 표기를 나란히 보여주니 역에서 표지판과 그대로 맞춰볼 수 있습니다.
      </p>
      <ul>
        {samples.map(([ko, orig]) => (
          <li key={ko}>
            <button type="button" onClick={() => onPick(ko)}>
              {ko}
              <em>{orig}</em>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function BoardPanel({ stop, data }: { stop: Stop; data: Board | null }) {
  const bays = stop.stop_id.split(",").filter(Boolean).length;
  return (
    <>
      <header className="stophead">
        <h1>{stop.stop_name_ko || stop.stop_name}</h1>
        {stop.stop_name_ko && <p className="orig">{stop.stop_name}</p>}
        <div className="meta">
          <span>
            {data?.agency ?? stop.agency}
            {bays > 1 && <em>승강장 {bays}곳 합산</em>}
          </span>
          {data?.local_time && <time className="num">현지 {data.local_time}</time>}
        </div>
      </header>

      {!data ? (
        <p className="status">불러오는 중…</p>
      ) : data.error ? (
        <p className="status">{data.error}</p>
      ) : !data.departures.length ? (
        <p className="status">앞으로 7일 동안 이 정류장에서 출발하는 차가 없습니다.</p>
      ) : (
        <ol className="runs">
          {data.departures.map((r, i) => (
            <li key={i} className={r.in_min <= 10 && r.day_offset <= 0 ? "soon" : ""}>
              <time className="at num">{r.time}</time>
              <span className="line">
                {r.route && <b style={chip(r)}>{r.route_ko || r.route}</b>}
              </span>
              <span className="to">
                <b>{r.headsign_ko || r.headsign || "행선지 미표기"}</b>
                {r.headsign_ko && r.headsign && <em>{r.headsign}</em>}
              </span>
              <span className="when num">{wait(r)}</span>
            </li>
          ))}
        </ol>
      )}
    </>
  );
}

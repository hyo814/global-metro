import type { Leg, Plan, Route, Stop } from "../api";

function label(l: Leg, which: "from" | "to") {
  return which === "from" ? l.from_ko || l.from : l.to_ko || l.to;
}

function Steps({ plan }: { plan: Plan }) {
  const rides = plan.legs.filter((l) => l.mode === "ride");
  return (
    <ol className="steps">
      {plan.legs.map((l, i) => {
        const prev = plan.legs[i - 1];
        // 앞 차 도착과 다음 차 출발 사이가 벌어지면 그게 실제로 기다리는 시간이다
        const wait =
          prev?.mode === "ride" && l.mode === "ride" && prev.arrive && l.depart
            ? gap(prev.arrive, l.depart)
            : 0;
        return (
          <li key={i} className={l.mode}>
            {wait > 0 && <span className="wait">{fmt(wait)} 대기</span>}
            {l.mode === "walk" ? (
              <>
                <span className="dot" aria-hidden />
                <span className="what">
                  도보 {l.minutes}분<em>{label(l, "to")}까지</em>
                </span>
              </>
            ) : (
              <>
                <time className="at num">{l.depart}</time>
                <span className="dot" aria-hidden />
                <span className="what">
                  <b>
                    <span className="route">{l.route}</span>
                    {l.headsign_ko || l.headsign || ""}
                  </b>
                  <em>
                    {label(l, "from")} → {label(l, "to")}
                  </em>
                  <i>
                    {l.stops}정거장 · {l.minutes}분
                  </i>
                </span>
                <time className="at end num">{l.arrive}</time>
              </>
            )}
          </li>
        );
      })}
      {!rides.length && <li className="walk">탈 것 없이 걸어갈 수 있습니다.</li>}
    </ol>
  );
}

const gap = (a: string, b: string) => {
  const m = (s: string) => +s.slice(0, 2) * 60 + +s.slice(3, 5);
  return (m(b) - m(a) + 1440) % 1440;
};
const fmt = (m: number) => (m < 60 ? `${m}분` : `${Math.floor(m / 60)}시간 ${m % 60}분`);

export default function JourneyPanel({
  from, to, data,
}: { from: Stop; to: Stop; data: Route | null }) {
  return (
    <>
      <header className="stophead">
        <h1>
          {from.stop_name_ko || from.stop_name}
          <span className="arrow" aria-label="에서"> → </span>
          {to.stop_name_ko || to.stop_name}
        </h1>
        <p className="orig">
          {from.stop_name} → {to.stop_name}
        </p>
        <div className="meta">
          <span>
            {data?.agency ?? from.agency}
            {data && data.feeds > 1 && <em>운영사 {data.feeds}곳을 이어서</em>}
          </span>
          {data?.local_time && <time className="num">현지 {data.local_time}</time>}
        </div>
      </header>

      {!data ? (
        <p className="status">길을 찾는 중…</p>
      ) : data.error ? (
        <p className="status">{data.error}</p>
      ) : !data.plans.length ? (
        <p className="status">
          오늘 이 두 정류장을 잇는 길을 찾지 못했습니다. 다른 시각이나 가까운 정류장을 골라 보세요.
        </p>
      ) : (
        data.plans.map((p, i) => (
          <section className="plan" key={i}>
            <div className="sum">
              <b className="num">{fmt(p.minutes)}</b>
              <span className="num">
                {p.depart} → {p.arrive}
              </span>
              <span>{p.transfers ? `환승 ${p.transfers}회` : "환승 없음"}</span>
              {p.walk_minutes > 0 && <span>걷기 {fmt(p.walk_minutes)}</span>}
              {p.max_wait >= 20 && <em>기다림 {fmt(p.max_wait)}</em>}
            </div>
            <Steps plan={p} />
          </section>
        ))
      )}
    </>
  );
}

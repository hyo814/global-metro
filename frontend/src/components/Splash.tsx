/** 들어갈 때 잠깐 보이는 화면.
 *
 * 꾸미려고 두는 게 아니다. 국가 목록을 불러오는 동안 빈 화면이 뜨는 자리를
 * 대신한다. 준비되면 바로 사라진다.
 *
 * 그림은 이모지 대신 직접 그린다. 앱이 선과 점으로 된 안내판 언어를 쓰고 있어서,
 * 붙여 넣은 그림은 겉돈다. 사람 셋이 노선을 향해 걸어가고, 그 끝에 도착지가 있다.
 */
function Walkers() {
  // 사람은 원 하나와 선 몇 개로 충분하다. 걸음의 각도만 조금씩 다르게 둔다.
  const people = [
    { x: 26, s: 1.0, stride: 7 },
    { x: 58, s: 0.86, stride: -6 },
    { x: 86, s: 0.94, stride: 8 },
  ];
  return (
    <svg className="walk" viewBox="0 0 300 124" role="img"
         aria-label="사람들이 노선을 따라 걸어가는 그림">
      {/* 노선 — 왼쪽 아래에서 오른쪽 위 도착지로 */}
      <path className="route" d="M12 108 C 96 108, 150 84, 196 62 S 262 34, 288 30" />
      <circle className="stop" cx="196" cy="62" r="5" />
      <circle className="stop" cx="252" cy="42" r="5" />
      {/* 도착지는 하나 더 크게, 가운데를 비워서 */}
      <circle className="goal" cx="288" cy="30" r="8" />

      {people.map(({ x, s, stride }, i) => (
        <g key={i} className="walker" style={{ ["--i" as string]: i }}
           transform={`translate(${x} 108) scale(${s})`}>
          <circle cx="0" cy="-34" r="5.4" />
          <path d={`M0 -28 L0 -13`} />
          <path d={`M0 -13 L${-stride} 0`} />
          <path d={`M0 -13 L${stride * 0.8} 0`} />
          <path d={`M0 -24 L${stride * 0.7} -18`} />
        </g>
      ))}
    </svg>
  );
}

export default function Splash({ done }: { done: boolean }) {
  return (
    <div className={`splash${done ? " out" : ""}`} aria-hidden={done}>
      <div className="splash-in">
        <Walkers />
        <b className="mark">
          어디로<span className="q">?</span>
        </b>
        <p>전 세계 대중교통을 한국어로. 63개국 368만 개 정류장.</p>
      </div>
    </div>
  );
}

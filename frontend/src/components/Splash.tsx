/** 들어갈 때 잠깐 보이는 화면.
 *
 * 꾸미려고 두는 게 아니다. 국가 목록을 불러오는 동안 빈 화면이 뜨는 자리를
 * 대신하고, 그 사이에 이 앱이 무엇을 하는지 한 장면으로 보여준다.
 * 준비되면 바로 사라진다 — 일부러 기다리게 하지 않는다.
 */
export default function Splash({ done }: { done: boolean }) {
  return (
    <div className={`splash${done ? " out" : ""}`} aria-hidden={done}>
      <div className="splash-in">
        <b className="mark">어디로 가는 차</b>
        <div className="swap" role="status" aria-label="한국어를 현지 표기로 바꿔 찾습니다">
          <span className="ko">신주쿠</span>
          <span className="to" aria-hidden="true">→</span>
          <span className="orig">新宿</span>
        </div>
        <p>한국어로 찾습니다. 63개국 368만 개 정류장.</p>
      </div>
    </div>
  );
}

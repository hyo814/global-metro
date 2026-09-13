/** 들어갈 때 잠깐 보이는 화면.
 *
 * 꾸미려고 두는 게 아니다. 국가 목록을 불러오는 동안 빈 화면이 뜨는 자리를
 * 대신하고, 그 사이에 이 앱이 무엇을 하는지 보여준다. 준비되면 바로 사라진다.
 *
 * 한 나라를 얼굴로 세우지 않는다. 왼쪽은 전부 한국어, 오른쪽은 문자 체계가
 * 제각각 — "어디든"이 이 앱의 메시지다. 세 쌍 모두 실제로 검색되는 것이다.
 */
const PAIRS: [string, string][] = [
  ["파리 북역", "Gare du Nord"],
  ["아테네 신타그마", "ΣΥΝΤΑΓΜΑ"],
  ["바르셀로나", "Barcelona-Sants"],
];

export default function Splash({ done }: { done: boolean }) {
  return (
    <div className={`splash${done ? " out" : ""}`} aria-hidden={done}>
      <div className="splash-in">
        <b className="mark">어디로 가는 차</b>
        <dl className="pairs" aria-label="한국어로 치면 현지 표기로 찾습니다">
          {PAIRS.map(([ko, orig], i) => (
            <div key={ko} style={{ ["--i" as string]: i }}>
              <dt>{ko}</dt>
              <dd>{orig}</dd>
            </div>
          ))}
        </dl>
        <p>한국어로 치면 현지 표기로 찾습니다. 63개국 368만 개 정류장.</p>
      </div>
    </div>
  );
}

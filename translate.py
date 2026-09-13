#!/usr/bin/env python3
"""정류장·노선 이름을 한글로. 화면에 뜨는 것만 번역하고 영구 캐싱한다.

정류장이 5.7M개라 전부 번역하는 건 말이 안 된다. 실제로 조회되는 건
한 화면당 10개 남짓이고, 한 번 번역하면 SQLite에 영구 저장되므로
같은 이름은 두 번 부르지 않는다.

API 키가 없으면 아무것도 번역하지 않고 조용히 빈 dict를 준다.
호출부는 원문을 그대로 쓰면 된다 — 번역 실패로 앱이 멈추지 않는다.

자체 점검:  python3 translate.py
"""
import json
import os
import pathlib
import sqlite3

from dotenv import load_dotenv

load_dotenv()

# 비용을 줄이려면 "claude-haiku-4-5"로. 다만 75개국 고유명사 음차는
# 생각보다 어려워서 품질 차이가 난다.
MODEL = "claude-opus-5"
DB = pathlib.Path(".cache/translations.db")

SYSTEM = """너는 대중교통 정류장·노선 이름을 한국어로 옮긴다.
한국인 여행자가 현지에서 표지판과 대조하며 쓸 것이다.

규칙:
- 고유명사는 현지 발음에 맞춰 한글로 음차한다. 영어식으로 읽지 않는다.
  Jean Jaurès -> 장 조레스 / 新宿 -> 신주쿠 / Peyrou -> 페루
- 교통 시설을 뜻하는 보통명사는 한국어로 옮긴다.
  Gare, Station, 駅, Estación -> 역 / Transit Center -> 환승센터 / Airport -> 공항
- 널리 쓰이는 한국어 명칭이 이미 있으면 그것을 쓴다. Gare du Nord -> 파리 북역
- 행선지 표기는 어순을 지킨다. "A To B Via C" -> "A → B (C 경유)"
  출발지가 앞, 도착지가 뒤다. 뒤집지 않는다. "To B" 뿐이면 "B 방면".
- 숫자·기호는 살린다. "5th & Monroe" -> "5번가 & 먼로"
- 원문이 이미 한국어면 그대로 둔다.

입력은 JSON 문자열 배열이다. 같은 길이의 JSON 배열로만 답한다.
설명도 코드펜스도 붙이지 않는다."""


def _db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS ko (src TEXT PRIMARY KEY, ko TEXT NOT NULL)")
    return con


def cached(texts):
    if not texts:
        return {}
    with _db() as con:
        q = ",".join("?" * len(texts))
        return dict(con.execute(f"SELECT src, ko FROM ko WHERE src IN ({q})", texts))


def _call_api(texts):
    """캐시에 없는 것만 한 번에 보낸다. 실패하면 빈 dict."""
    import anthropic

    try:
        client = anthropic.Anthropic()
        # effort를 low로 낮추면 안전 분류기가 이 작업을 cyber로 오탐해서 절반쯤
        # refusal이 난다(실측 4회 중 2회). 기본값을 쓴다. 캐싱 덕에 호출량이
        # 적어서 어차피 비용 차이가 거의 없다.
        r = client.beta.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=SYSTEM,
            betas=["server-side-fallback-2026-06-01"],
            fallbacks=[{"model": "claude-opus-4-8"}],   # 그래도 거절되면 자동 재시도
            messages=[{"role": "user",
                       "content": json.dumps(texts, ensure_ascii=False)}],
        )
        if r.stop_reason == "refusal":
            cat = getattr(r.stop_details, "category", None)
            print(f"[번역 거절됨] category={cat} — 원문을 그대로 씁니다")
            return {}
        body = "".join(b.text for b in r.content if b.type == "text").strip()
        if body.startswith("```"):             # 코드펜스를 붙이면 벗겨낸다
            body = body.split("\n", 1)[1].rsplit("```", 1)[0]
        out = json.loads(body)
        if not isinstance(out, list) or len(out) != len(texts):
            return {}
        return {s: str(k).strip() for s, k in zip(texts, out) if str(k).strip()}
    except Exception as e:                     # 번역 실패로 앱을 멈추지 않는다
        print(f"[번역 실패] {type(e).__name__}: {e}")
        return {}


def korean(texts, use_api=True):
    """원문 -> 한글. use_api=False면 캐시에 있는 것만 돌려준다.

    번역 한 번에 5초쯤 걸린다. 검색 응답에서 그걸 기다리면 못 쓴다.
    그래서 검색은 use_api=False로 즉시 답하고, 화면을 그린 뒤에
    /api/translate로 나머지를 채운다.
    """
    uniq = sorted({t.strip() for t in texts if t and t.strip()})
    if not uniq:
        return {}

    hit = cached(uniq)
    missing = [t for t in uniq if t not in hit]
    if not missing or not use_api or not os.getenv("ANTHROPIC_API_KEY"):
        return hit

    fresh = _call_api(missing)
    if fresh:
        with _db() as con:
            con.executemany("INSERT OR REPLACE INTO ko VALUES (?, ?)", fresh.items())
    return {**hit, **fresh}


QUERY_SYSTEM = """사용자가 입력한 대중교통 정류장·역 이름을,
현지에서 실제로 표기되는 형태의 후보로 바꿔라.
입력은 한국어일 수도, 로마자일 수도, 영어 명칭일 수도 있다.

- 현지 문자로 쓴 형태를 반드시 포함한다.
  "신주쿠" -> ["新宿", "新宿駅", "Shinjuku"]
  "Shibuya" -> ["渋谷", "渋谷駅", "Shibuya"]
  "샤를드골" -> ["Charles de Gaulle", "Charles-de-Gaulle"]
- 역·정류장 접미사는 붙인 형태와 뗀 형태를 모두 낸다.
  "도쿄역" -> ["東京駅", "東京", "Tokyo Station"]
- 입력이 이미 현지 표기면 그것도 후보에 넣는다.
- 확실하지 않으면 그럴듯한 후보를 여러 개 낸다. 최대 6개.

어느 나라 지명인지도 함께 판단한다. 확실하지 않으면 country를 ""로 둔다.

{"country": "JP", "names": ["新宿", "新宿駅", "Shinjuku"]} 형태의
JSON 객체로만 답한다. country는 ISO 3166-1 alpha-2 두 글자다.
설명도 코드펜스도 붙이지 않는다."""


def to_original(q):
    """질의 -> (국가코드, 현지 표기 후보들).

    호출부는 인덱스 직접 검색이 0건일 때만 부른다. 그래서 한글뿐 아니라
    로마자도 변환한다 — 일본 피드의 이름은 일본어라 "Shibuya"로는 안 걸린다.

    국가까지 받는 이유: 후보만으로 순위를 매기면 "Tokyo"(5자)가 "東京駅"(3자)를
    이겨서 자카르타의 Tokyo Riverside가 東京駅을 누른다. 글자 수는 문자 체계가
    다르면 비교가 성립하지 않는다. 국가를 알면 한 문자 체계 안에서만 비교하게
    되어 그 문제가 사라진다.
    """
    q = q.strip()
    if len(q) < 2:
        return "", []

    with _db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS q (src TEXT PRIMARY KEY, cands TEXT)")
        hit = con.execute("SELECT cands FROM q WHERE src = ?", (q,)).fetchone()
    if hit:
        got = json.loads(hit[0])
        return got.get("country", ""), got.get("names", [])
    if not os.getenv("ANTHROPIC_API_KEY"):
        return "", []

    import anthropic
    try:
        r = anthropic.Anthropic().beta.messages.create(
            model=MODEL, max_tokens=1000, system=QUERY_SYSTEM,
            betas=["server-side-fallback-2026-06-01"],
            fallbacks=[{"model": "claude-opus-4-8"}],
            messages=[{"role": "user", "content": q}],
        )
        if r.stop_reason == "refusal":
            return "", []
        body = "".join(b.text for b in r.content if b.type == "text").strip()
        if body.startswith("```"):
            body = body.split("\n", 1)[1].rsplit("```", 1)[0]
        got = json.loads(body)
        country = str(got.get("country") or "").strip().upper()[:2]
        names = [str(x).strip() for x in (got.get("names") or []) if str(x).strip()][:6]
    except Exception as e:
        print(f"[질의 변환 실패] {type(e).__name__}: {e}")
        return "", []

    with _db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS q (src TEXT PRIMARY KEY, cands TEXT)")
        con.execute("INSERT OR REPLACE INTO q VALUES (?, ?)",
                    (q, json.dumps({"country": country, "names": names},
                                   ensure_ascii=False)))
    return country, names


def demo():
    samples = ["Jean Jaurès", "Oregon City Transit Center", "Agde Gare SNCF",
               "5th & Monroe", "Britomart To Waikowhai Via Mt Eden"]

    # 캐시 왕복
    with _db() as con:
        con.execute("INSERT OR REPLACE INTO ko VALUES ('__test__', '테스트')")
    assert cached(["__test__"]) == {"__test__": "테스트"}
    with _db() as con:
        con.execute("DELETE FROM ko WHERE src = '__test__'")

    # 키가 없어도 죽지 않아야 한다
    if not os.getenv("ANTHROPIC_API_KEY"):
        assert isinstance(korean(samples), dict), "키 없을 때 dict가 아님"
        print("자체 점검 통과 (ANTHROPIC_API_KEY 없음 — API 경로는 미검증)")
        return

    got = korean(samples)
    assert got, "번역 결과가 비었음"
    for src, ko in got.items():
        assert any("가" <= c <= "힣" for c in ko), f"한글이 없음: {src} -> {ko}"
        print(f"  {src:<38} -> {ko}")
    assert cached(list(got)) == got, "캐시에 저장이 안 됨"
    cc, cands = to_original("신주쿠")
    assert any("新宿" in c for c in cands), f"질의 변환 실패: {cands}"
    assert cc == "JP", f"국가 판정 실패: {cc}"
    print(f"  질의 변환: '신주쿠' -> [{cc}] {cands}")
    cc2, romaji = to_original("Shibuya")
    assert any("渋谷" in c for c in romaji) and cc2 == "JP", f"로마자 변환 실패: {cc2} {romaji}"
    print(f"  질의 변환: 'Shibuya' -> [{cc2}] {romaji}")
    print("자체 점검 통과 (캐시 저장 확인)")


if __name__ == "__main__":
    demo()

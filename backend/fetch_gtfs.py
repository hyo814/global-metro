#!/usr/bin/env python3
"""Mobility Database에서 GTFS zip을 일괄 다운로드한다.

사용법:
    python fetch_gtfs.py                          # 전 세계 전부 (75개국 2838개, ~15.6GB)
    python fetch_gtfs.py --inspect                # 응답 구조만 보고 종료
    python fetch_gtfs.py --country JP             # 일본만 (590개, ~107MB)
    python fetch_gtfs.py --provider BART          # 운영사 이름으로
    python fetch_gtfs.py --limit 5                # 개수 제한 (맛보기)

중단해도 다시 실행하면 이미 받은 파일은 건너뛴다.
한국(KR)은 이 DB에 피드가 없다. TOPIS / data.go.kr을 따로 봐야 한다.
"""
import argparse
import os
import pathlib
import sys
import time

import requests
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent

BASE = "https://api.mobilitydatabase.org/v1"
OUT = ROOT / "gtfs"
MAX_LIMIT = 2500  # API가 이보다 크면 422로 거부한다
DELAY = 0.2       # ponytail: 고정 지연. 429가 실제로 나면 백오프로 올릴 것

load_dotenv(ROOT / ".env")
REFRESH = os.getenv("MOBILITY_REFRESH_TOKEN")


def die(msg):
    print(f"오류: {msg}", file=sys.stderr)
    sys.exit(1)


def get_access_token():
    """리프레시 토큰을 1시간짜리 액세스 토큰으로 교환한다."""
    if not REFRESH:
        die(".env에 MOBILITY_REFRESH_TOKEN이 없습니다.")

    r = requests.post(f"{BASE}/tokens", json={"refresh_token": REFRESH}, timeout=30)
    if r.status_code == 401:
        die("리프레시 토큰이 거부됐습니다. 계정 페이지에서 다시 복사하세요.")
    r.raise_for_status()

    body = r.json()
    if not body.get("access_token"):
        die(f"응답에 액세스 토큰이 없습니다. 받은 키: {list(body)}")
    return body["access_token"]


def fetch_feeds(token, country=None, provider=None, limit=None):
    """offset으로 끝까지 긁는다. 전 세계는 한 번에 안 오고 2500개씩 끊긴다."""
    headers = {"Authorization": f"Bearer {token}"}
    base_params = {}
    if country:
        base_params["country_code"] = country
    if provider:
        base_params["provider"] = provider

    feeds = []
    while True:
        page_size = MAX_LIMIT if limit is None else min(MAX_LIMIT, limit - len(feeds))
        r = requests.get(
            f"{BASE}/gtfs_feeds",
            headers=headers,
            params={**base_params, "limit": page_size, "offset": len(feeds)},
            timeout=300,
        )
        if r.status_code == 401:
            die("액세스 토큰이 만료됐거나 잘못됐습니다.")
        r.raise_for_status()

        page = r.json()
        feeds += page
        if len(page) < page_size or (limit is not None and len(feeds) >= limit):
            break

    # status 파라미터는 서버가 무시한다. 여기서 직접 거른다.
    live = [f for f in feeds if f.get("status") == "active"]
    if len(live) != len(feeds):
        print(f"active 아닌 피드 {len(feeds) - len(live)}개 제외 "
              f"(deprecated/inactive/future)")
    return live


def total_mb(feeds):
    return sum((f.get("latest_dataset") or {}).get("zipped_folder_size_mb") or 0
               for f in feeds)


def download_url(feed):
    """피드에서 zip 주소를 뽑는다. 없으면 None."""
    hosted = (feed.get("latest_dataset") or {}).get("hosted_url")
    if hosted:
        return hosted
    # active 피드 52개는 미러본이 없다. 운영사 원본으로 폴백 (끊길 수 있음)
    return (feed.get("source_info") or {}).get("producer_url")


def filename(feed):
    raw = f"{feed.get('id', 'unknown')}_{feed.get('provider') or ''}"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in raw)
    return safe[:80].strip("_") + ".zip"


def download(url, path):
    """스트리밍으로 받아 .part에 쓰고 다 받은 뒤에 이름을 바꾼다.

    중간에 끊겨도 멀쩡한 zip처럼 보이는 파일이 남지 않는다.
    """
    tmp = path.with_suffix(".part")
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
    tmp.rename(path)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--country", help="ISO 국가코드. 예: JP, FR, US. 생략하면 전 세계")
    p.add_argument("--provider", help="운영사 이름 일부. 예: BART")
    p.add_argument("--limit", type=int, help="가져올 피드 수. 생략하면 제한 없음")
    p.add_argument("--inspect", action="store_true",
                   help="첫 피드의 원본 JSON만 출력하고 종료")
    p.add_argument("--yes", action="store_true", help="용량 확인 프롬프트 생략")
    args = p.parse_args()

    token = get_access_token()
    feeds = fetch_feeds(token, args.country, args.provider, args.limit)
    mb = total_mb(feeds)
    print(f"피드 {len(feeds)}개 / 약 {mb:,.0f} MB")

    if args.inspect:
        import json
        print(json.dumps(feeds[0] if feeds else {}, indent=2, ensure_ascii=False))
        return

    if mb > 1024 and not args.yes:
        answer = input(f"{mb / 1024:.1f} GB를 받습니다. 계속할까요? [y/N] ")
        if answer.strip().lower() != "y":
            sys.exit("취소됨.")

    OUT.mkdir(exist_ok=True)
    ok = skipped = failed = 0
    total = len(feeds)

    for i, feed in enumerate(feeds, 1):
        path = OUT / filename(feed)
        label = feed.get("provider") or feed.get("id")

        if path.exists():
            skipped += 1
            continue

        url = download_url(feed)
        if not url:
            print(f"[{i}/{total}] [주소없음] {label}")
            failed += 1
            continue

        try:
            print(f"[{i}/{total}] {label}")
            download(url, path)
            ok += 1
        except requests.HTTPError as e:
            print(f"[{i}/{total}] [실패] {label}: {e}")
            failed += 1
        except requests.RequestException as e:
            print(f"[{i}/{total}] [네트워크] {label}: {e}")
            failed += 1

        time.sleep(DELAY)  # 429 방지

    print(f"\n완료 {ok} / 건너뜀 {skipped} / 실패 {failed}")
    print(f"저장 위치: {OUT.resolve()}")


if __name__ == "__main__":
    main()

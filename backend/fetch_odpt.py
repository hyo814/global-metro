#!/usr/bin/env python3
"""ODPT(공공교통 오픈데이터센터)에서 GTFS를 받는다. 도쿄메트로·도에이 등.

Mobility Database에는 일본 철도가 거의 없다(590개 JP 피드의 92%가 버스).
도쿄메트로는 여기에만 있다. developer.odpt.org 에서 무료 가입 후 관리자
승인을 받으면 consumer key가 나온다.

    .env 에  ODPT_CONSUMER_KEY=...  추가

    python3 fetch_odpt.py --probe      # 키와 URL 규칙이 맞는지만 확인
    python3 fetch_odpt.py              # 알려진 피드 전부
    python3 fetch_odpt.py --only Metro

검증 안 된 코드다. URL 규칙은 문서에서 확인했지만 실제 응답으로 돌려본 적이
없다. 그래서 --probe 를 먼저 돌려 가정이 맞는지 보라. 틀리면 조용히 넘어가지
않고 무엇이 어긋났는지 말하도록 짰다.
"""
import argparse
import os
import pathlib
import sys
import zipfile

import requests
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OUT = ROOT / "gtfs"
BASE = "https://api.odpt.org/api/v4/files"
KEY = os.getenv("ODPT_CONSUMER_KEY")

# (운영사, 파일명, 사람이 읽을 이름). 카탈로그에서 확인한 GTFS 제공 사업자다.
# 파일명이 틀리면 404가 나므로 --probe 로 먼저 확인할 것.
FEEDS = [
    ("TokyoMetro", "TokyoMetro-Train-GTFS", "도쿄메트로"),
    ("Toei", "ToeiBus-GTFS", "도에이버스"),
    ("Toei", "ToeiTrain-GTFS", "도에이지하철"),
    ("TWR", "TWR-Train-GTFS", "린카이선"),
    ("MIR", "MIR-Train-GTFS", "쓰쿠바익스프레스"),
    ("TamaMonorail", "TamaMonorail-Train-GTFS", "다마모노레일"),
]

NEEDED = {"agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"}


def die(msg):
    print(f"오류: {msg}", file=sys.stderr)
    sys.exit(1)


def url_for(operator, name):
    return f"{BASE}/{operator}/data/{name}.zip?acl:consumerKey={KEY}"


def grab(operator, name, label, out_dir):
    """하나 받아서 GTFS zip인지 확인하고 저장한다. 성공하면 경로, 아니면 None."""
    r = requests.get(url_for(operator, name), timeout=300)
    if r.status_code == 401 or r.status_code == 403:
        die(f"{label}: 키가 거부됐습니다({r.status_code}). 승인이 끝났는지 확인하세요.")
    if r.status_code == 404:
        print(f"  [없음] {label}: {operator}/{name}.zip 이 없습니다. 파일명이 바뀌었을 수 있습니다.")
        return None
    r.raise_for_status()

    tmp = out_dir / f"odpt-{operator}_{label}.zip.part"
    tmp.write_bytes(r.content)
    try:
        with zipfile.ZipFile(tmp) as z:
            missing = NEEDED - set(z.namelist())
    except zipfile.BadZipFile:
        head = r.content[:120]
        tmp.unlink(missing_ok=True)
        print(f"  [실패] {label}: zip이 아닙니다. 받은 내용 앞부분: {head!r}")
        return None
    if missing:
        tmp.unlink(missing_ok=True)
        print(f"  [실패] {label}: GTFS 필수 파일이 없습니다 — {sorted(missing)}")
        return None

    final = tmp.with_suffix("")
    tmp.replace(final)
    return final


def probe():
    """키와 URL 규칙만 확인한다. 하나만 받아보고 무엇이 왔는지 그대로 보여준다."""
    operator, name, label = FEEDS[0]
    shown = url_for(operator, name).replace(KEY or "", "<키>")
    print(f"요청: {shown}")
    r = requests.get(url_for(operator, name), timeout=120)
    print(f"  HTTP {r.status_code} · {r.headers.get('content-type')} · {len(r.content):,} bytes")
    if r.status_code != 200:
        print(f"  본문 앞부분: {r.content[:300]!r}")
        print("\n  401/403이면 승인 대기 중이거나 키가 틀린 것입니다.")
        print("  404면 운영사/파일 이름이 다른 것입니다 — ckan.odpt.org 에서 확인하세요.")
        return
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(r.content)) as z:
            names = sorted(z.namelist())
        print(f"  zip 내용: {names[:12]}")
        print(f"  GTFS 필수 파일 모두 있음: {'예' if NEEDED <= set(names) else '아니오'}")
        print("\n  여기까지 나오면 URL 규칙이 맞습니다. 그냥 실행하세요: python3 fetch_odpt.py")
    except zipfile.BadZipFile:
        print(f"  zip이 아닙니다. 앞부분: {r.content[:300]!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="키·URL 규칙만 확인하고 종료")
    ap.add_argument("--only", help="운영사 이름 일부로 거르기")
    args = ap.parse_args()

    if not KEY:
        die(".env 에 ODPT_CONSUMER_KEY 가 없습니다. developer.odpt.org 에서 발급하세요.")
    if args.probe:
        return probe()

    OUT.mkdir(exist_ok=True)
    ok = 0
    todo = [f for f in FEEDS if not args.only or args.only.lower() in f[0].lower()]
    for operator, name, label in todo:
        print(f"[받는중] {label}")
        if grab(operator, name, label, OUT):
            ok += 1

    print(f"\n완료 {ok} / {len(todo)}")
    if ok:
        print("이제 인덱스를 다시 만드세요: python3 build_index.py")


if __name__ == "__main__":
    main()

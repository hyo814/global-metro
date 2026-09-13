#!/usr/bin/env python3
"""파비콘을 만든다. 표준 라이브러리만 쓴다.

아이콘 하나 만들자고 이미지 라이브러리를 깔 이유는 없다. PNG는 zlib으로
직접 쓰고, ICO는 그 PNG들을 담는 container일 뿐이다.

모양은 앱의 타임라인 모티프 — 세로 노선 위의 정류장 하나. 16px에서 읽히도록
형태를 둘로만 제한했다. 가장자리는 4배로 그린 뒤 줄여서 계단을 없앤다.

    python3 frontend/make_icons.py
"""
import pathlib
import struct
import zlib

OUT = pathlib.Path(__file__).resolve().parent / "public"
SS = 4                                   # 4배로 그린 뒤 줄인다(안티에일리어싱)

BG = (0x0d, 0x14, 0x20)                  # 안내판 바탕
LINE = (0x2f, 0xd4, 0x7a)                # 노선
DOT = (0xee, 0xf2, 0xf7)                 # 정류장


def canvas(n):
    return [[(0, 0, 0, 0)] * n for _ in range(n)]


def over(dst, src):
    """src를 dst 위에 알파 합성."""
    sr, sg, sb, sa = src
    if sa == 255:
        return src
    if sa == 0:
        return dst
    dr, dg, db, da = dst
    a = sa + da * (255 - sa) // 255
    if a == 0:
        return (0, 0, 0, 0)
    f = lambda s, d: (s * sa + d * da * (255 - sa) // 255) // a
    return (f(sr, dr), f(sg, dg), f(sb, db), a)


def round_rect(img, n, r, color):
    rr = r * n
    for y in range(n):
        for x in range(n):
            cx = min(max(x + 0.5, rr), n - rr)
            cy = min(max(y + 0.5, rr), n - rr)
            if (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= rr * rr:
                img[y][x] = over(img[y][x], (*color, 255))


def bar(img, n, x0, y0, x1, y1, color):
    for y in range(int(y0 * n), int(y1 * n)):
        for x in range(int(x0 * n), int(x1 * n)):
            if 0 <= y < n and 0 <= x < n:
                img[y][x] = over(img[y][x], (*color, 255))


def disc(img, n, cx, cy, r, color):
    px, py, pr = cx * n, cy * n, r * n
    for y in range(max(0, int(py - pr - 1)), min(n, int(py + pr + 2))):
        for x in range(max(0, int(px - pr - 1)), min(n, int(px + pr + 2))):
            if (x + 0.5 - px) ** 2 + (y + 0.5 - py) ** 2 <= pr * pr:
                img[y][x] = over(img[y][x], (*color, 255))


def draw(n):
    """단위 사각형 기준으로 그린다. 형태는 둘뿐 — 노선과 점."""
    img = canvas(n)
    round_rect(img, n, 0.22, BG)
    bar(img, n, 0.455, 0.17, 0.545, 0.83, LINE)
    disc(img, n, 0.5, 0.5, 0.20, DOT)
    return img


def shrink(img, n, k):
    """k배로 그린 것을 줄인다. 평균을 내면 가장자리가 부드러워진다."""
    m = n // k
    out = canvas(m)
    for y in range(m):
        for x in range(m):
            acc = [0, 0, 0, 0]
            for dy in range(k):
                for dx in range(k):
                    p = img[y * k + dy][x * k + dx]
                    a = p[3]
                    acc[0] += p[0] * a; acc[1] += p[1] * a
                    acc[2] += p[2] * a; acc[3] += a
            a = acc[3]
            out[y][x] = (0, 0, 0, 0) if a == 0 else (
                acc[0] // a, acc[1] // a, acc[2] // a, a // (k * k))
    return out


def png(img, n):
    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in img)
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def ico(pngs):
    """ICO는 PNG를 담는 container다. Vista 이후 모든 브라우저가 읽는다."""
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, blob = b"", b""
    for n, data in pngs:
        entries += struct.pack("<BBBBHHII", n if n < 256 else 0, n if n < 256 else 0,
                               0, 0, 1, 32, len(data), offset)
        blob += data
        offset += len(data)
    return head + entries + blob


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    made = []
    for n in (16, 32, 48, 64, 128, 180, 256):
        img = shrink(draw(n * SS), n * SS, SS)
        data = png(img, n)
        made.append((n, data))
        if n in (32, 180, 256):
            name = {32: "favicon-32.png", 180: "apple-touch-icon.png",
                    256: "icon-256.png"}[n]
            (OUT / name).write_bytes(data)

    (OUT / "favicon.ico").write_bytes(
        ico([(n, d) for n, d in made if n in (16, 32, 48, 64)]))

    # 최신 브라우저는 SVG를 먼저 쓴다. 같은 모양을 벡터로.
    (OUT / "favicon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<rect width="100" height="100" rx="22" fill="#0d1420"/>'
        '<rect x="45.5" y="17" width="9" height="66" fill="#2fd47a"/>'
        '<circle cx="50" cy="50" r="20" fill="#eef2f7"/></svg>\n')

    for f in sorted(OUT.iterdir()):
        print(f"  {f.name:<22} {f.stat().st_size:>7,} bytes")


if __name__ == "__main__":
    main()

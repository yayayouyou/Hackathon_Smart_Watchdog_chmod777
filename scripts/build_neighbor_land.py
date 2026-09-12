"""鄰縣市的陸地輪廓 — 讓「新北以外反灰」只反灰陸地，不反灰海。

    python run.py neighbor-land          # 或
    PYTHONPATH=src .venv/Scripts/python scripts/build_neighbor_land.py

輸出 `data/external/tw_neighbor_land.json`，前端由 `GET /api/land` 取得。

**為什麼需要這份檔案。** 原本的反灰是「整個世界當外框、新北當洞」的
even-odd 多邊形，一次蓋掉畫面上除了新北以外的所有東西——包含海。海被蓋成
一片死灰之後，新北是個沿海城市這件事就看不出來了，淡水河口、北海岸、東北角
全部沒入背景。要讓海留著，就必須知道哪裡是陸地，而那是資料問題，不是樣式
問題：把鄰近縣市的**陸地面**畫成灰色，海自然就留在底圖原本的顏色。

來源與 `ntpc_town_boundary.json` 同一份（內政部鄉鎮市區界線，
`kiang/taiwan_basecode` → `city/topo/20230317.json`），不新增外部相依。

處理：
  1. TopoJSON 解量化還原座標。
  2. **以 arc 為單位做聯集**：TopoJSON 的相鄰邊界共用同一條 arc，同一縣市內
     被用到兩次的 arc 就是內部的鄉鎮界，丟掉；剩下的接成縣市外框。這比在座標
     層面比對浮點數可靠——共用的是同一個物件，不是兩份剛好相等的數字。
  3. Douglas–Peucker 簡化，eps = 0.0006°（約 60 公尺）。這些是背景形狀，
     比新北自己的區界（0.00035）可以更鬆。
  4. 只留與地圖可視範圍相交的縣市；台灣其他地方永遠不會出現在畫面上。
"""

from __future__ import annotations

import json
import math
import pathlib
import sys
import urllib.request
from collections import Counter, defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.console import use_utf8

use_utf8()

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_URL = ("https://raw.githubusercontent.com/kiang/taiwan_basecode/master/"
           "city/topo/20230317.json")
OUT = ROOT / "data/external/tw_neighbor_land.json"
CACHE = ROOT / "data/external/.cache_tw_towns_20230317.topo.json"

EXCLUDE = "新北市"
# 地圖被 maxBounds 綁在新北附近，這個框之外的縣市不可能入鏡。放寬一點，
# 免得使用者把視窗拉得很寬時邊緣露出沒上色的陸地。
VIEW = (120.6, 24.2, 122.6, 25.8)   # lon0, lat0, lon1, lat1
EPS = 0.0006
MIN_AREA = 2e-6      # 度²，約 25 公頃；再小的環是外島礁石與簡化雜訊


def fetch() -> dict:
    if CACHE.exists():
        print(f"用快取 {CACHE.name}")
        return json.loads(CACHE.read_text(encoding="utf-8"))
    print(f"下載 {SRC_URL}")
    with urllib.request.urlopen(SRC_URL, timeout=180) as r:
        blob = r.read().decode("utf-8")
    CACHE.write_text(blob, encoding="utf-8")
    return json.loads(blob)


def decode_arcs(topo: dict) -> list[list[tuple[float, float]]]:
    sx, sy = topo["transform"]["scale"]
    tx, ty = topo["transform"]["translate"]
    out = []
    for arc in topo["arcs"]:
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append((x * sx + tx, y * sy + ty))
        out.append(pts)
    return out


def ring_arc_lists(geom: dict) -> list[list[int]]:
    """把 Polygon／MultiPolygon 攤平成一串「環＝有號 arc 索引列表」。"""
    if geom["type"] == "Polygon":
        return list(geom["arcs"])
    if geom["type"] == "MultiPolygon":
        return [ring for poly in geom["arcs"] for ring in poly]
    raise ValueError(f"未預期的幾何型別：{geom['type']}")


def union(arc_lists: list[list[int]], arcs: list) -> list[list[tuple]]:
    """同一縣市內用到兩次的 arc 是鄉鎮界，丟掉；其餘接成外框環。"""
    used_n = Counter()
    for ring in arc_lists:
        for i in ring:
            used_n[i if i >= 0 else ~i] += 1

    segs: dict[int, list[tuple]] = {}
    adj: dict[tuple, list[int]] = defaultdict(list)
    for ring in arc_lists:
        for i in ring:
            if used_n[i if i >= 0 else ~i] != 1:
                continue
            pts = arcs[~i][::-1] if i < 0 else arcs[i]
            n = len(segs)
            segs[n] = pts
            adj[pts[0]].append(n)

    taken: set[int] = set()
    rings = []
    for n in segs:
        if n in taken:
            continue
        ring = list(segs[n])
        taken.add(n)
        while ring[0] != ring[-1]:
            nxt = [m for m in adj.get(ring[-1], []) if m not in taken]
            if not nxt:
                break                      # 接不回去：資料破洞，丟掉這一圈
            taken.add(nxt[0])
            ring.extend(segs[nxt[0]][1:])
        else:
            rings.append(ring)
    return rings


def simplify(pts: list[tuple], eps: float) -> list[tuple]:
    """封閉環的 Douglas–Peucker。

    環的頭尾是同一點，直接丟進 DP 會退化：基線長度為零，所有頂點到它的距離
    都算成 0，整圈被砍成兩點（這個 bug 真的發生過，桃園市 15,372 → 2）。
    先取離起點最遠的頂點當第二個錨，分兩段各做一次。
    """
    if len(pts) < 4:
        return pts
    if pts[0] == pts[-1]:
        x0, y0 = pts[0]
        piv = max(range(1, len(pts) - 1),
                  key=lambda i: (pts[i][0] - x0) ** 2 + (pts[i][1] - y0) ** 2)
        return _dp(pts[:piv + 1], eps)[:-1] + _dp(pts[piv:], eps)
    return _dp(pts, eps)


def _dp(pts: list[tuple], eps: float) -> list[tuple]:
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        (x1, y1), (x2, y2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy) or 1e-12
        far, dmax = -1, 0.0
        for i in range(a + 1, b):
            x, y = pts[i]
            d = abs(dy * (x - x1) - dx * (y - y1)) / norm
            if d > dmax:
                far, dmax = i, d
        if dmax > eps:
            keep[far] = True
            stack.append((a, far))
            stack.append((far, b))
    return [p for p, k in zip(pts, keep) if k]


def area(ring: list[tuple]) -> float:
    s = 0.0
    for i, (x1, y1) in enumerate(ring):
        x2, y2 = ring[(i + 1) % len(ring)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def intersects_view(rings: list[list[tuple]]) -> bool:
    lo_x, lo_y, hi_x, hi_y = VIEW
    for ring in rings:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        if max(xs) >= lo_x and min(xs) <= hi_x and max(ys) >= lo_y and min(ys) <= hi_y:
            return True
    return False


def main() -> None:
    topo = fetch()
    arcs = decode_arcs(topo)
    towns = topo["objects"][next(iter(topo["objects"]))]["geometries"]

    by_county: dict[str, list[list[int]]] = defaultdict(list)
    for t in towns:
        county = t["properties"]["COUNTYNAME"]
        if county == EXCLUDE:
            continue
        by_county[county].extend(ring_arc_lists(t))

    out = []
    for county, arc_lists in sorted(by_county.items()):
        rings = [r for r in union(arc_lists, arcs) if area(r) >= MIN_AREA]
        if not rings or not intersects_view(rings):
            continue
        poly = []
        for ring in rings:
            s = simplify(ring, EPS)
            if len(s) > 1 and s[0] == s[-1]:
                s = s[:-1]                 # 前端的 polygon 會自己閉合
            if len(s) >= 3 and area(s) >= MIN_AREA:
                poly.append([[round(x, 5), round(y, 5)] for x, y in s])
        if poly:
            out.append({"c": county, "poly": poly})
            n_in = sum(len(r) for r in rings)
            n_out = sum(len(r) for r in poly)
            print(f"  {county:<6} 環 {len(poly):>2}　頂點 {n_in:>6} → {n_out:>5}")

    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"✓ {OUT} （{len(out)} 個縣市，{OUT.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()

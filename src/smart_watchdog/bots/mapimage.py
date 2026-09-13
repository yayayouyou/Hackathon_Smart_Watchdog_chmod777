"""把「人力先派哪一區」與本批建議查核名單畫成一張手機直立的 PNG。

為什麼是伺服器端畫圖，而不是在聊天室裡丟一個網址：

* 網址要 HTTPS 才能在手機上正常開（PWA 那一節講過），會場不一定有通道。
* 聊天室裡的圖片**一眼就看完**；網址要點、要等、要登入。長官在電梯裡問「先派哪」，
  要的是一張圖，不是一個連結。

圖上只有兩層資訊，都與派工台同一套定義：

* **底色**：各行政區進入全市前 100 名的家數，級距與色階同地圖室的 `--c1~c4`。
* **紅點＋編號**：本批建議查核名單，編號＝隨圖送出的清單序號。

⚠️ 用詞界線照樣適用：頁尾那句「建議查核的優先序，非違法認定」是畫進圖裡的，
不是放在圖說——圖片會被轉傳，圖說不會跟著走。
"""

from __future__ import annotations

import collections
import functools
import io
import math
import pathlib

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1350        # 4:5 直立：Telegram 與 LINE 的預覽都不裁切這個比例
SS = 2                   # 超取樣：先畫兩倍大再縮回來，多邊形邊緣才不會鋸齒

#: (最少家數, 顏色, 圖例文字)。與 style.css 的 --c4 ~ --c1 同值。
LEVELS = ((9, "#B23A2F", "9 家以上"), (6, "#C9756D", "6–8 家"),
          (3, "#DCA6A1", "3–5 家"), (1, "#ECCECB", "1–2 家"))
ZERO = "#E3EAEE"
INK, INK3, SEAL, PAPER, RULE = "#14202A", "#5F7080", "#B23A2F", "#F4F7F9", "#CFD9E0"

FONT_CANDIDATES = {
    False: (r"C:\Windows\Fonts\msjh.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "/System/Library/Fonts/PingFang.ttc"),
    True: (r"C:\Windows\Fonts\msjhbd.ttc",
           "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
           "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
           r"C:\Windows\Fonts\msjh.ttc",
           "/System/Library/Fonts/PingFang.ttc"),
}


def _truetype(path: str, size: int) -> ImageFont.FreeTypeFont:
    """載入字型；Noto CJK 的 .ttc 要挑**繁中**那一套。

    `NotoSansCJK-*.ttc` 一個檔裡同時裝著日、韓、簡、繁、港五套字形，Pillow 不指定
    index 就拿第 0 套——日文。「產」「說」「查」這類字的日文字形跟繁中不同，圖在
    台灣的長官手機上看起來會「怪怪的」，而且沒有任何錯誤訊息。依字族名稱挑，
    不寫死 index：不同版本的 .ttc 排列順序不保證一樣。
    """
    if path.endswith(".ttc") and "NotoSansCJK" in path:
        for index in range(12):
            try:
                face = ImageFont.truetype(path, size, index=index)
            except OSError:
                break
            if face.getname()[0].endswith("TC"):
                return face
    return ImageFont.truetype(path, size)


@functools.lru_cache(maxsize=32)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES[bold]:
        if pathlib.Path(path).exists():
            return _truetype(path, size)
    # 沒有中文字型的機器（精簡容器）會變成方框字。圖照樣產得出來，
    # 但部署時要裝 fonts-noto-cjk——那是字型問題，不該讓 bot 整個不能用。
    return ImageFont.load_default(size=size)


def _level(n: int) -> str:
    for low, colour, _ in LEVELS:
        if n >= low:
            return colour
    return ZERO


def _centroid(ring: list) -> tuple[float, float]:
    """多邊形重心（shoelace）。算不出面積時退回頂點平均。"""
    a = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(a) < 1e-12:
        return (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))
    return cx / (3 * a), cy / (3 * a)


def render(points: list[dict], boundary: list[dict], proposal: list[dict], *,
           top: int = 100, as_of: str = "") -> bytes:
    s = SS
    img = Image.new("RGB", (W * s, H * s), PAPER)
    d = ImageDraw.Draw(img)

    # ── 標題 ──
    d.text((56 * s, 40 * s), "小小守護員｜建議查核分布", font=_font(48 * s, True), fill=INK)
    d.text((56 * s, 106 * s),
           f"底色＝各區進入全市前 {top} 名的家數　紅點＝本批建議查核 {len(proposal)} 家",
           font=_font(27 * s), fill=INK3)

    # ── 地圖 ──
    xs = [p[0] for f in boundary for ring in f["poly"] for p in ring]
    ys = [p[1] for f in boundary for ring in f["poly"] for p in ring]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    k = math.cos(math.radians((y0 + y1) / 2))
    bx0, by0, bx1, by1 = 40 * s, 168 * s, (W - 40) * s, (H - 236) * s
    scale = min((bx1 - bx0) / ((x1 - x0) * k), (by1 - by0) / (y1 - y0))
    ox = bx0 + ((bx1 - bx0) - (x1 - x0) * k * scale) / 2
    oy = by0 + ((by1 - by0) - (y1 - y0) * scale) / 2

    def xy(lon: float, lat: float) -> tuple[float, float]:
        return ox + (lon - x0) * k * scale, oy + (y1 - lat) * scale

    counts = collections.Counter(p["d"] for p in points if p.get("r", 10**9) <= top)
    for f in boundary:
        colour = _level(counts.get(f["d"], 0))
        for ring in f["poly"]:
            d.polygon([xy(x, y) for x, y in ring], fill=colour, outline="#FFFFFF", width=3 * s)

    # 家數 6 以上的區才標名字：全標 29 個區會蓋掉紅點，而那才是這張圖的主角。
    label_font = _font(26 * s, True)
    for f in boundary:
        if counts.get(f["d"], 0) < 6:
            continue
        ring = max(f["poly"], key=len)
        cx, cy = xy(*_centroid(ring))
        d.text((cx, cy), f["d"].removesuffix("區"), font=label_font, fill=INK,
               anchor="mm", stroke_width=5 * s, stroke_fill="#FFFFFF")

    # 紅點倒著畫，讓 1 號疊在最上面。
    num_font = _font(21 * s, True)
    for i, p in reversed(list(enumerate(proposal, 1))):
        cx, cy = xy(p["x"], p["y"])
        r = 19 * s
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=SEAL, outline="#FFFFFF", width=4 * s)
        d.text((cx, cy), str(i), font=num_font, fill="#FFFFFF", anchor="mm")

    # ── 圖例（放不下就換行） ──
    legend_font = _font(26 * s)
    lx, ly = 56 * s, (H - 214) * s
    for _, colour, label in (*LEVELS, (0, ZERO, f"未進前 {top} 名")):
        width = 40 * s + 12 * s + d.textlength(label, font=legend_font) + 34 * s
        if lx + width > (W - 40) * s:
            lx, ly = 56 * s, ly + 42 * s
        d.rounded_rectangle([lx, ly, lx + 40 * s, ly + 26 * s], 5 * s,
                            fill=colour, outline=RULE)
        d.text((lx + 52 * s, ly + 13 * s), label, font=legend_font, fill=INK, anchor="lm")
        lx += width

    # ── 頁尾：界線寫進圖裡，轉傳時才會跟著走 ──
    d.text((56 * s, (H - 118) * s), "本圖為建議查核的優先序，非違法認定。",
           font=_font(31 * s, True), fill=SEAL)
    tail = f"全市 {len(points):,} 園"
    if as_of:
        tail = f"資料截至 {as_of}　·　" + tail
    d.text((56 * s, (H - 72) * s), tail, font=_font(26 * s), fill=INK3)

    out = io.BytesIO()
    img.resize((W, H), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()

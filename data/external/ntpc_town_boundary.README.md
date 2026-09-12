# ntpc_town_boundary.json

新北市 29 個行政區界線，供前端離線繪圖用。

- 來源：`kiang/taiwan_basecode` → `city/topo/20230317.json`（內政部鄉鎮市區界線，
  TopoJSON），取 `COUNTYNAME == "新北市"` 的 geometry。
- 處理：TopoJSON 解量化 → Douglas–Peucker 簡化，eps = 0.00035°（約 35 公尺）。
  頂點 107,855 → 4,744（4%），檔案 97 KB。
- 格式：`[{"d": "板橋區", "poly": [[[lon, lat], ...], ...]}, ...]`
  一個行政區可有多個環（離島、飛地）。

**為什麼要簡化並進版控**：發布後的前端受 CSP 限制，不能向外取圖磚或 GeoJSON，
必須內嵌。原始 10 萬頂點會讓頁面肥大且渲染卡頓，簡化到 4% 後在螢幕尺度上
肉眼無差別。重建指令見 `scripts/build_frontend.py` 的 docstring。

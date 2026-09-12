"""Build the console: analysis outputs -> payload -> self-contained page.

    PYTHONPATH=src .venv/bin/python scripts/build_frontend.py

Writes ``dist/data/payload.json`` and ``dist/index.html``. The HTML is the
template in ``frontend/index.html`` with the payload substituted in, because a
published Artifact's CSP blocks fetch/XHR and cannot load a sibling JSON file.

The split is deliberate and survives that constraint:

    src/smart_watchdog/api/payload.py   what the data means      (contract)
    frontend/index.html                 how it is rendered       (view)
    this script                         how they are joined      (build)

``frontend/index.html`` never contains data and never reads a CSV. To serve the
same console from a live API at the finals, point the front end at
``GET /api/payload`` instead of the inlined constant -- the payload shape is
identical, so nothing else changes.

Prerequisites (run these first if the tables are stale)::

    scripts/run_compliance_checks.py
    scripts/check_reserve_timeseries.py
    scripts/build_audit_priority.py

The district boundary is pinned in data/external/ntpc_town_boundary.json; see
its README for provenance and the simplification that produced it.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.console import use_utf8

use_utf8()

from smart_watchdog.api.payload import (
    benchmarks,
    build_payload,
    district_summary,
    dossiers,
    institution_points,
    realtime,
)
from smart_watchdog.features.compliance import is_opening_year

ROOT = pathlib.Path(".")
TEMPLATE = ROOT / "frontend/index.html"
DIST = ROOT / "dist"
MARKER = "/*__PAYLOAD__*/"


def _coords() -> dict[str, tuple[float, float]]:
    geo = json.loads((ROOT / "data/external/preschools.json").read_text(encoding="utf-8"))
    out = {}
    for f in geo["features"]:
        p = f["properties"]
        if p.get("city") != "新北市":
            continue
        c = (f.get("geometry") or {}).get("coordinates")
        if c:
            out[p["id"]] = (round(c[0], 5), round(c[1], 5))
    return out


def main() -> None:
    coords = _coords()
    priority = pd.read_csv("data/processed/audit_priority_ntpc.csv")
    missing = (~priority["id"].isin(coords)).sum()
    if missing:
        print(f"⚠️ {missing} 園無座標，將不會出現在地圖上")

    points = institution_points(priority, coords)
    districts = district_summary(points)
    dossier = dossiers(
        ROOT / "data/extracted/nonprofit",
        pd.read_csv("data/processed/nonprofit_registry_crosswalk.csv"),
        pd.read_csv("data/processed/compliance_findings.csv"),
        pd.read_csv("data/processed/reserve_timeseries.csv"),
        is_opening_year,
    )
    bench = benchmarks(dossier)
    boundary = json.loads(
        (ROOT / "data/external/ntpc_town_boundary.json").read_text(encoding="utf-8")
    )
    panel = {}
    sweep_csv = ROOT / "data/processed/realtime_mentions_ntpc.csv"
    sweep_stamp = ROOT / "data/processed/realtime_sweep.json"
    if sweep_csv.exists() and sweep_stamp.exists():
        panel = realtime(pd.read_csv(sweep_csv),
                         json.loads(sweep_stamp.read_text(encoding="utf-8")))
    else:
        print("⚠️ 尚未執行 scripts/run_realtime_sweep.py，即時面板將為空")

    payload = build_payload(points=points, districts=districts, dossier=dossier,
                            bench=bench, boundary=boundary, realtime_panel=panel)

    (DIST / "data").mkdir(parents=True, exist_ok=True)
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    (DIST / "data/payload.json").write_text(blob, encoding="utf-8")

    html = TEMPLATE.read_text(encoding="utf-8")
    if MARKER not in html:
        sys.exit(f"{TEMPLATE} 缺少 {MARKER} 佔位符")
    # U+2028/2029 are valid in JSON strings but terminate a JavaScript line, so a
    # payload carrying one would silently blank the page once inlined.
    for ch, name in ((" ", "U+2028"), (" ", "U+2029")):
        if ch in blob:
            sys.exit(f"payload 含 {name}，內嵌後會中斷 JS 解析")
    if "</script" in blob.lower():
        sys.exit("payload 含 </script，內嵌後會提早關閉腳本區塊")
    (DIST / "index.html").write_text(html.replace(MARKER, blob), encoding="utf-8")

    kb = (DIST / "index.html").stat().st_size // 1024
    print(f"payload  {len(blob) // 1024} KB　schema v{payload['schema_version']}")
    print(f"  園 {len(points)}　行政區 {len(districts)}　卷宗 {len(dossier)} 所")
    print(f"  有財報 {sum(p['fin'] for p in points)}　"
          f"法遵未通過 {sum(1 for p in points if p['cf'])}　"
          f"近 90 日事件 {sum(1 for p in points if p['e90'])}")
    print(f"  員工基準：每人人事費中位 {bench['ph_med']:,}／IQR 外 {bench['ph_outliers']} 份"
          f"／師生比中位 1:{bench['ratio_med']}")
    print(f"  附註三可見人事補助的報告 {bench['n_subsidised']}/{bench['n_reports']} 份")
    if panel:
        print(f"  即時面板：{panel['channels_live']}/{panel['channels_total']} 管道，"
              f"{sum(len(v) for v in panel['by_institution'].values())} 則，"
              f"涉及 {len(panel['by_institution'])} 所園（掃描於 {panel['swept_at']}）")
    print(f"\nwrote {DIST}/index.html  ({kb} KB)")


if __name__ == "__main__":
    main()

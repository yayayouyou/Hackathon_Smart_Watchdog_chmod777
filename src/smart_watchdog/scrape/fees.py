"""Fetch and flatten 收費明細 (fee schedules) for 幼兒園.

Why this matters: 第43條 (費用超收) and 第38條 (收費未報備查/超收) together account
for 200 of 新北市's 1,457 penalty records -- the second largest violation family
after staffing. Detecting it needs the declared fee schedule, which is exactly what
these files carry.

Each slip nests as
    slip[age_group][semester]["class"][class_type][fee_item] = {收費期間, 單價, 小計}

and carries its own checksum: 全學期總收費 equals the sum of the per-semester fee
items (學費 + 雜費 + 材料費 + 活動費 + 午餐費 + 點心費), while 交通費 / 課後延托費 /
家長會費 are optional add-ons quoted separately and excluded from that total.
Verified on 南山附設私立員工幼兒園 113 全日班: 15,000 + 15,250 + 4,250 + 20,000 +
7,500 + 6,000 = 68,000 = the declared 全學期總收費.

The mirror's ``has_slip`` property is **not** a reliable existence flag -- a 園
marked ``"no"`` can still have a published slip (南山 above is one), so the file
list is taken from the repository tree rather than inferred from that field.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import urllib.error
import urllib.parse
import urllib.request

MIRROR = "https://kiang.github.io/ap.ece.moe.edu.tw"
TREE_API = "https://api.github.com/repos/kiang/ap.ece.moe.edu.tw/git/trees"

# Fee items that make up 全學期總收費.
CORE_ITEMS = ("學費", "雜費", "材料費", "活動費", "午餐費", "點心費")
# Quoted separately, per-trip or optional -- never part of the semester total.
ADDON_ITEMS = ("交通費", "課後延托費", "家長會費")
TOTAL_ITEM = "全學期總收費"


@dataclasses.dataclass(frozen=True)
class FeeRow:
    """One fee line: institution × year × age group × semester × class × item."""

    title: str
    year: int
    age_group: str
    semester: str
    class_type: str
    months: float | None
    item: str
    period: str
    unit_price: float | None
    subtotal: float | None


def _get_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.load(resp)


def _tree(sha: str) -> list[dict]:
    """One level of the repository tree.

    Deliberately non-recursive: ``?recursive=1`` on this repo returns
    ``truncated: true`` (55k+ entries), which would silently drop files. Walking
    level by level returns complete listings.
    """
    return _get_json(f"{TREE_API}/{sha}")["tree"]  # type: ignore[index]


def list_slip_files(city: str, years: tuple[int, ...]) -> dict[int, list[str]]:
    """Exact published slip filenames per 學年度 for one city."""
    root = _tree("master")
    docs = next(e for e in root if e["path"] == "docs")
    data = next(e for e in _tree(docs["sha"]) if e["path"] == "data")
    levels = _tree(data["sha"])

    out: dict[int, list[str]] = {}
    for year in years:
        entry = next((e for e in levels if e["path"] == f"slip{year}"), None)
        if entry is None:
            out[year] = []
            continue
        cities = _tree(entry["sha"])
        city_entry = next((e for e in cities if e["path"] == city), None)
        if city_entry is None:
            out[year] = []
            continue
        listing = _get_json(f"{TREE_API}/{city_entry['sha']}")
        if listing.get("truncated"):  # type: ignore[union-attr]
            raise RuntimeError(f"slip{year}/{city} listing truncated; refusing partial data")
        out[year] = [
            e["path"][:-5] for e in listing["tree"] if e["path"].endswith(".json")  # type: ignore[index]
        ]
    return out


def fetch_slip(city: str, title: str, year: int) -> dict | None:
    url = (
        f"{MIRROR}/data/slip{year}/"
        f"{urllib.parse.quote(city)}/{urllib.parse.quote(title)}.json"
    )
    try:
        return _get_json(url)  # type: ignore[return-value]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _num(value: object) -> float | None:
    """Parse a fee cell. Blank means "not charged", which is not the same as zero."""
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_slip(title: str, year: int, payload: dict) -> list[FeeRow]:
    """Flatten one slip into tidy rows, skipping items with no declared amount."""
    rows: list[FeeRow] = []
    for age_group, semesters in (payload.get("slip") or {}).items():
        if not isinstance(semesters, dict):
            continue
        for semester, block in semesters.items():
            if not isinstance(block, dict):
                continue
            months = _num(block.get("months"))
            for class_type, items in (block.get("class") or {}).items():
                if not isinstance(items, dict):
                    continue
                for item, cell in items.items():
                    if not isinstance(cell, dict):
                        continue
                    unit = _num(cell.get("單價"))
                    sub = _num(cell.get("小計"))
                    if unit is None and sub is None:
                        continue  # not charged -- omit rather than record a false zero
                    rows.append(
                        FeeRow(
                            title=title, year=year, age_group=str(age_group),
                            semester=str(semester), class_type=str(class_type),
                            months=months, item=str(item),
                            period=str(cell.get("收費期間") or ""),
                            unit_price=unit, subtotal=sub,
                        )
                    )
    return rows


def fetch_city(
    city: str, years: tuple[int, ...], max_workers: int = 8
) -> tuple[list[FeeRow], list[tuple[int, str]]]:
    """Fetch and parse every published slip for a city.

    Returns the rows plus a list of (year, title) that 404'd despite being in the
    tree listing -- a mismatch worth reporting rather than swallowing.
    """
    listing = list_slip_files(city, years)
    tasks = [(y, t) for y, titles in listing.items() for t in titles]
    rows: list[FeeRow] = []
    missing: list[tuple[int, str]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(fetch_slip, city, title, year): (year, title)
            for year, title in tasks
        }
        for fut in concurrent.futures.as_completed(futures):
            year, title = futures[fut]
            payload = fut.result()
            if payload is None:
                missing.append((year, title))
                continue
            rows.extend(parse_slip(title, year, payload))
    return rows, missing


def semester_total(rows: list[FeeRow]) -> dict[tuple[str, int, str, str, str], dict]:
    """Group rows and compare the declared 全學期總收費 to the sum of its parts.

    A mismatch is the primary 超收 signal available from public data: the itemised
    charges and the headline total are both filed with the authority, so they
    should agree.
    """
    groups: dict[tuple[str, int, str, str, str], dict] = {}
    for r in rows:
        key = (r.title, r.year, r.age_group, r.semester, r.class_type)
        g = groups.setdefault(key, {"core": 0.0, "declared": None, "addons": 0.0})
        if r.item in CORE_ITEMS and r.subtotal is not None:
            g["core"] += r.subtotal
        elif r.item == TOTAL_ITEM:
            g["declared"] = r.subtotal
        elif r.item in ADDON_ITEMS and r.subtotal is not None:
            g["addons"] += r.subtotal
    for g in groups.values():
        d, c = g["declared"], g["core"]
        g["diff"] = (d - c) if d is not None else None
    return groups

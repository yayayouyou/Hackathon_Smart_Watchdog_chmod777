"""Client for the public preschool registry data (機構主檔 / 裁罰 / 收費).

Two upstreams, deliberately in this order:

1. ``kiang.github.io/ap.ece.moe.edu.tw`` -- a civic-tech mirror of 全國教保資訊網
   (台灣幼兒園地圖, MIT-licensed, by 江明宗). Already normalised to JSON/GeoJSON.
2. ``ap.ece.moe.edu.tw/webecems/*.aspx`` -- the official ASP.NET query pages.

We prefer the mirror for a reason that matters to this competition: **the official
site delists 裁罰紀錄 after a retention period**, while the mirror keeps history.
The mirror's own documentation cites a case where the official page showed 2
penalties for a facility that the mirror had 15 records for. Training labels built
only from the live official site would therefore be silently and severely
truncated -- the worst kind of bias, because it looks like clean data.

The official endpoints are still needed to (a) verify the mirror against source of
truth for a sample, and (b) pick up records newer than the mirror's last sync.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
import pathlib
import re
import urllib.error
import urllib.parse
import urllib.request

MIRROR = "https://kiang.github.io/ap.ece.moe.edu.tw"
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
EXTERNAL_DIR = PROJECT_ROOT / "data" / "external"
DEFAULT_PRESCHOOLS = EXTERNAL_DIR / "preschools.json"
DEFAULT_PENALTIES = EXTERNAL_DIR / "punish_all.json"

OFFICIAL = {
    "punish": "https://ap.ece.moe.edu.tw/webecems/punishSearch.aspx",
    "basic": "https://ap.ece.moe.edu.tw/webecems/pubSearch.aspx",
    "evaluation": "https://ap.ece.moe.edu.tw/webecems/evaSearch.aspx",
}

# 受託法人 embedded in a 非營利園 title, e.g.
# "新北市安溪非營利幼兒園(委託社團法人桃園市教保服務人員協會辦理)"
OPERATOR_RE = re.compile(r"[（(](?:委託)?(.+?)(?:申請)?辦理[）)]")
# 分班 suffix, e.g. "新北市立五股幼兒園德音分班" -> parent "新北市立五股幼兒園"
BRANCH_RE = re.compile(r"^(.+?幼兒園).+分班$")
LAW_ARTICLE_RE = re.compile(r"第(\d+)條")
LAW_CITATION_RE = re.compile(r"第(\d+)條(?:第(\d+)項)?")
ACTOR_RE = re.compile(r"^([^：:]+)[：:](.*)$")
FINE_RE = re.compile(r"([\d,]+)\s*元")
# Registry areas arrive as strings: "1,280平方公尺"
AREA_RE = re.compile(r"([\d,]+(?:\.\d+)?)")


def _fetch_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=120) as resp:
        return json.load(resp)


def verify_snapshot(path: pathlib.Path | str) -> dict:
    """Verify a pinned payload against its sidecar manifest before use."""
    snapshot = pathlib.Path(path)
    manifest_path = snapshot.parent / "manifest.json"
    if not manifest_path.exists():
        if snapshot.resolve().parent == EXTERNAL_DIR.resolve():
            raise ValueError(f"missing snapshot manifest: {manifest_path}")
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = (manifest.get("files") or {}).get(snapshot.name)
    if not isinstance(entry, dict) or not entry.get("sha256"):
        raise ValueError(f"manifest has no hash for {snapshot.name}")
    data = snapshot.read_bytes()
    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != entry["sha256"] or len(data) != entry.get("byte_length"):
        raise ValueError(
            f"snapshot integrity mismatch for {snapshot.name}; "
            "use the explicit adoption/update command after review"
        )
    return entry


def _load_json(path: pathlib.Path | str) -> object:
    verify_snapshot(path)
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def _preschools_from_raw(raw: object) -> list[dict]:
    if not isinstance(raw, dict) or not isinstance(raw.get("features"), list):
        raise ValueError("preschool snapshot must be a GeoJSON FeatureCollection")
    out = []
    for feature in raw["features"]:
        props = dict(feature["properties"])
        coords = (feature.get("geometry") or {}).get("coordinates") or [None, None]
        props["lon"], props["lat"] = coords[0], coords[1]
        out.append(props)
    return out


def load_preschools(path: pathlib.Path | str = DEFAULT_PRESCHOOLS) -> list[dict]:
    """Load the pinned preschool snapshot used for reproducible builds."""
    return _preschools_from_raw(_load_json(path))


def fetch_preschools() -> list[dict]:
    """Fetch the current live preschool registry; does not update the snapshot."""
    return _preschools_from_raw(_fetch_json(f"{MIRROR}/preschools.json"))


def parse_actor(actor: object) -> tuple[str, str]:
    """Split ``負責人：王小明`` without discarding the actor's legal role."""
    text = str(actor or "").strip()
    match = ACTOR_RE.match(text)
    if not match:
        return "unknown", text
    return match.group(1).strip(), match.group(2).strip()


def sanction_type(punishment: object) -> str:
    """Classify the five sanction forms currently present in the source."""
    text = str(punishment or "").strip()
    if fine_amount(text) is not None:
        return "fine"
    prefixes = {
        "停止招生": "stop_enrollment",
        "減少招收人數": "reduced_enrollment",
        "廢止設立許可": "permit_revoked",
        "停辦": "operation_suspended",
    }
    for prefix, category in prefixes.items():
        if text.startswith(prefix):
            return category
    raise ValueError(f"unrecognised penalty sanction: {text!r}")


def _law_citation(law: object) -> tuple[str, str]:
    match = LAW_CITATION_RE.search(str(law or ""))
    return match.groups(default="") if match else ("", "")


def _normalise_law(law: object) -> str:
    return "".join(str(law or "").split())


def _penalty_group_id(entry: dict) -> str:
    article, paragraph = _law_citation(entry.get("law"))
    identity = [
        entry.get("id", ""),
        entry.get("date", ""),
        article or _normalise_law(entry.get("law")),
        paragraph,
        entry.get("actor_role", ""),
        entry.get("actor_name", ""),
        entry.get("punishment", ""),
    ]
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return f"pengrp_{digest[:20]}"


def _canonical_penalty(cluster: list[dict]) -> dict:
    variants = sorted(
        {str(entry.get("law") or "").strip() for entry in cluster},
        key=lambda value: (len(_normalise_law(value)), value),
    )
    entry = dict(cluster[0])
    entry["law"] = variants[-1] if variants else ""
    entry["law_variants"] = variants
    entry["source_row_count"] = len(cluster)
    return entry


def _deduplicate_penalty_group(entries: list[dict]) -> list[dict]:
    """Merge only an unambiguous short/long law representation pair.

    Identical source rows are retained as separate candidates: without an
    upstream event identifier, equal fields do not prove that two sanctions are
    the same event. Their shared ``penalty_group_id`` makes the ambiguity auditable.
    """
    by_law: dict[str, list[dict]] = collections.defaultdict(list)
    for entry in entries:
        by_law[_normalise_law(entry.get("law"))].append(entry)

    targets: dict[str, str] = {}
    laws = sorted(by_law, key=lambda value: (len(value), value))
    for law in laws:
        longer = [
            other for other in laws if len(other) > len(law) and other.startswith(law)
        ]
        if len(longer) == 1:
            targets[law] = longer[0]

    clusters: dict[str, list[dict]] = collections.defaultdict(list)
    cluster_laws: dict[str, set[str]] = collections.defaultdict(set)
    for law, law_entries in by_law.items():
        target = targets.get(law, law)
        clusters[target].extend(law_entries)
        cluster_laws[target].add(law)

    canonical: list[dict] = []
    for target in sorted(clusters):
        cluster = clusters[target]
        if len(cluster_laws[target]) == 1 and len(cluster) > 1:
            canonical.extend(_canonical_penalty([entry]) for entry in cluster)
        else:
            canonical.append(_canonical_penalty(cluster))

    canonical.sort(
        key=lambda entry: (
            _normalise_law(entry.get("law")),
            str(entry.get("actor") or ""),
        )
    )
    group_id = _penalty_group_id(canonical[0])
    for index, entry in enumerate(canonical, start=1):
        entry["penalty_group_id"] = group_id
        entry["group_record_index"] = index
        entry["group_record_count"] = len(canonical)
    return canonical


def deduplicate_penalties(entries: list[dict]) -> list[dict]:
    """Conservatively collapse duplicate source rows while preserving actors.

    Records are candidates only when institution, date, article/paragraph,
    punishment, actor role, and actor name all agree. Within that group we merge
    only a short law text that prefixes exactly one longer variant. Exact source
    rows remain separate ambiguous candidates because the upstream supplies no
    event identifier. Same-day sanctions against different people always remain
    separate.
    """
    groups: dict[tuple[str, ...], list[dict]] = collections.defaultdict(list)
    for source in entries:
        entry = dict(source)
        actor_role, actor_name = parse_actor(entry.get("actor"))
        entry["actor_role"] = actor_role
        entry["actor_name"] = actor_name
        entry["fine"] = fine_amount(entry.get("punishment"))
        entry["sanction_type"] = sanction_type(entry.get("punishment"))
        article, paragraph = _law_citation(entry.get("law"))
        key = (
            str(entry.get("id") or ""),
            str(entry.get("date") or "").strip(),
            article,
            paragraph,
            str(entry.get("punishment") or "").strip(),
            actor_role,
            actor_name,
        )
        # Without a parseable citation, only exact law text may be merged.
        if not article:
            key += (_normalise_law(entry.get("law")),)
        groups[key].append(entry)

    canonical = [
        entry
        for group in groups.values()
        for entry in _deduplicate_penalty_group(group)
    ]
    return sorted(
        canonical,
        key=lambda entry: (
            str(entry.get("date") or ""),
            str(entry.get("id") or ""),
            str(entry.get("actor") or ""),
            str(entry.get("law") or ""),
            str(entry.get("punishment") or ""),
        ),
    )


def _penalties_from_raw(raw: object) -> dict[str, list[dict]]:
    if not isinstance(raw, dict):
        raise ValueError("penalty snapshot root must be an object")
    entries: list[dict] = []
    for actor, records in raw.items():
        if not isinstance(records, list):
            raise ValueError(f"penalty actor value must be a list: {actor}")
        for record in records:
            entry = dict(record)
            entry["actor"] = actor
            entries.append(entry)

    by_id: dict[str, list[dict]] = collections.defaultdict(list)
    for entry in deduplicate_penalties(entries):
        by_id[str(entry["id"])].append(entry)
    return dict(by_id)


def load_penalties(path: pathlib.Path | str = DEFAULT_PENALTIES) -> dict[str, list[dict]]:
    """Load and canonicalise the pinned historical penalty snapshot."""
    return _penalties_from_raw(_load_json(path))


def fetch_penalties() -> dict[str, list[dict]]:
    """Fetch and canonicalise live penalties; does not update the snapshot."""
    return _penalties_from_raw(_fetch_json(f"{MIRROR}/punish_all.json"))


def fetch_fee_slip(city: str, title: str, year: int) -> dict | None:
    """收費明細 for one preschool in one 學年度 (109-114). ``None`` if not published."""
    url = (
        f"{MIRROR}/data/slip{year}/"
        f"{urllib.parse.quote(city)}/{urllib.parse.quote(title)}.json"
    )
    try:
        return _fetch_json(url)
    except urllib.error.HTTPError as exc:
        # 404 means "this 園 did not publish a slip for this year" -- an answer, not a fault.
        if exc.code == 404:
            return None
        raise


# --------------------------------------------------------------------------- #
# derived helpers
# --------------------------------------------------------------------------- #


def operator_of(title: str) -> str | None:
    """The 受託法人 running a 非營利園, parsed out of its registered title."""
    m = OPERATOR_RE.search(title)
    return m.group(1) if m else None


def normalise_operator(value: object) -> str:
    """Normalise a legal entity name for report-to-registry comparison.

    Financial reports inconsistently retain the legal-form prefix while registry
    titles may repeat it inside a university's full name.  Whitespace and those
    prefixes therefore cannot be identity-bearing comparison signals.
    """
    text = "".join(str(value or "").split())
    for prefix in ("財團法人", "社團法人", "學校財團法人"):
        text = text.replace(prefix, "")
    return text


def operators_match(left: object, right: object) -> bool:
    """Whether two non-empty operator names refer to the same legal entity."""
    left_normalised = normalise_operator(left)
    right_normalised = normalise_operator(right)
    return bool(
        left_normalised
        and right_normalised
        and (
            left_normalised in right_normalised
            or right_normalised in left_normalised
        )
    )


def parent_of(title: str) -> str:
    """Collapse a 分班 to its 本園.

    Financial data for 公立幼兒園 exists per 分基金 (i.e. per 本園), while the
    registry and 裁罰 records are per 分班. Joining the two requires this.
    """
    m = BRANCH_RE.match(title)
    return m.group(1) if m else title


def entity_key(title: str) -> str:
    """Identify the *physical* 幼兒園, collapsing duplicate registry entries.

    The registry issues a new entry when a 非營利園's contract is re-tendered to a
    different 受託法人, so the same building appears twice with titles differing
    only inside the parentheses. 碧城 is the clear case: one entry carries zero
    penalties and the other carries two, so keying on the raw title both
    double-counts the 園 and can miss its penalties entirely.

    Measured effect on 新北市: 52 非營利 registry entries collapse to 45 physical
    園, and the penalty rate corrects from 26.9% (per entry) to 22.2% (per 園).

    Two normalisations, in order:
      1. drop the ``（委託…辦理）`` operator clause  -- contract re-tendering
      2. collapse a 分班 to its 本園                -- 公立園 branch structure
    """
    without_operator = OPERATOR_RE.sub("", title).strip()
    return parent_of(without_operator)


def law_article(law: str | None) -> str | None:
    """``第33條第1項...`` -> ``33``."""
    m = LAW_ARTICLE_RE.match(law or "")
    return m.group(1) if m else None


def fine_amount(punishment: str | None) -> int | None:
    """``罰鍰：60,000元`` -> ``60000``.

    Returns ``None`` when the sanction is not monetary (停業, 廢止許可), which the
    caller must not conflate with a zero fine.
    """
    m = FINE_RE.search(punishment or "")
    return int(m.group(1).replace(",", "")) if m else None


def parse_area(value: object) -> float | None:
    """``"1,280平方公尺"`` -> ``1280.0``. ``None`` when unstated."""
    m = AREA_RE.search(str(value or ""))
    return float(m.group(1).replace(",", "")) if m else None


def has_shuttle(value: object) -> bool:
    """Whether the 園 runs a 幼童專用車.

    Registry values are messy -- empty, whitespace/tab-only, or a description.
    This matters because 幼照法 第31條 (vehicle markings, ride-along staff, load
    limits) can only be violated by a 園 that operates one, so it is an exposure
    flag rather than a quality signal.
    """
    return bool(str(value or "").strip())


@dataclasses.dataclass
class Institution:
    """A preschool joined to its penalty history."""

    id: str
    title: str
    city: str
    town: str
    type: str
    owner: str
    count_approved: int | None
    monthly: int | None
    reg_date: str
    is_active: int
    operator: str | None
    parent: str
    entity: str = ""
    size: float | None = None
    size_in: float | None = None
    size_out: float | None = None
    shuttle: bool = False
    after_care: bool = False
    pre_public: str = ""
    penalties: list[dict] = dataclasses.field(default_factory=list)

    @property
    def penalty_count(self) -> int:
        return len(self.penalties)

    @property
    def nonmonetary_sanction_count(self) -> int:
        return sum(p.get("fine") is None for p in self.penalties)

    @property
    def total_fine(self) -> int:
        """Sum monetary fines; non-monetary sanctions remain separate records."""
        return sum(
            amount
            for penalty in self.penalties
            if (amount := penalty.get("fine")) is not None
        )

    @property
    def indoor_area_per_child(self) -> float | None:
        """室內樓地板面積 / 核定招收人數.

        幼照法設施設備標準訂有每人室內樓地板面積下限, so an unusually low value is
        a legality question, not merely a comfort one. 第26條 (設施設備) is the third
        most common violation article in 新北市.
        """
        if not self.size_in or not self.count_approved:
            return None
        return self.size_in / self.count_approved


def _to_int(value: object) -> int | None:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def build_institutions(
    city: str | None = None,
    preschools_path: pathlib.Path | str = DEFAULT_PRESCHOOLS,
    penalties_path: pathlib.Path | str = DEFAULT_PENALTIES,
) -> list[Institution]:
    """Join pinned registry and penalty snapshots, optionally filtered to a city."""
    penalties = load_penalties(penalties_path)
    out = []
    for p in load_preschools(preschools_path):
        if city and p.get("city") != city:
            continue
        out.append(
            Institution(
                id=p["id"],
                title=p["title"],
                city=p.get("city", ""),
                town=p.get("town", ""),
                type=p.get("type", ""),
                owner=p.get("owner", ""),
                count_approved=_to_int(p.get("count_approved")),
                monthly=_to_int(p.get("monthly")),
                reg_date=p.get("reg_date", ""),
                is_active=_to_int(p.get("is_active")) or 0,
                operator=operator_of(p["title"]),
                parent=parent_of(p["title"]),
                entity=entity_key(p["title"]),
                size=parse_area(p.get("size")),
                size_in=parse_area(p.get("size_in")),
                size_out=parse_area(p.get("size_out")),
                shuttle=has_shuttle(p.get("shuttle")),
                after_care=str(p.get("is_after") or "").startswith("有"),
                pre_public=str(p.get("pre_public") or "").strip(),
                penalties=penalties.get(p["id"], []),
            )
        )
    return out


def save_json(obj: object, path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")

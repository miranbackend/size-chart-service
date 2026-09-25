"""
Pure size-chart extraction logic.

Nothing in this module touches the network or the database, so every rule
here can be exercised with fixtures.

Pipeline for one fetched product page:

    retailer adapter (e.g. Myntra window.__myx)
        -> generic embedded JSON (ld+json, __NEXT_DATA__, window.__STATE__ = ...)
        -> HTML <table> size charts
        -> size-chart image candidates (never stored as measurements)

A chart is only accepted when it has at least 2 size rows, each with at least
one recognised measurement field whose value is numeric. Available/purchasable
sizes on their own are never a size chart.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup


# ============================================================
# LIMITS
# ============================================================

MIN_CHART_ROWS = 2
DEFAULT_MAX_CHART_ROWS = 100
MAX_TABLE_ROWS = 200
MAX_TABLE_COLS = 30
MAX_SCRIPT_CHARS = 5_000_000
MAX_JSON_NODES = 300_000
MAX_JSON_DEPTH = 60
MAX_EMBEDDED_HTML_CHARS = 500_000
MAX_VALUE_CHARS = 40
MAX_SIZE_LABEL_CHARS = 30
MAX_IMAGE_CANDIDATES = 5

CHART_TYPE = "measurement_chart"


# ============================================================
# SITE DETECTION
# ============================================================

_SITE_HOSTS = (
    ("amazon", ("amazon.in", "amazon.com", "amazon.")),
    ("myntra", ("myntra.com",)),
    ("ajio", ("ajio.com",)),
    ("flipkart", ("flipkart.com",)),
    ("nykaa", ("nykaafashion.com", "nykaaman.com", "nykaa.com")),
    ("manyavar", ("manyavar.com",)),
    ("pernia", ("perniaspopupshop.com",)),
)


def _host(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_google_url(url: str) -> bool:
    host = _host(url)
    return host == "google.com" or host.startswith("google.") or ".google." in host


def site_from_url(url: str) -> str:
    host = _host(url)

    if is_google_url(url):
        return "google_shopping"

    labels = host.split(".")
    for site, needles in _SITE_HOSTS:
        for needle in needles:
            if needle.endswith("."):
                # "amazon." = any amazon.<tld>, matched as a whole label.
                if needle[:-1] in labels[:-1]:
                    return site
            elif host == needle or host.endswith("." + needle):
                return site

    return "other"


# ============================================================
# TEXT HELPERS
# ============================================================

def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_key(value: str) -> str:
    """Plain snake_case of a label, no alias mapping."""
    return re.sub(r"[^a-z0-9]+", "_", clean_text(value).lower()).strip("_")


def contains_number(value: Any) -> bool:
    return bool(re.search(r"\d", clean_text(value)))


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group(0)) if m else None


# ============================================================
# UNITS
# ============================================================

_UNIT_ALIASES = {
    "in": "inch",
    "ins": "inch",
    "inch": "inch",
    "inches": "inch",
    '"': "inch",
    "''": "inch",
    "cm": "cm",
    "cms": "cm",
    "centimeter": "cm",
    "centimeters": "cm",
    "centimetre": "cm",
    "centimetres": "cm",
    "mm": "mm",
    "millimeter": "mm",
    "millimeters": "mm",
    "millimetre": "mm",
    "millimetres": "mm",
}


def normalize_unit(value: Any) -> str | None:
    """Map a unit token ('Inches', 'cms', '"') to inch / cm / mm."""
    token = clean_text(value).lower().strip(" .()[]")
    return _UNIT_ALIASES.get(token)


_UNIT_TEXT_PATTERNS = (
    # Explicit unit words anywhere are unambiguous.
    ("cm", re.compile(r"\b(cms?|centimet(?:er|re)s?)\b")),
    ("mm", re.compile(r"\b(mm|millimet(?:er|re)s?)\b")),
    ("inch", re.compile(r"\b(inch|inches)\b")),
    # Bare "in" is only a unit when it is clearly used as one:
    # "(in)", "[in]", "in:" or directly after a number ("38 in").
    ("inch", re.compile(r"[(\[]\s*in\s*[)\]]|\bin\s*:|\d\s*in\b")),
    ("inch", re.compile(r"\d\s*(\"|'')")),
)


def detect_unit(text: str) -> str | None:
    t = clean_text(text).lower()
    if not t:
        return None
    for unit, pattern in _UNIT_TEXT_PATTERNS:
        if pattern.search(t):
            return unit
    return None


# ============================================================
# MEASUREMENT KEYS
# ============================================================

MEASUREMENT_KEY_ALIASES = {
    "bust size": "bust",
    "bust measurement": "bust",
    "chest size": "chest",
    "chest measurement": "chest",
    "waist size": "waist",
    "waist measurement": "waist",
    "hip size": "hip",
    "hip measurement": "hip",
    "hips": "hip",
    "shoulder width": "shoulder",
    "shoulder measurement": "shoulder",
    "across shoulder": "shoulder",
    "across shoulders": "shoulder",
    "shoulders": "shoulder",
    "sleeve": "sleeve_length",
    "sleeves": "sleeve_length",
    "sleeve length": "sleeve_length",
    "length": "length",
    "body length": "length",
    "garment length": "length",
    "front length": "length",
    "top length": "length",
    "total length": "length",
    "full length": "length",
    "dress length": "dress_length",
    "foot length": "foot_length",
    "footlength": "foot_length",
    "leg opening": "leg_opening",
    "bottom opening": "leg_opening",
    "neck size": "neck",
    "collar size": "collar",
    "inside leg": "inseam",
    "inner leg": "inseam",
    "outside leg": "outseam",
    "to fit chest": "chest",
    "to fit bust": "bust",
    "to fit waist": "waist",
    "to fit hip": "hip",
}

KNOWN_MEASUREMENT_KEYS = frozenset({
    "chest", "bust", "underbust", "waist", "hip", "shoulder", "shoulder_width",
    "length", "body_length", "garment_length", "dress_length", "sleeve",
    "sleeve_length", "inseam", "outseam", "thigh", "rise", "front_rise",
    "back_rise", "calf", "knee", "neck", "collar", "foot_length", "footlength",
    "foot_width", "leg_opening", "bottom", "hem", "armhole", "bicep", "wrist",
    "ankle", "height", "head", "cup",
})

# Tokens that make an unknown header a plausible measurement
# (e.g. "Bottom Width" -> bottom_width, "Waist Band" -> waist_band).
_MEASUREMENT_TOKENS = (
    "chest", "bust", "waist", "hip", "shoulder", "length", "sleeve", "inseam",
    "outseam", "thigh", "rise", "calf", "neck", "collar", "foot", "leg",
    "opening", "bottom", "hem", "width", "circumference", "girth", "around",
    "armhole", "bicep", "knee", "ankle", "wrist", "height", "across", "round",
)

# Header words that are never measurements even though their values are numeric.
# Whole-word tokens (short words that would otherwise match inside others):
_NON_MEASUREMENT_TOKENS = (
    "mrp", "offer", "qty", "sku", "seller", "age", "count", "code", "id", "box",
)
# Substrings: package/shipping dimensions and image sizes are not garment measurements.
_NON_MEASUREMENT_SUBSTRINGS = (
    "price", "discount", "stock", "quantity", "avail", "rating", "review",
    "color", "colour", "weight", "package", "packag", "shipping", "parcel",
    "dimension", "image", "img", "thumb", "media", "banner", "video", "screen",
    "viewport", "pixel",
)

# Size-scale columns (UK 7, EU 41, brand size S) are conversions, not measurements.
_SIZE_SCALE_KEYS = frozenset({
    "size", "sizes", "brand_size", "size_label", "tag_size", "label", "uk",
    "us", "eu", "euro", "in", "ind", "india", "indian", "jp", "cn", "br",
    "fr", "it", "au", "international", "int", "intl", "standard_size",
    "uk_size", "us_size", "eu_size", "india_size", "indian_size",
    "international_size", "euro_size", "myntra_size", "letter_size",
    "alpha_size", "numeric_size",
})

_HEADER_NOISE_RE = re.compile(
    r"\((?:[^)]*)\)|\[(?:[^\]]*)\]|"
    r"\b(?:in|cm|cms|mm|inch|inches|centimet(?:er|re)s?)\b\.?|"
    r"\b(?:approx\.?|approximately|actual|product|garment\s+measurement)\b",
    re.I,
)


def canonical_measurement_key(header: Any) -> tuple[str, str | None]:
    """
    Normalise a measurement header.

    Returns (key, unit_from_header). Units written in the header
    ("Chest (cm)", "Waist in inches") are returned separately.
    """
    # JSON keys: "sleeveLength" -> "sleeve Length", "packageLength" -> "package Length"
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", clean_text(header))
    text = text.lower().replace("&", " and ").replace("_", " ")
    unit = detect_unit(text)

    base = clean_text(_HEADER_NOISE_RE.sub(" ", text)).strip(" :-_/")
    base = re.sub(r"[^a-z0-9 ]+", " ", base)
    base = clean_text(base)

    if base in MEASUREMENT_KEY_ALIASES:
        return MEASUREMENT_KEY_ALIASES[base], unit

    stripped = clean_text(
        re.sub(r"^(to fit|fits|body|garment)\s+|\s+(size|measurement|measurements)$", "", base)
    )
    if stripped in MEASUREMENT_KEY_ALIASES:
        return MEASUREMENT_KEY_ALIASES[stripped], unit

    key = normalize_key(stripped or base)
    return key, unit


def is_measurement_key(key: str) -> bool:
    if not key or key in _SIZE_SCALE_KEYS:
        return False
    if key in KNOWN_MEASUREMENT_KEYS:
        return True
    parts = key.split("_")
    if any(tok in parts for tok in _NON_MEASUREMENT_TOKENS):
        return False
    if any(tok in key for tok in _NON_MEASUREMENT_SUBSTRINGS):
        return False
    return any(tok in key for tok in _MEASUREMENT_TOKENS)


# ============================================================
# SIZE LABELS
# ============================================================

_LETTER_SIZE = r"(?:\d{0,2}x{0,4}[sl]|m|xs|xl|\d?xl|x{1,5}l|x{1,5}s)"
_NUM = r"\d{1,3}(?:\.\d)?"

_SIZE_LABEL_RE = re.compile(
    r"(?:size\s*)?(?:"
    rf"{_LETTER_SIZE}(?:\s*[-/]\s*{_LETTER_SIZE})?(?:\s*\(\s*{_NUM}(?:\s*[-/]\s*{_NUM})?\s*\))?"
    r"|free\s*size|one\s*size|onesize|os|fs|standard|regular fit"
    rf"|{_NUM}\s*[a-z]{{0,3}}(?:\s*[-/]\s*{_NUM}\s*[a-z]{{0,3}})?"
    rf"|(?:uk|us|eu|euro|in|ind|india|jp|cn|br|fr|it|au)\s*[-:]?\s*{_NUM}"
    r"|\d{1,2}\s*(?:-|to)\s*\d{1,2}\s*(?:y|yr|yrs|years?|m|mo|mos|months?)"
    r"|\d{1,2}\s*(?:y|yr|yrs|years?|m|mo|mos|months?)"
    r"|new\s*born|newborn"
    r")",
    re.I,
)

_NOT_SIZE_WORDS = re.compile(
    r"\b(available|unavailable|in stock|out of stock|sold out|yes|no|notify)\b", re.I
)


def looks_like_size(value: Any) -> bool:
    text = clean_text(value)
    if not text or len(text) > MAX_SIZE_LABEL_CHARS:
        return False
    if _NOT_SIZE_WORDS.search(text):
        return False
    return bool(_SIZE_LABEL_RE.fullmatch(text.lower()))


def normalize_size_label(value: Any) -> str:
    text = clean_text(value)
    text = re.sub(r"^size\s*", "", text, flags=re.I)
    if re.fullmatch(r"[a-zA-Z0-9]{1,5}", text) and not text.isdigit():
        text = text.upper()
    return text


# ============================================================
# MEASUREMENT VALUES
# ============================================================

_VALUE_UNIT_RE = re.compile(
    r"(?<=\d)\s*(?:cms?|mm|inch(?:es)?|ins?|\"|'')(?![a-z])|"
    r"\b(?:cms?|mm|inch(?:es)?|centimet(?:er|re)s?)\b",
    re.I,
)
_ALLOWED_VALUE_RE = re.compile(r"[\d\s.,\-/½¼¾⅓⅔⅛⅜⅝⅞]+")


def normalize_measurement_value(value: Any) -> str | None:
    """
    Clean a raw cell ("38 cm", "38 – 40", "38 / 40in") into "38", "38-40",
    "38/40". Returns None when the value is not a numeric measurement
    ("Available", "Yes", "Regular", "38 (M)" ...).
    """
    text = clean_text(value)
    if not text or len(text) > MAX_VALUE_CHARS:
        return None
    if not contains_number(text):
        return None

    text = text.replace("–", "-").replace("—", "-").replace("−", "-")
    text = _VALUE_UNIT_RE.sub(" ", text)
    text = re.sub(r"\s+to\s+", "-", text, flags=re.I)
    text = clean_text(text)
    text = re.sub(r"\s*([-/])\s*", r"\1", text)

    if not text or not _ALLOWED_VALUE_RE.fullmatch(text):
        return None
    if not contains_number(text):
        return None
    return text.strip(" -/")


def measurement_value_is_valid(value: Any) -> bool:
    return normalize_measurement_value(value) is not None


# ============================================================
# CHART MODEL + VALIDATION
# ============================================================

@dataclass
class Chart:
    sizes: dict[str, dict[str, str]]
    unit: str | None
    method: str
    source: str
    confidence: float
    chart_type: str = CHART_TYPE
    measurement_basis: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    raw_rows: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sizes": self.sizes,
            "unit": self.unit,
            "chart_type": self.chart_type,
            "method": self.method,
            "source": self.source,
            "confidence": self.confidence,
            "measurement_basis": self.measurement_basis,
            "evidence": self.evidence,
        }


def validate_chart(
    sizes: dict[str, dict[str, Any]],
    max_rows: int = DEFAULT_MAX_CHART_ROWS,
) -> tuple[dict[str, dict[str, str]] | None, str | None]:
    """
    Keep only size rows with at least one numeric measurement.

    Returns (clean_sizes, None) or (None, rejection_reason).
    """
    clean: dict[str, dict[str, str]] = {}

    for label, measurements in sizes.items():
        if not looks_like_size(label):
            continue
        row = {}
        for key, value in (measurements or {}).items():
            if not is_measurement_key(key):
                continue
            norm = normalize_measurement_value(value)
            if norm is not None:
                row[key] = norm
        if row:
            clean[normalize_size_label(label)] = row

    if len(clean) < MIN_CHART_ROWS:
        return None, f"only {len(clean)} measured size row(s); need {MIN_CHART_ROWS}"
    if len(clean) > max_rows:
        return None, f"{len(clean)} rows exceeds MAX_CHART_ROWS={max_rows}"
    return clean, None


# ============================================================
# TABULAR (HTML TABLE / JSON MATRIX) -> CHART
# ============================================================

def table_rows(table) -> list[list[str]]:
    """Text matrix of an HTML table, expanding colspans, bounded in size."""
    rows: list[list[str]] = []
    for tr in table.find_all("tr", limit=MAX_TABLE_ROWS):
        row: list[str] = []
        for cell in tr.find_all(["th", "td"], limit=MAX_TABLE_COLS):
            text = clean_text(cell.get_text(" ", strip=True))
            try:
                span = max(1, min(int(cell.get("colspan", 1)), MAX_TABLE_COLS))
            except (TypeError, ValueError):
                span = 1
            row.extend([text] * span)
        if any(row):
            rows.append(row[:MAX_TABLE_COLS])
    return rows


def _is_size_header(cell: str) -> bool:
    key = normalize_key(cell)
    return key in _SIZE_SCALE_KEYS or key.endswith("_size") or key.startswith("size")


def _measurement_columns(headers: list[str]) -> dict[int, tuple[str, str | None]]:
    cols = {}
    for i, header in enumerate(headers):
        if _is_size_header(header):
            continue
        key, unit = canonical_measurement_key(header)
        if is_measurement_key(key):
            cols[i] = (key, unit)
    return cols


def find_header_row(rows: list[list[str]]) -> int | None:
    """Index of the first row (within the first 3) that names measurement columns."""
    for i, row in enumerate(rows[:3]):
        if _measurement_columns(row):
            return i
    return None


def _find_size_column(headers: list[str], body: list[list[str]]) -> int | None:
    for i, header in enumerate(headers):
        if _is_size_header(header):
            return i
    for i in range(len(headers)):
        values = [r[i] for r in body if i < len(r) and r[i]]
        if values and sum(looks_like_size(v) for v in values) >= max(2, len(values) * 0.8):
            if not measurement_value_is_valid(headers[i]):
                return i
    return None


def _transpose_if_needed(rows: list[list[str]]) -> list[list[str]]:
    """
    Detect charts laid out as measurement-per-row:

        Size   | S  | M  | L
        Chest  | 38 | 40 | 42
    """
    if len(rows) < 2 or len(rows[0]) < 3:
        return rows
    header_sizes = sum(looks_like_size(c) for c in rows[0][1:])
    first_col_keys = sum(
        is_measurement_key(canonical_measurement_key(r[0])[0]) for r in rows[1:] if r
    )
    if header_sizes >= 2 and header_sizes >= (len(rows[0]) - 1) * 0.6 and first_col_keys >= 1:
        width = max(len(r) for r in rows)
        padded = [r + [""] * (width - len(r)) for r in rows]
        return [list(col) for col in zip(*padded)]
    return rows


def rows_to_sizes(
    rows: list[list[str]],
    context_text: str = "",
) -> tuple[dict[str, dict[str, str]], str | None, dict[str, Any]] | None:
    """
    Turn a text matrix into {size: {measurement: value}}, the chart unit and
    raw audit data. Returns None when the matrix is not a measurement chart.
    """
    rows = [[clean_text(c) for c in r[:MAX_TABLE_COLS]] for r in rows[:MAX_TABLE_ROWS] if r]
    rows = [r for r in rows if any(r)]
    if len(rows) < MIN_CHART_ROWS + 1:
        return None

    rows = _transpose_if_needed(rows)

    header_index = find_header_row(rows)
    if header_index is None:
        return None

    headers = rows[header_index]
    body = rows[header_index + 1:]
    columns = _measurement_columns(headers)
    size_col = _find_size_column(headers, body)
    if size_col is None or not columns:
        return None
    columns.pop(size_col, None)

    header_units = [u for (_k, u) in columns.values() if u]
    chart_unit = (
        max(set(header_units), key=header_units.count)
        if header_units
        else detect_unit(" ".join(headers) + " " + context_text)
    )

    keyed: dict[int, str] = {}
    used: set[str] = set()
    for i, (key, unit) in columns.items():
        if unit and chart_unit and unit != chart_unit:
            key = f"{key}_{unit}"
        if key in used:
            key = normalize_key(headers[i]) or f"{key}_{i}"
        if key in used:
            continue
        used.add(key)
        keyed[i] = key

    sizes: dict[str, dict[str, str]] = {}
    raw_rows: dict[str, list[str]] = {}
    for row in body:
        if size_col >= len(row):
            continue
        label = row[size_col]
        if not looks_like_size(label):
            continue
        label = normalize_size_label(label)
        if label in sizes:
            continue
        measurements = {}
        for i, key in keyed.items():
            if i < len(row):
                norm = normalize_measurement_value(row[i])
                if norm is not None:
                    measurements[key] = norm
        if chart_unit is None:
            chart_unit = detect_unit(" ".join(row))
        sizes[label] = measurements
        raw_rows[label] = row

    if not sizes:
        return None
    return sizes, chart_unit, {"headers": headers, "rows": raw_rows}


def table_to_chart(table, context_text: str = "", max_rows: int = DEFAULT_MAX_CHART_ROWS):
    """Return (clean_sizes, unit, raw) for an HTML table, or None."""
    parsed = rows_to_sizes(table_rows(table), context_text)
    if not parsed:
        return None
    sizes, unit, raw = parsed
    clean, _reason = validate_chart(sizes, max_rows)
    if not clean:
        return None
    return clean, unit, raw


# ============================================================
# JSON HELPERS
# ============================================================

_STATE_ASSIGNMENT_RE = re.compile(
    r"window\.(__NEXT_DATA__|__INITIAL_STATE__|__PRELOADED_STATE__|__STATE__|"
    r"__APOLLO_STATE__|__NUXT__|__INITIAL_DATA__|__PRODUCT_DATA__)\s*=(?!=)\s*"
)


def decode_js_assignment(text: str, marker: re.Pattern | str) -> Any:
    """
    Parse the JSON value assigned at `marker` inside a script, e.g.
    'window.__myx = {...};'. Uses JSONDecoder.raw_decode so trailing
    JavaScript after the object is ignored.
    """
    if isinstance(marker, str):
        pos = text.find(marker)
        if pos < 0:
            return None
        start = pos + len(marker)
    else:
        m = marker.search(text)
        if not m:
            return None
        start = m.end()

    while start < len(text) and text[start] in " \t\r\n":
        start += 1
    try:
        value, _end = json.JSONDecoder().raw_decode(text, start)
    except (json.JSONDecodeError, RecursionError):
        return None
    return value


def json_objects_from_html(soup) -> Iterator[tuple[str, Any]]:
    """Yield (origin, parsed_json) for embedded JSON on the page."""
    for script in soup.find_all("script"):
        body = script.string or script.get_text() or ""
        if not body or len(body) > MAX_SCRIPT_CHARS:
            continue
        script_type = (script.get("type") or "").lower()

        if script_type in ("application/ld+json", "application/json"):
            try:
                yield (script.get("id") or script_type), json.loads(body)
            except (json.JSONDecodeError, RecursionError):
                pass
            continue

        for m in _STATE_ASSIGNMENT_RE.finditer(body):
            value = decode_js_assignment(body[m.start():], _STATE_ASSIGNMENT_RE)
            if value is not None:
                yield f"window.{m.group(1)}", value


def iter_json_nodes(data: Any) -> Iterator[tuple[Any, tuple[str, ...]]]:
    """
    Bounded iterative walk yielding (node, ancestor_ids). ancestor_ids holds
    product-identity values (id/sku/styleId...) seen on enclosing objects so
    a chart can be tied to the product it sits under.
    """
    stack: list[tuple[Any, int, tuple[str, ...]]] = [(data, 0, ())]
    seen = 0
    while stack and seen < MAX_JSON_NODES:
        node, depth, ids = stack.pop()
        seen += 1
        yield node, ids
        if depth >= MAX_JSON_DEPTH:
            continue
        if isinstance(node, dict):
            own = tuple(
                str(v) for k, v in node.items()
                if isinstance(v, (str, int)) and not isinstance(v, bool)
                and normalize_key(k) in _IDENTITY_KEYS
            )
            child_ids = ids + own if own else ids
            for v in node.values():
                if isinstance(v, (dict, list, str)):
                    stack.append((v, depth + 1, child_ids))
        elif isinstance(node, list):
            for v in node:
                if isinstance(v, (dict, list, str)):
                    stack.append((v, depth + 1, ids))


_IDENTITY_KEYS = frozenset({
    "id", "product_id", "productid", "style_id", "styleid", "sku", "sku_id",
    "skuid", "asin", "pid", "item_id", "itemid", "code", "product_code",
    "article_id", "articleid",
})

_SIZE_LABEL_KEYS = (
    "size", "label", "sizeLabel", "size_label", "sizeName", "size_name",
    "displaySize", "display_size", "sizeValue", "brandSize", "name", "value",
)
_MEASUREMENT_LIST_KEYS = ("measurements", "measurement", "sizeMeasurements", "attributes")


def _size_label_of(item: dict) -> str | None:
    for key in _SIZE_LABEL_KEYS:
        value = item.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            if looks_like_size(value):
                return clean_text(value)
    return None


def measurement_entry_value(entry: dict) -> str | None:
    """Value of a {name, value, minValue, maxValue, displayText} entry."""
    value = entry.get("value")
    if value in (None, ""):
        lo, hi = entry.get("minValue"), entry.get("maxValue")
        if lo not in (None, "") and hi not in (None, "") and str(lo) != str(hi):
            value = f"{lo}-{hi}"
        else:
            value = lo if lo not in (None, "") else hi
    if value in (None, ""):
        value = entry.get("displayText")
    return None if value in (None, "") else str(value)


def measurement_entries_to_row(
    entries: list[dict],
    basis: str | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Convert [{name, value, unit, measurementType}, ...] to {key: value}."""
    row: dict[str, str] = {}
    units: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_basis = clean_text(entry.get("measurementType") or entry.get("type")).upper() or None
        if basis and entry_basis and entry_basis != basis:
            continue
        name = entry.get("name") or entry.get("label") or entry.get("key")
        if not name:
            continue
        key, header_unit = canonical_measurement_key(name)
        if not is_measurement_key(key):
            continue
        value = measurement_entry_value(entry)
        if value is None:
            continue
        unit = normalize_unit(entry.get("unit")) or header_unit or detect_unit(str(entry.get("displayText") or ""))
        if unit:
            units.append(unit)
        if key not in row:
            row[key] = value
    return row, units


def _dominant_basis(items: list[dict]) -> str | None:
    """Prefer GARMENT measurements; never mix garment and body values."""
    bases = set()
    for item in items:
        for entry in item.get("measurements") or []:
            if isinstance(entry, dict):
                b = clean_text(entry.get("measurementType") or entry.get("type")).upper()
                if b:
                    bases.add(b)
    if "GARMENT" in bases:
        return "GARMENT"
    if "BODY" in bases:
        return "BODY"
    return None


def json_list_to_chart(items: list) -> tuple[dict[str, dict[str, str]], str | None, dict] | None:
    """
    Recognise a JSON list of per-size objects, either
      [{"size": "M", "chest": 40, "waist": 34}, ...] or
      [{"label": "M", "measurements": [{"name": "Chest", "value": "40"}]}, ...].
    """
    if not (MIN_CHART_ROWS <= len(items) <= MAX_TABLE_ROWS):
        return None
    dict_items = [i for i in items if isinstance(i, dict)]
    if len(dict_items) < MIN_CHART_ROWS:
        return None

    basis = _dominant_basis(dict_items)
    sizes: dict[str, dict[str, str]] = {}
    raw_rows: dict[str, Any] = {}
    units: list[str] = []

    for item in dict_items:
        label = _size_label_of(item)
        if not label or label in sizes:
            continue
        row: dict[str, str] = {}
        for list_key in _MEASUREMENT_LIST_KEYS:
            entries = item.get(list_key)
            if isinstance(entries, list):
                entry_row, entry_units = measurement_entries_to_row(entries, basis)
                row.update({k: v for k, v in entry_row.items() if k not in row})
                units.extend(entry_units)
        for key, value in item.items():
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                mkey, munit = canonical_measurement_key(key)
                if is_measurement_key(mkey) and mkey not in row:
                    row[mkey] = str(value)
                    if munit:
                        units.append(munit)
        if row:
            sizes[label] = row
            raw_rows[label] = item

    if len(sizes) < MIN_CHART_ROWS:
        return None
    unit = max(set(units), key=units.count) if units else None
    return sizes, unit, {"rows": raw_rows, "measurement_basis": basis}


def _json_matrix_to_chart(node: dict) -> tuple[dict, str | None, dict] | None:
    """{"headers": [...], "rows": [[...], ...]} style charts."""
    headers = node.get("headers") or node.get("columns") or node.get("header")
    rows = node.get("rows") or node.get("data") or node.get("values")
    if not (isinstance(headers, list) and isinstance(rows, list)):
        return None
    if not all(isinstance(r, list) for r in rows[:MAX_TABLE_ROWS]):
        return None
    header_cells = [
        clean_text(h.get("label") or h.get("name") or h.get("title")) if isinstance(h, dict) else clean_text(h)
        for h in headers
    ]
    matrix = [header_cells] + [[clean_text(c) for c in r] for r in rows[:MAX_TABLE_ROWS]]
    return rows_to_sizes(matrix, clean_text(node.get("unit")))


# ============================================================
# CANDIDATE CHARTS
# ============================================================

@dataclass
class Candidate:
    sizes: dict[str, dict[str, str]]
    unit: str | None
    method: str
    confidence: float
    evidence: dict[str, Any]
    raw: dict[str, Any]
    measurement_basis: str | None = None


def extract_from_json(
    soup,
    product_ids: tuple[str, ...] = (),
    max_rows: int = DEFAULT_MAX_CHART_ROWS,
) -> list[Candidate]:
    """Search embedded JSON for per-size measurement structures."""
    found: list[Candidate] = []
    for origin, data in json_objects_from_html(soup):
        for node, ancestor_ids in iter_json_nodes(data):
            parsed = None
            method = "embedded_json"
            if isinstance(node, list):
                parsed = json_list_to_chart(node)
            elif isinstance(node, dict):
                parsed = _json_matrix_to_chart(node)
            elif isinstance(node, str) and "<table" in node[:MAX_EMBEDDED_HTML_CHARS].lower():
                inner = BeautifulSoup(node[:MAX_EMBEDDED_HTML_CHARS], "lxml")
                for table in inner.find_all("table"):
                    result = table_to_chart(table, max_rows=max_rows)
                    if result:
                        parsed = result
                        method = "embedded_json_html"
                        break
            if not parsed:
                continue

            sizes, unit, raw = parsed
            clean, _reason = validate_chart(sizes, max_rows)
            if not clean:
                continue

            id_match = bool(product_ids) and any(pid in ancestor_ids for pid in product_ids)
            found.append(Candidate(
                sizes=clean,
                unit=unit,
                method=method,
                confidence=0.85 if id_match else 0.70,
                evidence={
                    "json_origin": origin,
                    "product_id_in_context": id_match,
                },
                raw=raw,
                measurement_basis=(raw.get("measurement_basis") or None),
            ))
    return found


def _table_context(table) -> str:
    parts = []
    caption = table.find("caption")
    if caption:
        parts.append(caption.get_text(" ", strip=True))
    prev = table.find_previous(string=True)
    if prev:
        parts.append(str(prev))
    parent = table.parent
    if parent is not None:
        parts.append(clean_text(parent.get_text(" ", strip=True))[:300])
    return " ".join(parts)


def extract_from_html(soup, max_rows: int = DEFAULT_MAX_CHART_ROWS) -> list[Candidate]:
    """All valid measurement tables on the page."""
    found: list[Candidate] = []
    for table in soup.find_all("table"):
        # Skip tables that merely wrap other tables; the inner ones are visited.
        if table.find("table"):
            continue
        context = _table_context(table)
        result = table_to_chart(table, context, max_rows)
        if not result:
            continue
        sizes, unit, raw = result
        in_size_guide = bool(SIZE_GUIDE_RE.search(context))
        found.append(Candidate(
            sizes=sizes,
            unit=unit,
            method="html_table",
            confidence=0.80 if in_size_guide else 0.75,
            evidence={"size_guide_context": in_size_guide},
            raw=raw,
        ))
    return found


# ============================================================
# SIZE-CHART IMAGES
# ============================================================

SIZE_GUIDE_RE = re.compile(
    r"size\s*[-_]?\s*(chart|guide)|size\s*&\s*fit|measurement\s*(chart|guide)|fit\s*guide",
    re.I,
)
_IMAGE_KEYWORD_RE = re.compile(
    r"size[\s_\-]?(chart|guide)|sizechart|sizeguide|measurement", re.I
)


def find_size_chart_images(soup, base_url: str = "") -> list[dict[str, str]]:
    """
    Likely size-chart images, from alt/title/src or nearby size-guide text.
    These are only candidates for OCR; they are never stored as measurements.
    """
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    for img in soup.find_all("img"):
        src = (
            img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            or (img.get("srcset") or "").split(" ")[0]
        )
        if not src or src.startswith("data:") or src.lower().endswith(".svg"):
            continue
        try:
            if int(img.get("width") or 999) < 100 or int(img.get("height") or 999) < 100:
                continue
        except ValueError:
            pass

        attrs = f"{img.get('alt', '')} {img.get('title', '')}"
        reason = None
        if _IMAGE_KEYWORD_RE.search(attrs):
            reason = "alt_or_title"
        elif _IMAGE_KEYWORD_RE.search(src):
            reason = "url"
        else:
            # Only small wrappers count as "near": a page-level container that
            # mentions "size chart" somewhere says nothing about this image.
            container = img.parent
            for _ in range(2):
                if container is None or container.name in ("body", "html", "[document]"):
                    break
                text = clean_text(container.get_text(" ", strip=True))
                if len(text) > 300:
                    break
                if SIZE_GUIDE_RE.search(text):
                    reason = "nearby_text"
                    break
                container = container.parent
        if not reason:
            continue

        url = urljoin(base_url, src) if base_url else src
        if url in seen:
            continue
        seen.add(url)
        candidates.append({"url": url, "reason": reason})
        if len(candidates) >= MAX_IMAGE_CANDIDATES:
            break

    return candidates


# ============================================================
# ANTI-BOT / CHALLENGE DETECTION
# ============================================================

_CHALLENGE_TEXT_RE = re.compile(
    r"unusual traffic|verify (?:that )?you are (?:a )?human|are you a robot|"
    r"automated requests|not a robot|captcha|enter the characters you see below|"
    r"pardon our interruption|press (?:&|and) hold|access denied|"
    r"request unsuccessful|checking your browser|attention required",
    re.I,
)
_CHALLENGE_ELEMENT_SELECTORS = (
    "iframe[src*='recaptcha']", "iframe[src*='hcaptcha']",
    "iframe[src*='challenges.cloudflare.com']", ".g-recaptcha", ".h-captcha",
    "#cf-challenge-running", "#challenge-form", "form[action*='captcha']",
    "#px-captcha", "form[action*='validateCaptcha']",
)
_CHALLENGE_URL_RE = re.compile(r"google\.[^/]+/sorry/|/captcha|validatecaptcha|/challenge", re.I)
# Real product pages have lots of visible text; challenge pages are short.
_SMALL_PAGE_CHARS = 4000


def visible_text(soup, limit: int = 20000) -> str:
    parts = []
    size = 0
    for s in soup.find_all(string=True):
        parent = s.parent.name if s.parent else ""
        if parent in ("script", "style", "noscript", "template", "head", "title"):
            continue
        text = clean_text(s)
        if text:
            parts.append(text)
            size += len(text)
            if size >= limit:
                break
    return " ".join(parts)


def detect_challenge(soup, http_status: int | None = None, url: str = "") -> str | None:
    """
    Return a short reason when the page is an anti-bot / CAPTCHA challenge.

    Only the page title, visible text and challenge widgets are inspected, not
    script bodies: real product pages routinely mention "captcha" in JS config.
    """
    if url and _CHALLENGE_URL_RE.search(url):
        return f"challenge_url:{urlparse(url).path[:80]}"

    title = clean_text(soup.title.get_text()) if soup.title else ""
    if title and _CHALLENGE_TEXT_RE.search(title):
        return f"challenge_title:{title[:80]}"

    text = visible_text(soup)
    small = len(text) < _SMALL_PAGE_CHARS

    if small:
        m = _CHALLENGE_TEXT_RE.search(text)
        if m:
            return f"challenge_text:{m.group(0).lower()}"
        for selector in _CHALLENGE_ELEMENT_SELECTORS:
            if soup.select_one(selector) is not None:
                return f"challenge_element:{selector}"

    if http_status in (403, 429):
        return f"http_{http_status}"
    return None


# ============================================================
# PRODUCT IDENTITY
# ============================================================

_TITLE_STOPWORDS = frozenset({
    "buy", "online", "india", "the", "and", "for", "with", "from", "best",
    "price", "shop", "men", "women", "mens", "womens", "kids", "boys", "girls",
    "unisex", "new", "size", "sizes", "pack", "set",
})


def _title_tokens(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-z0-9]+", clean_text(text).lower())
        if len(t) >= 3 and t not in _TITLE_STOPWORDS and not t.isdigit()
    }


def page_titles(soup) -> str:
    parts = []
    if soup.title:
        parts.append(soup.title.get_text(" ", strip=True))
    for sel in ("meta[property='og:title']", "meta[name='twitter:title']"):
        tag = soup.select_one(sel)
        if tag and tag.get("content"):
            parts.append(tag["content"])
    h1 = soup.find("h1")
    if h1:
        parts.append(h1.get_text(" ", strip=True))
    return " ".join(parts)


def title_overlap(expected_title: str | None, page_title_text: str) -> float | None:
    """Share of the catalogue title's words that appear in the page's title/h1."""
    expected = _title_tokens(expected_title or "")
    if len(expected) < 3 or not page_title_text:
        return None
    return len(expected & _title_tokens(page_title_text)) / len(expected)


# ============================================================
# RETAILER ADAPTERS
# ============================================================

@dataclass
class AdapterResult:
    chart: Candidate | None = None
    # True when the adapter found the retailer's authoritative product data
    # and it has no measurements: skip generic fallbacks and the browser.
    authoritative: bool = False
    status: str | None = None          # e.g. "product_mismatch"
    error: str | None = None
    images: list[dict[str, str]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetailerAdapter:
    name: str
    product_id_from_url: Callable[[str], str | None]
    extract_embedded: Callable[[str, str, str | None, int], AdapterResult] | None = None
    use_browser: bool = True
    size_guide_selectors: tuple[str, ...] = (
        "text=/size\\s*(chart|guide)/i",
        "text=/size\\s*&\\s*fit/i",
        "text=/measurement\\s*(chart|guide)/i",
    )


def _regex_id(pattern: str, group: int = 1, flags: int = re.I) -> Callable[[str], str | None]:
    compiled = re.compile(pattern, flags)

    def extract(url: str) -> str | None:
        m = compiled.search(urlparse(url).path or "")
        return m.group(group) if m else None

    return extract


def _query_id(param: str) -> Callable[[str], str | None]:
    def extract(url: str) -> str | None:
        values = parse_qs(urlparse(url).query).get(param)
        return values[0] if values else None

    return extract


def _no_id(_url: str) -> str | None:
    return None


# ------------------------------------------------------------
# Myntra
# ------------------------------------------------------------

# Exact assignment only. A bare "__myx" search would hit
# "window.__myx_deviceType__ = 'desktop';" which appears first on the page.
MYNTRA_STATE_RE = re.compile(r"window\.__myx\s*=(?!=)")
_MYNTRA_ID_RE = re.compile(r"/(\d{4,})(?:/buy)?/?$")


def myntra_product_id(url: str) -> str | None:
    m = _MYNTRA_ID_RE.search(urlparse(url).path or "")
    return m.group(1) if m else None


def extract_myntra_state(html: str) -> dict | None:
    for m in MYNTRA_STATE_RE.finditer(html):
        value = decode_js_assignment(html[m.start():], MYNTRA_STATE_RE)
        if isinstance(value, dict):
            return value
    return None


def extract_myntra(
    html: str,
    url: str,
    url_product_id: str | None,
    max_rows: int = DEFAULT_MAX_CHART_ROWS,
) -> AdapterResult:
    """
    Product-specific measurements from window.__myx.pdpData.sizes[].measurements.

    Only sizes whose styleId equals pdpData.id are used, and pdpData.id must
    equal the style id in the URL when the URL carries one.
    """
    state = extract_myntra_state(html)
    pdp = state.get("pdpData") if isinstance(state, dict) else None
    if not isinstance(pdp, dict):
        return AdapterResult(error="myntra_state_not_found")

    pdp_id = str(pdp.get("id") or "")
    evidence = {
        "url_product_id": url_product_id,
        "embedded_product_id": pdp_id or None,
        "embedded_name": pdp.get("name"),
    }

    if url_product_id and pdp_id and url_product_id != pdp_id:
        return AdapterResult(
            authoritative=True,
            status="product_mismatch",
            error=f"myntra pdpData.id={pdp_id} != url id={url_product_id}",
            evidence=evidence,
        )

    sizes_data = [
        s for s in (pdp.get("sizes") or [])
        if isinstance(s, dict)
        and (not pdp_id or str(s.get("styleId") or pdp_id) == pdp_id)
    ]
    basis = _dominant_basis(sizes_data)

    sizes: dict[str, dict[str, str]] = {}
    raw_rows: dict[str, Any] = {}
    units: list[str] = []
    for item in sizes_data:
        label = clean_text(item.get("label"))
        if not label or label in sizes:
            continue
        row, row_units = measurement_entries_to_row(item.get("measurements") or [], basis)
        if not row:
            continue
        sizes[label] = row
        units.extend(row_units)
        raw_rows[label] = {
            "skuId": item.get("skuId"),
            "styleId": item.get("styleId"),
            "available": item.get("available"),
            "measurements": item.get("measurements"),
            "allSizesList": item.get("allSizesList"),
        }

    # The size-chart image on Myntra ("sizeRepresentationUrl") is a generic
    # how-to-measure illustration per category; only sizeChartUrl is a chart.
    chart_image = (pdp.get("sizechart") or {}).get("sizeChartUrl")
    images = [{"url": chart_image, "reason": "myntra_sizeChartUrl"}] if chart_image else []

    clean, reason = validate_chart(sizes, max_rows)
    if not clean:
        return AdapterResult(
            authoritative=bool(pdp_id),
            error=f"myntra: {reason}",
            images=images,
            evidence=evidence,
        )

    id_match = bool(url_product_id) and url_product_id == pdp_id
    evidence["product_id_match"] = id_match
    evidence["sku_ids"] = [raw_rows[k]["skuId"] for k in raw_rows]

    unit = max(set(units), key=units.count) if units else None
    return AdapterResult(
        chart=Candidate(
            sizes=clean,
            unit=unit,
            method="embedded_json",
            # URL style id == pdpData.id == every size's styleId.
            confidence=0.95 if id_match else 0.80,
            evidence=evidence,
            raw={"rows": raw_rows, "state": "window.__myx.pdpData.sizes"},
            measurement_basis=basis.lower() if basis else None,
        ),
        authoritative=True,
        evidence=evidence,
    )


# ------------------------------------------------------------
# Registry
# ------------------------------------------------------------

ADAPTERS: dict[str, RetailerAdapter] = {
    "myntra": RetailerAdapter("myntra", myntra_product_id, extract_myntra),
    "amazon": RetailerAdapter(
        "amazon", _regex_id(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", flags=0)
    ),
    "flipkart": RetailerAdapter("flipkart", _query_id("pid")),
    "ajio": RetailerAdapter("ajio", _regex_id(r"/p/([A-Za-z0-9_]+)")),
    "nykaa": RetailerAdapter("nykaa", _regex_id(r"/p/(\d+)")),
    "manyavar": RetailerAdapter("manyavar", _regex_id(r"/([A-Za-z0-9]+)\.html$")),
    "pernia": RetailerAdapter("pernia", _no_id),
    "other": RetailerAdapter("other", _no_id),
}


def adapter_for(url: str) -> RetailerAdapter:
    return ADAPTERS.get(site_from_url(url), ADAPTERS["other"])


# ============================================================
# PAGE-LEVEL EXTRACTION
# ============================================================

@dataclass
class PageResult:
    """
    status: found | not_found | blocked | image_chart | product_mismatch
    """
    status: str
    chart: Chart | None = None
    error: str | None = None
    images: list[dict[str, str]] = field(default_factory=list)
    authoritative: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


def _pick_candidate(candidates: list[Candidate]) -> tuple[Candidate, int]:
    """
    Highest-confidence candidate. When several distinct charts are present
    (e.g. a men's and a women's guide) the choice is ambiguous, so confidence
    is reduced rather than asserted.
    """
    best = max(candidates, key=lambda c: c.confidence)
    distinct = {tuple(sorted(c.sizes)) + tuple(sorted({k for r in c.sizes.values() for k in r}))
                for c in candidates}
    if len(distinct) > 1:
        best.confidence = round(max(0.5, best.confidence - 0.15), 4)
        best.evidence["ambiguous_candidates"] = len(distinct)
    return best, len(candidates)


def extract_page(
    html: str,
    url: str,
    *,
    requested_url: str | None = None,
    expected_title: str | None = None,
    http_status: int | None = None,
    via: str = "http",
    max_rows: int = DEFAULT_MAX_CHART_ROWS,
) -> PageResult:
    """
    Run the full extraction priority on one page.

    `url` is the final (post-redirect) URL; `requested_url` the URL we asked
    for, used to detect redirects away from the product.
    """
    soup = BeautifulSoup(html or "", "lxml")
    site = site_from_url(url)
    adapter = adapter_for(url)
    prefix = "" if via == "http" else f"{via}_"

    challenge = detect_challenge(soup, http_status, url)
    if challenge:
        return PageResult(status="blocked", error=challenge)

    requested_id = adapter.product_id_from_url(requested_url or url)
    final_id = adapter.product_id_from_url(url)
    if requested_id and final_id != requested_id:
        return PageResult(
            status="product_mismatch",
            error=f"redirected away from product {requested_id} to {url[:200]}",
        )

    product_ids = tuple(p for p in (final_id,) if p)
    evidence: dict[str, Any] = {"site": site, "url_product_id": final_id}

    overlap = title_overlap(expected_title, page_titles(soup))
    evidence["title_overlap"] = None if overlap is None else round(overlap, 3)

    # 1. Retailer-specific embedded product JSON.
    images: list[dict[str, str]] = []
    if adapter.extract_embedded:
        res = adapter.extract_embedded(html, url, final_id, max_rows)
        evidence.update(res.evidence)
        images.extend(res.images)
        if res.status == "product_mismatch":
            return PageResult(status="product_mismatch", error=res.error, evidence=evidence)
        if res.chart:
            return _found(res.chart, site, prefix, evidence, overlap, strong_id=True)
        if res.authoritative:
            if images:
                return PageResult(status="image_chart", images=images, error=res.error,
                                  authoritative=True, evidence=evidence)
            return PageResult(status="not_found", error=res.error,
                              authoritative=True, evidence=evidence)

    # 2. Generic embedded JSON, 3. HTML tables.
    candidates = extract_from_json(soup, product_ids, max_rows) + extract_from_html(soup, max_rows)
    if candidates:
        best, count = _pick_candidate(candidates)
        evidence["candidate_count"] = count
        strong = bool(best.evidence.get("product_id_in_context"))
        return _found(best, site, prefix, evidence, overlap, strong_id=strong)

    # 5. Image candidates (OCR is a separate stage).
    images.extend(find_size_chart_images(soup, url))
    if images:
        return PageResult(status="image_chart", images=images[:MAX_IMAGE_CANDIDATES], evidence=evidence)

    return PageResult(status="not_found", error="no measurement chart on page", evidence=evidence)


def _found(
    cand: Candidate,
    site: str,
    prefix: str,
    evidence: dict[str, Any],
    overlap: float | None,
    strong_id: bool,
) -> PageResult:
    evidence = {**evidence, **cand.evidence}
    # Without an id tie, a page whose title does not look like the catalogue
    # product is not trusted: its chart may belong to something else.
    if not strong_id and overlap is not None and overlap < 0.3:
        return PageResult(
            status="product_mismatch",
            error=f"page title does not match product (overlap={overlap:.2f})",
            evidence=evidence,
        )
    confidence = cand.confidence
    if not strong_id and overlap is None:
        confidence -= 0.05
    chart = Chart(
        sizes=cand.sizes,
        unit=cand.unit,
        method=(prefix + cand.method)[:50],
        source=site,
        confidence=round(max(0.0, min(confidence, 1.0)), 4),
        measurement_basis=cand.measurement_basis,
        evidence=evidence,
        raw_rows=cand.raw.get("rows") or {},
        raw={k: v for k, v in cand.raw.items() if k != "rows"},
    )
    return PageResult(status="found", chart=chart, evidence=evidence)

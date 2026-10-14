"""
Read size-chart images with Gemini (Interactions API).

Used only for images that sit in the product's own description. Each image
is read twice with differently worded instructions; the chart is accepted
only when both reads agree, pass the normal chart validation, and their
sizes match the sizes the product is sold in. Nothing is guessed: any doubt
returns a non-"found" status and nothing is saved.
"""

from __future__ import annotations

import base64
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx

from extraction import (
    Chart,
    canonical_measurement_key,
    clean_text,
    is_measurement_key,
    normalize_size_label,
    normalize_unit,
    validate_chart,
)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/heic", "image/heif")
MAX_IMAGE_BYTES = 5_000_000
MIN_SIZE_OVERLAP = 2

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_measurement_chart": {"type": "boolean"},
        "chart_count": {"type": "integer"},
        "unit": {"type": "string", "enum": ["cm", "inch", "mm", "unknown"]},
        "measurement_basis": {"type": "string", "enum": ["garment", "body", "unknown"]},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "size": {"type": "string"},
                    "measurements": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["name", "value"],
                        },
                    },
                },
                "required": ["size", "measurements"],
            },
        },
    },
    "required": ["is_measurement_chart", "chart_count", "unit", "measurement_basis", "rows"],
}

_RULES = (
    "Rules: copy every number exactly as printed (keep ranges like 38-40, keep decimals). "
    "Do not convert units, round, estimate or fill in missing cells; leave a cell out if it is unreadable. "
    "If the image is not a size/measurement chart, set is_measurement_chart to false and rows to []. "
    "If the image contains more than one chart (for example for different products or categories), "
    "set chart_count to how many there are. "
    "Measurement names are the column/row headers such as Chest, Waist, Hip, Length, Shoulder, Sleeve, Inseam. "
    "Size-conversion columns (UK, US, EU) and anything that is not a body or garment measurement are not measurements."
)
PROMPTS = (
    "This image is from a clothing product's description. Transcribe the size chart into JSON: "
    "one row per size, each with its measurements. " + _RULES,
    "Read the measurement table in this image. For each size label in the table, list every "
    "measurement name and its value exactly as shown. Report the unit printed on the chart. " + _RULES,
)


@dataclass
class OcrResult:
    """status: found | not_chart | disagree | size_mismatch | ambiguous | error"""
    status: str
    chart: Chart | None = None
    error: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


class GeminiError(Exception):
    pass


def fetch_image(client: httpx.Client, url: str) -> tuple[bytes, str]:
    with client.stream("GET", url) as response:
        if response.status_code != 200:
            raise GeminiError(f"image HTTP {response.status_code}")
        mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if mime not in IMAGE_TYPES:
            raise GeminiError(f"unsupported image type {mime or 'unknown'}")
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                raise GeminiError("image larger than 5 MB")
    return bytes(data), mime


def _response_text(body: dict) -> str:
    """Model text from an Interaction: steps[].content[] items of type text."""
    texts = []
    for step in body.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        for item in step.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                texts.append(item["text"])
    return "".join(texts)


def ask_gemini(client: httpx.Client, api_key: str, model: str, prompt: str,
               image: bytes, mime: str, timeout: float) -> dict:
    payload = {
        "model": model,
        "input": [
            {"type": "text", "text": prompt},
            {"type": "image", "data": base64.b64encode(image).decode("ascii"), "mime_type": mime},
        ],
        "response_format": {"type": "text", "mime_type": "application/json", "schema": RESPONSE_SCHEMA},
    }
    try:
        response = client.post(GEMINI_URL, json=payload, timeout=timeout,
                               headers={"x-goog-api-key": api_key, "Content-Type": "application/json"})
    except httpx.HTTPError as exc:
        raise GeminiError(f"gemini request failed: {type(exc).__name__}") from None
    if response.status_code != 200:
        # Body may describe the problem (bad key, quota); it never contains the key.
        raise GeminiError(f"gemini HTTP {response.status_code}: {clean_text(response.text)[:200]}")
    body = response.json()
    if body.get("status") not in (None, "completed"):
        raise GeminiError(f"gemini status {body.get('status')}: {body.get('errors')}")
    text = _response_text(body)
    if not text:
        raise GeminiError("gemini returned no text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise GeminiError("gemini returned invalid JSON") from None


def reading_to_sizes(reading: dict) -> dict[str, dict[str, str]]:
    sizes: dict[str, dict[str, str]] = {}
    for row in reading.get("rows") or []:
        if not isinstance(row, dict):
            continue
        label = clean_text(row.get("size"))
        if not label or label in sizes:
            continue
        measurements: dict[str, str] = {}
        for entry in row.get("measurements") or []:
            if not isinstance(entry, dict):
                continue
            key, _unit = canonical_measurement_key(entry.get("name"))
            if is_measurement_key(key) and key not in measurements:
                measurements[key] = clean_text(entry.get("value"))
        sizes[label] = measurements
    return sizes


def _norm_size(label: str) -> str:
    return normalize_size_label(label).upper().replace(" ", "")


def size_overlap(chart_sizes, product_sizes) -> int:
    product = {_norm_size(s) for s in (product_sizes or []) if clean_text(s)}
    return len({_norm_size(s) for s in chart_sizes} & product)


@dataclass
class ImageReading:
    """Product-independent result of reading one image (cached by URL)."""
    status: str                     # chart | not_chart | disagree | ambiguous | error
    sizes: dict[str, dict[str, str]] = field(default_factory=dict)
    unit: str | None = None
    basis: str | None = None
    raw_rows: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# Store-wide chart images appear on every product of a store; read each once.
_CACHE: "OrderedDict[str, ImageReading]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
CACHE_SIZE = 5000


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def read_image(client: httpx.Client, image_url: str, *, api_key: str, model: str,
               timeout: float = 60, max_rows: int = 100) -> ImageReading:
    with _CACHE_LOCK:
        if image_url in _CACHE:
            _CACHE.move_to_end(image_url)
            return _CACHE[image_url]
    reading = _read_image_uncached(client, image_url, api_key=api_key, model=model,
                                   timeout=timeout, max_rows=max_rows)
    if reading.status != "error":  # errors (quota, network) are retried later
        with _CACHE_LOCK:
            _CACHE[image_url] = reading
            while len(_CACHE) > CACHE_SIZE:
                _CACHE.popitem(last=False)
    return reading


def _read_image_uncached(client, image_url, *, api_key, model, timeout, max_rows) -> ImageReading:
    try:
        image, mime = fetch_image(client, image_url)
        readings = [ask_gemini(client, api_key, model, prompt, image, mime, timeout) for prompt in PROMPTS]
    except GeminiError as exc:
        return ImageReading("error", error=str(exc))
    except (httpx.HTTPError, ValueError) as exc:
        return ImageReading("error", error=f"{type(exc).__name__}: {exc}"[:300])

    if not all(r.get("is_measurement_chart") for r in readings):
        return ImageReading("not_chart", error="image is not a measurement chart")
    if any((r.get("chart_count") or 1) > 1 for r in readings):
        return ImageReading("ambiguous", error="image holds more than one chart")

    validated = []
    for reading in readings:
        clean, reason = validate_chart(reading_to_sizes(reading), max_rows)
        if not clean:
            return ImageReading("not_chart", error=f"image chart invalid: {reason}")
        validated.append(clean)

    units = {normalize_unit(r.get("unit")) for r in readings}
    if validated[0] != validated[1] or len(units) != 1:
        return ImageReading("disagree", error="the two reads of the image differ")

    basis = readings[0].get("measurement_basis")
    raw_by_label = {
        normalize_size_label(clean_text(row.get("size"))): row
        for row in readings[0].get("rows") or [] if isinstance(row, dict)
    }
    return ImageReading(
        "chart", sizes=validated[0], unit=units.pop(),
        basis=basis if basis in ("garment", "body") else None,
        raw_rows={label: raw_by_label.get(label) for label in validated[0]},
    )


def read_chart_image(
    client: httpx.Client,
    image_url: str,
    *,
    api_key: str,
    model: str,
    product_sizes: list[str] | None,
    timeout: float = 60,
    max_rows: int = 100,
    scope: str = "product",
) -> OcrResult:
    """Read one image and check its sizes against this product's sizes."""
    evidence: dict[str, Any] = {"image_url": image_url, "model": model}
    reading = read_image(client, image_url, api_key=api_key, model=model,
                         timeout=timeout, max_rows=max_rows)
    if reading.status != "chart":
        status = {"error": "error", "disagree": "disagree", "ambiguous": "ambiguous"}.get(
            reading.status, "not_chart")
        return OcrResult(status, error=reading.error, evidence=evidence)

    overlap = size_overlap(reading.sizes, product_sizes)
    evidence.update(reads_agree=True, size_overlap=overlap, product_sizes=list(product_sizes or []),
                    chart_scope=scope)
    needed = min(MIN_SIZE_OVERLAP, len(product_sizes or []))
    if not product_sizes or overlap < max(needed, 1):
        return OcrResult("size_mismatch", evidence=evidence,
                         error=f"chart sizes {list(reading.sizes)} don't match product sizes "
                               f"{list(product_sizes or [])}")
    chart = Chart(
        sizes=reading.sizes, unit=reading.unit, method="image_ocr", source="image",
        confidence=0.70 if scope == "product" else 0.65,
        measurement_basis=reading.basis, evidence=evidence, raw_rows=reading.raw_rows,
    )
    return OcrResult("found", chart=chart, evidence=evidence)

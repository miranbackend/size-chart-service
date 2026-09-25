"""
Resolve a catalogue product URL to the retailer's product page.

Separate from extraction on purpose: this stage only decides *which* page
belongs to the product. It never fabricates a URL and never tries to get
past Google's CAPTCHA / "unusual traffic" pages.

Order:
  1. Direct retailer URL                      -> used as-is
  2. Destination embedded in a Google URL     -> ?url= / ?q= / ?adurl=
  3. Retailer link already stored in gsa_product.raw
  4. (opt-in) plain HTTP redirect follow of the Google URL
Anything else is "unresolved".
"""

from __future__ import annotations

import json
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from extraction import detect_challenge, is_google_url, site_from_url

# Keys in the stored Google Shopping payload that point at the seller's page,
# most specific first.
RAW_LINK_KEYS = (
    "product_link_direct", "merchant_link", "offer_link", "seller_link",
    "retailer_url", "merchant_url", "link", "url", "product_link", "source_url",
)
_REDIRECT_PARAMS = ("url", "q", "adurl", "u", "dest")
MAX_RAW_NODES = 20_000


def _is_http_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 4000:
        return False
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def unwrap_google_redirect(url: str) -> str | None:
    """Destination URL carried as a query parameter of a Google link."""
    if not is_google_url(url):
        return None
    params = parse_qs(urlparse(url).query)
    for name in _REDIRECT_PARAMS:
        for value in params.get(name, []):
            if _is_http_url(value) and not is_google_url(value):
                return value
    return None


def _retailer_url(value: Any) -> str | None:
    if not _is_http_url(value):
        return None
    if is_google_url(value):
        return unwrap_google_redirect(value)
    return value


def _iter_raw_links(raw: Any) -> Iterator[tuple[str, str]]:
    """(key_path, url) pairs for link-like keys, bounded walk."""
    stack: list[tuple[Any, str]] = [(raw, "")]
    seen = 0
    while stack and seen < MAX_RAW_NODES:
        node, path = stack.pop(0)
        seen += 1
        if isinstance(node, dict):
            for key in RAW_LINK_KEYS:
                if key in node:
                    url = _retailer_url(node[key])
                    if url:
                        yield f"{path}.{key}".lstrip("."), url
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    stack.append((value, f"{path}.{key}".lstrip(".")))
        elif isinstance(node, list):
            for i, value in enumerate(node[:200]):
                if isinstance(value, (dict, list)):
                    stack.append((value, f"{path}[{i}]"))


def retailer_links_from_raw(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for path, url in _iter_raw_links(raw):
        if url not in seen:
            seen.add(url)
            found.append({"path": path, "url": url, "retailer": site_from_url(url)})
    return found


def _result(status: str, original: str, **kw) -> dict[str, Any]:
    return {
        "status": status,
        "original_url": original,
        "resolved_url": kw.pop("resolved_url", None),
        "retailer": kw.pop("retailer", None),
        "method": kw.pop("method", None),
        "error": kw.pop("error", None),
        "http_status": kw.pop("http_status", None),
        **kw,
    }


def resolve_product_url(
    product_url: str,
    raw: Any = None,
    *,
    http_client: httpx.Client | None = None,
    allow_google_http: bool = False,
) -> dict[str, Any]:
    """
    status: resolved | unresolved | blocked | failed
    """
    original = (product_url or "").strip()
    if not _is_http_url(original):
        return _result("failed", original, error="missing or invalid product_url")

    if not is_google_url(original):
        return _result("resolved", original, resolved_url=original,
                       retailer=site_from_url(original), method="direct_url")

    embedded = unwrap_google_redirect(original)
    if embedded:
        return _result("resolved", original, resolved_url=embedded,
                       retailer=site_from_url(embedded), method="google_url_param")

    links = retailer_links_from_raw(raw)
    if links:
        best = links[0]
        return _result("resolved", original, resolved_url=best["url"],
                       retailer=best["retailer"], method=f"raw:{best['path']}"[:60],
                       candidates=links[:10])

    if allow_google_http and http_client is not None:
        return _resolve_via_http(original, http_client)

    return _result("unresolved", original,
                   error="Google Shopping URL has no retailer link in URL or raw data")


def _resolve_via_http(original: str, client: httpx.Client) -> dict[str, Any]:
    """Follow ordinary redirects only; a challenge page ends resolution."""
    try:
        response = client.get(original)
    except httpx.TooManyRedirects:
        return _result("failed", original, method="http_redirect", error="too_many_redirects")
    except httpx.HTTPError as exc:
        return _result("failed", original, method="http_redirect",
                       error=f"{type(exc).__name__}: {exc}"[:500])

    final_url = str(response.url)
    soup = BeautifulSoup(response.text[:500_000], "lxml")
    challenge = detect_challenge(soup, response.status_code, final_url)
    if challenge:
        return _result("blocked", original, method="http_redirect",
                       http_status=response.status_code, error=f"google: {challenge}")

    if is_google_url(final_url):
        destination = unwrap_google_redirect(final_url)
        if not destination:
            return _result("unresolved", original, method="http_redirect",
                           http_status=response.status_code,
                           error="Google Shopping URL did not redirect to a retailer")
        final_url = destination

    return _result("resolved", original, resolved_url=final_url,
                   retailer=site_from_url(final_url), method="http_redirect",
                   http_status=response.status_code)

import httpx

from url_resolution import resolve_product_url

GOOGLE = "https://www.google.com/search?ibp=oshop&q=cahoot+shirt&prds=catalogid:123&tbm=shop"
MYNTRA = "https://www.myntra.com/shirts/cahoot/x/31472271/buy"


def test_direct_retailer_url_is_used_as_is():
    r = resolve_product_url(MYNTRA)
    assert r["status"] == "resolved"
    assert r["resolved_url"] == MYNTRA
    assert r["retailer"] == "myntra"
    assert r["method"] == "direct_url"


def test_google_url_with_embedded_destination():
    url = "https://www.google.com/url?q=https://www.ajio.com/p/469999&sa=U"
    r = resolve_product_url(url)
    assert r["status"] == "resolved"
    assert r["resolved_url"] == "https://www.ajio.com/p/469999"
    assert r["retailer"] == "ajio"


def test_google_url_resolved_from_raw_payload():
    raw = {"title": "Shirt", "product_link": GOOGLE,
           "offers": [{"merchant": "Myntra", "link": MYNTRA}]}
    r = resolve_product_url(GOOGLE, raw)
    assert r["status"] == "resolved"
    assert r["resolved_url"] == MYNTRA
    assert r["method"].startswith("raw:")


def test_google_url_without_retailer_link_is_unresolved_and_not_fetched():
    def boom(request):  # the resolver must not hit Google unless opted in
        raise AssertionError("unexpected HTTP request")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    r = resolve_product_url(GOOGLE, {"product_link": GOOGLE}, http_client=client)
    assert r["status"] == "unresolved"
    assert r["resolved_url"] is None


def test_opt_in_http_resolution_reports_google_block():
    def sorry(request):
        return httpx.Response(429, text="<html><body>Our systems have detected unusual traffic"
                                        "</body></html>", request=request)

    client = httpx.Client(transport=httpx.MockTransport(sorry))
    r = resolve_product_url(GOOGLE, None, http_client=client, allow_google_http=True)
    assert r["status"] == "blocked"
    assert r["resolved_url"] is None


def test_opt_in_http_resolution_follows_plain_redirect():
    def handler(request):
        if request.url.host == "www.google.com":
            return httpx.Response(302, headers={"location": MYNTRA})
        return httpx.Response(200, text="<html><title>Shirt</title><body>" + "x " * 3000 + "</body></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    r = resolve_product_url(GOOGLE, None, http_client=client, allow_google_http=True)
    assert r["status"] == "resolved"
    assert r["resolved_url"] == MYNTRA


def test_invalid_url_fails():
    assert resolve_product_url("")["status"] == "failed"

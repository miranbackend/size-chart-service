import gzip
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from extraction import (
    canonical_measurement_key,
    detect_challenge,
    detect_unit,
    extract_myntra_state,
    extract_page,
    find_size_chart_images,
    looks_like_size,
    measurement_value_is_valid,
    normalize_measurement_value,
    normalize_unit,
    site_from_url,
    validate_chart,
)

FIXTURES = Path(__file__).parent / "fixtures"
MYNTRA_URL = (
    "https://www.myntra.com/shirts/cahoot/"
    "cahoot-men-comfort-opaque-checked-casual-shirt/31472271/buy"
)


def myntra_html() -> str:
    with gzip.open(FIXTURES / "myntra_31472271.html.gz", "rt", encoding="utf-8") as f:
        return f.read()


def page(body: str, title: str = "Product page") -> str:
    filler = "<p>" + ("Product description and details. " * 150) + "</p>"
    return f"<html><head><title>{title}</title></head><body>{body}{filler}</body></html>"


# ------------------------------------------------------------------
# 1. Myntra embedded measurement JSON (real downloaded page)
# ------------------------------------------------------------------

def test_myntra_fixture_extracts_product_specific_garment_measurements():
    result = extract_page(myntra_html(), MYNTRA_URL, requested_url=MYNTRA_URL, http_status=200)

    assert result.status == "found", result.error
    chart = result.chart
    assert chart.sizes == {
        "38": {"chest": "44.0", "length": "27.5", "shoulder": "20.5"},
        "40": {"chest": "46.0", "length": "28.0", "shoulder": "21.5"},
        "42": {"chest": "48.0", "length": "28.5", "shoulder": "22.5"},
        "44": {"chest": "50.0", "length": "29.0", "shoulder": "23.5"},
    }
    assert chart.unit == "inch"
    assert chart.chart_type == "measurement_chart"
    assert chart.method == "embedded_json"
    assert chart.source == "myntra"
    assert chart.measurement_basis == "garment"
    assert chart.evidence["product_id_match"] is True
    assert chart.evidence["embedded_product_id"] == "31472271"
    assert chart.confidence == 0.95


def test_myntra_state_marker_skips_device_type_assignment():
    html = myntra_html()
    # The fixture really contains the decoy before the real assignment.
    assert html.find("window.__myx_deviceType__") < html.find("window.__myx =")
    state = extract_myntra_state(html)
    assert state["pdpData"]["id"] == 31472271


def test_myntra_rejects_mismatched_product_id():
    other_url = MYNTRA_URL.replace("31472271", "99999999")
    result = extract_page(myntra_html(), other_url, requested_url=other_url, http_status=200)
    assert result.status == "product_mismatch"
    assert result.chart is None


def test_myntra_without_measurements_is_not_found_not_available_sizes():
    state = {
        "pdpData": {
            "id": 123456,
            "sizes": [
                {"styleId": 123456, "label": "S", "available": True, "measurements": []},
                {"styleId": 123456, "label": "M", "available": True, "measurements": []},
                {"styleId": 123456, "label": "L", "available": False, "measurements": []},
            ],
            "sizechart": {"sizeChartUrl": None, "sizeRepresentationUrl": "http://x/shirt_men.png"},
        }
    }
    html = page(f"<script>window.__myx_deviceType__ = 'desktop';</script>"
                f"<script>window.__myx = {json.dumps(state)};</script>")
    url = "https://www.myntra.com/tshirts/x/y/123456/buy"
    result = extract_page(html, url, requested_url=url)
    assert result.status == "not_found"
    assert result.chart is None


def test_myntra_ignores_sizes_of_other_styles():
    def size(style, label, chest):
        return {"styleId": style, "label": label, "measurements": [
            {"measurementType": "GARMENT", "name": "Chest", "value": chest, "unit": "Inches"}]}

    state = {"pdpData": {"id": 111111, "sizes": [
        size(111111, "S", "38"), size(111111, "M", "40"), size(222222, "L", "99")]}}
    html = page(f"<script>window.__myx = {json.dumps(state)}</script>")
    url = "https://www.myntra.com/x/111111/buy"
    result = extract_page(html, url, requested_url=url)
    assert result.status == "found"
    assert set(result.chart.sizes) == {"S", "M"}


# ------------------------------------------------------------------
# 2. HTML measurement table
# ------------------------------------------------------------------

HTML_TABLE = """
<div class="size-chart"><h3>Size Chart</h3>
<table>
  <tr><th>Size</th><th>Chest (in)</th><th>Waist (in)</th><th>Length (in)</th></tr>
  <tr><td>S</td><td>38</td><td>32</td><td>27</td></tr>
  <tr><td>M</td><td>40</td><td>34</td><td>28</td></tr>
  <tr><td>L</td><td>42 - 44</td><td>36</td><td>29</td></tr>
</table></div>
"""


def test_html_measurement_table_found():
    url = "https://shop.example.com/products/tee"
    result = extract_page(page(HTML_TABLE), url, requested_url=url)
    assert result.status == "found"
    assert result.chart.sizes == {
        "S": {"chest": "38", "waist": "32", "length": "27"},
        "M": {"chest": "40", "waist": "34", "length": "28"},
        "L": {"chest": "42-44", "waist": "36", "length": "29"},
    }
    assert result.chart.unit == "inch"
    assert result.chart.method == "html_table"


def test_transposed_html_table_found():
    table = """<table>
      <tr><th>Size</th><th>S</th><th>M</th><th>L</th></tr>
      <tr><td>Bust (cm)</td><td>86</td><td>91</td><td>96</td></tr>
      <tr><td>Hips (cm)</td><td>94</td><td>99</td><td>104</td></tr>
    </table>"""
    url = "https://shop.example.com/products/dress"
    result = extract_page(page(table), url, requested_url=url)
    assert result.status == "found"
    assert result.chart.sizes["M"] == {"bust": "91", "hip": "99"}
    assert result.chart.unit == "cm"


# ------------------------------------------------------------------
# 3. Generic embedded JSON
# ------------------------------------------------------------------

def test_generic_embedded_json_measurements_tied_to_product_id():
    data = {"props": {"product": {"productId": "SKU123", "sizeChart": [
        {"size": "S", "chest": "36", "waist": "30"},
        {"size": "M", "chest": "38", "waist": "32"},
        {"size": "L", "chest": "40", "waist": "34"},
    ]}}}
    html = page(f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>')
    url = "https://www.ajio.com/brand-shirt/p/SKU123"
    result = extract_page(html, url, requested_url=url)
    assert result.status == "found"
    assert result.chart.sizes["M"] == {"chest": "38", "waist": "32"}
    assert result.chart.method == "embedded_json"
    assert result.chart.evidence["product_id_in_context"] is True
    assert result.chart.confidence == 0.85


def test_window_state_measurement_entries():
    data = {"pdp": {"sizes": [
        {"sizeLabel": "30", "measurements": [{"name": "Waist", "value": "30", "unit": "in"},
                                             {"name": "Inseam", "value": "32", "unit": "in"}]},
        {"sizeLabel": "32", "measurements": [{"name": "Waist", "value": "32", "unit": "in"},
                                             {"name": "Inseam", "value": "32", "unit": "in"}]},
    ]}}
    html = page(f"<script>window.__INITIAL_STATE__ = {json.dumps(data)};</script>")
    url = "https://example.com/jeans"
    result = extract_page(html, url, requested_url=url)
    assert result.status == "found"
    assert result.chart.sizes == {"30": {"waist": "30", "inseam": "32"},
                                  "32": {"waist": "32", "inseam": "32"}}
    assert result.chart.unit == "inch"


# ------------------------------------------------------------------
# 4-6. Things that must NOT be accepted
# ------------------------------------------------------------------

def test_available_sizes_only_not_accepted():
    body = """
    <ul class="sizes"><li>S</li><li>M</li><li>L</li><li>XL</li></ul>
    <table><tr><th>Size</th><th>Availability</th><th>Price</th></tr>
      <tr><td>S</td><td>Available</td><td>999</td></tr>
      <tr><td>M</td><td>In stock</td><td>999</td></tr>
      <tr><td>L</td><td>Unavailable</td><td>1099</td></tr></table>
    <script type="application/ld+json">{"@type":"Product","offers":[
      {"size":"S","price":"999","availability":"InStock"},
      {"size":"M","price":"999","availability":"InStock"}]}</script>
    """
    url = "https://example.com/p/1"
    result = extract_page(page(body), url, requested_url=url)
    assert result.status == "not_found"
    assert result.chart is None


def test_single_measured_row_not_accepted():
    table = """<table><tr><th>Size</th><th>Chest</th><th>Length</th></tr>
      <tr><td>M</td><td>40</td><td>28</td></tr>
      <tr><td>L</td><td>-</td><td></td></tr></table>"""
    url = "https://example.com/p/2"
    assert extract_page(page(table), url, requested_url=url).status == "not_found"
    assert validate_chart({"M": {"chest": "40"}})[0] is None


def test_non_numeric_measurements_not_accepted():
    table = """<table><tr><th>Size</th><th>Chest</th><th>Fit</th></tr>
      <tr><td>S</td><td>Regular</td><td>Yes</td></tr>
      <tr><td>M</td><td>Regular</td><td>Yes</td></tr>
      <tr><td>L</td><td>Available</td><td>Yes</td></tr></table>"""
    url = "https://example.com/p/3"
    assert extract_page(page(table), url, requested_url=url).status == "not_found"
    for bad in ("Available", "In stock", "Yes", "Regular", "Unavailable", "38 (M)", ""):
        assert not measurement_value_is_valid(bad), bad


# ------------------------------------------------------------------
# 7. Image chart detection
# ------------------------------------------------------------------

def test_image_chart_detected_but_not_found():
    body = """
      <img src="/img/logo.png" alt="logo">
      <img src="/media/catalog/abc_size-chart.jpg" alt="product">
      <img data-src="https://cdn.example.com/x.jpg" alt="Size Guide for Kurta">
      <div><h4>Size Chart</h4><img src="/img/table123.png"></div>
    """
    url = "https://example.com/p/4"
    result = extract_page(page(body), url, requested_url=url)
    assert result.status == "image_chart"
    assert result.chart is None
    urls = [i["url"] for i in result.images]
    assert "https://example.com/media/catalog/abc_size-chart.jpg" in urls
    assert "https://cdn.example.com/x.jpg" in urls
    assert "https://example.com/img/table123.png" in urls
    assert "https://example.com/img/logo.png" not in urls


def test_image_helper_ignores_icons():
    soup = BeautifulSoup('<img src="/size-chart-icon.svg"><img src="data:image/png;base64,xx" '
                         'alt="size chart"><img src="/sizechart.png" width="16" height="16">', "lxml")
    assert find_size_chart_images(soup, "https://e.com") == []


# ------------------------------------------------------------------
# 8. CAPTCHA / anti-bot detection
# ------------------------------------------------------------------

@pytest.mark.parametrize("html", [
    "<html><head><title>Sorry...</title></head><body>Our systems have detected unusual "
    "traffic from your computer network.</body></html>",
    "<html><body><h4>Enter the characters you see below</h4>"
    "<form action='/errors/validateCaptcha'></form></body></html>",
    "<html><head><title>Access Denied</title></head><body>You don't have permission</body></html>",
    "<html><body>Please verify you are human<div class='g-recaptcha'></div></body></html>",
    "<html><body><div id='px-captcha'></div>Press &amp; Hold to confirm</body></html>",
])
def test_challenge_pages_are_blocked(html):
    url = "https://example.com/p/5"
    result = extract_page(html, url, requested_url=url, http_status=200)
    assert result.status == "blocked"
    assert result.chart is None


def test_google_sorry_url_is_blocked():
    soup = BeautifulSoup("<html><body>x</body></html>", "lxml")
    assert detect_challenge(soup, 200, "https://www.google.com/sorry/index?continue=x")


def test_real_product_page_mentioning_captcha_in_js_is_not_blocked():
    html = myntra_html()
    assert "captcha" in html  # present in the page's JS config
    assert detect_challenge(BeautifulSoup(html, "lxml"), 200, MYNTRA_URL) is None


def test_http_403_is_blocked():
    url = "https://example.com/p/6"
    assert extract_page(page("<p>hello</p>"), url, requested_url=url, http_status=403).status == "blocked"


# ------------------------------------------------------------------
# 9. Multiple numeric rows accepted
# ------------------------------------------------------------------

def test_multiple_numeric_rows_accepted():
    sizes = {
        "38": {"chest": "96-101 cm", "waist": "38 / 40"},
        "40": {"chest": "101-106 cm", "waist": "40 / 42"},
        "42": {"chest": "38.5", "waist": "Available"},
    }
    clean, reason = validate_chart(sizes)
    assert reason is None
    assert clean == {
        "38": {"chest": "96-101", "waist": "38/40"},
        "40": {"chest": "101-106", "waist": "40/42"},
        "42": {"chest": "38.5"},
    }


@pytest.mark.parametrize("value,expected", [
    ("38", "38"), ("38.5", "38.5"), ("38-40", "38-40"), ("38 / 40", "38/40"),
    ("38 cm", "38"), ("96-101", "96-101"), ("38 – 40 in", "38-40"), ('40"', "40"),
])
def test_valid_measurement_values(value, expected):
    assert normalize_measurement_value(value) == expected


# ------------------------------------------------------------------
# 10. Unit + key normalisation
# ------------------------------------------------------------------

@pytest.mark.parametrize("raw,unit", [
    ("Inches", "inch"), ("in", "inch"), ("inch", "inch"), ('"', "inch"),
    ("cm", "cm"), ("CMS", "cm"), ("Centimetres", "cm"), ("mm", "mm"), ("kg", None),
])
def test_normalize_unit(raw, unit):
    assert normalize_unit(raw) == unit


def test_detect_unit_does_not_treat_english_in_as_inches():
    assert detect_unit("Available in 3 colours") is None
    assert detect_unit("Chest (in)") == "inch"
    assert detect_unit("measurements in inches") == "inch"
    assert detect_unit("38 in") == "inch"
    assert detect_unit("Chest (cm)") == "cm"


@pytest.mark.parametrize("header,key", [
    ("Front Length", "length"), ("Across Shoulder", "shoulder"), ("Bust Size", "bust"),
    ("bust measurement", "bust"), ("Chest Size", "chest"), ("Waist Size", "waist"),
    ("Hips", "hip"), ("Shoulder Width", "shoulder"), ("Sleeve", "sleeve_length"),
    ("Sleeve Length (in)", "sleeve_length"), ("Body Length", "length"),
    ("Garment Length", "length"), ("Foot Length (cm)", "foot_length"), ("Inseam", "inseam"),
    ("To Fit Chest", "chest"), ("Bottom Width", "bottom_width"), ("Thigh", "thigh"),
])
def test_canonical_measurement_keys(header, key):
    assert canonical_measurement_key(header)[0] == key


def test_mixed_unit_columns_keep_both():
    table = """<table><tr><th>Size</th><th>Chest (in)</th><th>Chest (cm)</th><th>Length (in)</th></tr>
      <tr><td>M</td><td>40</td><td>101.6</td><td>28</td></tr>
      <tr><td>L</td><td>42</td><td>106.7</td><td>29</td></tr></table>"""
    url = "https://example.com/p/7"
    result = extract_page(page(table), url, requested_url=url)
    assert result.chart.unit == "inch"
    assert result.chart.sizes["M"] == {"chest": "40", "chest_cm": "101.6", "length": "28"}


# ------------------------------------------------------------------
# Misc guards
# ------------------------------------------------------------------

@pytest.mark.parametrize("label", ["S", "XL", "3XL", "XXL", "38", "UK 7", "32B", "5-6Y", "Free Size", "S/M"])
def test_size_labels(label):
    assert looks_like_size(label)


@pytest.mark.parametrize("label", ["Available", "Chest", "Cotton", "In stock", ""])
def test_not_size_labels(label):
    assert not looks_like_size(label)


def test_site_detection():
    assert site_from_url("https://www.google.com/search?q=x&tbm=shop") == "google_shopping"
    assert site_from_url(MYNTRA_URL) == "myntra"
    assert site_from_url("https://www.amazon.in/dp/B0ABCDEFGH") == "amazon"
    assert site_from_url("https://www.manyavar.com/en-in/kurta/ABC123.html") == "manyavar"
    assert site_from_url("https://www.perniaspopupshop.com/x") == "pernia"
    assert site_from_url("https://www.nykaaman.com/p/123") == "nykaa"
    assert site_from_url("https://notamazon.example.com/x") == "other"


def test_title_mismatch_without_id_is_rejected():
    url = "https://example.com/products/blue-denim-jacket"
    html = page(HTML_TABLE, title="Blue Denim Jacket for Men")
    ok = extract_page(html, url, requested_url=url, expected_title="Levis Blue Denim Trucker Jacket")
    bad = extract_page(html, url, requested_url=url, expected_title="Floral Silk Saree With Blouse Piece")
    assert ok.status == "found"
    assert bad.status == "product_mismatch"


def test_redirect_away_from_product_is_mismatch():
    requested = "https://www.amazon.in/dp/B0ABCDEFGH"
    result = extract_page(page(HTML_TABLE), "https://www.amazon.in/", requested_url=requested)
    assert result.status == "product_mismatch"


@pytest.mark.parametrize("header,key", [
    ("sleeveLength", "sleeve_length"), ("chestSize", "chest"), ("waist_size", "waist"),
    ("frontLength", "length"),
])
def test_json_style_keys(header, key):
    assert canonical_measurement_key(header)[0] == key


def test_package_and_image_dimensions_are_not_measurements():
    data = {"variants": [
        {"size": "S", "sku": "A1", "price": 999, "packageLength": 30, "weight": 200,
         "imageWidth": 800, "shippingHeight": 5},
        {"size": "M", "sku": "A2", "price": 999, "packageLength": 31, "weight": 210,
         "imageWidth": 800, "shippingHeight": 5},
    ]}
    html = page(f"<script>window.__INITIAL_STATE__ = {json.dumps(data)};</script>")
    url = "https://example.com/p/8"
    assert extract_page(html, url, requested_url=url).status == "not_found"


# ------------------------------------------------------------------
# Product-specific only (product_scope_only=True, used by the worker)
# ------------------------------------------------------------------

from extraction import extract_shopify_product, shopify_json_url  # noqa: E402

CHART_TABLE = """<table><tr><th>Size</th><th>Chest (in)</th><th>Length (in)</th></tr>
<tr><td>S</td><td>38</td><td>27</td></tr><tr><td>M</td><td>40</td><td>28</td></tr></table>"""


def test_strict_rejects_theme_size_guide_table():
    html = page(f'<div class="size-guide-modal"><h3>Size Chart</h3>{CHART_TABLE}</div>')
    url = "https://store.example.com/products/kurta"
    r = extract_page(html, url, requested_url=url, product_scope_only=True)
    assert r.status == "generic_chart"
    assert r.chart is None
    # Without the strict rule the same table would have been accepted.
    assert extract_page(html, url, requested_url=url).status == "found"


@pytest.mark.parametrize("wrapper", [
    '<div class="product__description rte">{}</div>',
    '<div class="product-single__description">{}</div>',
    '<div itemprop="description"><section>{}</section></div>',
])
def test_strict_accepts_table_inside_product_description(wrapper):
    html = page(wrapper.format(CHART_TABLE))
    url = "https://store.example.com/products/kurta"
    r = extract_page(html, url, requested_url=url, product_scope_only=True)
    assert r.status == "found"
    assert r.chart.sizes["M"] == {"chest": "40", "length": "28"}
    assert r.chart.evidence["in_product_description"] is True


def test_strict_image_outside_description_is_generic():
    html = page('<div class="size-chart-popup"><img src="/size-chart.jpg" alt="Size Chart"></div>')
    url = "https://store.example.com/products/kurta"
    assert extract_page(html, url, requested_url=url, product_scope_only=True).status == "generic_chart"


def test_strict_image_inside_description_is_pending_ocr():
    html = page('<div class="product-description"><img src="/kurta-size-chart.jpg" alt="Size Chart"></div>')
    url = "https://store.example.com/products/kurta"
    r = extract_page(html, url, requested_url=url, product_scope_only=True)
    assert r.status == "image_chart"


def test_strict_keeps_myntra_product_data():
    r = extract_page(myntra_html(), MYNTRA_URL, requested_url=MYNTRA_URL, product_scope_only=True)
    assert r.status == "found" and len(r.chart.sizes) == 4


def test_shopify_json_description_chart():
    data = {"id": 7894561230123, "title": "Kurta", "description": f"<p>Soft linen.</p>{CHART_TABLE}"}
    r = extract_shopify_product(data, "7894561230123", "https://store.example.com/products/kurta")
    assert r.status == "found"
    assert r.chart.method == "shopify_description"
    assert r.chart.sizes == {"S": {"chest": "38", "length": "27"}, "M": {"chest": "40", "length": "28"}}
    assert r.chart.evidence["product_id_match"] is True


def test_shopify_json_wrong_product_id_rejected():
    data = {"id": 111, "description": CHART_TABLE}
    r = extract_shopify_product(data, "7894561230123", "https://store.example.com/products/kurta")
    assert r.status == "product_mismatch" and r.chart is None


def test_shopify_json_description_without_chart():
    r = extract_shopify_product({"id": 5, "description": "<p>Linen kurta. Sizes S, M, L available.</p>"},
                                "5", "https://store.example.com/products/kurta")
    assert r.status == "not_found"


@pytest.mark.parametrize("url,expected", [
    ("https://store.example.com/products/kurta", "https://store.example.com/products/kurta.js"),
    ("https://store.example.com/collections/x/products/kurta?variant=12#top",
     "https://store.example.com/collections/x/products/kurta.js"),
    ("https://store.example.com/products/kurta/", "https://store.example.com/products/kurta.js"),
    ("https://www.myntra.com/shirts/x/31472271/buy", None),
])
def test_shopify_json_url(url, expected):
    assert shopify_json_url(url) == expected


def test_swatch_links_and_product_photos_are_not_chart_images():
    # Real case (buywithouttravel.com): "More shades" swatches next to a "Size & fit guide" link.
    html = page("""<div class="simi-box-d">More shades: <a href="#">Size &amp; fit guide</a>
      <a href="/products/gold-emerald-necklace"><img src="/files/GoldEmerald_2.png?width=300"
         alt="Gold Emerald Diamante teardrop Pendant Necklace"></a>
      <a href="/products/gold-heart"><img src="/files/GoldHeart.png"></a></div>
      <div><h4>Size &amp; fit guide</h4><img src="/files/photo.jpg" alt="Model wearing the necklace"></div>""")
    soup = BeautifulSoup(html, "lxml")
    assert find_size_chart_images(soup, "https://store.example.com/products/x") == []


def test_unlabelled_image_next_to_size_chart_heading_still_found():
    soup = BeautifulSoup(page('<div><h4>Size Chart</h4><img src="/files/table123.png"></div>'), "lxml")
    assert [i["url"] for i in find_size_chart_images(soup, "https://e.com/p")] == ["https://e.com/files/table123.png"]

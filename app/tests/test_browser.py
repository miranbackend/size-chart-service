"""
Browser path: a chart that only exists after clicking a "Size Chart" control.
Skipped when Chromium is not installed (`playwright install chromium`).
"""

import pytest

import worker
from extraction import adapter_for, extract_page

JS_PAGE = """<html><head><title>Linen Kurta</title></head><body>
<p>{filler}</p>
<button onclick="document.getElementById('guide').innerHTML = `
  <table><tr><th>Size</th><th>Chest (in)</th><th>Length (in)</th></tr>
  <tr><td>S</td><td>38</td><td>40</td></tr>
  <tr><td>M</td><td>40</td><td>41</td></tr></table>`">Size Chart</button>
<div id="guide"></div>
</body></html>""".replace("{filler}", "Kurta description. " * 200)


@pytest.fixture(scope="module")
def browser():
    fetcher = worker.BrowserFetcher()
    try:
        fetcher._ensure()
    except Exception as exc:
        pytest.skip(f"chromium unavailable: {exc}")
    yield fetcher
    fetcher.close()


def test_browser_clicks_size_guide_and_extracts_rendered_table(browser, tmp_path):
    path = tmp_path / "kurta.html"
    path.write_text(JS_PAGE, encoding="utf-8")
    url = path.as_uri()

    # Plain HTML has no chart yet.
    assert extract_page(JS_PAGE, url, requested_url=url).status == "not_found"

    rendered = browser.fetch(url, adapter_for("https://shop.example.com/kurta"))
    page = extract_page(rendered.html, url, requested_url=url, via="browser")
    assert page.status == "found"
    assert page.chart.method == "browser_html_table"
    assert page.chart.sizes == {"S": {"chest": "38", "length": "40"},
                                "M": {"chest": "40", "length": "41"}}

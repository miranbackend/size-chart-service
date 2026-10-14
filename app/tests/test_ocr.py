import json

import httpx
import pytest

from ocr import GEMINI_URL, PROMPTS, read_chart_image

IMAGE_URL = "https://cdn.example.com/files/kurta-size-chart.jpg"
KEY = "test-key-123"

CHART = {
    "is_measurement_chart": True, "chart_count": 1, "unit": "inch", "measurement_basis": "garment",
    "rows": [
        {"size": "S", "measurements": [{"name": "Chest", "value": "38"}, {"name": "Length", "value": "40"}]},
        {"size": "M", "measurements": [{"name": "Chest", "value": "40"}, {"name": "Length", "value": "41"}]},
        {"size": "L", "measurements": [{"name": "Chest", "value": "42"}, {"name": "Length", "value": "42"}]},
    ],
}


def interaction(reading: dict) -> dict:
    return {"object": "interaction", "status": "completed",
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": json.dumps(reading)}]}]}


def client_for(readings, gemini_status=200, image_type="image/jpeg", seen=None):
    """Fake image host + Gemini. `readings` are returned in order, one per prompt."""
    queue = list(readings)

    def handler(request: httpx.Request):
        if str(request.url) == IMAGE_URL:
            return httpx.Response(200, content=b"\xff\xd8fakejpeg", headers={"content-type": image_type})
        if str(request.url) == GEMINI_URL:
            if seen is not None:
                seen.append(request)
            if gemini_status != 200:
                return httpx.Response(gemini_status, json={"error": {"message": "API key not valid"}})
            return httpx.Response(200, json=interaction(queue.pop(0)))
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def read(client, sizes=("S", "M", "L", "XL")):
    from ocr import clear_cache
    clear_cache()
    return read_chart_image(client, IMAGE_URL, api_key=KEY, model="gemini-test", product_sizes=list(sizes))


def test_two_agreeing_reads_give_chart():
    seen = []
    r = read(client_for([CHART, CHART], seen=seen))
    assert r.status == "found"
    assert r.chart.sizes == {"S": {"chest": "38", "length": "40"}, "M": {"chest": "40", "length": "41"},
                             "L": {"chest": "42", "length": "42"}}
    assert (r.chart.unit, r.chart.method, r.chart.source) == ("inch", "image_ocr", "image")
    assert r.chart.evidence["reads_agree"] is True and r.chart.evidence["size_overlap"] == 3

    # Two different prompts; key only in the header; image sent inline as base64.
    assert len(seen) == 2
    bodies = [json.loads(req.content) for req in seen]
    assert [b["input"][0]["text"] for b in bodies] == list(PROMPTS)
    assert all(req.headers["x-goog-api-key"] == KEY for req in seen)
    assert all(KEY not in str(req.url) for req in seen)
    assert bodies[0]["input"][1]["type"] == "image" and bodies[0]["input"][1]["mime_type"] == "image/jpeg"
    assert bodies[0]["response_format"]["mime_type"] == "application/json"


def test_reads_that_differ_are_not_saved():
    other = json.loads(json.dumps(CHART))
    other["rows"][1]["measurements"][0]["value"] = "41"   # M chest misread once
    r = read(client_for([CHART, other]))
    assert r.status == "disagree" and r.chart is None


def test_different_units_between_reads_are_not_saved():
    other = dict(CHART, unit="cm")
    assert read(client_for([CHART, other])).status == "disagree"


def test_not_a_chart():
    nothing = {"is_measurement_chart": False, "chart_count": 0, "unit": "unknown",
               "measurement_basis": "unknown", "rows": []}
    assert read(client_for([nothing, nothing])).status == "not_chart"


def test_two_charts_in_one_image_are_ambiguous():
    two = dict(CHART, chart_count=2)
    assert read(client_for([two, two])).status == "ambiguous"


def test_chart_sizes_must_match_product_sizes():
    r = read(client_for([CHART, CHART]), sizes=("28", "30", "32"))
    assert r.status == "size_mismatch" and r.chart is None


def test_product_without_sizes_cannot_be_verified():
    assert read(client_for([CHART, CHART]), sizes=()).status == "size_mismatch"


def test_single_row_or_text_values_rejected():
    bad = {"is_measurement_chart": True, "chart_count": 1, "unit": "inch", "measurement_basis": "garment",
           "rows": [{"size": "S", "measurements": [{"name": "Chest", "value": "Regular"}]},
                    {"size": "M", "measurements": [{"name": "Chest", "value": "40"}]}]}
    assert read(client_for([bad, bad])).status == "not_chart"


def test_gemini_error_is_reported_without_the_key():
    r = read(client_for([], gemini_status=403))
    assert r.status == "error"
    assert "403" in r.error and KEY not in r.error


def test_non_image_url_rejected():
    r = read(client_for([CHART, CHART], image_type="text/html"))
    assert r.status == "error" and "unsupported image type" in r.error


@pytest.mark.parametrize("chart_sizes,product_sizes,overlap", [
    (["S", "M", "L"], ["s", "m", "l", "xl"], 3),
    (["38", "40"], ["38", "40", "42"], 2),
    (["Size M", "L"], ["M", "L"], 2),
])
def test_size_overlap(chart_sizes, product_sizes, overlap):
    from ocr import size_overlap
    assert size_overlap(chart_sizes, product_sizes) == overlap

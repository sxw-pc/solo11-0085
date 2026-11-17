"""Selector parsing, malformed-page isolation, and normalization tests."""
from __future__ import annotations
from jobtrace.config import DEFAULT_SELECTORS, load_selectors
from jobtrace.normalize import (
    normalize_employment_type,
    normalize_experience,
    normalize_location,
    normalize_tags,
)
from jobtrace.parser import merge_selector_misses, parse_detail_page, parse_list_page
SELECTORS = load_selectors(DEFAULT_SELECTORS)
def _html(app, path: str) -> tuple[str, str]:
    response = app.test_client().get(path)
    assert response.status_code == 200
    return response.get_data(as_text=True), f"http://127.0.0.1{path}"
def test_list_page_extracts_cards_and_next_link(mock_apps):
    html, url = _html(mock_apps("baseline"), "/jobs?page=1")
    page = parse_list_page(html, url, SELECTORS)
    assert page.card_count == 10
    assert len(page.items) == 10
    assert page.issues == ()
    assert page.next_url == "http://127.0.0.1/jobs?page=2"
    first = page.items[0]
    assert first.external_id == "JT-0001"
    assert first.title == "后端开发工程师"
    assert first.detail_url.endswith("/jobs/JT-0001")
def test_last_list_page_has_no_next_link(mock_apps):
    html, url = _html(mock_apps("baseline"), "/jobs?page=7")
    page = parse_list_page(html, url, SELECTORS)
    assert page.next_url is None
    assert page.card_count == 4
    assert page.selector_misses["next_page"] == 1
def test_detail_parsing_normalizes_public_fields(mock_apps):
    list_html, list_url = _html(mock_apps("baseline"), "/jobs?page=1")
    item = parse_list_page(list_html, list_url, SELECTORS).items[0]
    html, url = _html(mock_apps("baseline"), "/jobs/JT-0001")
    result = parse_detail_page(html, url, "site-a", item, SELECTORS)
    assert result.ok
    job = result.observation
    assert job.title == "后端开发工程师"
    assert job.company.startswith("示例公司甲")
    assert job.city == "上海"
    assert job.district == "徐汇区"
    assert job.salary.min_monthly == 30000
    assert job.experience.min_years == 3
    assert job.employment_type == "全职"
    assert "Python" in job.tags
    assert job.content_hash
def test_missing_detail_required_company_is_isolated(mock_apps):
    page_html, page_url = _html(mock_apps("malformed"), "/jobs?page=3")
    item = next(item for item in parse_list_page(page_html, page_url, SELECTORS).items
                if item.external_id == "JT-0022")
    html, url = _html(mock_apps("malformed"), "/jobs/JT-0022")
    result = parse_detail_page(html, url, "site-a", item, SELECTORS)
    assert result.observation is None
    assert result.required_missing == ("company",)
def test_missing_optional_fields_keep_observation(mock_apps):
    page_html, page_url = _html(mock_apps("malformed"), "/jobs?page=2")
    item = next(item for item in parse_list_page(page_html, page_url, SELECTORS).items
                if item.external_id == "JT-0019")
    html, url = _html(mock_apps("malformed"), "/jobs/JT-0019")
    result = parse_detail_page(html, url, "site-a", item, SELECTORS)
    assert result.ok
    assert result.observation.salary.empty
    assert result.observation.location_raw == ""
    assert "location" in result.selector_misses
    assert any("salary" in issue for issue in result.issues)
def test_malformed_list_card_is_reported_but_link_remains(mock_apps):
    html, url = _html(mock_apps("malformed"), "/jobs?page=4")
    page = parse_list_page(html, url, SELECTORS)
    item = next(item for item in page.items if item.external_id == "JT-0040")
    assert item.title == ""
    assert any("missing title" in issue for issue in page.issues)
def test_duplicate_card_is_visible_to_parser(mock_apps):
    html, url = _html(mock_apps("day2"), "/jobs?page=7")
    page = parse_list_page(html, url, SELECTORS)
    ids = [item.external_id for item in page.items]
    assert ids.count("JT-0065") == 2
def test_normalizers():
    location = normalize_location(" 北京 - 海淀区 ")
    assert (location.city, location.district) == ("北京", "海淀区")
    assert normalize_experience("3-5年经验").min_years == 3
    assert normalize_experience("5年以上").max_years is None
    assert normalize_experience("应届生").text == "应届"
    assert normalize_employment_type("实习生") == "实习"
    assert normalize_tags([" Python ", "python", "SQL", ""]) == ("Python", "SQL")
def test_selector_miss_merge():
    assert merge_selector_misses({"a": 1}, {"a": 2, "b": 1}) == {"a": 3, "b": 1}

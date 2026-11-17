"""Chinese salary parsing and monthly normalization tests."""
from __future__ import annotations
import pytest
from jobtrace.salary import parse_salary, salary_payload
@pytest.mark.parametrize(("text", "minimum", "maximum", "months"), [
    ("20-30K·14薪", 20000, 30000, 14), ("15K-25K", 15000, 25000, 12),
    ("8千-1.2万/月", 8000, 12000, 12), ("1.5-2.5万·13薪", 15000, 25000, 13),
    ("年薪30-45万", 25000, 37500, 12), ("250-350元/天", 5438, 7613, 12),
])
def test_ranges_are_normalized_to_monthly_yuan(text, minimum, maximum, months):
    salary = parse_salary(text)
    assert salary.valid is True
    assert (salary.min_monthly, salary.max_monthly) == (minimum, maximum)
    assert (salary.months_per_year, salary.currency) == (months, "CNY")
def test_daily_salary_conversion_uses_21_75_days():
    salary = parse_salary("200-300元/天")
    assert (salary.min_monthly, salary.max_monthly) == (4350, 6525)
    assert salary.source_period == "day"
def test_negotiable_salary_has_no_bounds():
    salary = parse_salary("薪资面议")
    assert salary.negotiable is True and salary.normalized == "面议"
    assert salary.min_monthly is None and salary.max_monthly is None
def test_open_ended_salaries_and_annual_projection():
    low = parse_salary("20K以上")
    high = parse_salary("5千以下")
    assert low.min_monthly == 20000 and low.max_monthly is None
    assert high.min_monthly is None and high.max_monthly == 5000
    assert parse_salary("20-25K·14薪").annual_max == 350000
def test_invalid_and_empty_salary_are_explicit():
    invalid = parse_salary("not-a-number")
    empty = parse_salary("")
    assert invalid.valid is False
    assert empty.empty is True
    assert empty.valid is True
def test_salary_payload_has_stable_field_names():
    payload = salary_payload(parse_salary("20-30K·14薪"))
    assert payload["salary_min"] == 20000
    assert payload["salary_max"] == 30000
    assert payload["salary_months"] == 14
    assert payload["salary_text"] == "20000-30000元/月·14薪"

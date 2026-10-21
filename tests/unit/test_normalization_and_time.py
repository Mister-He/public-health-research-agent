from datetime import UTC, date, datetime

import pytest

from research_agent.metadata.normalization import (is_preprint_doi, normalize_arxiv, normalize_doi, normalize_pmcid,
                                                   normalize_pmid, normalize_text_for_match, normalize_title,
                                                   phrase_in, strip_markup, surname)
from research_agent.timeutil import date_chunks, local_today, parse_date, parse_partial_date, report_filename


@pytest.mark.parametrize("raw,expected", [
    ("10.1016/S2468-2667(26)00184-2", "10.1016/s2468-2667(26)00184-2"),
    ("https://doi.org/10.1371/journal.pmed.1004000", "10.1371/journal.pmed.1004000"),
    ("http://dx.doi.org/10.1136/BMJ.n123", "10.1136/bmj.n123"),
    ("doi: 10.1001/jamanetworkopen.2026.1234.", "10.1001/jamanetworkopen.2026.1234"),
    ("https://doi.org/10.1002%2Fsim.9999", "10.1002/sim.9999"),
    ("urn:doi:10.64898/2026.07.31.26358854", "10.64898/2026.07.31.26358854"),
    ("not a doi", None), ("", None), (None, None), ("10.12/too-short-prefix", None),
])
def test_doi_normalization(raw, expected):
    assert normalize_doi(raw) == expected


def test_identifier_normalization():
    assert normalize_pmid("PMID: 42831613") == "42831613"
    assert normalize_pmid("0123") is None and normalize_pmid("abc") is None
    assert normalize_pmcid("13673138") == "PMC13673138" and normalize_pmcid("pmc1") == "PMC1"
    assert normalize_arxiv("arXiv:2401.01234v3") == ("2401.01234", 3)
    assert normalize_arxiv("https://arxiv.org/abs/2401.01234") == ("2401.01234", None)
    assert normalize_arxiv("math.PR/0601001v2") == ("math.pr/0601001", 2)


def test_title_normalization_unicode_and_markup():
    a = "Cost‑effectiveness of <i>HCV</i> screening: a <b>micro-simulation</b> study"
    b = "COST-EFFECTIVENESS OF HCV SCREENING — A MICRO SIMULATION STUDY."
    assert normalize_title(a) == normalize_title(b)
    assert normalize_title("Café") == normalize_title("Cafe")
    # Non-Latin scripts are kept, so different Chinese titles never collapse to "".
    assert normalize_title("肝癌筛查") != normalize_title("肺癌筛查") and normalize_title("肝癌筛查")
    assert strip_markup("<jats:p>A &amp; B</jats:p>") == "A & B"


def test_phrase_matching():
    t = normalize_text_for_match("An agent-based model of HCV; epidemiology review")
    assert phrase_in("agent based model", t) and phrase_in("agent-based model", t)
    assert not phrase_in("agent", normalize_text_for_match("reagents were used"))
    assert phrase_in("epidemiolog*", t) and not phrase_in("epidemiolog", t)


def test_surname_and_preprint_hint():
    assert surname("Smith, John") == "smith" and surname("Smith JA") == "smith" and surname("John Smith") == "smith"
    assert is_preprint_doi("10.1101/2024.01.01.24300001") and is_preprint_doi("10.64898/2026.07.31.26358854")
    assert not is_preprint_doi("10.1016/s0140-6736(26)00001-1")


def test_dates_and_timezone():
    assert parse_date("2026-10-21T13:00:00Z") == date(2026, 10, 21)
    assert parse_date("2026-02-30") is None and parse_date("2026") is None
    assert parse_partial_date([2026, 10]) == (date(2026, 10, 1), "month")
    assert parse_partial_date([2026]) == (date(2026, 1, 1), "year")
    assert parse_partial_date([2026, 2, 30]) == (date(2026, 2, 1), "month")
    # 22:00 UTC on Oct 20 is 06:00 on Oct 21 in Shanghai: the report is named for Oct 21.
    assert local_today(datetime(2026, 10, 20, 22, 0, tzinfo=UTC)) == date(2026, 10, 21)
    assert local_today(datetime(2026, 10, 20, 15, 59, tzinfo=UTC)) == date(2026, 10, 20)
    # A delayed run (05:00 UTC = 13:00 local) still produces the same local date.
    assert local_today(datetime(2026, 10, 21, 5, 0, tzinfo=UTC)) == date(2026, 10, 21)
    assert report_filename(date(2026, 1, 5)) == "2026-01-05.md"


def test_date_chunks():
    ch = date_chunks(date(2026, 1, 1), date(2026, 1, 20), 7)
    assert ch == [(date(2026, 1, 1), date(2026, 1, 7)), (date(2026, 1, 8), date(2026, 1, 14)),
                  (date(2026, 1, 15), date(2026, 1, 20))]
    assert date_chunks(date(2026, 1, 2), date(2026, 1, 1), 7) == []

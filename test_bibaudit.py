#!/usr/bin/env python3
"""
Unit tests for bibaudit.py, organized by the clauses of reference-requirements.txt.

These exercise the *offline* audit only — no network is touched. Each test class
maps to one requirement from the reviewers' reference-pass guidance:

  - Completeness: required fields per entry type (InProceedings / Article)
  - Pages OR article number (and the "In press / To appear" exception)
  - TVCG volume/year parity (publication year, not presentation year)
  - DOI present, bare, and de-duplicated
  - Article-number vs single-page consistency
  - Short publisher town ("New York" not "New York, NY, USA")
  - Proceedings short form "Proc. X" consistency
  - Title capitalization (acronyms / proper nouns brace-protected)
  - Months on articles: always-or-never, and as bare macros without braces

Run:  python -m unittest test_bibaudit -v
"""

import unittest

import bibaudit
from bibaudit import Report


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def parse(bibstr):
    """Parse a .bib string into a list of entry dicts, like bibaudit's main()."""
    from bibtexparser.bparser import BibTexParser
    p = BibTexParser(common_strings=True)
    p.ignore_nonstandard_types = False
    import bibtexparser
    return bibtexparser.loads(bibstr, parser=p).entries


def run_offline(bibstr):
    """Run the offline audit over a .bib string; return the Report."""
    rep = Report()
    bibaudit.audit_offline(parse(bibstr), rep)
    return rep


def msgs(rep, severity=None, key=None):
    """All messages, optionally filtered by severity and/or citation key."""
    out = []
    sevs = [severity] if severity else (Report.ERROR, Report.WARN, Report.INFO)
    for sev in sevs:
        for k, m in rep.items.get(sev, []):
            if key is None or k == key:
                out.append(m)
    return out


def has(rep, needle, severity=None, key=None):
    """True if any (optionally filtered) message contains the substring."""
    return any(needle.lower() in m.lower() for m in msgs(rep, severity, key))


# A complete, clean entry of each type — used as a baseline to mutate.
GOOD_INPROC = r"""
@inproceedings{good_inproc,
  author    = {Ann Bee and Cy Dee},
  title     = {A Study of {VIS} Things},
  booktitle = {Proc. CHI},
  publisher = {ACM},
  address   = {New York},
  pages     = {1--10},
  doi       = {10.1145/1234567.1234568}
}
"""

GOOD_ARTICLE = r"""
@article{good_article,
  author  = {Ann Bee},
  title    = {A Journal Study of {VIS}},
  journal = {IEEE Trans. Vis. Comput. Graph.},
  volume  = {30},
  number  = {1},
  pages   = {100--112},
  year    = {2024},
  doi     = {10.1109/TVCG.2023.9999999}
}
"""


# ---------------------------------------------------------------------------
# Completeness — required fields per entry type
# ---------------------------------------------------------------------------

class TestRequiredFieldsInproceedings(unittest.TestCase):
    def test_complete_entry_has_no_missing_field_errors(self):
        rep = run_offline(GOOD_INPROC)
        self.assertFalse(has(rep, "missing required", Report.ERROR))
        self.assertFalse(has(rep, "missing pages", Report.ERROR))

    def test_each_required_field_flagged_when_absent(self):
        for field in ("author", "title", "booktitle", "publisher", "address", "doi"):
            # drop the line for this field, then expect an ERROR naming it
            bib = "\n".join(l for l in GOOD_INPROC.splitlines()
                            if not l.strip().startswith(field))
            rep = run_offline(bib)
            self.assertTrue(
                has(rep, f"missing required field '{field}'", Report.ERROR),
                f"expected ERROR for missing {field}",
            )

    def test_town_address_is_required(self):
        # requirement: publisher "with town" — town lives in the address field
        bib = "\n".join(l for l in GOOD_INPROC.splitlines()
                        if not l.strip().startswith("address"))
        rep = run_offline(bib)
        self.assertTrue(has(rep, "missing required field 'address'", Report.ERROR))


class TestRequiredFieldsArticle(unittest.TestCase):
    def test_complete_entry_has_no_missing_field_errors(self):
        rep = run_offline(GOOD_ARTICLE)
        self.assertFalse(has(rep, "missing required", Report.ERROR))
        self.assertFalse(has(rep, "missing pages", Report.ERROR))

    def test_each_required_field_flagged_when_absent(self):
        for field in ("author", "title", "journal", "volume", "doi"):
            bib = "\n".join(l for l in GOOD_ARTICLE.splitlines()
                            if not l.strip().startswith(field))
            rep = run_offline(bib)
            self.assertTrue(
                has(rep, f"missing required field '{field}'", Report.ERROR),
                f"expected ERROR for missing {field}",
            )

    def test_number_is_optional(self):
        # requirement: "number (if exists)" — absence must NOT be an error
        bib = "\n".join(l for l in GOOD_ARTICLE.splitlines()
                        if not l.strip().startswith("number"))
        rep = run_offline(bib)
        self.assertFalse(has(rep, "missing required field 'number'", Report.ERROR))


# ---------------------------------------------------------------------------
# Pages OR article number (+ the In-press / To-appear exception)
# ---------------------------------------------------------------------------

class TestPagesOrArticleNumber(unittest.TestCase):
    def test_articleno_satisfies_requirement(self):
        bib = GOOD_INPROC.replace("pages     = {1--10},", "articleno = {117},")
        rep = run_offline(bib)
        self.assertFalse(has(rep, "missing pages or article number", Report.ERROR))

    def test_neither_pages_nor_articleno_is_error(self):
        bib = "\n".join(l for l in GOOD_INPROC.splitlines()
                        if not l.strip().startswith("pages"))
        rep = run_offline(bib)
        self.assertTrue(has(rep, "missing pages or article number", Report.ERROR))

    def test_in_press_note_exempts_missing_pages(self):
        bib = "\n".join(l for l in GOOD_ARTICLE.splitlines()
                        if not l.strip().startswith("pages"))
        bib = bib.replace("year    = {2024},", "year    = {2024},\n  note    = {To appear},")
        rep = run_offline(bib)
        self.assertFalse(has(rep, "missing pages or article number", Report.ERROR))

    def test_in_press_must_not_carry_a_page_range(self):
        bib = GOOD_ARTICLE.replace(
            "pages   = {100--112},", "pages   = {1--11},\n  note    = {In press},")
        rep = run_offline(bib)
        self.assertTrue(has(rep, "in press", Report.WARN))


# ---------------------------------------------------------------------------
# TVCG volume/year parity
# ---------------------------------------------------------------------------

class TestTVCGParity(unittest.TestCase):
    def _tvcg(self, volume, year):
        return f"""
@article{{t,
  author={{A B}}, title={{X}},
  journal={{IEEE Transactions on Visualization and Computer Graphics}},
  volume={{{volume}}}, year={{{year}}}, pages={{1--2}},
  doi={{10.1109/TVCG.2024.1}}
}}"""

    def test_mismatched_parity_is_error(self):
        rep = run_offline(self._tvcg(30, 2023))  # even vol, odd year
        self.assertTrue(has(rep, "parity mismatch", Report.ERROR))

    def test_both_even_is_clean(self):
        rep = run_offline(self._tvcg(30, 2024))
        self.assertFalse(has(rep, "parity mismatch"))

    def test_both_odd_is_clean(self):
        rep = run_offline(self._tvcg(31, 2025))
        self.assertFalse(has(rep, "parity mismatch"))

    def test_parity_only_applies_to_tvcg(self):
        bib = self._tvcg(30, 2023).replace(
            "IEEE Transactions on Visualization and Computer Graphics",
            "Computer Graphics Forum")
        rep = run_offline(bib)
        self.assertFalse(has(rep, "parity mismatch"))


# ---------------------------------------------------------------------------
# DOI present, bare, and de-duplicated
# ---------------------------------------------------------------------------

class TestDOI(unittest.TestCase):
    def test_doi_org_url_is_warned(self):
        # a full https://doi.org/... URL would double under a hyperref DOI bst style
        # (the style prepends the resolver), so it must be flagged, not tolerated
        bib = GOOD_ARTICLE.replace(
            "doi     = {10.1109/TVCG.2023.9999999}",
            "doi     = {https://doi.org/10.1109/TVCG.2023.9999999}")
        rep = run_offline(bib)
        self.assertTrue(has(rep, "resolver url", Report.WARN))
        self.assertTrue(has(rep, "doubled link", Report.WARN))

    def test_dx_doi_org_url_is_warned(self):
        bib = GOOD_ARTICLE.replace(
            "doi     = {10.1109/TVCG.2023.9999999}",
            "doi     = {http://dx.doi.org/10.1109/TVCG.2023.9999999}")
        rep = run_offline(bib)
        self.assertTrue(has(rep, "resolver url", Report.WARN))

    def test_non_doi_string_is_warned(self):
        # a value that isn't a bare DOI (and isn't a doi.org URL) is flagged
        bib = GOOD_ARTICLE.replace(
            "doi     = {10.1109/TVCG.2023.9999999}",
            "doi     = {TVCG.2023.9999999}")
        rep = run_offline(bib)
        self.assertTrue(has(rep, "bare doi", Report.WARN))

    def test_bare_doi_not_warned(self):
        rep = run_offline(GOOD_ARTICLE)
        self.assertFalse(has(rep, "bare doi"))

    def test_arxiv_doi_accepted_as_bare(self):
        bib = GOOD_ARTICLE.replace(
            "doi     = {10.1109/TVCG.2023.9999999}",
            "doi     = {10.48550/arXiv.2401.01234}")
        rep = run_offline(bib)
        self.assertFalse(has(rep, "bare doi"))

    def test_duplicate_doi_across_entries_warned(self):
        bib = GOOD_ARTICLE + GOOD_INPROC.replace(
            "10.1145/1234567.1234568", "10.1109/TVCG.2023.9999999")
        rep = run_offline(bib)
        self.assertTrue(has(rep, "duplicate doi", Report.WARN))

    def test_duplicate_citation_key_is_error(self):
        rep = run_offline(GOOD_ARTICLE + GOOD_ARTICLE)
        self.assertTrue(has(rep, "duplicate citation key", Report.ERROR))


# ---------------------------------------------------------------------------
# Article-number vs page-number consistency
# ---------------------------------------------------------------------------

class TestPageStyleConsistency(unittest.TestCase):
    def test_single_page_warned_as_possible_article_number(self):
        bib = GOOD_ARTICLE.replace("pages   = {100--112},", "pages   = {117},")
        rep = run_offline(bib)
        self.assertTrue(has(rep, "single page", Report.WARN))

    def test_mixed_page_and_articleno_styles_flagged_globally(self):
        a = GOOD_ARTICLE  # range pages
        b = GOOD_INPROC.replace("pages     = {1--10},", "articleno = {117},")
        rep = run_offline(a + b)
        self.assertTrue(has(rep, "mixed page", Report.INFO, key="(global)"))

    def test_consistent_styles_not_flagged(self):
        # two range-style entries → no mixed-style verdict
        b = GOOD_INPROC  # also range pages
        rep = run_offline(GOOD_ARTICLE + b)
        self.assertFalse(has(rep, "mixed page"))

    def test_classify_page_style(self):
        self.assertEqual(bibaudit.classify_page_style({"articleno": "12"}), "artno")
        self.assertEqual(bibaudit.classify_page_style({"pages": "117"}), "single")
        self.assertEqual(bibaudit.classify_page_style({"pages": "1--10"}), "range")
        self.assertEqual(bibaudit.classify_page_style({"pages": "72:1--72:23"}), "colon")
        self.assertEqual(
            bibaudit.classify_page_style({"pages": "article no. 117, 12 pages"}), "acmnote")
        self.assertIsNone(bibaudit.classify_page_style({}))


# ---------------------------------------------------------------------------
# Short publisher town & short proceedings names
# ---------------------------------------------------------------------------

class TestBrevityConsistency(unittest.TestCase):
    def test_longform_address_flagged(self):
        bib = GOOD_INPROC.replace("address   = {New York},",
                                  "address   = {New York, NY, USA},")
        rep = run_offline(bib)
        self.assertTrue(has(rep, "long-form publisher address", Report.INFO))

    def test_short_town_not_flagged(self):
        rep = run_offline(GOOD_INPROC)
        self.assertFalse(has(rep, "long-form publisher address"))

    def test_mixed_proceedings_forms_flagged(self):
        short = GOOD_INPROC  # "Proc. CHI"
        long = GOOD_INPROC.replace("good_inproc", "long_inproc").replace(
            "booktitle = {Proc. CHI},",
            "booktitle = {Conference on Human Factors in Computing Systems},")
        rep = run_offline(short + long)
        self.assertTrue(has(rep, "mix forms", Report.INFO, key="(global)"))

    def test_uniform_proceedings_form_not_flagged(self):
        rep = run_offline(GOOD_INPROC)
        self.assertFalse(has(rep, "mix forms"))


# ---------------------------------------------------------------------------
# Title capitalization (acronyms / proper nouns)
# ---------------------------------------------------------------------------

class TestTitleCapitalization(unittest.TestCase):
    def test_unbraced_acronym_flagged(self):
        bib = GOOD_ARTICLE.replace("A Journal Study of {VIS}", "A Journal Study of VIS")
        rep = run_offline(bib)
        self.assertTrue(has(rep, "brace-protected", Report.INFO))

    def test_braced_acronym_not_flagged(self):
        # GOOD_ARTICLE already braces {VIS}
        rep = run_offline(GOOD_ARTICLE)
        self.assertFalse(has(rep, "brace-protected", Report.INFO, key="good_article"))

    def test_camelcase_token_flagged(self):
        bib = GOOD_ARTICLE.replace("A Journal Study of {VIS}", "Using PacificVis Data")
        rep = run_offline(bib)
        self.assertTrue(has(rep, "brace-protected", Report.INFO))


# ---------------------------------------------------------------------------
# Months: always-or-never, and bare macros without braces
# ---------------------------------------------------------------------------

class TestMonths(unittest.TestCase):
    def _art(self, key, extra=""):
        return f"""
@article{{{key},
  author={{A B}}, title={{T {key}}}, journal={{J}}, volume={{1}},
  year={{2024}}, pages={{1--2}}, doi={{10.1/{key}}}{extra}
}}"""

    def test_inconsistent_month_usage_flagged(self):
        with_m = self._art("withm", ", month=jan")
        without_m = self._art("withoutm")
        rep = run_offline(with_m + without_m)
        self.assertTrue(has(rep, "month usage", Report.INFO, key="(global)"))

    def test_consistent_month_usage_not_flagged(self):
        rep = run_offline(self._art("a", ", month=jan") + self._art("b", ", month=feb"))
        self.assertFalse(has(rep, "month usage"))

    def test_braced_month_literal_flagged(self):
        rep = Report()
        bibaudit.audit_month_format("@article{x, month = {July}, }", rep)
        self.assertTrue(has(rep, "braced/quoted literal", Report.INFO))

    def test_quoted_month_literal_flagged(self):
        rep = Report()
        bibaudit.audit_month_format('@article{x, month = "jul", }', rep)
        self.assertTrue(has(rep, "braced/quoted literal", Report.INFO))

    def test_bare_month_macro_not_flagged(self):
        rep = Report()
        bibaudit.audit_month_format("@article{x, month = jul, }", rep)
        self.assertFalse(has(rep, "braced/quoted literal"))

    def test_month_substring_not_falsely_matched(self):
        # a field whose name merely ends in a word-boundary mismatch must not trip it
        rep = Report()
        bibaudit.audit_month_format("@article{x, nomonthish = {July}, }", rep)
        self.assertFalse(has(rep, "braced/quoted literal"))


# ---------------------------------------------------------------------------
# Preprint reminder (arXiv vs. published version)
# ---------------------------------------------------------------------------

class TestPreprintHeuristic(unittest.TestCase):
    def test_arxiv_doi_is_preprint(self):
        self.assertTrue(bibaudit.is_preprint({"doi": "10.48550/arXiv.2401.01234"}))

    def test_publisher_doi_is_not_preprint(self):
        self.assertFalse(bibaudit.is_preprint({"doi": "10.1145/1234567.1234568"}))

    def test_eprint_field_is_preprint(self):
        self.assertTrue(bibaudit.is_preprint({"archiveprefix": "arXiv", "eprint": "2401.1"}))

    def test_arxiv_entry_warns(self):
        bib = GOOD_ARTICLE.replace(
            "doi     = {10.1109/TVCG.2023.9999999}",
            "doi     = {10.48550/arXiv.2401.01234}")
        rep = run_offline(bib)
        self.assertTrue(has(rep, "arxiv preprint", Report.WARN))


# ---------------------------------------------------------------------------
# Pure helpers used by the cross-check layer (no network)
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):
    def test_clean_doi_strips_url_prefix(self):
        self.assertEqual(bibaudit._clean_doi("https://doi.org/10.1/x"), "10.1/x")
        self.assertEqual(bibaudit._clean_doi("http://dx.doi.org/10.1/x"), "10.1/x")
        self.assertEqual(bibaudit._clean_doi("10.1/x"), "10.1/x")

    def test_is_arxiv_doi(self):
        self.assertTrue(bibaudit.is_arxiv_doi("10.48550/arXiv.2401.01234"))
        self.assertTrue(bibaudit.is_arxiv_doi("https://doi.org/10.48550/ARXIV.2401.1"))
        self.assertFalse(bibaudit.is_arxiv_doi("10.1145/1234567.1234568"))

    def test_has_pages_or_artno(self):
        self.assertTrue(bibaudit.has_pages_or_artno({"pages": "1--2"}))
        self.assertTrue(bibaudit.has_pages_or_artno({"articleno": "117"}))
        self.assertTrue(bibaudit.has_pages_or_artno({"article-number": "117"}))
        self.assertFalse(bibaudit.has_pages_or_artno({}))

    def test_page_span_normalizes_articleno_style(self):
        # '72:1--72:23' and '1-23' describe the same span
        self.assertEqual(bibaudit._page_span("72:1--72:23"), ["1", "23"])
        self.assertEqual(bibaudit._page_span("1-23"), ["1", "23"])

    def test_title_match_score(self):
        self.assertGreaterEqual(
            bibaudit._title_match_score("Deep Learning for Vision",
                                        "Deep Learning for Vision"), 1.0)
        self.assertLess(
            bibaudit._title_match_score("Against Access", "Toward Access Control"), 1.0)

    def test_venue_compatible_abbreviation(self):
        self.assertTrue(bibaudit._venue_compatible(
            "Proc. PacificVis", "IEEE Pacific Visualization Symposium"))
        self.assertFalse(bibaudit._venue_compatible(
            "Proc. CHI", "IEEE VIS Conference"))


# ---------------------------------------------------------------------------
# compare_record — offline cross-check comparison (fed a synthetic record)
# ---------------------------------------------------------------------------

class TestCompareRecord(unittest.TestCase):
    def _rec(self, **kw):
        base = {"source": "Crossref", "title": "", "surname": "", "year": None}
        base.update(kw)
        return base

    def test_year_far_off_is_error(self):
        e = parse(GOOD_ARTICLE)[0]
        rep = Report()
        bibaudit.compare_record(e, self._rec(year=2020), rep)
        self.assertTrue(has(rep, "year mismatch", Report.ERROR))

    def test_year_off_by_one_is_warn(self):
        e = parse(GOOD_ARTICLE)[0]  # year 2024
        rep = Report()
        bibaudit.compare_record(e, self._rec(year=2025), rep)
        self.assertTrue(has(rep, "year mismatch", Report.WARN))
        self.assertFalse(has(rep, "year mismatch", Report.ERROR))

    def test_surname_missing_from_bib_warns(self):
        e = parse(GOOD_ARTICLE)[0]  # author "Ann Bee"
        rep = Report()
        bibaudit.compare_record(e, self._rec(surname="Zylinski"), rep)
        self.assertTrue(has(rep, "surname", Report.WARN))

    def test_matching_record_is_silent(self):
        e = parse(GOOD_ARTICLE)[0]
        rep = Report()
        bibaudit.compare_record(
            e, self._rec(title="A Journal Study of VIS", surname="Bee", year=2024), rep)
        self.assertEqual(msgs(rep), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

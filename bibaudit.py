#!/usr/bin/env python3
"""
bibaudit.py — Audit a BibTeX file for correctness, completeness, and consistency.

Checks (offline):
  - Required fields per entry type (@inproceedings / @article)
  - DOI presence + format
  - Page vs. article-number usage; cross-file consistency of style
  - TVCG volume/year even-odd parity
  - Long-form publisher/address ("New York, NY, USA")
  - Booktitle not in "Proc. X" short form
  - Months on articles: always-or-never, and entered as bare macros (jan…dec)
  - Title acronym/proper-noun tokens not brace-protected (capitalization loss)
  - Duplicate citation keys / duplicate DOIs
  - "In press"-style entries that still carry page ranges

Checks (network, Crossref + DataCite + DBLP):
  - DOI resolves (Crossref for publisher DOIs; DataCite for arXiv 10.48550 DOIs)
  - Entry found on DBLP (best for CS conference papers)
  - Stored title / first-author surname / year match source metadata
  - Source-vs-source disagreement (when querying both) — flags bad DB records
  - arXiv preprint entries are flagged to manually confirm a published version
    exists (and should be cited instead) — see "What you still have to check"

DBLP is rate-limited hard (it asks for 1–2s between requests and 429s past that),
so by default we use it *surgically*: Crossref (broad DOI coverage, polite pool)
verifies every DOI'd entry, and DBLP is only queried where it adds unique value —
entries with no DOI, and TVCG/VIS articles (where DBLP venue/year data is
authoritative). Non-CS types (@book/@misc/@incollection) never hit DBLP. Use
--dblp-all to force DBLP on every conference/journal entry instead.

Output: terminal report grouped by severity (ERROR / WARN / INFO).

Usage:
  pip install bibtexparser requests
  python bibaudit.py refs.bib                      # offline + surgical DBLP + Crossref
  python bibaudit.py refs.bib --dblp-all           # offline + DBLP on every CS entry
  python bibaudit.py refs.bib --source dblp        # offline + DBLP only
  python bibaudit.py refs.bib --source crossref    # offline + Crossref only
  python bibaudit.py refs.bib --source none        # offline only (== --no-crossref)

Suggestions (always on when a source is queried; free — reuses fetched records):
  For fields an entry is MISSING (DOI, pages/article no., publisher, address,
  volume, venue) bibaudit reports the value found on the sources already queried
  (Crossref primary, DBLP secondary), each with the relevant caveat, a "Check:"
  strategy, and a link to verify. It also flags fields that are PRESENT but
  disagree with the source (a wrong-version / wrong-DOI tell). Advisory only —
  never auto-applied. DBLP suggestions only cover entries DBLP already queries;
  add --dblp-all to extend them to DOI'd papers.

Rate control:
  DBLP and Crossref are paced separately: --dblp-sleep (default 1.5s) and
  --crossref-sleep (default 0.1s). A 429's Retry-After is honored in full and
  carried forward to later calls; --max-retry-after caps how long we'll wait
  before giving up on a single lookup. --mailto puts you in Crossref's polite pool.

Caching:
  Network results are cached to .bibaudit_cache.json keyed by each entry's
  doi/title/author/year. Re-runs skip the network for unchanged entries;
  fixing one of those fields invalidates just that entry.
  --cache PATH   use a different cache file
  --no-cache     ignore the cache for this run
  --clear-cache  delete the cache first, forcing fresh lookups
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from collections import defaultdict

try:
    import bibtexparser
    from bibtexparser.bparser import BibTexParser
except ImportError:
    sys.exit("Missing dependency: pip install bibtexparser")

# ---------------------------------------------------------------------------
# Severity-collecting reporter
# ---------------------------------------------------------------------------

class Report:
    ERROR, WARN, INFO = "ERROR", "WARN", "INFO"

    def __init__(self):
        self.items = defaultdict(list)  # severity -> [(key, message)]

    def add(self, severity, key, msg):
        self.items[severity].append((key, msg))

    def err(self, key, msg):  self.add(self.ERROR, key, msg)
    def warn(self, key, msg): self.add(self.WARN, key, msg)
    def info(self, key, msg): self.add(self.INFO, key, msg)

    def render(self):
        order = [self.ERROR, self.WARN, self.INFO]
        labels = {
            self.ERROR: "ERRORS   (missing required data / almost certainly wrong)",
            self.WARN:  "WARNINGS (likely problems — review each)",
            self.INFO:  "INFO     (consistency nudges / heuristic flags)",
        }
        total = sum(len(v) for v in self.items.values())
        out = []
        out.append("=" * 78)
        out.append(f"BIBTEX AUDIT — {total} item(s) flagged")
        out.append("=" * 78)
        for sev in order:
            rows = self.items.get(sev, [])
            if not rows:
                continue
            out.append("")
            out.append(f"### {labels[sev]}  ({len(rows)})")
            out.append("-" * 78)
            by_key = defaultdict(list)
            for key, msg in rows:
                by_key[key].append(msg)
            for key in sorted(by_key):
                out.append(f"  [{key}]")
                for msg in by_key[key]:
                    out.append(f"      - {msg}")
        if total == 0:
            out.append("\nNo issues found by automated checks. (Manual review of")
            out.append("capitalization correctness and author spelling still required.)")
        out.append("")
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Field requirements
# ---------------------------------------------------------------------------

REQUIRED = {
    "inproceedings": ["author", "title", "booktitle", "publisher", "address", "doi"],
    "article":       ["author", "title", "journal", "volume", "doi"],
}
# pages OR articleno handled separately for both types.

DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.", re.I)  # arXiv DOIs live in DataCite, not Crossref
LONGFORM_ADDR_RE = re.compile(r",\s*[A-Z]{2}\s*,\s*USA|,\s*USA\b", re.I)
ACRONYM_TOKEN_RE = re.compile(r"\b([A-Z]{2,}|[A-Z][a-z]*[A-Z][A-Za-z]*)\b")
TVCG_RE = re.compile(r"transactions on visualization|TVCG", re.I)
INPRESS_RE = re.compile(r"in press|to appear|forthcoming", re.I)
SINGLE_PAGE_RE = re.compile(r"^\s*\d+\s*$")
COLON_PAGE_RE = re.compile(r"^\s*\d+:\d+--?\d+:\d+\s*$")
RANGE_PAGE_RE = re.compile(r"^\s*\d+\s*--?\s*\d+\s*$")
# A `month` field whose value is a braced/quoted literal ({January}, "jan", {7}).
# The guidelines want BibTeX month macros (jan…dec) entered bare, so the style
# file formats them. bibtexparser expands `jan` and `{January}` to the same
# string, so this can only be detected from the raw .bib source.
MONTH_LITERAL_RE = re.compile(r'(?i)\bmonth\b[ \t]*=[ \t]*([{"][^}"\n]*[}"])')


def has_pages_or_artno(e):
    return bool(e.get("pages", "").strip() or e.get("articleno", "").strip()
                or e.get("article-number", "").strip())


def is_arxiv_doi(doi):
    """True for arXiv DOIs (prefix 10.48550/arXiv.*), which are registered with
    DataCite, not Crossref — so a Crossref lookup of one is a guaranteed 404."""
    return bool(ARXIV_DOI_RE.match(_clean_doi(doi or "")))


def is_preprint(e):
    """
    Heuristic: does this entry cite an arXiv *preprint* rather than a published
    version? A real (non-arXiv) DOI is taken as evidence the entry already points
    at a published record, so we don't nag those even if they mention arXiv.
    """
    doi = e.get("doi", "").strip()
    if is_arxiv_doi(doi):
        return True
    if doi:  # has a genuine publisher DOI → treat as already published
        return False
    if e.get("archiveprefix", "").strip().lower() == "arxiv":
        return True
    if e.get("eprint", "").strip():
        return True
    hay = " ".join(e.get(f, "") for f in
                   ("journal", "publisher", "note", "howpublished", "url"))
    return bool(re.search(r"\barxiv\b", hay, re.I))


def classify_page_style(e):
    """Return 'single', 'range', 'colon', 'acmnote', 'artno', or None."""
    if e.get("articleno", "").strip() or e.get("article-number", "").strip():
        return "artno"
    p = e.get("pages", "").strip()
    if not p:
        return None
    if "article no" in p.lower() or "pages" in p.lower():
        return "acmnote"
    if COLON_PAGE_RE.match(p):
        return "colon"
    if SINGLE_PAGE_RE.match(p):
        return "single"
    if RANGE_PAGE_RE.match(p):
        return "range"
    return "other"


# ---------------------------------------------------------------------------
# Offline audit
# ---------------------------------------------------------------------------

def audit_offline(entries, report):
    seen_keys = {}
    seen_dois = {}
    month_keys = {"with": [], "without": []}
    page_styles = defaultdict(list)
    long_addr = []    # keys with long-form publisher/address (brevity)
    proc_short = []   # inproceedings keys using 'Proc. X' short form
    proc_long = []    # inproceedings keys using long-form proceedings names

    for e in entries:
        key = e.get("ID", "?")
        etype = e.get("ENTRYTYPE", "").lower()

        # duplicate keys
        if key in seen_keys:
            report.err(key, "Duplicate citation key.")
        seen_keys[key] = True

        # required fields
        req = REQUIRED.get(etype)
        if req:
            for f in req:
                if not e.get(f, "").strip():
                    report.err(key, f"Missing required field '{f}' for @{etype}.")
            if not has_pages_or_artno(e) and not INPRESS_RE.search(
                    e.get("note", "") + e.get("pages", "")):
                report.err(key, "Missing pages or article number "
                                "(and not marked In press / To appear).")

        # preprint reminder (always manual to resolve — see README)
        if is_preprint(e):
            report.warn(key, "Cites an arXiv preprint — confirm whether a peer-reviewed "
                             "published version now exists and cite that instead. arXiv is "
                             "appropriate only when no published version exists (e.g. a "
                             "poster or workshop with no proceedings DOI).")

        # DOI format + dedupe
        doi = e.get("doi", "").strip()
        if doi:
            doi_clean = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
            # arXiv DOIs are bare 10.48550/arXiv.NNNN.NNNNN and valid even though
            # DOI_RE's generic shape still matches; no special-casing needed here.
            stored_as_url = doi_clean != doi   # a doi.org/ resolver prefix was present
            if stored_as_url and DOI_RE.match(doi_clean):
                # The DOI itself is fine, but it's stored as a resolver URL. The
                # hyperref DOI bst styles wrap the field in \doi{...}, which prepends
                # https://doi.org/ itself — so a URL here renders as a DOUBLED, broken
                # link. Store the bare DOI so the style can link it correctly.
                report.warn(key, f"DOI field holds a resolver URL, not a bare DOI: '{doi}'. "
                                 "The hyperref DOI styles prepend https://doi.org/ "
                                 "themselves, so this renders as a doubled link "
                                 f"(https://doi.org/{doi}). Store the bare DOI '{doi_clean}'.")
            elif not DOI_RE.match(doi_clean):
                report.warn(key, f"DOI field doesn't look like a bare DOI: '{doi}'. "
                                 "Store as '10.xxxx/...' not a full URL.")
            if doi_clean.lower() in seen_dois:
                report.warn(key, f"Duplicate DOI — also in [{seen_dois[doi_clean.lower()]}].")
            else:
                seen_dois[doi_clean.lower()] = key

        # TVCG parity
        journal = e.get("journal", "")
        if etype == "article" and TVCG_RE.search(journal):
            vol = e.get("volume", "").strip()
            yr = e.get("year", "").strip()
            if vol.isdigit() and yr.isdigit():
                if (int(vol) % 2) != (int(yr) % 2):
                    report.err(key, f"TVCG parity mismatch: volume {vol} and year {yr} "
                                    "differ in even/odd. Year should be the TVCG special-issue "
                                    "publication year, not presentation year.")

        # long-form publisher/address — collected; reported once globally below
        # (a brevity nudge, not a per-entry error)
        if any(LONGFORM_ADDR_RE.search(e.get(f, "")) for f in ("address", "publisher")):
            long_addr.append(key)

        # proceedings-name form — collected; reported once globally below.
        # Short form is optional (a consistency/brevity choice), so we only flag a
        # MIX of forms, never a single entry.
        if etype == "inproceedings" and e.get("booktitle", "").strip():
            if re.search(r"\bProc\.?\b", e.get("booktitle", "")):
                proc_short.append(key)
            else:
                proc_long.append(key)

        # months on articles (always-or-never)
        if etype == "article":
            if e.get("month", "").strip():
                month_keys["with"].append(key)
            else:
                month_keys["without"].append(key)

        # title brace-protection heuristic
        title = e.get("title", "")
        # strip already-braced spans, then look for risky tokens
        unbraced = re.sub(r"\{[^}]*\}", "", title)
        risky = set(ACRONYM_TOKEN_RE.findall(unbraced))
        # ignore the leading word (sentence case start) and common words
        risky.discard("")
        if risky:
            report.info(key, "Title has acronym/CamelCase token(s) not brace-protected "
                             f"{sorted(risky)} — may lose capitalization under some styles. "
                             "Wrap in {} if they must stay capitalized.")

        # in-press but has page range
        note = e.get("note", "")
        if INPRESS_RE.search(note):
            if RANGE_PAGE_RE.match(e.get("pages", "")):
                report.warn(key, "Marked In press / To appear but still has a page range; "
                                 "drop speculative page numbers.")

        # collect page style
        st = classify_page_style(e)
        if st:
            page_styles[st].append(key)
            if st == "single":
                report.warn(key, f"pages = '{e.get('pages','').strip()}' is a single page — "
                                 "if this is an article number, cite it consistently as an "
                                 "article number, not a single page.")

    # months consistency verdict (always-or-never). List the keys on the smaller
    # side so the reader knows where the fewest edits bring the file in line.
    if month_keys["with"] and month_keys["without"]:
        n_with, n_without = len(month_keys["with"]), len(month_keys["without"])
        minority = "with" if n_with <= n_without else "without"
        verb = "have a month" if minority == "with" else "omit it"
        shown = ", ".join(month_keys[minority][:10]) + (
            " …" if len(month_keys[minority]) > 10 else "")
        report.info("(global)", f"Month usage on @article is inconsistent: {n_with} with "
                                f"month, {n_without} without. Use months always or never; the "
                                f"fewer to change are the {len(month_keys[minority])} that "
                                f"{verb}: {shown}.")

    # page-style consistency verdict (flag, don't pick)
    style_kinds = {k for k in page_styles if k in ("range", "colon", "acmnote", "artno")}
    if len(style_kinds) > 1:
        summary = "; ".join(f"{k}: {len(page_styles[k])} entr(ies)" for k in sorted(style_kinds))
        # Show keys for every style except the most common one — those are the
        # fewer entries to convert to reach a single convention.
        majority = max(sorted(style_kinds), key=lambda k: len(page_styles[k]))
        minority_keys = [key for k in sorted(style_kinds) if k != majority
                         for key in page_styles[k]]
        shown = ", ".join(minority_keys[:10]) + (" …" if len(minority_keys) > 10 else "")
        report.info("(global)", "Mixed page / article-number styles across file "
                                f"({summary}). Pick one convention and apply it everywhere; "
                                f"the fewer to change are the {len(minority_keys)} not using the "
                                f"most common '{majority}' style: {shown}.")

    # proceedings-name consistency (short form is optional/brevity — flag only a MIX)
    if proc_short and proc_long:
        # List whichever form is less common — the smaller set to bring in line.
        if len(proc_short) <= len(proc_long):
            minority_label, minority_keys = "short 'Proc. X'", proc_short
        else:
            minority_label, minority_keys = "long-name", proc_long
        shown = ", ".join(minority_keys[:10]) + (" …" if len(minority_keys) > 10 else "")
        report.info("(global)", f"Proceedings names mix forms: {len(proc_short)} use the short "
                                f"'Proc. X' style, {len(proc_long)} use long names. "
                                "Short form (e.g. 'Proc. CHI') is optional — a brevity/"
                                "consistency choice, not required — but if you use it, apply it "
                                f"throughout (it also avoids repeating the year). The fewer to "
                                f"change are the {len(minority_keys)} {minority_label} ones: "
                                f"{shown}.")

    # long-form addresses (optional brevity, aggregated)
    if long_addr:
        shown = ", ".join(long_addr[:10]) + (" …" if len(long_addr) > 10 else "")
        report.info("(global)", f"{len(long_addr)} entr(ies) use long-form publisher addresses "
                                f"(e.g. 'New York, NY, USA'): {shown}. The short town "
                                "('New York') is fine and more concise — optional, but keep it "
                                "consistent.")


def audit_month_format(raw_text, report):
    """
    Requirement: if months are used on @article entries, enter them as BibTeX
    month macros (jan…dec) *without* curly braces or quotes, so the bibliography
    style formats them consistently. bibtexparser expands the macro to a full
    month name, indistinguishable from a braced literal — so this is the one
    check that must read the raw .bib text rather than the parsed entries.
    """
    literals = [m.group(1).strip() for m in MONTH_LITERAL_RE.finditer(raw_text or "")]
    if literals:
        shown = ", ".join(literals[:10]) + (" …" if len(literals) > 10 else "")
        report.info("(global)", f"{len(literals)} month field(s) use a braced/quoted "
                                f"literal ({shown}); enter months as bare BibTeX macros "
                                "(e.g. month = jul, not month = {July}) so the style file "
                                "formats them consistently.")


# ---------------------------------------------------------------------------
# Cross-check against authoritative sources (Crossref, DBLP)
#
# Each fetcher returns a normalized record or a status string:
#   {"source": str, "title": str, "surname": str, "year": int|None}
#   or "notfound" / "error"
# DBLP is preferred for CS conference papers (cleaner @inproceedings venue
# metadata); Crossref has broad journal/DOI coverage. With --source both,
# we compare the bib against each source AND flag source-vs-source
# disagreement, which usually means one database has a bad record.
# ---------------------------------------------------------------------------

def normalize(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _clean_doi(doi):
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi.strip(), flags=re.I)


def _clean_venue(v):
    """
    Strip the IEEE catalog-number artifact that Crossref carries in older
    proceedings titles, e.g. "Proceedings. Visualization '97 (Cat. No. 97CB36155)"
    → "Proceedings. Visualization '97". Only that parenthetical is removed;
    legitimate ones (e.g. "(VIS)") are left intact.
    """
    return re.sub(r"\s*[\(\[]\s*Cat\.?\s*No\.?[^)\]]*[\)\]]", "", v or "", flags=re.I).strip()


def _describe_exception(ex):
    """Turn a requests/network exception into a short human-readable reason."""
    try:
        import requests
        if isinstance(ex, requests.exceptions.ConnectTimeout):
            return "connection timed out (could not reach server)"
        if isinstance(ex, requests.exceptions.ReadTimeout):
            return "read timed out (server too slow — try a higher --timeout)"
        if isinstance(ex, requests.exceptions.Timeout):
            return "request timed out"
        if isinstance(ex, requests.exceptions.SSLError):
            return "SSL/TLS error"
        if isinstance(ex, requests.exceptions.ConnectionError):
            # often DNS failure, refused connection, or no network
            return "connection error (DNS failure, no network, or refused)"
        if isinstance(ex, requests.exceptions.TooManyRedirects):
            return "too many redirects"
    except ImportError:
        pass
    return f"{type(ex).__name__}: {str(ex)[:100]}"


def _is_retryable_exception(ex):
    """Timeouts and connection errors are transient; retry them. SSL/redirects aren't."""
    try:
        import requests
        # SSLError subclasses ConnectionError but a bad cert won't fix itself — exclude it.
        if isinstance(ex, requests.exceptions.SSLError):
            return False
        return isinstance(ex, (requests.exceptions.Timeout,
                               requests.exceptions.ConnectionError))
    except ImportError:
        return False


# HTTP status codes worth retrying (transient server-side / throttling).
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _http_get_with_retry(session, url, timeout, label, params=None,
                         max_retries=4, base_delay=1.0, max_delay=60.0,
                         max_retry_after=300.0, limiter=None, on_retry=None):
    """
    GET with exponential backoff for transient failures (timeouts, connection
    errors, and retryable HTTP statuses incl. 429). Honors the server's
    Retry-After header *in full* when present. Returns a successful Response, or
    a final ('error', reason) tuple after exhausting retries.

    A server-supplied Retry-After is obeyed exactly (plus a small buffer) — never
    capped below it and never jittered downward, since retrying sooner than the
    server allowed only deepens the throttle. If it exceeds max_retry_after we
    give up on this lookup rather than sleeping absurdly long. Self-computed
    backoff (no header) still uses exponential growth capped at max_delay + jitter.

    limiter (optional RateLimiter): paces every real call to the host's minimum
    interval and absorbs a 429 cooldown so subsequent calls (other entries) wait
    too. on_retry(attempt, wait_seconds, reason) lets the caller show progress.
    """
    import requests  # caller guarantees availability

    attempt = 0
    while True:
        # --- pace against the host's limiter (min interval / known cooldown) ---
        if limiter:
            limiter.wait()

        # --- attempt the request ---
        try:
            r = session.get(url, params=params, timeout=timeout)
            transient = r.status_code in RETRYABLE_STATUS
            if not transient:
                return r  # success or a non-retryable status — let caller classify
            reason = f"HTTP {r.status_code} from {label}"
            retry_after = r.headers.get("Retry-After")
        except Exception as ex:
            if not _is_retryable_exception(ex):
                return ("error", _describe_exception(ex))
            reason = _describe_exception(ex)
            retry_after = None

        # --- compute backoff: honor server's Retry-After fully, else exponential ---
        server_wait = None
        if retry_after is not None:
            try:
                server_wait = float(retry_after)  # seconds form
            except ValueError:
                server_wait = None  # HTTP-date form: fall back to exponential

        if server_wait is not None:
            # too long to be worth waiting on a single entry — bail out
            if server_wait > max_retry_after:
                return ("error", f"{reason}: server asked to wait {server_wait:.0f}s "
                                 f"(> --max-retry-after {max_retry_after:.0f}s); gave up")
            wait = server_wait + 0.5  # honor exactly + tiny buffer; no cap, no jitter
            if limiter:
                limiter.penalize(wait)  # carry the cooldown to later calls
        else:
            wait = min(base_delay * (2 ** attempt), max_delay)
            wait *= 0.8 + 0.4 * _rand()  # ±20% jitter against thundering-herd

        # --- out of retries? give up with a descriptive reason ---
        if attempt >= max_retries:
            return ("error", f"{reason} (gave up after {max_retries + 1} attempts)")

        if on_retry:
            on_retry(attempt + 1, wait, reason)
        time.sleep(wait)
        attempt += 1


def _rand():
    # tiny indirection so tests can monkeypatch determinism if needed
    import random
    return random.random()


class RateLimiter:
    """
    Per-host pacing. Enforces a minimum interval between consecutive real calls
    and carries a server-imposed cooldown (from a 429 Retry-After) forward, so
    the next entry's lookup doesn't fire straight into a known throttle window.
    Cache hits never touch the limiter, so they stay free.
    """

    def __init__(self, min_interval):
        self.min_interval = max(0.0, min_interval)
        self.next_allowed = 0.0  # monotonic timestamp before which we must not call

    def wait(self):
        now = time.monotonic()
        if now < self.next_allowed:
            time.sleep(self.next_allowed - now)
        # schedule the floor for the following call
        self.next_allowed = time.monotonic() + self.min_interval

    def penalize(self, seconds):
        """Push the next allowed call at least `seconds` into the future."""
        self.next_allowed = max(self.next_allowed, time.monotonic() + seconds)


def fetch_crossref(session, e, timeout=15, mailto=None, **retry_kw):
    """
    Look up by DOI on Crossref.
    Returns a normalized record, 'notfound', None (nothing to query),
    or ('error', reason_str) with a specific diagnostic.
    """
    doi = e.get("doi", "").strip()
    if not doi:
        return None
    url = f"https://api.crossref.org/works/{_clean_doi(doi)}"
    # mailto enters Crossref's "polite pool" (faster, more reliable than anonymous)
    params = {"mailto": mailto} if mailto else None
    r = _http_get_with_retry(session, url, timeout, "Crossref", params=params, **retry_kw)
    if isinstance(r, tuple):       # exhausted retries / non-retryable network error
        return r
    if r.status_code == 404:
        return "notfound"
    if r.status_code != 200:
        return ("error", f"HTTP {r.status_code} from Crossref")
    try:
        msg = r.json().get("message", {})
    except ValueError:
        snippet = (r.text or "")[:80].replace("\n", " ")
        return ("error", f"non-JSON response (starts: {snippet!r})")

    titles = msg.get("title") or []
    authors = msg.get("author") or []
    year = None
    for fld in ("published-print", "published-online", "issued"):
        parts = msg.get(fld, {}).get("date-parts", [[None]])
        if parts and parts[0] and parts[0][0]:
            year = parts[0][0]
            break

    def _first(x):
        return (x[0] if isinstance(x, list) and x else (x or "")) or ""

    cr_doi = _clean_doi(msg.get("DOI", ""))
    return {
        "source": "Crossref",
        "title": titles[0] if titles else "",
        "surname": authors[0].get("family", "") if authors else "",
        "year": year,
        # richer fields kept for suggestions/disagreement checks; all best-effort
        "doi": cr_doi,
        "url": f"https://doi.org/{cr_doi}" if cr_doi else "",  # human-checkable link
        "publisher": msg.get("publisher", "") or "",
        "address": msg.get("publisher-location", "") or "",
        "pages": msg.get("page", "") or "",
        "articleno": msg.get("article-number", "") or "",
        "volume": str(msg.get("volume", "") or ""),
        "number": str(msg.get("issue", "") or ""),
        "venue": _clean_venue(_first(msg.get("container-title"))),
        "venue_short": _clean_venue(_first(msg.get("short-container-title"))),
        "type": msg.get("type", "") or "",
    }


def fetch_datacite(session, e, timeout=20, **retry_kw):
    """
    Verify an arXiv (DataCite) DOI. Crossref doesn't index arXiv DOIs
    (prefix 10.48550/arXiv.*), so those are checked here instead.
    Returns a normalized record, 'notfound', None, or ('error', reason_str).
    """
    doi = e.get("doi", "").strip()
    if not doi:
        return None
    url = f"https://api.datacite.org/dois/{_clean_doi(doi)}"
    r = _http_get_with_retry(session, url, timeout, "DataCite", **retry_kw)
    if isinstance(r, tuple):
        return r
    if r.status_code == 404:
        return "notfound"
    if r.status_code != 200:
        return ("error", f"HTTP {r.status_code} from DataCite")
    try:
        attr = r.json().get("data", {}).get("attributes", {})
    except ValueError:
        snippet = (r.text or "")[:80].replace("\n", " ")
        return ("error", f"non-JSON response from DataCite (starts: {snippet!r})")

    titles = attr.get("titles") or []
    creators = attr.get("creators") or []
    surname = ""
    if creators:
        c0 = creators[0]
        # DataCite gives familyName, or a single "Last, First" / "First Last" name
        surname = (c0.get("familyName") or "").strip()
        if not surname:
            nm = (c0.get("name") or "").strip()
            surname = nm.split(",")[0].strip() if "," in nm else (nm.split()[-1] if nm else "")
    yr = attr.get("publicationYear")
    return {
        "source": "DataCite",
        "title": titles[0].get("title", "") if titles else "",
        "surname": surname,
        "year": int(yr) if str(yr).isdigit() else None,
        "url": f"https://doi.org/{_clean_doi(doi)}",  # human-checkable link
    }


# Words too common to count toward a title match (prepositions/articles plus a
# few generic connectives). Keeps "Against Access" from matching on "against".
_TITLE_STOP = {
    "a", "an", "the", "of", "for", "and", "or", "to", "in", "on", "with", "without",
    "against", "via", "using", "from", "by", "at", "as", "is", "are", "be", "toward",
    "towards", "into", "through", "over", "under", "new",
}

# Minimum two-directional title coverage to accept a DBLP title-search hit.
TITLE_MATCH_MIN = 0.5


def _title_tokens(t):
    toks = re.findall(r"[a-z0-9]+", (t or "").lower())
    return {w for w in toks if w not in _TITLE_STOP and len(w) > 1}


def _title_match_score(a, b):
    """
    Confidence that two titles name the same work: the *smaller* of how much of
    each title's content words the other covers. Requiring both directions to be
    high means a short query can't latch onto a long unrelated title that merely
    contains its words (and vice versa). 0.0 when either has no content words.
    """
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if not inter:
        return 0.0
    return min(inter / len(ta), inter / len(tb))


def fetch_dblp(session, e, timeout=20, **retry_kw):
    """
    Look up on DBLP. Try DOI first (precise), then title search.
    DBLP's JSON search API: https://dblp.org/search/publ/api?q=...&format=json
    Returns a normalized record, 'notfound', None, or ('error', reason_str).
    """
    doi = e.get("doi", "").strip()
    query = None
    if doi:
        query = _clean_doi(doi)            # DBLP indexes DOIs as searchable text
    else:
        title = e.get("title", "")
        title = re.sub(r"[{}]", "", title)  # strip brace protection for the query
        if not title.strip():
            return None
        query = title

    url = "https://dblp.org/search/publ/api"
    params = {"q": query, "format": "json", "h": 5}
    r = _http_get_with_retry(session, url, timeout, "DBLP", params=params, **retry_kw)
    if isinstance(r, tuple):
        return r
    if r.status_code != 200:
        return ("error", f"HTTP {r.status_code} from DBLP")
    try:
        hits = r.json().get("result", {}).get("hits", {})
    except ValueError:
        # DBLP serves an HTML error/throttle page instead of JSON when overloaded
        snippet = (r.text or "")[:80].replace("\n", " ")
        if "rate" in snippet.lower() or "too many" in snippet.lower():
            return ("error", "DBLP returned a rate-limit page (raise --sleep)")
        return ("error", f"non-JSON response from DBLP (starts: {snippet!r})")

    if int(hits.get("@total", "0")) == 0 or "hit" not in hits:
        return "notfound"

    hit_list = hits["hit"]
    if isinstance(hit_list, dict):
        hit_list = [hit_list]

    infos = [h["info"] for h in hit_list]

    # Strongest signal: a hit whose DOI matches ours — accept it outright.
    best = None
    if doi:
        want_doi = _clean_doi(doi).lower()
        best = next((info for info in infos
                     if _clean_doi(info.get("doi", "")).lower() == want_doi), None)

    # Otherwise require a confident title match in BOTH directions, so a short or
    # common-word title (e.g. "Against Access") can't latch onto an unrelated paper
    # that merely shares a word or two. Below threshold → treat as not found.
    if best is None:
        bib_title = re.sub(r"[{}]", "", e.get("title", ""))
        best = max(infos, key=lambda info: _title_match_score(bib_title, info.get("title", "")))
        if _title_match_score(bib_title, best.get("title", "")) < TITLE_MATCH_MIN:
            return "notfound"

    # DBLP author field: dict, list of dicts, or list of strings
    authors_field = best.get("authors", {}).get("author", [])
    if isinstance(authors_field, dict):
        authors_field = [authors_field]
    first_author = ""
    if authors_field:
        a0 = authors_field[0]
        first_author = a0.get("text", "") if isinstance(a0, dict) else str(a0)
    # DBLP gives full name "Jane Q. Public", sometimes with a numeric homonym
    # disambiguator like "Ashish Vaswani 0001" — strip a trailing digit group,
    # then take the last token as surname.
    first_author = re.sub(r"\s+\d{4}$", "", first_author).strip()
    surname = first_author.split()[-1] if first_author else ""

    yr = best.get("year", "")
    venue = best.get("venue", "")
    if isinstance(venue, list):          # DBLP gives a list when a paper has multiple venues
        venue = venue[0] if venue else ""
    return {
        "source": "DBLP",
        # DBLP appends a period and HTML-escapes (&amp; etc.) — clean both up
        "title": html.unescape(re.sub(r"\.$", "", best.get("title", ""))),
        "surname": surname,
        "year": int(yr) if str(yr).isdigit() else None,
        # richer fields kept for suggestions/disagreement checks; no publisher/address
        "doi": _clean_doi(best.get("doi", "")) if best.get("doi") else "",
        "url": best.get("url", "") or "",  # DBLP record page (links out to publisher)
        "publisher": "",
        "address": "",
        "pages": best.get("pages", "") or "",
        "articleno": "",
        "volume": str(best.get("volume", "") or ""),
        "number": str(best.get("number", "") or ""),
        "venue": html.unescape(venue) if venue else "",
        "venue_short": "",
        "type": best.get("type", "") or "",  # e.g. "Conference and Workshop Papers"
    }


def compare_record(e, rec, report):
    """Compare one normalized source record against the bib entry."""
    key = e.get("ID", "?")
    src = rec["source"]

    # title
    bib_t, src_t = normalize(e.get("title")), normalize(rec["title"])
    if bib_t and src_t and src_t not in bib_t and bib_t not in src_t:
        report.warn(key, f"Title differs from {src}:\n"
                         f"          bib:  {e.get('title','').strip()}\n"
                         f"          {src.lower():9}{rec['title'].strip()}")

    # first-author surname
    src_surname = normalize(rec["surname"])
    bib_first = normalize(e.get("author", "").split(" and ")[0])
    if src_surname and src_surname not in bib_first:
        report.warn(key, f"First-author surname '{rec['surname']}' from {src} not found "
                         "in bib author field — verify names (incl. special characters).")

    # year
    bib_year = e.get("year", "").strip()
    if rec["year"] and bib_year.isdigit() and int(bib_year) != rec["year"]:
        sev = report.err if abs(int(bib_year) - rec["year"]) > 1 else report.warn
        sev(key, f"Year mismatch: bib says {bib_year}, {src} says {rec['year']}. "
                 "(For TVCG/VIS, confirm against special-issue publication year.)")


# Crossref 'type' → BibTeX entry type, for catching misclassified entries.
CROSSREF_TYPE_MAP = {
    "journal-article": "article",
    "proceedings-article": "inproceedings",
    "book": "book",
    "book-chapter": "incollection",
    "monograph": "book",
    "posted-content": "misc",      # preprints
}


def _page_span(s):
    """
    Numbers identifying the page span, normalized so equivalent formats compare
    equal. For article-number style 'N:a--N:b' (or 'N:a-N:b') take the inner page
    numbers (a, b) so e.g. '72:1--72:23' matches a source's '1-23'; otherwise take
    all integers. Lets the pages-disagreement check ignore pure formatting while
    still catching genuinely different spans.
    """
    colon = re.findall(r"\d+:(\d+)", s or "")
    return colon if colon else re.findall(r"\d+", s or "")


def _pick_field(records, field):
    """Best value for a field across sources (Crossref > DBLP > DataCite),
    returned with its source name and the record's verification URL."""
    for s in ("Crossref", "DBLP", "DataCite"):
        r = records.get(s)
        if r and str(r.get(field, "")).strip():
            return str(r[field]).strip(), s, (r.get("url", "") or "")
    return None, None, ""


# Boilerplate dropped when comparing venue names so "Proc. CHI" can match the full
# "Proceedings of the 2024 CHI Conference on Human Factors…". Distinctive tokens
# (acronyms, topic words) survive; generic scaffolding does not.
_VENUE_STOP = {
    "proceedings", "proc", "the", "of", "on", "in", "and", "for", "annual",
    "international", "conference", "symposium", "workshop", "joint", "vol", "volume",
    "extended", "abstracts", "companion", "part",
}


def _venue_tokens(v):
    toks = re.findall(r"[a-z0-9]+", (v or "").lower())
    return {t for t in toks
            if t not in _VENUE_STOP and len(t) >= 3 and not t.isdigit()
            and not re.match(r"\d+(st|nd|rd|th)$", t)}


def _venue_compatible(bib, src, src_short=""):
    """Heuristic: do two venue strings plausibly name the same venue? Shared
    distinctive token, or one's compact form appearing inside the other's (so an
    abbreviation like 'PacificVis' still matches 'Pacific Visualization')."""
    b = _venue_tokens(bib)
    s = _venue_tokens(src) | _venue_tokens(src_short)
    if not b or not s:
        return True                      # not enough signal → don't flag
    if b & s:
        return True
    bc = "".join(re.findall(r"[a-z0-9]+", (bib or "").lower()))
    sc = "".join(re.findall(r"[a-z0-9]+", (src or "").lower()))
    return any(t in bc for t in s) or any(t in sc for t in b)


def flag_disagreements(e, records, report):
    """
    Flag fields the entry *already has* that disagree with the source — the tell
    that a DOI resolves to a slightly different record (wrong version / wrong
    metadata), which the missing-field suggestions can't catch. Kept conservative
    to limit noise; severities reflect confidence (DOI/volume WARN, pages/venue INFO).
    """
    key = e.get("ID", "?")
    etype = e.get("ENTRYTYPE", "").lower()

    # DOI differs — mainly meaningful from DBLP, since Crossref echoes the DOI we
    # queried it with. A different DOI often means preprint-vs-published.
    bib_doi = _clean_doi(e.get("doi", ""))
    if bib_doi:
        for s in ("DBLP", "Crossref", "DataCite"):
            sd = (records.get(s) or {}).get("doi", "")
            if sd and sd.lower() != bib_doi.lower():
                report.warn(key, f"{s} lists a different DOI ({sd}) than your entry "
                                 f"({bib_doi}) — you may be citing the wrong version "
                                 "(e.g. preprint vs. published). Check: open "
                                 f"https://doi.org/{sd} and compare.")
                break

    # volume differs (both numeric)
    bib_vol = e.get("volume", "").strip()
    if bib_vol.isdigit():
        val, s, url = _pick_field(records, "volume")
        if val and val.isdigit() and val != bib_vol:
            opn = f"open {url} and " if url else ""
            report.warn(key, f"Volume mismatch: your entry says {bib_vol}, {s} says {val}. "
                             f"Check: {opn}confirm the right volume (for TVCG/VIS also "
                             "re-check volume/year parity).")

    # pages differ (compare the integers, ignoring punctuation/format)
    bib_pages = e.get("pages", "").strip()
    if bib_pages:
        val, s, url = _pick_field(records, "pages")
        if val:
            bnums = _page_span(bib_pages)
            snums = _page_span(val)
            if bnums and snums and bnums != snums:
                see = f" See {url}." if url else ""
                report.info(key, f"Pages differ: your entry has {bib_pages!r}, {s} has "
                                 f"{val!r}. Often just formatting, but confirm it's the same "
                                 f"span — and the same paper.{see}")

    # venue differs (conservative; abbreviations can still trip it, hence INFO)
    bib_venue = (e.get("booktitle", "") if etype == "inproceedings"
                 else e.get("journal", "")).strip()
    if bib_venue:
        val, s, url = _pick_field(records, "venue")
        src_short = (records.get(s) or {}).get("venue_short", "") if s else ""
        if val and not _venue_compatible(bib_venue, val, src_short):
            see = f" See {url}." if url else ""
            report.info(key, f"Venue may differ: your entry says {bib_venue!r}, {s} says "
                             f"{val!r}. Verify you have the right venue (heuristic — "
                             f"abbreviations can trip this).{see}")


def suggest_completions(e, records, report):
    """
    Offer source metadata for fields the bib entry is *missing*, merged across the
    sources that already ran (preferring Crossref > DBLP > DataCite). Suggestions
    are advisory only — no source is ground truth — so each one states the specific
    caveat the venue guidelines warn about, then a "Check:" with a concrete way to
    verify it and a link to the record. Costs no extra network calls: it reuses
    records already fetched.
    """
    key = e.get("ID", "?")
    etype = e.get("ENTRYTYPE", "").lower()

    def pick(field):
        return _pick_field(records, field)

    def at(url):
        return f"Open {url} and " if url else ""

    # DOI — missing entirely
    if not e.get("doi", "").strip():
        val, s, _ = pick("doi")
        if val:
            link = f"https://doi.org/{val}"
            report.info(key, f"{s} has a DOI for this entry: {val}. A resolvable DOI isn't "
                             "proof it's the right record — it may point at an arXiv preprint "
                             "or a different edition. Check: open " + link + " and confirm the "
                             "title, authors, and venue match THIS paper before adding it to "
                             "the DOI field (a hyperref style will then link it).")

    # pages / article number — only when the entry has neither
    if not has_pages_or_artno(e):
        artno, asrc, aurl = pick("articleno")
        pages, psrc, purl = pick("pages")
        vol_any, _, _ = pick("volume")
        if artno:
            report.info(key, f"{asrc} lists an article number ({artno}), so this paper is "
                             "numbered, not paginated; cite it consistently rather than mixing "
                             f"article numbers and page ranges across the file. Check: {at(aurl)}"
                             "confirm it, then cite it the same way as your other numbered "
                             "entries — ACM-style 'article no. N, M pages' or 'N:1--N:M' — "
                             "never as a single page.")
        elif pages:
            # No volume anywhere + a low page range usually means not-yet-paginated.
            if etype == "article" and not e.get("volume", "").strip() and not vol_any \
                    and RANGE_PAGE_RE.match(pages):
                report.info(key, f"{psrc} lists pages {pages!r}, but no volume is available "
                                 "from any source — a sign the paper isn't in a finalized issue "
                                 "yet, and digital libraries emit placeholder ranges like "
                                 f"'1--11' for unpaginated papers. Check: {at(purl)}if no "
                                 "volume/issue is assigned, cite it WITHOUT pages and add a "
                                 f"'To appear' note rather than copying {pages!r}.")
            else:
                report.info(key, f"{psrc} lists pages {pages!r} — but that may be a placeholder "
                                 "range for a paper that isn't fully paginated. Check: "
                                 f"{at(purl)}confirm the paper is finalized before adding it; "
                                 "if it's still early-access, omit pages and mark 'To appear' "
                                 "instead.")

    # publisher / address — for @inproceedings missing them
    if etype == "inproceedings":
        if not e.get("publisher", "").strip():
            val, s, url = pick("publisher")
            if val:
                report.info(key, f"{s} lists the publisher as {val!r}; entries should use the "
                                 f"short form. Check: {at(url)}add it as e.g. 'ACM', 'IEEE', or "
                                 "'Springer' — not the full legal name.")
        if not e.get("address", "").strip():
            val, s, url = pick("address")
            if val:
                report.info(key, f"{s} lists the publisher location as {val!r}; the guidelines "
                                 f"want just the town. Check: {at(url)}add the short form "
                                 "(e.g. 'New York'), dropping the state/country (', NY, USA').")

    # volume — for @article missing it. The volume/year parity rule is a TVCG
    # special-issue artifact, so only raise it for TVCG/VIS — not e.g. CGF/EuroVis.
    if etype == "article" and not e.get("volume", "").strip():
        val, s, url = pick("volume")
        if val:
            venue_text = " ".join([e.get("journal", ""),
                                   (records.get("Crossref") or {}).get("venue", ""),
                                   (records.get("DBLP") or {}).get("venue", "")])
            byr = e.get("year", "").strip()
            if TVCG_RE.search(venue_text):
                parity = ""
                if val.isdigit() and byr.isdigit() and (int(val) % 2) != (int(byr) % 2):
                    parity = (f" Note: volume {val} and your year {byr} have opposite parity — "
                              "for TVCG/VIS that means the YEAR is likely wrong (use the "
                              "special-issue publication year, not the presentation year).")
                report.info(key, f"{s} lists volume {val}. For TVCG/VIS, volume and year must "
                                 f"share even/odd parity. Check: {at(url)}add the volume and "
                                 "confirm the parity holds." + parity)
            else:
                report.info(key, f"{s} lists volume {val}. Check: {at(url)}confirm it matches "
                                 "the issue you're citing and add it.")

    # venue — only when the venue field is entirely missing (else the short-form
    # nudges in the offline pass already cover wording)
    if etype == "inproceedings" and not e.get("booktitle", "").strip():
        val, s, url = pick("venue")
        if val:
            report.info(key, f"{s} lists the proceedings as {val!r}; long names repeat the year "
                             f"and bloat the entry. Check: {at(url)}add a booktitle in short "
                             "'Proc. CONF' form (e.g. 'Proc. CHI').")
    if etype == "article" and not e.get("journal", "").strip():
        val, s, url = pick("venue")
        if val:
            report.info(key, f"{s} lists the journal as {val!r}; journal-name style should be "
                             f"consistent across entries. Check: {at(url)}add it, using the "
                             "ISO4 abbreviation if you abbreviate the others.")

    # entry-type mismatch (Crossref classification vs the bib @type)
    cr = records.get("Crossref")
    if cr and etype in ("article", "inproceedings"):
        expected = CROSSREF_TYPE_MAP.get(cr.get("type", ""))
        if expected and expected != etype:
            url = cr.get("url", "") or ""
            report.warn(key, f"Crossref classifies this as '{cr.get('type')}' (→ @{expected}) "
                             f"but the entry is @{etype}; citing the wrong version "
                             "(preprint/conference vs. journal) carries the wrong venue, year, "
                             f"and pages. Check: {at(url)}decide which version you mean; if it's "
                             f"the @{expected}, switch the entry type and its fields "
                             "(journal+volume vs. booktitle+publisher).")


# ---------------------------------------------------------------------------
# On-disk cache for network lookups
#
# Keyed by a hash of only the fields that affect a lookup (doi, title, author,
# year) + the source name + a schema version. So editing an unrelated field
# (pages, booktitle, note) reuses the cache, but fixing a DOI or title
# correctly invalidates that entry's cached result. Stored as JSON; results
# include normalized records and status strings ('notfound'). Transient
# 'error' results are never cached (so a flaky network gets retried).
# ---------------------------------------------------------------------------

CACHE_SCHEMA = "v2"  # bump if record shape or comparison semantics change


def _lookup_key(e, source):
    relevant = "|".join([
        _clean_doi(e.get("doi", "")),
        (e.get("title", "") or "").strip(),
        (e.get("author", "") or "").strip(),
        (e.get("year", "") or "").strip(),
    ])
    digest = hashlib.sha256(relevant.encode("utf-8")).hexdigest()[:16]
    return f"{CACHE_SCHEMA}:{source}:{digest}"


class LookupCache:
    def __init__(self, path):
        self.path = path
        self.data = {}
        self.hits = 0
        self.misses = 0
        self.dirty = False
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                # tolerate older/foreign files; only keep current schema entries
                if isinstance(loaded, dict):
                    self.data = {k: v for k, v in loaded.items()
                                 if k.startswith(CACHE_SCHEMA + ":")}
            except (ValueError, OSError):
                self.data = {}  # corrupt cache → start fresh, don't crash

    def get(self, e, source):
        hit = self.data.get(_lookup_key(e, source), _MISS)
        if hit is _MISS:
            self.misses += 1
            return _MISS
        self.hits += 1
        return hit

    def put(self, e, source, value):
        # never cache transient errors; cache records and definitive 'notfound'
        if value == "error" or (isinstance(value, tuple) and value and value[0] == "error"):
            return
        self.data[_lookup_key(e, source)] = value
        self.dirty = True

    def save(self):
        if not self.path or not self.dirty:
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)  # atomic; won't corrupt on interrupt
        except OSError:
            pass  # caching is best-effort; failure to write shouldn't break the audit


_MISS = object()  # sentinel distinct from None/'notfound'


def entry_sources(e, sources, dblp_all=False):
    """
    Decide which sources to actually query for one entry.

    Crossref handles any DOI'd entry (broad coverage, generous polite pool).
    DBLP is reserved for where it uniquely helps and is spent sparingly:
    only @inproceedings/@article, and (in the default 'both' mode) only when the
    entry has no DOI for Crossref to verify, or it's TVCG/VIS — where DBLP's
    venue/year data is authoritative and the year-parity check matters most.
    Forcing a single --source, or passing --dblp-all, skips that surgical gate.
    """
    etype = e.get("ENTRYTYPE", "").lower()
    has_doi = bool(e.get("doi", "").strip())
    arxiv = is_arxiv_doi(e.get("doi", ""))
    is_cs = etype in ("inproceedings", "article")
    is_tvcg = bool(TVCG_RE.search(e.get("journal", "") + " " + e.get("booktitle", "")))
    # surgical = the default dual-source mode; a forced single source or --dblp-all opts out
    surgical = ("crossref" in sources) and ("dblp" in sources) and not dblp_all

    chosen = []
    # Crossref verifies registered DOIs — but arXiv DOIs live in DataCite, so a
    # Crossref lookup of one is a guaranteed 404. Route arXiv DOIs to DataCite.
    if "crossref" in sources and has_doi and not arxiv:
        chosen.append("crossref")
    if "datacite" in sources and arxiv:
        chosen.append("datacite")
    if "dblp" in sources and is_cs and (not surgical or not has_doi or is_tvcg):
        chosen.append("dblp")
    return chosen


def audit_sources(entries, report, sources, cache=None, timeout=15,
                  max_retries=4, base_delay=1.0, max_retry_after=300.0,
                  dblp_sleep=1.5, crossref_sleep=0.1, mailto=None, dblp_all=False):
    try:
        import requests
    except ImportError:
        report.warn("(global)", "requests not installed; skipping cross-check. "
                                "pip install requests to enable it.")
        return

    session = requests.Session()
    ua_mail = f" (mailto:{mailto})" if mailto else ""
    session.headers.update({
        "User-Agent": f"bibaudit/1.0{ua_mail}"  # polite API citizen
    })
    fetchers = {"crossref": fetch_crossref, "datacite": fetch_datacite, "dblp": fetch_dblp}
    # Pace each host independently: DBLP wants 1–2s; Crossref/DataCite pools are generous.
    limiters = {"crossref": RateLimiter(crossref_sleep),
                "datacite": RateLimiter(crossref_sleep),
                "dblp": RateLimiter(dblp_sleep)}
    fail_reasons = defaultdict(int)  # normalized reason -> count, for a summary
    calls = defaultdict(int)         # source -> real network calls made (cache misses)

    # Progress line on stderr (stdout stays clean for the piped report).
    # Only animate when stderr is an interactive terminal; otherwise stay quiet.
    show_progress = sys.stderr.isatty()
    total = len(entries)
    src_label = ("+".join(sources)) + ("/surgical" if (
        "dblp" in sources and "crossref" in sources and not dblp_all) else "")

    def _err_count():
        return len(report.items.get(Report.ERROR, []))

    def progress(i):
        if not show_progress:
            return
        bar_w = 24
        filled = int(bar_w * i / total) if total else bar_w
        bar = "#" * filled + "-" * (bar_w - filled)
        net = "  ".join(f"{s}: {calls[s]}" for s in sources)
        cached = f"  cached: {cache.hits}" if cache else ""
        sys.stderr.write(
            f"\r  cross-checking ({src_label}) [{bar}] {i}/{total}"
            f"  errors: {_err_count()}  {net}{cached}   "
        )
        sys.stderr.flush()

    for idx, e in enumerate(entries, 1):
        key = e.get("ID", "?")
        records = {}  # source name -> normalized record

        def _on_retry(attempt, wait, reason, _src=None):
            if show_progress:
                sys.stderr.write(
                    f"\r  [{key}] {_src} {reason}; backing off "
                    f"{wait:.1f}s (retry {attempt}/{max_retries})…   "
                )
                sys.stderr.flush()

        def do_lookup(src):
            """Cache-first lookup for one source; only hits the network (and paces) on a miss."""
            result = cache.get(e, src) if cache else _MISS
            if result is _MISS:
                fetch_kw = dict(
                    timeout=timeout, max_retries=max_retries, base_delay=base_delay,
                    max_retry_after=max_retry_after, limiter=limiters[src],
                    on_retry=lambda a, w, r, _s=src: _on_retry(a, w, r, _s),
                )
                if src == "crossref":
                    fetch_kw["mailto"] = mailto
                result = fetchers[src](session, e, **fetch_kw)
                calls[src] += 1
                if cache:
                    cache.put(e, src, result)
            return result

        def note_failure(src, reason):
            report.warn(key, f"{src.capitalize()} lookup failed: {reason}.")
            # bucket by leading phrase so the summary stays compact
            fail_reasons[f"{src}: {reason.split(' — ')[0].split(' (')[0]}"] += 1

        for src in entry_sources(e, sources, dblp_all=dblp_all):
            result = do_lookup(src)

            # Crossref 404 ≠ bad DOI: arXiv, university presses, and data repos
            # register with DataCite, not Crossref. Fall back to DataCite before
            # calling a DOI missing — only a DOI absent from BOTH is a real error.
            if (src == "crossref" and result == "notfound"
                    and e.get("doi", "").strip() and "datacite" in sources):
                doi_show = _clean_doi(e.get("doi", ""))
                fb = do_lookup("datacite")
                if isinstance(fb, dict):
                    # Valid DOI, just DataCite-registered (arXiv / university press /
                    # data repo) — nothing to report; still cross-check its metadata.
                    records[fb["source"]] = fb
                    compare_record(e, fb, report)
                elif fb == "notfound":
                    report.err(key, f"DOI not found in Crossref or DataCite: {doi_show} "
                                    "— verify the DOI.")
                elif isinstance(fb, tuple) and fb[0] == "error":
                    note_failure("datacite", fb[1])  # transient; don't hard-error the DOI
                continue

            if result is None:
                continue  # nothing to query on (no DOI for crossref, no title for dblp)
            if isinstance(result, tuple) and result[0] == "error":
                note_failure(src, result[1])
                continue
            if result == "notfound":
                # A DOI that doesn't resolve in its registry is a hard error; a title
                # miss on DBLP just means DBLP doesn't index that venue.
                if src == "crossref" and e.get("doi", "").strip():
                    report.err(key, f"DOI not found on Crossref: {_clean_doi(e.get('doi',''))}.")
                elif src == "datacite" and e.get("doi", "").strip():
                    report.err(key, f"arXiv/DataCite DOI did not resolve on DataCite: "
                                    f"{_clean_doi(e.get('doi',''))} — verify the DOI.")
                else:
                    report.info(key, f"Not found on {src.capitalize()} "
                                     f"(may be a venue {src.capitalize()} doesn't index).")
                continue
            records[result["source"]] = result
            compare_record(e, result, report)

        # source-vs-source agreement (only meaningful with 2+ records)
        if len(records) >= 2:
            recs = list(records.values())
            a, b = recs[0], recs[1]
            if a["year"] and b["year"] and a["year"] != b["year"]:
                report.warn(key, f"Sources disagree on year: "
                                 f"{a['source']} {a['year']} vs {b['source']} {b['year']} "
                                 "— at least one database record is wrong; verify manually.")
            ta, tb = normalize(a["title"]), normalize(b["title"])
            if ta and tb and ta not in tb and tb not in ta:
                report.info(key, f"Sources disagree on title wording "
                                 f"({a['source']} vs {b['source']}); usually harmless "
                                 "(subtitle/punctuation) but worth a glance.")

        # always (free): suggest values for missing fields, and flag fields that
        # are present but disagree with the source (a wrong-version / wrong-DOI tell)
        if records:
            suggest_completions(e, records, report)
            flag_disagreements(e, records, report)

        progress(idx)

    if show_progress:
        sys.stderr.write("\r" + " " * 72 + "\r")  # wipe the progress line
        sys.stderr.flush()

    net_summary = ", ".join(f"{s}: {calls[s]}" for s in sources)
    report.info("(global)", f"Network calls — {net_summary}. "
                            "(DBLP is queried surgically by default; see --dblp-all "
                            "to cross-check every conference/journal entry against it.)")

    if cache:
        cache.save()
        report.info("(global)", f"Cache: {cache.hits} hit(s), {cache.misses} network "
                                f"lookup(s). Re-runs reuse results for unchanged entries.")

    if fail_reasons:
        total_fails = sum(fail_reasons.values())
        lines = "; ".join(f"{count}× {reason}"
                          for reason, count in sorted(fail_reasons.items(),
                                                      key=lambda kv: -kv[1]))
        report.warn("(global)", f"{total_fails} lookup failure(s) by cause: {lines}. "
                                "If DBLP rate-limiting dominates, re-run with a higher "
                                "--dblp-sleep (e.g. 2.0) and/or --timeout; failures aren't "
                                "cached, so a re-run retries only the ones that failed.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Audit a BibTeX file for a conference reference pass.")
    ap.add_argument("bibfile")
    ap.add_argument("--source", choices=["crossref", "dblp", "both", "none"],
                    default="both",
                    help="Which authoritative source(s) to cross-check against. "
                         "'dblp' is best for CS conference papers; 'crossref' has broad "
                         "DOI/journal coverage; 'both' (default) uses Crossref for DOI'd "
                         "entries and DBLP surgically (no-DOI + TVCG/VIS), and flags "
                         "source-vs-source disagreement; 'none' for offline-only.")
    ap.add_argument("--dblp-all", action="store_true",
                    help="In 'both' mode, query DBLP for every @inproceedings/@article "
                         "(not just no-DOI + TVCG/VIS) for maximal cross-checking. Slower "
                         "and far more DBLP load — expect rate-limit backoff on large files.")
    ap.add_argument("--mailto", default="jonathan.zong@gmail.com",
                    help="Contact email sent to Crossref (User-Agent + ?mailto=) to enter "
                         "its faster, more reliable 'polite pool'. Set to '' to opt out.")
    ap.add_argument("--no-crossref", action="store_true",
                    help="Alias for --source none (offline only). Overrides --source.")
    ap.add_argument("--dblp-sleep", type=float, default=1.5,
                    help="Minimum seconds between real DBLP calls (default 1.5; DBLP's FAQ "
                         "asks for 1–2s). Raise toward 2.0+ if you still see 429s.")
    ap.add_argument("--crossref-sleep", type=float, default=0.1,
                    help="Minimum seconds between real Crossref calls (default 0.1; the "
                         "polite pool is generous).")
    ap.add_argument("--sleep", type=float, default=None,
                    help="Deprecated: if set, seeds BOTH --dblp-sleep and --crossref-sleep "
                         "to this value. Prefer the per-source flags.")
    ap.add_argument("--timeout", type=float, default=15.0,
                    help="Per-request network timeout in seconds (default 15). "
                         "Raise if you see read-timeout failures on a slow connection.")
    ap.add_argument("--max-retries", type=int, default=4,
                    help="Max retries per request on transient failures (429/5xx/"
                         "timeouts/connection errors) with exponential backoff (default 4). "
                         "Set 0 to disable retrying.")
    ap.add_argument("--retry-base-delay", type=float, default=1.0,
                    help="Base backoff delay in seconds; doubles each retry, with jitter, "
                         "capped at 60s. Used only when the server sends no Retry-After "
                         "header (those are honored in full instead) (default 1.0).")
    ap.add_argument("--max-retry-after", type=float, default=300.0,
                    help="If a server's Retry-After asks us to wait longer than this many "
                         "seconds, give up on that lookup instead of sleeping (default 300).")
    ap.add_argument("--cache", metavar="PATH", default=".bibaudit_cache.json",
                    help="Path to the on-disk lookup cache (default: "
                         ".bibaudit_cache.json next to where you run it). Re-runs reuse "
                         "results for entries whose doi/title/author/year are unchanged.")
    ap.add_argument("--no-cache", action="store_true",
                    help="Disable caching for this run (always hit the network).")
    ap.add_argument("--clear-cache", action="store_true",
                    help="Delete the cache file before running, forcing fresh lookups.")
    args = ap.parse_args()

    if args.no_crossref:
        args.source = "none"

    # Deprecated --sleep seeds both per-source rates if given.
    dblp_sleep, crossref_sleep = args.dblp_sleep, args.crossref_sleep
    if args.sleep is not None:
        dblp_sleep = crossref_sleep = args.sleep

    if args.clear_cache and args.cache and os.path.exists(args.cache):
        try:
            os.remove(args.cache)
        except OSError:
            pass

    parser = BibTexParser(common_strings=True)
    parser.ignore_nonstandard_types = False
    with open(args.bibfile, encoding="utf-8", errors="replace") as f:
        raw_text = f.read()
    db = bibtexparser.loads(raw_text, parser=parser)

    entries = db.entries
    report = Report()

    audit_offline(entries, report)
    audit_month_format(raw_text, report)
    if args.source != "none":
        sources = ["dblp", "crossref"] if args.source == "both" else [args.source]
        # arXiv DOIs aren't in Crossref; verify them against DataCite wherever
        # Crossref is enabled (it's only ever queried for arXiv-DOI entries).
        if "crossref" in sources:
            sources.append("datacite")
        cache = None if args.no_cache else LookupCache(args.cache)
        audit_sources(entries, report, sources, cache=cache,
                      timeout=args.timeout, max_retries=args.max_retries,
                      base_delay=args.retry_base_delay,
                      max_retry_after=args.max_retry_after,
                      dblp_sleep=dblp_sleep, crossref_sleep=crossref_sleep,
                      mailto=(args.mailto or None), dblp_all=args.dblp_all)

    print(report.render())
    # exit non-zero if any ERROR, so it can gate a CI step
    sys.exit(1 if report.items.get(Report.ERROR) else 0)


if __name__ == "__main__":
    main()

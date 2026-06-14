# bibaudit

A command-line tool that audits a BibTeX file for **correctness, completeness, and
consistency** before a conference camera-ready / reference pass. It cross-checks each
entry against authoritative sources (Crossref and DBLP) and flags problems grouped by
severity.

> **bibaudit is an assistant, not an authority.** It catches mechanical mistakes and
> surfaces likely problems, but it cannot certify a bibliography as correct. Several
> important checks are still **manual** — see [What you still have to check by
> hand](#what-you-still-have-to-check-by-hand). Treat every flag as "look at this,"
> not "this is wrong," and treat a clean run as "no *automated* issues found," not
> "done."

---

## Install

```bash
pip install bibtexparser requests
```

`requests` is only needed for the network cross-check; offline checks run without it.

## Run

```bash
# Offline checks + network cross-check (the default, recommended)
python bibaudit.py refs.bib

# Offline only (fast, no network)
python bibaudit.py refs.bib --source none

# Maximum cross-checking: query DBLP for every conference/journal entry (slow)
python bibaudit.py refs.bib --dblp-all
```

Output is a report on **stdout** grouped into `ERROR` / `WARN` / `INFO`; progress and
per-source call counts go to **stderr**, so you can pipe the report cleanly.

**Tip:** before auditing, run [clean_bibtex](https://github.com/SFRL/clean_bibtex) to get a
copy of your `.bib` that only contains the citations actually used in the paper.

If you use Crossref, pass `--mailto you@example.edu` so your requests go in Crossref's
faster, more reliable polite pool. Use a real address.

---

## What it checks

### Offline (no network, always run)
- **Required fields per entry type** — `@inproceedings` needs author, title, booktitle,
  publisher, address, DOI; `@article` needs author, title, journal, volume, DOI; both
  need page numbers *or* an article number (unless marked *In press* / *To appear*).
- **DOI format** — must be a bare `10.xxxx/...`, not a full `https://doi.org/...` URL.
- **Duplicate citation keys** and **duplicate DOIs**.
- **TVCG/VIS year parity** — for TVCG papers, volume and year must share even/odd parity;
  a mismatch usually means the year is the *presentation* year instead of the special-issue
  *publication* year (a common VIS citation error).
- **Venue / address consistency** — flags when the file *mixes* short (`Proc. CHI`) and long
  proceedings forms, and summarizes long-form publisher addresses (`New York, NY, USA` vs
  `New York`).
- **Month consistency** on `@article` — months should be used always or never.
- **Page vs. article-number style** — flags single-page values that are really article
  numbers, and mixed conventions across the file.
- **In-press entries** that still carry speculative page ranges.
- **arXiv / preprint entries** — flagged so you can confirm whether a peer-reviewed published
  version now exists and should be cited instead.
- **Title capitalization risk** — acronyms / CamelCase tokens not wrapped in `{}` that
  some BibTeX styles will down-case.

### Network cross-check (Crossref + DataCite + DBLP)
- **DOI resolves** on Crossref or DataCite (arXiv and other non-Crossref DOIs are checked on
  DataCite). A DOI missing from *both* registries is a real error.
- **Title / first-author surname / year** match the source's metadata.
- **Source-vs-source disagreement** when two sources are consulted — a strong hint that
  one database record is wrong.

### Suggestions (automatic)
Whenever a source is queried, bibaudit **suggests values for fields an entry is missing**
(DOI, pages/article number, publisher, address, volume, venue) and **flags fields that are
present but disagree** with the source. This is on by default and adds no extra network
calls — see [Suggestions](#suggestions).

---

## Useful flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--source {crossref,dblp,both,none}` | `both` | Which source(s) to consult. |
| `--dblp-all` | off | Query DBLP for *every* conference/journal entry (also extends suggestions to DOI'd papers). |
| `--mailto EMAIL` | (set me) | Crossref polite-pool contact address. |
| `--timeout SECS` | `15` | Per-request network timeout. |
| `--cache PATH` / `--no-cache` / `--clear-cache` | `.bibaudit_cache.json` | Cache control. |

Run `python bibaudit.py --help` for the complete list.

Network results are cached to `.bibaudit_cache.json` so re-runs skip unchanged entries.
Editing an entry's doi/title/author/year invalidates just that entry; use `--clear-cache`
to force fresh lookups (e.g. if a database has since fixed a record).

---

## Suggestions

Whenever a source is queried (i.e. not `--source none`), bibaudit does this automatically.
Two things happen:

**1. Missing fields** — for fields an entry lacks (DOI, pages / article number, publisher,
address, volume, venue), it reports the value found on the sources it already queried, plus
entry-type mismatches (e.g. a journal paper filed as `@inproceedings`). Each suggestion states
the relevant caveat, a `Check:` strategy, and a link to verify in one click:

```
[zong_rich_2022]
    - Crossref lists pages '15-27' — but that may be a placeholder range for a paper that
      isn't fully paginated. Check: Open https://doi.org/10.1111/cgf.14519 and confirm the
      paper is finalized before adding it; if it's still early-access, omit pages and mark
      'To appear' instead.
```

**2. Present-but-different fields** — for fields you *have* that disagree with the source (DOI,
volume, pages, venue), it flags the mismatch. This is the tell that a DOI resolves to a
slightly different record — a preprint instead of the published version, or simply the wrong
paper:

```
[hill_deixis_1991]
    - Pages differ: your entry has '253--253', Crossref has '253-259'. Often just formatting,
      but confirm it's the same span — and the same paper. See https://doi.org/...
```

Suggestions are **advisory only and never auto-applied** — everything is worded as "check,"
not "this is correct." You make the edit.

---

## Caveats and limitations

**Read this before trusting the output.**

- **No single source is ground truth — including the ones it queries.** Crossref, DBLP,
  IEEE Xplore, ACM DL, Google Scholar, Zotero all contain errors. bibaudit surfaces
  *disagreement*; resolving it is your job, ideally against the publisher's authoritative
  page or the PDF itself.

- **"Not found on DBLP" is usually fine.** DBLP only indexes computer science. Workshops,
  posters, non-CS venues, books, and preprints won't be there. It's reported as `INFO`, not
  an error. DBLP matching is deliberately conservative, so a genuine paper whose DBLP title
  differs a lot (heavy subtitle, rewording) may also be reported as "Not found."

- **The preprint flag is a heuristic and can't find the published version for you.** It only
  reminds you to check; it does not search for a peer-reviewed version, and it can't tell
  whether one exists. Sometimes the preprint is the right thing to cite (a poster or workshop
  talk with no proceedings DOI).

- **The title-capitalization check is a heuristic.** It flags acronyms/CamelCase that aren't
  brace-protected, but it cannot tell an acronym from an ordinary capitalized word, and it
  does not verify that your capitalization is *correct*. Expect false positives, and expect
  it to miss real capitalization errors.

- **It checks presence and format, not truth.** "Has a DOI in the right format" is not "has
  the *right* DOI." "Has a volume field" is not "has the *correct* volume." A page range
  that exists is not necessarily the page range of *this* paper.

- **`@incollection`, `@book`, `@misc` get only light checks.** Required-field validation
  targets `@inproceedings` and `@article`; other types are checked for DOIs/format but not
  for type-specific completeness.

- **A clean run means "no automated issues found," not "correct."** The checks below are
  not automated at all.

---

## What you still have to check by hand

These require human judgment or a source bibliography can't reliably provide, and bibaudit
**does not** verify them:

1. **Capitalization correctness** — proper nouns, product names, and acronyms in titles
   actually rendered correctly, and brace-protected where needed.
2. **Author name spelling and special characters** — diacritics (`Müller`, `Bañ`), hyphenation,
   particles (`van der`), and name order — in both the `.bib` *and* the main document.
   The surname check only tests presence, not exact spelling.
3. **TVCG / VIS publication year** — when a parity warning fires (or even when it doesn't),
   confirm the year against the *special-issue publication* year, not the conference year.
4. **In-press / to-appear status** — whether speculative page numbers from a digital library
   should be dropped and the entry marked "In press" / "To appear."
5. **Whether a preprint has a published version** — when an arXiv/preprint entry is flagged,
   check for a peer-reviewed version (search the title on the venue's site, DBLP, or Google
   Scholar) and cite that instead, with the proper venue, year, and DOI. Cite the preprint
   only if no published version exists.
6. **Article-number vs. page-number style** — pick one convention (e.g. `article no. 117, 12
   pages` or `117:1--117:12`) and apply it consistently; the tool flags mixing but won't
   choose for you.
7. **Venue/journal name consistency** — short proceedings forms (`Proc. CHI`) and ISO4
   journal abbreviations, applied uniformly.
8. **The right DOI / volume / pages for *this* paper** — confirm against the publisher's
   page, especially for papers cited from secondary sources.
9. **Entry type correctness** — e.g. a journal version cited as a conference paper, or vice
   versa.

When in doubt, open the publisher's official record (or the PDF) and copy the metadata from
there.

---

## Exit codes

- `0` — no `ERROR`-severity items (warnings/info may still be present).
- `1` — at least one `ERROR` was flagged.

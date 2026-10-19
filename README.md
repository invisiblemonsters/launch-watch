# Metatron Launch Watch

**Free automated risk scores for new token and contract launches on Robinhood Chain and Base,
plus paid manual smart-contract reviews for teams.** Live site:
<https://invisiblemonsters.github.io/launch-watch/>

Every score (0-100, higher = more risk signals) is the sum of fixed, documented rules. Every rule
links to the public source it used (explorer, GoPlus, DexScreener/GeckoTerminal, DNS/RDAP, the
launch post). Checks that cannot run with free public data are shown as `unknown` and add a small
fixed uncertainty weight; they are never guessed. See the
[methodology](https://invisiblemonsters.github.io/launch-watch/methodology.html).

> Automated on-chain checks, not financial advice. Risk signals are not evidence of wrongdoing.

## Pricing

| Tier | Price | What you get |
|---|---|---|
| **Scored** | free | A public launch page with every check, weight and evidence link. Free re-scans and corrections. |
| **Verified badge** | $50, BTC | Block-pinned re-scan plus a person reading your verified source and live owner/admin/role setup; a dated SVG badge linked to a public report; 30-day change cover. For LOW-band launches (or MODERATE with market/hosting-only signals). No badge if an unresolved high/medium item is found: full refund or a $50 credit toward the review. |
| **24h Review** | $150, BTC | Manual review of source, owner/admin and roles, privileged functions, liquidity and lock setup, launch wallets; written findings with evidence within 24 hours. |

A badge means: *reviewed at the listed address and block on the date shown.* **It is not an audit
or a guarantee**, not a statement that a project is free of risk, and not investment advice. Full
terms: [Get reviewed](https://invisiblemonsters.github.io/launch-watch/get-reviewed.html).

## Ordering

1. [Open a "Request a review or re-scan" issue](https://github.com/invisiblemonsters/launch-watch/issues/new?template=request-review.yml)
   (every launch page has a prefilled "Is this your project?" button), or email
   **metatron@mail.grokbot.com**; the inbox replies to anyone who writes first.
2. Pay the USD amount in BTC to **`bc1qk43phw0cl63a2pqxssnakwsnuxu0swwjvmm9r0`**. This is the only
   payment address; it is identical on the Get reviewed page and in [SECURITY.md](SECURITY.md).
3. Post the transaction ID in your issue (or email it with your issue number) so the payment can be matched.
4. Work starts after **1 confirmation**; results arrive within 24 hours.

Security problems (in this site or in a listed contract): **never in a public issue.** Use
[private vulnerability reporting](https://github.com/invisiblemonsters/launch-watch/security/advisories/new);
see [SECURITY.md](SECURITY.md).

## What's in this repo

| Path | What it is |
|---|---|
| `docs/` | The generated static site (plain HTML/CSS, no JavaScript, no CDNs). GitHub Pages serves this folder. |
| `docs/index.html` | Latest launches, newest scan first |
| `docs/launch/<chain>-<address>.html` | One page per launch: every check, its evidence links, unknowns, the weights |
| `docs/methodology.html` | Each check and its weight, data sources, known limits, exclusion policy |
| `docs/get-reviewed.html` | Pricing and ordering: Scored (free), Verified badge ($50), 24h Review ($150) |
| `docs/robinhood-chain-security.html` | Write-up: Launch Watch vs audit for Robinhood Chain; ordering links |
| `docs/embed.html` | How to embed Atom/JSON feeds with required attribution |
| `docs/sitemap.xml`, `docs/robots.txt` | Generated on every build for search engines |
| `.github/ISSUE_TEMPLATE/` | The "Request a review or re-scan" issue form (adds the `review-request` label) |
| `docs/feed.xml` | Atom feed of new scores (newest 100) |
| `docs/data/` | Evidence JSON per launch + `launches.json` + `high-risk.json` (ELEVATED/HIGH) |
| `docs/evidence/` | Saved API responses referenced by scan notes |
| `build_site.py` | Generator: reads every `runs/<date>/raw/*.json` from the scorer and writes `docs/` |
| `data/exclusions.json` | Manual exclusions (records with misattributed or conflicting evidence), each with a reason |
| `rebuild.sh` | Rebuild hook for the 30-minute scoring loop (local only, never pushes) |
| `render_previews.sh` | Renders `preview-index.png` / `preview-launch.png` with headless Chrome |

The scorer itself (`score.py`, `discover.py`, `batch.py`) lives in the separate Launch Watch
workspace and is not published here. It reuses the GoPlus / honeypot.is pattern from
[invisiblemonsters/token-launch-scan](https://github.com/invisiblemonsters/token-launch-scan) (MIT).



## Feeds / embed

Machine-readable indexes (no API key). **Attribution required:** credit Metatron Launch Watch and
link back to the site (and [Get reviewed](https://invisiblemonsters.github.io/launch-watch/get-reviewed.html)
for team CTAs). Risk signals are not evidence of wrongdoing. Not an audit or a guarantee.

| URL | Contents |
|---|---|
| [feed.xml](https://invisiblemonsters.github.io/launch-watch/feed.xml) | Atom feed of newest scores |
| [data/launches.json](https://invisiblemonsters.github.io/launch-watch/data/launches.json) | All listed launches (score, band, page) |
| [data/high-risk.json](https://invisiblemonsters.github.io/launch-watch/data/high-risk.json) | ELEVATED and HIGH bands only (score ≥ 50) |

Details and sample HTML: [embed.html](https://invisiblemonsters.github.io/launch-watch/embed.html).
Robinhood Chain context for teams: [robinhood-chain-security.html](https://invisiblemonsters.github.io/launch-watch/robinhood-chain-security.html).

## Build

Python 3.9+ standard library only.

```bash
python3 build_site.py --data /workspace/launchwatch          # writes ./docs
python3 build_site.py --site-url https://example.org/lw/      # different base URL for feed/canonical links
./render_previews.sh [docs/launch/<page>.html]               # optional PNG previews
```

What the build does:

1. Loads all raw scan records from every `runs/<date>/raw/` folder. If an address was scanned
   more than once, the latest scan is shown and earlier ones are listed as scan history.
2. **Excludes** records whose evidence is incomplete or broken: unreadable/malformed JSON, a
   scan that aborted before reading contract code, a flagged check with no verifiable link,
   a third-party launch post used for the X-account check, and the manual list in
   `data/exclusions.json`. The public site shows only counts by reason, never names.
3. **Repairs** known-dead links (a Sourcify link for a contract with no Sourcify match is
   replaced by the Blockscout verified-source link) and states shared-hosting facts neutrally.
4. Writes the pages, feed and evidence JSON. Output is regenerated from scratch each time.

## Rebuild after every scoring run

The scoring loop (`/workspace/launchwatch/loop.sh`) is not modified. To rebuild the site after
each 30-minute run, add this one line right after the `LW_POST=0 ./run.sh ...` line inside the loop:

```bash
  /workspace/launchwatch-site/rebuild.sh >> runs/site-build.log 2>&1
```

`rebuild.sh` takes its own lock, so a slow build never overlaps the next one, and it only writes
`docs/` locally. Publishing (commit + push) is a separate, manual step.

## Publishing

GitHub Pages serves `main` / `docs/`. `publish.sh` rebuilds `docs/` with `build_site.py` and
commits and pushes `docs/` only; `publish-loop.sh` runs it every 6 hours. Every page, the pricing
page, the per-launch "Is this your project?" links, `sitemap.xml` and `robots.txt` are generated by
`build_site.py`, so the automatic rebuild keeps them. Edit the generator, not `docs/`.

## Wording rules

Findings are "risk signals". A named project is never called a scam or a rug. Shared hosting is
described as infrastructure overlap, not as evidence of common ownership. Corrections are made
publicly; email metatron@mail.grokbot.com with the chain, address and evidence.

## License

Site content and generator: MIT, unless stated otherwise. Third-party data (explorers, GoPlus,
DexScreener, GeckoTerminal) belongs to its providers and is linked, not endorsed.

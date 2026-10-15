# Metatron Launch Watch

A free public feed of automated risk scores for new token and contract launches on
**Robinhood Chain** and **Base**: <https://invisiblemonsters.github.io/launch-watch/>

Every score (0-100, higher = more risk signals) is the sum of fixed, documented rules.
Every rule links to the public source it used (explorer, GoPlus, DexScreener/GeckoTerminal,
DNS/RDAP, the launch post). Checks that cannot run with free public data are shown as
`unknown` and add a small fixed uncertainty weight; they are never guessed.

> Automated on-chain checks, not financial advice. Risk signals are not evidence of wrongdoing.
> Teams can request a re-scan or correction at metatron@mail.grokbot.com.

## What's in this repo

| Path | What it is |
|---|---|
| `docs/` | The generated static site (plain HTML/CSS, no JavaScript, no CDNs). GitHub Pages serves this folder. |
| `docs/index.html` | Latest launches, newest scan first |
| `docs/launch/<chain>-<address>.html` | One page per launch: every check, its evidence links, unknowns, the weights |
| `docs/methodology.html` | Each check and its weight, data sources, known limits, exclusion policy |
| `docs/get-reviewed.html` | Free scan / paid manual review / badge |
| `docs/feed.xml` | Atom feed of new scores (newest 100) |
| `docs/data/` | Published evidence JSON per launch + `launches.json` index |
| `docs/evidence/` | Saved API responses referenced by scan notes |
| `build_site.py` | Generator: reads every `runs/<date>/raw/*.json` from the scorer and writes `docs/` |
| `data/exclusions.json` | Manual exclusions (records with misattributed or conflicting evidence), each with a reason |
| `rebuild.sh` | Rebuild hook for the 30-minute scoring loop (local only, never pushes) |
| `render_previews.sh` | Renders `preview-index.png` / `preview-launch.png` with headless Chrome |

The scorer itself (`score.py`, `discover.py`, `batch.py`) lives in the separate Launch Watch
workspace and is not published here. It reuses the GoPlus / honeypot.is pattern from
[invisiblemonsters/token-launch-scan](https://github.com/invisiblemonsters/token-launch-scan) (MIT).

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

## Publish on GitHub Pages (not done yet)

1. Create a public repo **`invisiblemonsters/launch-watch`**.
2. From this folder:
   ```bash
   git init -b main && git add . && git commit -m "Launch Watch site"
   git remote add origin git@github.com:invisiblemonsters/launch-watch.git
   git push -u origin main
   ```
3. Repo **Settings → Pages → Build and deployment**: Source **Deploy from a branch**,
   Branch **`main`**, folder **`/docs`**, Save.
4. The site appears at <https://invisiblemonsters.github.io/launch-watch/> within a few minutes
   (`docs/.nojekyll` disables Jekyll processing).
5. To keep it current, commit and push `docs/` after rebuilds (manually or with a scheduled job
   you approve separately).

## Wording rules

Findings are "risk signals". A named project is never called a scam or a rug. Shared hosting is
described as infrastructure overlap, not as evidence of common ownership. Corrections are made
publicly; email metatron@mail.grokbot.com with the chain, address and evidence.

## License

Site content and generator: MIT, unless stated otherwise. Third-party data (explorers, GoPlus,
DexScreener, GeckoTerminal) belongs to its providers and is linked, not endorsed.

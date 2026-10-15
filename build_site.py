#!/usr/bin/env python3
"""Metatron Launch Watch - static site generator.

Reads every scored launch in <data>/runs/*/raw/*.json (written by batch.py/score.py),
drops records whose evidence is incomplete or broken, and writes plain HTML/CSS
(no JavaScript, no external CDNs) plus an Atom feed into ./docs for GitHub Pages.

Usage:
  python3 build_site.py [--data /workspace/launchwatch] [--out docs] [--site-url URL]
Env: LW_DATA, LW_SITE_URL override the defaults.
Only the Python standard library is needed (score.py is imported for CHAINS/WEIGHTS
only, and needs pycryptodome; if that import fails, a bundled copy of the constants is used).
"""
import argparse, datetime as dt, glob, html, json, os, re, shutil, sys
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
PT = ZoneInfo("America/Los_Angeles")
CONTACT = "metatron@mail.grokbot.com"
SCAN_REPO = "https://github.com/invisiblemonsters/token-launch-scan"
FOOTER = ("Automated on-chain checks, not financial advice. Risk signals are not evidence of wrongdoing. "
          "Teams can request a re-scan or correction at metatron@mail.grokbot.com.")
ADDR_RX = re.compile(r"^0x[0-9a-fA-F]{40}$")

# --------------------------------------------------------------------------- scorer constants
def load_scorer(data_dir):
    sys.path.insert(0, data_dir)
    try:
        import score  # noqa
        return score.CHAINS, score.WEIGHTS, score.UNKNOWN_POINTS, score.OWNER_RENOUNCED_FACTOR, score.VERSION
    except Exception as e:  # fallback copy of score.py v0.1.0 constants
        print("note: could not import score.py (%s); using bundled constants" % e, file=sys.stderr)
        CH = {"robinhood": {"id": 4663, "name": "Robinhood Chain", "explorer": "https://robinhoodchain.blockscout.com",
                            "dexscreener": "robinhood", "gecko": "robinhood", "goplus": "4663"},
              "base": {"id": 8453, "name": "Base", "explorer": "https://base.blockscout.com",
                       "dexscreener": "base", "gecko": "base", "goplus": "8453"}}
        W = {"not_contract": 40, "unverified": 15, "unverified_template_match": 4, "upgradeable_proxy": 15, "proxy_admin_eoa": 5,
             "owner_eoa": 8, "owner_contract": 3, "fn_mint": 12, "fn_blacklist": 12, "fn_pause": 8, "fn_fee_setter": 8,
             "fn_withdraw": 5, "fn_upgrade": 10, "fn_trading_switch": 6, "honeypot": 35, "high_tax": 10, "goplus_flag": 8,
             "same_creator_honeypot": 15, "low_liquidity": 10, "very_low_liquidity": 15, "no_pool": 6, "lp_unlocked": 12,
             "launcher_holds_supply": 10, "holder_concentration": 10, "fresh_deployer": 8, "serial_deployer": 8,
             "young_deployer": 6, "hosting_cluster": 25, "hosting_cluster_subnet": 8, "young_domain": 5, "young_x_account": 4}
        return CH, W, 3, 0.5, "0.1.0"

# What each weight means (methodology page). Order = display order.
CHECK_DOCS = [
    ("Contract", [
        ("not_contract", "No contract code", "eth_getCode returns empty: the address is a wallet or the contract is not deployed on this chain."),
        ("unverified", "Source not verified", "Neither Sourcify nor Blockscout has verified source code, so the logic can only be read from bytecode."),
        ("unverified_template_match", "Unverified, but matches a verified template", "Not verified itself, but the runtime bytecode is the same length and at least 97% byte-identical to a verified reference contract (for example the Pons launchpad token). Differences are constructor values such as name and symbol."),
        ("upgradeable_proxy", "Upgradeable proxy", "An EIP-1967 implementation or beacon slot is set (or Sourcify reports a proxy), so the logic can be replaced after launch. EIP-1167 clones are not upgradeable and are not flagged."),
        ("proxy_admin_eoa", "Proxy admin is a single wallet", "The EIP-1967 admin slot points at an address with no code: one key controls upgrades."),
    ]),
    ("Owner and privileged functions", [
        ("owner_eoa", "Owner is a single wallet", "owner()/getOwner() returns an address with no code, so one key controls every owner-only function."),
        ("owner_contract", "Owner is a contract", "owner() is a contract (multisig, timelock or factory). Not classified further, so it carries a small weight."),
        ("fn_mint", "Mint function", "A mint function exists (exact selector in bytecode, or a name match in the verified ABI). New supply can be created."),
        ("fn_blacklist", "Blacklist / bot-block function", "A function that can block specific addresses from transferring."),
        ("fn_pause", "Pause function", "Transfers can be paused."),
        ("fn_fee_setter", "Fee / tax / limit setter", "Buy/sell fees or max-transaction/wallet limits can be changed after launch."),
        ("fn_trading_switch", "Trading on/off switch", "Trading can be enabled or disabled by a privileged caller."),
        ("fn_withdraw", "Withdraw / rescue function", "Tokens or ETH held by the contract can be withdrawn by a privileged caller."),
        ("fn_upgrade", "Upgrade function", "upgradeTo/upgradeToAndCall is present."),
    ]),
    ("Trading (GoPlus, honeypot.is)", [
        ("honeypot", "Sell blocked in simulation", "GoPlus is_honeypot=1, or the honeypot.is buy/sell simulation fails to sell (Base only)."),
        ("high_tax", "Buy or sell tax above 10%", "Reported by GoPlus or measured by the honeypot.is simulation."),
        ("goplus_flag", "GoPlus contract flags", "Any of: cannot_sell_all, transfer_pausable, hidden_owner, can_take_back_ownership, owner_change_balance, selfdestruct, slippage_modifiable, is_blacklisted, is_mintable."),
        ("same_creator_honeypot", "Creator history flag", "GoPlus honeypot_with_same_creator=1."),
    ]),
    ("Liquidity", [
        ("very_low_liquidity", "Top pool under $5k", "Largest DexScreener/GeckoTerminal pool holds less than $5,000. If the two sources disagree by more than 5x the check is marked unknown instead."),
        ("low_liquidity", "Top pool under $25k", "Largest pool holds $5,000-$25,000."),
        ("no_pool", "No pool listed", "Neither DexScreener nor GeckoTerminal lists a pool at scan time."),
        ("lp_unlocked", "LP not locked or burned", "Less than 90% of LP tokens are locked or burned (GoPlus LP holders, or the burned share of a V2 pair). Concentrated-liquidity and bonding-curve pools cannot be read from free sources and are marked unknown."),
    ]),
    ("Holders", [
        ("launcher_holds_supply", "Launch wallet holds 5%+", "The wallet that sent the launch transaction (or deployed the contract) still holds 5% or more of supply."),
        ("holder_concentration", "One wallet holds 10%+", "The largest non-contract holder (pools, vaults and curves excluded; EIP-7702 delegated wallets kept) holds 10% or more of supply."),
    ]),
    ("Deployer", [
        ("fresh_deployer", "Fresh deployer wallet", "The launch wallet has sent 5 or fewer transactions in total."),
        ("serial_deployer", "Serial deployer", "The launch wallet directly created 10 or more live contracts, or has sent more than 400 transactions."),
        ("young_deployer", "Young deployer wallet", "The launch wallet's oldest transaction is less than 7 days before the scan."),
    ]),
    ("Site and social", [
        ("hosting_cluster", "Shared server with watchlist sites", "The project website resolves (Google DNS-over-HTTPS) to the same IP as a server on the hosting watchlist. Shared hosting is common and is a risk signal only; it is not evidence that the projects share owners or of any wrongdoing. Shared CDN/PaaS ranges (Cloudflare, Vercel, Netlify, GitHub Pages, Fastly) are never counted."),
        ("hosting_cluster_subnet", "Same /24 network as a watchlist server", "The site's IP is in the same /24 block as a watchlist server (outside shared CDN ranges)."),
        ("young_domain", "Domain under 14 days old", "RDAP registration date is less than 14 days before the scan. Subdomains of site builders (lovable.app, vercel.app, ...) are not scored."),
        ("young_x_account", "X account under 3 days old", "The X account behind the launch post was created less than 3 days before the scan."),
    ]),
]

BAND_CLASS = {"LOW": "low", "MODERATE": "mod", "ELEVATED": "elev", "HIGH": "high"}

def esc(s): return html.escape(str(s if s is not None else ""), quote=True)

def fmt_pt(ts_or_iso, with_date=True):
    if ts_or_iso is None: return "unknown"
    if isinstance(ts_or_iso, (int, float)): d = dt.datetime.fromtimestamp(ts_or_iso, dt.timezone.utc)
    else: d = dt.datetime.fromisoformat(str(ts_or_iso).replace("Z", "+00:00"))
    d = d.astimezone(PT)
    return d.strftime("%b %d, %Y %-I:%M %p PT" if with_date else "%-I:%M %p PT")

def iso_utc(s): return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))

def short(a): return a[:6] + "…" + a[-4:]

def slug(r): return f"{r['chain']}-{r['address'].lower()}"

# --------------------------------------------------------------------------- load + validate
def load_records(data_dir, CHAINS):
    out = []
    for f in sorted(glob.glob(os.path.join(data_dir, "runs", "*", "raw", "*.json"))):
        day = f.split(os.sep)[-3]
        try:
            r = json.load(open(f))
        except Exception as e:
            out.append({"_file": f, "_day": day, "_broken": f"unreadable JSON ({e.__class__.__name__})"}); continue
        r["_file"], r["_day"] = f, day
        out.append(r)
    return out

def auto_problems(r, CHAINS):
    """Return a reason string if a record's evidence is incomplete/broken, else None."""
    if r.get("_broken"): return r["_broken"]
    for k in ("chain", "address", "score", "band", "checks", "scanned_at_utc", "coverage"):
        if k not in r: return f"missing field '{k}'"
    if r["chain"] not in CHAINS: return f"unknown chain '{r['chain']}'"
    if not ADDR_RX.match(r["address"]): return "malformed address"
    if not r["checks"]: return "no checks recorded"
    ce = [c for c in r["checks"] if c["id"] == "code.exists"]
    if not ce or ce[0]["status"] == "unknown": return "scan aborted (contract code could not be read)"
    for c in r["checks"]:
        if c["status"] == "flag" and not any(str(e).startswith("http") for e in c.get("evidence", [])):
            return f"flagged check '{c['title']}' has no verifiable evidence link"
    pn = str((r.get("target") or {}).get("post_note") or "").lower()
    if "third-party" in pn and any(c["id"] == "social.x_age" for c in r["checks"]):
        return "launch post is marked third-party, but the X account age check was run on it"
    try: iso_utc(r["scanned_at_utc"])
    except Exception: return "unreadable scan timestamp"
    if not r.get("label") and not (r.get("token") or {}).get("name"): return "no name/label"
    return None

def repair(r, CHAINS, clusters):
    """Fix known-broken evidence links and neutralise wording. Returns list of edits."""
    c = CHAINS[r["chain"]]; edits = []
    sfy_ok = (r.get("sourcify") or {}).get("match") in ("match", "exact_match")
    bs_ok = (r.get("blockscout") or {}).get("status") == "verified"
    contract_tab = f"{c['explorer']}/address/{r['address']}?tab=contract"
    for ch in r["checks"]:
        ev = []
        for e in ch.get("evidence", []):
            if not sfy_ok and "repo.sourcify.dev" in str(e):
                edits.append(f"{ch['id']}: dropped Sourcify link (contract has no Sourcify match)")
                if bs_ok and contract_tab not in ev: ev.append(contract_tab); edits.append(f"{ch['id']}: linked Blockscout verified source instead")
                continue
            if e not in ev: ev.append(e)
        ch["evidence"] = ev
        if ch["id"] == "host.ip" and ch["status"] == "flag" and "watchlist cluster" in ch["detail"]:
            host = (re.match(r"^(\S+) resolves to", ch["detail"]) or [None, "the site"])[1]
            ips = ", ".join(r.get("site_ips") or [])
            cl = next((x for x in clusters.get("clusters", []) if set(x["ips"]) & set(r.get("site_ips") or [])), None)
            if cl:
                others = [m["project"] for m in cl["members"] if m["domain"].lower() != str(host).lower()]
                ch["detail"] = (f"At scan time {host} resolved to {ips}, a single server (Hostinger, AS47583). "
                                f"{len(others)} other project sites on the Launch Watch hosting watchlist resolved to the same IP: {', '.join(others)}. "
                                "Shared hosting is common; this is an infrastructure overlap, not evidence of common ownership or wrongdoing.")
                edits.append("host.ip: neutral hosting wording")
        ch["detail"] = re.sub(r"\s*Seed: [^.]*\(\w+\), \d{4}-\d{2}-\d{2}\.", "", ch["detail"])
    for tr in r.get("top_reasons", []):  # keep top_reasons consistent with edited checks
        m = next((x for x in r["checks"] if x["id"] == tr["id"]), None)
        if m: tr.update(detail=m["detail"], evidence=m["evidence"])
    return edits

# --------------------------------------------------------------------------- HTML
def page(title, body, depth=0, desc=None, canonical=None):
    up = "../" * depth
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc or 'Free public feed of automated risk scores for new token and contract launches on Robinhood Chain and Base.')}">
{f'<link rel="canonical" href="{esc(canonical)}">' if canonical else ''}
<link rel="stylesheet" href="{up}style.css">
<link rel="alternate" type="application/atom+xml" title="Metatron Launch Watch: new scores" href="{up}feed.xml">
</head>
<body>
<header class="top"><div class="wrap">
<a class="brand" href="{up}index.html">Metatron <span>Launch Watch</span></a>
<nav><a href="{up}index.html">Launches</a><a href="{up}methodology.html">Methodology</a><a href="{up}get-reviewed.html">Get reviewed</a><a href="{up}feed.xml">Feed</a></nav>
</div></header>
<main class="wrap">
{body}
</main>
<footer class="foot"><div class="wrap">
<p>{esc(FOOTER).replace(esc(CONTACT), f'<a href="mailto:{CONTACT}">{CONTACT}</a>')}</p>
</div></footer>
</body>
</html>
"""

def band_pill(score, band):
    return f'<span class="pill {BAND_CLASS.get(band, "mod")}">{esc(score)} <small>{esc(band)}</small></span>'

def ev_html(e, CH=None):
    e = str(e)
    if e.startswith("http"):
        return f'<a href="{esc(e)}" rel="nofollow noopener" title="{esc(e)}">{esc(ev_label(e))}</a>'
    return f"<code>{esc(e)}</code>"

def linkify(text, depth):
    """Escape text, link URLs and local evidence paths."""
    t = esc(text)
    t = re.sub(r"runs/(\d{4}-\d{2}-\d{2})/evidence/([\w.\-]+)",
               lambda m: f'<a href="{"../" * depth}evidence/{m[1]}/{m[2]}">saved response ({m[2]})</a>', t)
    t = re.sub(r"(?<![\"=])(https?://[^\s<)]+)", lambda m: f'<a href="{m[1]}" rel="nofollow noopener">{m[1]}</a>', t)
    return t

def reason_label(ch):
    """Short, signal-phrased label for a check that added points (index, feed, top signal)."""
    i, st, d = ch["id"], ch["status"], ch.get("detail", "")
    if st == "unknown":
        return {"trade.sim": "Sell simulation unavailable", "trade.goplus": "No GoPlus data yet", "liq.lock": "LP lock status unknown",
                "liq.pool": "Liquidity figure unknown", "host.ip": "No project website known", "holders.concentration": "Holder data unavailable",
                "deployer.age": "Deployer age unknown", "deployer.fresh": "Deployer not identified", "host.domain_age": "Domain age unknown"
                }.get(i, ch["title"] + " (unknown)")
    if i == "verify.source": return "Unverified, matches verified template" if "template" in d else "Source not verified"
    if i == "liq.pool": return "No DEX pool listed" if d.startswith("Neither") else "Low DEX liquidity"
    if i == "owner.owner": return "Owner is a contract" if "a contract" in d else "Owner is a single wallet"
    return {"liq.lock": "LP not locked or burned", "holders.concentration": "Holder concentration", "holders.launcher": "Launch wallet holds 5%+",
            "deployer.fresh": "Fresh deployer wallet", "deployer.age": "Young deployer wallet", "deployer.contracts": "Serial deployer",
            "host.ip": "Shared hosting overlap", "host.domain_age": "Young domain", "social.x_age": "Young X account",
            "proxy.type": "Upgradeable proxy", "proxy.admin": "Proxy admin is a single wallet", "trade.goplus": "GoPlus flags",
            "trade.sim": "Sell simulation flag", "trade.honeypot": "GoPlus sell-block flag", "code.exists": "No contract code"
            }.get(i, ch["title"])

EV_LABELS = [(r"blockscout\.com/api/", "Blockscout API"), (r"blockscout\.com/address/(0x[0-9a-fA-F]{40})\?tab=contract", "explorer: source code"),
             (r"blockscout\.com/address/(0x[0-9a-fA-F]{40})", "explorer: address {s}"), (r"blockscout\.com/tx/(0x[0-9a-fA-F]+)", "explorer: tx {s}"),
             (r"blockscout\.com/token/", "explorer: token holders"), (r"blockscout\.com/block/(\d+)", "explorer: block {0}"),
             (r"gopluslabs\.io", "GoPlus API"), (r"api\.dexscreener\.com", "DexScreener API"), (r"dexscreener\.com/", "DexScreener pool"),
             (r"api\.geckoterminal\.com", "GeckoTerminal API"), (r"geckoterminal\.com/", "GeckoTerminal pool"), (r"dns\.google", "DNS lookup (Google DoH)"),
             (r"rdap\.org", "RDAP domain record"), (r"ipinfo\.io", "ipinfo (IP owner)"), (r"(x|twitter)\.com/", "X post"),
             (r"sourcify\.dev", "Sourcify"), (r"honeypot\.is", "honeypot.is simulation")]
def ev_label(u):
    for rx, lab in EV_LABELS:
        m = re.search(rx, u)
        if m:
            g = m.group(1) if m.groups() else ""
            return lab.replace("{s}", short(g) if g.startswith("0x") and len(g) >= 42 else (g[:10] + "…" if g.startswith("0x") else g)).replace("{0}", g)
    return u.split("//", 1)[1].split("/", 1)[0]

STATUS_TXT = {"flag": "risk signal", "pass": "pass", "info": "info", "unknown": "unknown"}

def launch_name(r):
    return r.get("label") or (r.get("token") or {}).get("name") or "Unnamed contract"

def is_older(r):
    ts = (r.get("creation") or {}).get("ts")
    return bool(ts) and (iso_utc(r["scanned_at_utc"]).timestamp() - ts) > 48 * 3600

def weights_table(W, UP, RF):
    rows = "".join(f"<tr><td><code>{esc(k)}</code></td><td class=num>{v}</td></tr>" for k, v in W.items())
    return (f'<table class="weights"><thead><tr><th>Rule</th><th class=num>Points when flagged</th></tr></thead><tbody>{rows}'
            f'<tr><td>any check <code>unknown</code></td><td class=num>{UP}</td></tr>'
            f'<tr><td>privileged-function points when owner() is 0x0/dead</td><td class=num>x{RF}</td></tr></tbody></table>')

def render_launch(r, ctx, history):
    CH, W, UP, RF = ctx["CHAINS"], ctx["WEIGHTS"], ctx["UNKNOWN"], ctx["RF"]
    c = CH[r["chain"]]; a = r["address"]; al = a.lower(); t = r.get("target") or {}; tok = r.get("token") or {}
    name = launch_name(r); cr = r.get("creation") or {}
    is_token = "totalSupply" in tok
    links = [("Explorer (Blockscout)", f"{c['explorer']}/address/{a}")]
    if is_token:
        links += [("GoPlus token security", f"https://api.gopluslabs.io/api/v1/token_security/{c['goplus']}?contract_addresses={al}"),
                  ("DexScreener", f"https://dexscreener.com/{c['dexscreener']}/{al}"),
                  ("GeckoTerminal", f"https://www.geckoterminal.com/{c['gecko']}/tokens/{al}")]
    if cr.get("tx"): links.append(("Creation transaction", f"{c['explorer']}/tx/{cr['tx']}"))
    if r.get("pairs"):
        p = r["pairs"][0]
        if p.get("url"): links.append((f"Top pool ({p.get('dex')})", p["url"]))
    post = r.get("post")
    post_label = "Launch post" + (f" ({t['post_note']})" if t.get("post_note") else "")
    li = "".join(f'<li><span>{esc(k)}</span> <a href="{esc(u)}" rel="nofollow noopener">{esc(u.split("//")[1][:70])}</a></li>' for k, u in links)
    li += (f'<li><span>{esc(post_label)}</span> <a href="{esc(post)}" rel="nofollow noopener">{esc(post.split("//")[1][:70])}</a></li>' if post
           else '<li><span>Launch post</span> <em>none found</em></li>')
    if not is_token: li += '<li><span>GoPlus / DexScreener / GeckoTerminal</span> <em>not applicable: this is not a token contract</em></li>'
    li += f'<li><span>Raw evidence (JSON)</span> <a href="../data/{slug(r)}.json">{slug(r)}.json</a></li>'

    rows = []
    for ch in r["checks"]:
        st = ch["status"]; ev = " ".join(ev_html(e) for e in ch.get("evidence", [])) or (
            '<em>no external link: this check could not run</em>' if st == "unknown" else '<em>-</em>')
        note = ' <span class="why">Could not run, so a fixed uncertainty weight is added. Not a guess.</span>' if st == "unknown" else ""
        rows.append(f'<tr class="st-{st}"><td><span class="st {st}">{STATUS_TXT[st]}</span></td><td class=num>{"+%g" % ch["points"] if ch["points"] else "0"}</td>'
                    f'<td><strong>{esc(ch["title"])}</strong><br><span class="detail">{esc(ch["detail"])}</span>{note}</td><td class="ev">{ev}</td></tr>')
    pts_sum = sum(ch["points"] for ch in r["checks"])
    applied = [ch for ch in r["checks"] if ch["points"]]
    applied_rows = "".join(f'<tr><td>{esc(ch["title"])} <small>({STATUS_TXT[ch["status"]]})</small></td><td class=num>+{ch["points"]:g}</td></tr>' for ch in applied)
    note = t.get("note")
    hist = ""
    if len(history) > 1:
        hist = "<h2>Scan history</h2><ul>" + "".join(f'<li>{fmt_pt(h["scanned_at_utc"])}: {band_pill(h["score"], h["band"])} (run {esc(h["_day"])}, block {esc(h.get("block"))})</li>' for h in history) + "</ul>"
    tr0 = (r.get("top_reasons") or [None])[0]
    body = f"""
<p class="crumbs"><a href="../index.html">Launches</a> / {esc(name)}</p>
<div class="hero">
  <div>
    <h1>{esc(name)}{' <span class="tag">older contract</span>' if is_older(r) else ''}</h1>
    <p class="meta">{esc(c['name'])} · <code class="addr">{esc(a)}</code></p>
    <p class="meta">{'Token: ' + esc(tok.get('name')) + ' (' + esc(tok.get('symbol')) + ')' if tok else 'Non-token contract'} · Created {fmt_pt(cr.get('ts'))} · Scanned {fmt_pt(r['scanned_at_utc'])} at block {esc(r.get('block'))}</p>
  </div>
  <div class="scorebox {BAND_CLASS.get(r['band'], 'mod')}"><div class="big">{r['score']}</div><div>{esc(r['band'])} risk signals</div><div class="small">{r['coverage']['run']} checks ran · {r['coverage']['unknown']} unknown</div></div>
</div>
{f'<p class="lead"><strong>Top signal:</strong> {esc(reason_label(tr0))} (+{tr0["points"]:g}). {esc(tr0["detail"])}</p>' if tr0 else '<p class="lead">No check added points.</p>'}
{f'<div class="note"><strong>Context note from the scan:</strong> {linkify(note, 1)}</div>' if note else ''}
<p class="small">Name and symbol are as reported by the contract itself; anyone can deploy a token with any name. This page is about the contract at the address above only, not about any other project with a similar name.</p>
<h2>Evidence links</h2>
<ul class="links">{li}</ul>
<h2>Every check</h2>
<p class="small">Score is 0-100; higher means more risk signals. "unknown" means the check could not run with free public data at scan time; it adds {UP} points so missing data never looks safe. Each row links to the source used.</p>
<div class="tablewrap"><table class="checks"><thead><tr><th>Status</th><th class=num>Points</th><th>Check</th><th>Evidence</th></tr></thead><tbody>
{''.join(rows)}
</tbody></table></div>
<h2>How this score adds up</h2>
<table class="weights"><tbody>{applied_rows}<tr class="total"><td>Total (capped at 100)</td><td class=num>{min(100, round(pts_sum))}</td></tr></tbody></table>
<details><summary>All scoring weights (scorer v{esc(r.get('version'))})</summary>{weights_table(W, UP, RF)}<p class="small">See the <a href="../methodology.html">methodology</a> for what each rule means.</p></details>
{hist}
<div class="note"><strong>Is this your project?</strong> On-chain state changes after a scan. Ask for a re-scan or correction at <a href="mailto:{CONTACT}?subject=Re-scan%20{esc(al)}">{CONTACT}</a>, or see <a href="../get-reviewed.html">Get reviewed</a>.</div>
"""
    return page(f"{name} ({c['name']}) - {r['score']}/100 {r['band']} | Metatron Launch Watch", body, depth=1,
                desc=f"Automated risk signals for {name} on {c['name']}: {r['score']}/100 ({r['band']}).",
                canonical=f"{ctx['SITE']}launch/{slug(r)}.html")

def render_index(recs, ctx, stats):
    CH = ctx["CHAINS"]
    rows = []
    for r in recs:
        c = CH[r["chain"]]; tr0 = (r.get("top_reasons") or [None])[0]
        reason = f'{esc(reason_label(tr0))} <small>(+{tr0["points"]:g})</small>' if tr0 else "<em>no points added</em>"
        rows.append(f'<tr><td><a href="launch/{slug(r)}.html">{esc(launch_name(r))}</a>{" <span class=tag>older</span>" if is_older(r) else ""}</td>'
                    f'<td>{esc(c["name"])}</td><td><a href="{esc(c["explorer"])}/address/{esc(r["address"])}" rel="nofollow noopener"><code>{esc(short(r["address"]))}</code></a></td>'
                    f'<td>{band_pill(r["score"], r["band"])}</td><td>{reason}</td><td class="nowrap">{fmt_pt(r["scanned_at_utc"])}</td></tr>')
    body = f"""
<section class="intro">
<h1>New launches, scored automatically</h1>
<p>Free public feed of automated risk scores for new token and contract launches on <strong>Robinhood Chain</strong> and <strong>Base</strong>. Every score is the sum of fixed rules, every rule links to its public source, and checks that could not run are shown as <em>unknown</em>. Higher score = more risk signals. <a href="methodology.html">How scoring works</a>.</p>
<p class="legend"><span class="pill low">0-24 <small>LOW</small></span> <span class="pill mod">25-49 <small>MODERATE</small></span> <span class="pill elev">50-74 <small>ELEVATED</small></span> <span class="pill high">75-100 <small>HIGH</small></span></p>
<p class="small">{stats['included']} launches listed · {stats['chains']} · last updated {fmt_pt(stats['built'])} · scans run about every 30 minutes · <a href="feed.xml">Atom feed</a></p>
</section>
<div class="tablewrap"><table class="launches">
<thead><tr><th>Name</th><th>Chain</th><th>Address</th><th>Score</th><th>Top reason</th><th>Scanned (PT)</th></tr></thead>
<tbody>
{''.join(rows)}
</tbody></table></div>
<p class="small">Sorted newest scan first. "older" = contract created more than 48h before the scan (promoted during the window, not newly deployed). Records whose evidence was incomplete or broken are not listed ({stats['excluded']} so far); see the <a href="methodology.html#exclusions">methodology</a>.</p>
"""
    return page("Metatron Launch Watch: risk scores for new launches on Robinhood Chain and Base", body, 0, canonical=ctx["SITE"])

def render_methodology(ctx, stats):
    W, UP, RF = ctx["WEIGHTS"], ctx["UNKNOWN"], ctx["RF"]
    sec = []
    documented = set()
    for grp, items in CHECK_DOCS:
        rows = ""
        for k, title, txt in items:
            if k not in W: continue
            documented.add(k)
            rows += f'<tr><td><strong>{esc(title)}</strong><br><code>{esc(k)}</code></td><td class=num>+{W[k]}</td><td>{esc(txt)}</td></tr>'
        sec.append(f'<h3>{esc(grp)}</h3><table class="checks"><thead><tr><th>Rule</th><th class=num>Points</th><th>What it means</th></tr></thead><tbody>{rows}</tbody></table>')
    extra = [k for k in W if k not in documented]
    if extra:
        sec.append("<h3>Other rules</h3><ul>" + "".join(f"<li><code>{esc(k)}</code>: +{W[k]}</li>" for k in extra) + "</ul>")
    reasons = "".join(f"<li>{esc(k)}: {v}</li>" for k, v in sorted(stats["excl_reasons"].items())) or "<li>none</li>"
    body = f"""
<h1>Methodology</h1>
<p class="lead">Each launch gets a deterministic 0-100 score. Higher means more <strong>risk signals</strong>. A risk signal is a measurable property that has been common in failed or abusive launches (for example, one wallet controlling owner functions). It is not a finding about any team's intent, and many legitimate projects carry several.</p>
<h2>How the score is built</h2>
<ul>
<li>Every check returns <strong>risk signal</strong> (adds its fixed weight), <strong>pass</strong> or <strong>info</strong> (adds 0), or <strong>unknown</strong> (adds {UP}).</li>
<li><strong>Unknown is honest:</strong> when a check cannot run with free public data (for example there is no free sell simulator for Robinhood Chain), it is shown as unknown and adds a small, fixed uncertainty weight so missing data never looks safe. It is never guessed.</li>
<li>Privileged-function points are multiplied by {RF} when owner() is provably 0x0 or dead (renounced), because other roles may still call them.</li>
<li>The total is capped at 100. Bands: 0-24 LOW, 25-49 MODERATE, 50-74 ELEVATED, 75-100 HIGH.</li>
<li>On-chain reads are pinned to one block per scan. DexScreener, GeckoTerminal and GoPlus are live APIs; their responses are saved in each launch's raw JSON so any score can be re-derived.</li>
</ul>
<h2>Checks and weights</h2>
{''.join(sec)}
<h2>Data sources</h2>
<p>Public, keyless sources only: chain RPC (Robinhood Chain 4663, Base 8453), Sourcify, Blockscout, GoPlus, honeypot.is (Base), DexScreener, GeckoTerminal, Google DNS-over-HTTPS and RDAP. Launch posts come from public X search. The GoPlus and honeypot.is query pattern is reused from the open-source <a href="{SCAN_REPO}">token-launch-scan</a>.</p>
<h2>Known limits</h2>
<ul>
<li>No free sell simulation exists for Robinhood Chain, so that check is always unknown there.</li>
<li>LP lock status cannot be read for bonding-curve (Pons) pools or Uniswap v3/v4 positions; it is computed for V2 pairs and taken from GoPlus where available.</li>
<li>The bytecode-only function scan matches {64} exact common signatures; renamed functions can be missed. Access control on those functions is not analysed.</li>
<li>Deployer contract counts include direct deployments only, not factory launches (Pons, Clanker, ape.store).</li>
<li>Hosting overlap is only as good as the watchlist. Shared CDN and PaaS ranges are excluded on purpose.</li>
<li>Scores are a snapshot. Owners can change settings, add or remove liquidity at any time after a scan.</li>
</ul>
<h2 id="exclusions">What is not listed</h2>
<p>A scored launch is left off this site when its evidence is incomplete or broken: a flagged check without a verifiable link, a scan that aborted, a malformed record, or a launch post that turned out to belong to a third party or to point at a different contract. These are not judgments about the projects. Excluded so far: {stats['excluded']}.</p>
<ul>{reasons}</ul>
<p>Known-dead evidence links are repaired before publishing (for example, a Sourcify link for a contract that is only verified on Blockscout is replaced by the Blockscout source link). Hosting facts are stated as infrastructure overlap only.</p>
<h2>Corrections</h2>
<p>If a team shows that a signal is wrong (for example a multisig owner misclassified), it is corrected publicly. Email <a href="mailto:{CONTACT}">{CONTACT}</a> with the chain, address and the evidence.</p>
<h2>All weights (scorer v{esc(ctx['VERSION'])})</h2>
{weights_table(W, UP, RF)}
"""
    return page("Methodology | Metatron Launch Watch", body, 0, desc="How Metatron Launch Watch scores new token launches: every check and its weight.")

def render_reviewed(ctx):
    body = f"""
<h1>Get reviewed</h1>
<p class="lead">Launching on Robinhood Chain or Base? Get your contract checked before (or right after) you go live, and show buyers what was checked.</p>
<div class="cards">
<div class="card">
<h2>Free: static scan + manual read</h2>
<p class="price">$0</p>
<ul>
<li>Run the open-source scanner yourself: <a href="{SCAN_REPO}">github.com/invisiblemonsters/token-launch-scan</a> (MIT).</li>
<li>Open an issue on that repo with your chain, contract address and launch link to ask for a quick manual read of the result.</li>
<li>Best effort, public, no turnaround guarantee.</li>
</ul>
</div>
<div class="card featured">
<h2>24-hour manual review</h2>
<p class="price">$150 <small>flat, paid in BTC</small></p>
<ul>
<li>A person reads your verified source, owner/admin setup, privileged functions, liquidity and lock setup, and launch wallets.</li>
<li>Written findings within 24 hours of payment confirmation, with evidence links for every point.</li>
<li>Teams that pass may display the <strong>Reviewed by Metatron</strong> badge, linked to their public review page.</li>
</ul>
<p><a class="btn" href="mailto:{CONTACT}?subject=Manual%20review%20request">Email {CONTACT}</a></p>
<p class="small">Include the chain, contract address(es), website and launch post. Payment details are sent by email after the scope is confirmed. Never send funds to an address you did not receive directly from {CONTACT}.</p>
</div>
</div>
<h2>The badge</h2>
<p><img src="badge.svg" alt="Reviewed by Metatron" width="220" height="40"></p>
<p>The badge is granted only after a manual review finds no unresolved high-severity issues. It reflects the contract and setup at the review date; it is not an audit, a guarantee, or investment advice, and it is withdrawn if the contract or admin setup changes in a way that adds risk. Each badge links to its review page so anyone can verify it.</p>
<pre><code>&lt;a href="{esc(ctx['SITE'])}"&gt;&lt;img src="{esc(ctx['SITE'])}badge.svg" alt="Reviewed by Metatron" width="220" height="40"&gt;&lt;/a&gt;</code></pre>
<h2>Already listed and think a signal is wrong?</h2>
<p>Re-scans and corrections are free. Email <a href="mailto:{CONTACT}?subject=Re-scan%20request">{CONTACT}</a> with the chain, the address and what changed.</p>
"""
    return page("Get reviewed | Metatron Launch Watch", body, 0, desc="Free static scan, or a flat $150 24-hour manual review paid in BTC.")

BADGE = """<svg xmlns="http://www.w3.org/2000/svg" width="220" height="40" viewBox="0 0 220 40" role="img" aria-label="Reviewed by Metatron">
<title>Reviewed by Metatron</title>
<rect width="220" height="40" rx="8" fill="#0f172a"/>
<rect x="1" y="1" width="218" height="38" rx="7" fill="none" stroke="#38bdf8" stroke-width="2"/>
<circle cx="22" cy="20" r="11" fill="#38bdf8"/>
<path d="M16.5 20.5l3.8 3.8 7.4-8" fill="none" stroke="#0f172a" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/>
<text x="42" y="17" font-family="Helvetica, Arial, sans-serif" font-size="10" fill="#94a3b8" letter-spacing="1">REVIEWED BY</text>
<text x="42" y="31" font-family="Helvetica, Arial, sans-serif" font-size="15" font-weight="700" fill="#f8fafc">Metatron</text>
<text x="132" y="31" font-family="Helvetica, Arial, sans-serif" font-size="10" fill="#94a3b8">Launch Watch</text>
</svg>
"""

CSS = """:root{--bg:#0b1020;--panel:#121a30;--line:#22304f;--txt:#e5ecf6;--mut:#93a4c0;--acc:#38bdf8;
--low:#16a34a;--mod:#ca8a04;--elev:#ea580c;--high:#dc2626}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--acc)}code{font:12.5px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#cbd5e1;word-break:break-all}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px}
.top{border-bottom:1px solid var(--line);background:#0d1328}.top .wrap{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;padding-top:14px;padding-bottom:14px}
.brand{font-weight:800;font-size:19px;color:var(--txt);text-decoration:none;letter-spacing:.2px}.brand span{color:var(--acc)}
nav a{color:var(--mut);text-decoration:none;margin-left:18px;font-weight:600}nav a:hover{color:var(--txt)}
main{padding:28px 20px 40px}h1{font-size:28px;margin:.2em 0 .4em}h2{font-size:20px;margin:1.6em 0 .5em}h3{font-size:16px;margin:1.4em 0 .4em;color:#cbd5e1}
.lead{font-size:16px;color:#d6deea}.small{font-size:13px;color:var(--mut)}.meta{color:var(--mut);margin:.2em 0}
.intro{margin-bottom:18px}.legend .pill{margin-right:6px}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
table{border-collapse:collapse;width:100%}th,td{padding:9px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--mut);background:#0f1730}tr:last-child td{border-bottom:0}
.launches td{vertical-align:middle}.num{text-align:right;white-space:nowrap}.nowrap{white-space:nowrap}
.pill{display:inline-block;min-width:74px;padding:3px 9px;border-radius:999px;font-weight:800;color:#fff;font-size:14px;text-align:center}
.pill small{font-weight:700;font-size:10px;letter-spacing:.5px;opacity:.95}
.low{background:var(--low)}.mod{background:var(--mod)}.elev{background:var(--elev)}.high{background:var(--high)}
.tag{display:inline-block;font-size:11px;font-weight:700;color:var(--mut);border:1px solid var(--line);border-radius:6px;padding:0 6px;margin-left:6px;vertical-align:middle}
.hero{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;flex-wrap:wrap}
.scorebox{border-radius:14px;padding:14px 22px;text-align:center;min-width:180px;color:#fff}.scorebox .big{font-size:46px;font-weight:900;line-height:1}
.scorebox .small{color:#fff;opacity:.9}.crumbs{font-size:13px;color:var(--mut);margin:0 0 8px}.addr{font-size:13px}
.note{border:1px solid var(--line);border-left:4px solid var(--acc);background:var(--panel);padding:10px 14px;border-radius:8px;margin:14px 0}
.links{list-style:none;padding:0;margin:0;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
.links li{padding:8px 12px;border-bottom:1px solid var(--line);word-break:break-all}.links li:last-child{border-bottom:0}.links span{display:inline-block;min-width:210px;color:var(--mut)}
.checks td .detail{color:#c4cfdf;font-size:14px}.checks .ev a,.checks .ev code{display:block;margin-bottom:3px}.checks .ev{max-width:330px;font-size:13px}
.st{display:inline-block;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.5px;padding:2px 7px;border-radius:6px;white-space:nowrap}
.st.flag{background:#7c2d12;color:#fed7aa}.st.pass{background:#14532d;color:#bbf7d0}.st.info{background:#1e293b;color:#cbd5e1}.st.unknown{background:#3f3f46;color:#e4e4e7;border:1px dashed #a1a1aa}
.why{display:block;font-size:12px;color:#a1a1aa;margin-top:3px}
.weights{max-width:640px;border:1px solid var(--line);border-radius:10px;background:var(--panel)}.weights .total td{font-weight:800}
details{margin:14px 0}summary{cursor:pointer;color:var(--acc);font-weight:600}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:18px}.card{border:1px solid var(--line);background:var(--panel);border-radius:12px;padding:6px 20px 14px}
.card.featured{border-color:var(--acc)}.price{font-size:30px;font-weight:900;margin:.2em 0}.price small{font-size:14px;color:var(--mut);font-weight:600}
.btn{display:inline-block;background:var(--acc);color:#04111d;font-weight:800;padding:9px 16px;border-radius:8px;text-decoration:none}
pre{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto}
.foot{border-top:1px solid var(--line);color:var(--mut);font-size:13px}.foot p{margin:16px 0}
@media (max-width:700px){nav a{margin:0 14px 0 0}.links span{display:block}h1{font-size:23px}}
"""

def atom(recs, ctx, built):
    site = ctx["SITE"]; CH = ctx["CHAINS"]
    ents = []
    for r in recs[:100]:
        c = CH[r["chain"]]; u = f"{site}launch/{slug(r)}.html"; tr0 = (r.get("top_reasons") or [None])[0]
        summ = (f"{launch_name(r)} on {c['name']} ({r['address']}) scored {r['score']}/100 ({r['band']} risk signals). "
                + (f"Top signal: {reason_label(tr0)} (+{tr0['points']:g}). " if tr0 else "") + FOOTER)
        ents.append(f"""<entry>
<title>{esc(launch_name(r))} ({esc(c['name'])}): {r['score']}/100 {esc(r['band'])}</title>
<link href="{esc(u)}"/>
<id>tag:invisiblemonsters.github.io,2026:launch-watch/{slug(r)}/{esc(r['scanned_at_utc'])}</id>
<updated>{esc(r['scanned_at_utc'])}</updated>
<category term="{esc(r['band'])}"/><category term="{esc(r['chain'])}"/>
<summary>{esc(summ)}</summary>
</entry>""")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Metatron Launch Watch: new scores</title>
<subtitle>Automated risk signals for new launches on Robinhood Chain and Base. {esc(FOOTER)}</subtitle>
<link href="{esc(site)}feed.xml" rel="self"/>
<link href="{esc(site)}"/>
<id>{esc(site)}</id>
<updated>{built.strftime('%Y-%m-%dT%H:%M:%SZ')}</updated>
<author><name>Metatron Launch Watch</name><email>{CONTACT}</email></author>
{chr(10).join(ents)}
</feed>
"""

# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("LW_DATA", "/workspace/launchwatch"))
    ap.add_argument("--out", default=os.path.join(HERE, "docs"))
    ap.add_argument("--site-url", default=os.environ.get("LW_SITE_URL", "https://invisiblemonsters.github.io/launch-watch/"))
    ap.add_argument("--report", action="store_true", help="print the exclusion list with reasons")
    a = ap.parse_args()
    site = a.site_url if a.site_url.endswith("/") else a.site_url + "/"
    CHAINS, WEIGHTS, UP, RF, VERSION = load_scorer(a.data)
    ctx = dict(CHAINS=CHAINS, WEIGHTS=WEIGHTS, UNKNOWN=UP, RF=RF, VERSION=VERSION, SITE=site)
    try: clusters = json.load(open(os.path.join(a.data, "data", "clusters.json")))
    except Exception: clusters = {"clusters": []}
    try: manual = {e["key"].lower(): e for e in json.load(open(os.path.join(HERE, "data", "exclusions.json")))["exclude"]}
    except FileNotFoundError: manual = {}

    recs = load_records(a.data, CHAINS)
    by_key, excluded, edits_total = {}, [], 0
    for r in recs:
        why = auto_problems(r, CHAINS)
        key = f"{r.get('chain')}:{str(r.get('address', '')).lower()}"
        if not why and key in manual: why = "manual: " + manual[key]["reason"]
        if why:
            excluded.append((key, r.get("label"), r.get("_day"), why)); continue
        edits_total += len(repair(r, CHAINS, clusters))
        by_key.setdefault(key, []).append(r)
    latest = []
    for key, lst in by_key.items():
        lst.sort(key=lambda x: x["scanned_at_utc"])
        r = lst[-1]; r["_history"] = lst; latest.append(r)
    latest.sort(key=lambda r: (r["scanned_at_utc"], (r.get("creation") or {}).get("ts") or 0), reverse=True)

    # exclusion reason categories (public: counts only)
    cats = {}
    for _, _, _, why in excluded:
        k = ("Launch post belongs to a third party or names a different contract" if why.startswith("manual:") or "third-party" in why
             else "Flagged check without a verifiable link" if "no verifiable evidence" in why
             else "Scan aborted or record malformed")
        cats[k] = cats.get(k, 0) + 1

    out = a.out
    if os.path.isdir(out): shutil.rmtree(out)
    os.makedirs(os.path.join(out, "launch")); os.makedirs(os.path.join(out, "data"))
    built = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    stats = dict(included=len(latest), excluded=len(excluded), excl_reasons=cats, built=built.isoformat(),
                 chains=" · ".join(f"{CHAINS[ch]['name']}: {sum(1 for r in latest if r['chain'] == ch)}" for ch in CHAINS))
    W = lambda p, s: open(os.path.join(out, p), "w", encoding="utf-8").write(s)
    W("style.css", CSS); W("badge.svg", BADGE); W(".nojekyll", "")
    W("index.html", render_index(latest, ctx, stats))
    W("methodology.html", render_methodology(ctx, stats))
    W("get-reviewed.html", render_reviewed(ctx))
    W("feed.xml", atom(latest, ctx, built))
    W("404.html", page("Not found | Metatron Launch Watch", '<h1>Not found</h1><p><a href="index.html">Back to the latest launches</a>.</p>'))
    index_json = []
    for r in latest:
        W(f"launch/{slug(r)}.html", render_launch(r, ctx, r["_history"]))
        pub = {k: v for k, v in r.items() if not k.startswith("_")}
        W(f"data/{slug(r)}.json", json.dumps(pub, indent=1))
        tr0 = (r.get("top_reasons") or [None])[0]
        index_json.append({"chain": r["chain"], "address": r["address"], "name": launch_name(r), "score": r["score"], "band": r["band"],
                           "top_reason": reason_label(tr0) if tr0 else None, "scanned_at_utc": r["scanned_at_utc"],
                           "created_ts": (r.get("creation") or {}).get("ts"), "page": f"launch/{slug(r)}.html"})
    W("data/launches.json", json.dumps({"generated_utc": stats["built"], "launches": index_json}, indent=1))
    # copy saved evidence files referenced by notes
    for ev in glob.glob(os.path.join(a.data, "runs", "*", "evidence", "*")):
        day = ev.split(os.sep)[-3]; os.makedirs(os.path.join(out, "evidence", day), exist_ok=True)
        shutil.copy2(ev, os.path.join(out, "evidence", day, os.path.basename(ev)))
    n_pages = len(glob.glob(os.path.join(out, "**", "*.html"), recursive=True))
    print(f"built {out}: {n_pages} HTML pages, {len(latest)} launches included, {len(excluded)} excluded, {edits_total} evidence/wording repairs, "
          f"{len(recs)} raw records from {len({r.get('_day') for r in recs})} run day(s)")
    if a.report or excluded:
        for key, label, day, why in excluded: print(f"  excluded {key} ({label}, {day}): {why}")

if __name__ == "__main__":
    main()

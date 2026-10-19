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
import argparse, datetime as dt, glob, html, json, os, re, shutil, sys, urllib.parse
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
PT = ZoneInfo("America/Los_Angeles")
CONTACT = "metatron@mail.grokbot.com"
SCAN_REPO = "https://github.com/invisiblemonsters/token-launch-scan"
FOOTER = ("Automated on-chain checks, not financial advice. Risk signals are not evidence of wrongdoing. "
          "Teams can request a re-scan or correction at metatron@mail.grokbot.com.")
ADDR_RX = re.compile(r"^0x[0-9a-fA-F]{40}$")
REPO = "invisiblemonsters/launch-watch"
REPO_URL = f"https://github.com/{REPO}"
ISSUE_NEW = f"{REPO_URL}/issues/new?template=request-review.yml"
# Public BTC receive address for orders (BIP84 account 0, receive index 1). Address only; no keys live here.
BTC_ADDR = "bc1qk43phw0cl63a2pqxssnakwsnuxu0swwjvmm9r0"
# Paid instant scan (GitHub Actions bot in invisiblemonsters/launch-watch-scan). Fixed rate, not live.
SCAN_FORM = "https://github.com/invisiblemonsters/launch-watch-scan/issues/new?template=paid-scan.yml"
SCAN_REPO_URL = "https://github.com/invisiblemonsters/launch-watch-scan"
SCAN_SATS, SCAN_MIN_SATS, PACK_SATS, SCAN_RATE_USD = 6100, 5490, 24500, 81771
INDEXNOW_KEY = "5f1c0d7e9a4b4e2c8a6d3b1f0e7c9a24"
SITE_URL = "https://invisiblemonsters.github.io/launch-watch/"  # set from --site-url in main()
DEFAULT_DESC = ("Free automated risk scores for new token and contract launches on Robinhood Chain and Base, "
                "with a $5 instant scan, a $50 Verified badge and a $150 24-hour manual review for teams.")

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
        ("unverified", "Source not verified", "Neither Sourcify nor Blockscout has verified source code, so the logic can only be read from bytecode. Verified on either one counts as verified. If one of them could not be checked (for example Blockscout's Cloudflare blocked the request after retries) and the other has no match, the check is marked unknown, not unverified."),
        ("unverified_template_match", "Unverified, but matches a verified template", "Not verified itself, but the runtime bytecode is the same length and at least 97% byte-identical to a verified reference contract (for example the Pons launchpad token). Differences are constructor values such as name and symbol."),
        ("upgradeable_proxy", "Upgradeable proxy", "An EIP-1967 implementation or beacon slot is set (or Sourcify reports a proxy), so the logic can be replaced after launch. EIP-1167 clones are not upgradeable and are not flagged."),
        ("proxy_admin_eoa", "Proxy admin is a single wallet", "The EIP-1967 admin slot points at an address with no code: one key controls upgrades."),
    ]),
    ("Owner and privileged functions", [
        ("owner_eoa", "Owner is a single wallet", "owner()/getOwner() returns an address with no code, or a wallet with an EIP-7702 delegation (code starting 0xef0100), so one key controls every owner-only function."),
        ("owner_contract", "Owner is a contract", "owner() is a contract (multisig, timelock or factory). Not classified further, so it carries a small weight. EIP-7702 delegated wallets are not counted here; they are single wallets."),
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
        ("lp_unlocked", "LP not locked or burned", "Less than 90% of LP tokens are locked or burned (GoPlus LP holders, or the burned share of a V2 pair). LP held by a verified locker contract (source verified on Sourcify or Blockscout; name or functions identify it as a locker) counts as locked even when GoPlus reports 0%, because GoPlus may not recognise custom lockers. The locker's unlock terms are not analysed. Concentrated-liquidity and bonding-curve pools cannot be read from free sources and are marked unknown."),
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
    if launch_name(r) == "Unnamed contract": return "no name/label"
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
def issue_url(name=None, chain=None, address=None, page_url=None):
    """Prefilled 'Request a review' issue form (GitHub issue-form query parameters)."""
    q = {}
    if name: q["title"] = f"[Review request] {name} on {chain or '<chain>'}"; q["project"] = name
    if chain: q["chain"] = chain
    if address: q["addresses"] = address
    if page_url: q["page"] = page_url
    return ISSUE_NEW + ("&" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote) if q else "")

def scan_url(chain_name=None, address=None):
    """Prefilled 'Paid instant scan' issue form in the launch-watch-scan repo."""
    q = {}
    if chain_name: q["chain"] = f"{chain_name} (chain ID {4663 if chain_name.startswith('Robinhood') else 8453})"
    if address: q["address"] = address; q["title"] = f"[Instant scan] {address}"
    return SCAN_FORM + ("&" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote) if q else "")

def page(title, body, depth=0, desc=None, canonical=None):
    up = "../" * depth
    d = desc or DEFAULT_DESC
    url = canonical or SITE_URL
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(d)}">
{f'<link rel="canonical" href="{esc(canonical)}">' if canonical else ''}
<meta property="og:type" content="website">
<meta property="og:site_name" content="Metatron Launch Watch">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(d)}">
<meta property="og:url" content="{esc(url)}">
<meta property="og:image" content="{esc(SITE_URL)}preview-index.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(title)}">
<meta name="twitter:description" content="{esc(d)}">
<meta name="twitter:image" content="{esc(SITE_URL)}preview-index.png">
<link rel="stylesheet" href="{up}style.css">
<link rel="alternate" type="application/atom+xml" title="Metatron Launch Watch: new scores" href="{up}feed.xml">
</head>
<body>
<header class="top"><div class="wrap">
<a class="brand" href="{up}index.html">Metatron <span>Launch Watch</span></a>
<nav><a href="{up}index.html">Launches</a><a href="{up}methodology.html">Methodology</a><a href="{up}robinhood-chain-security.html">RH Chain security</a><a href="{up}get-reviewed.html">Get reviewed &amp; pricing</a><a href="{up}embed.html">Embed</a><a href="{up}feed.xml">Feed</a></nav>
</div></header>
<main class="wrap">
{body}
</main>
<footer class="foot"><div class="wrap">
<p>{esc(FOOTER).replace(esc(CONTACT), f'<a href="mailto:{CONTACT}">{CONTACT}</a>')}</p>
<p><a href="{up}get-reviewed.html">Get reviewed &amp; pricing</a> · <a href="{up}robinhood-chain-security.html">Robinhood Chain security</a> · <a href="{up}embed.html">Embed / feeds</a> · <a href="{ISSUE_NEW}">Request a review (GitHub issue)</a> · <a href="{REPO_URL}">Source on GitHub</a> · <a href="{REPO_URL}/security/advisories/new">Report a vulnerability privately</a></p>
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
                "deployer.age": "Deployer age unknown", "deployer.fresh": "Deployer not identified", "host.domain_age": "Domain age unknown",
                "verify.source": "Verification status unknown", "owner.owner": "Owner type unknown"
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

try: LABELS = json.load(open(os.path.join(HERE, "data", "labels.json")))["labels"]
except FileNotFoundError: LABELS = {}

def launch_name(r):
    lk = f"{r.get('chain')}:{str(r.get('address', '')).lower()}"
    if not r.get("label") and lk in LABELS: return LABELS[lk]
    # tokens: label or token name; non-token contracts (routers, factories): the verified contract name from Blockscout
    return r.get("label") or (r.get("token") or {}).get("name") or (r.get("blockscout") or {}).get("name") or "Unnamed contract"

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
<div class="note owner"><strong>Is this your project?</strong> On-chain state changes after a scan. <a class="btn" href="{esc(issue_url(name, c['name'], a, ctx['SITE'] + 'launch/' + slug(r) + '.html'))}">Claim this page: request a re-scan, badge or review</a><br><span class="small">Opens a prefilled GitHub issue (public; never post vulnerabilities there). Free re-scans and corrections; <a href="{esc(scan_url(c['name'], a))}">$5 instant scan</a>, <a href="../get-reviewed.html">$50 Verified badge or $150 24-hour review</a>. Or email <a href="mailto:{CONTACT}?subject=Re-scan%20{esc(al)}">{CONTACT}</a>.</span></div>
"""
    return page(f"{name} ({c['name']}) - {r['score']}/100 {r['band']} | Metatron Launch Watch", body, depth=1,
                desc=f"Automated risk signals for {name} on {c['name']}: {r['score']}/100 ({r['band']}).",
                canonical=f"{ctx['SITE']}launch/{slug(r)}.html")

def render_index(recs, ctx, stats, featured_html=""):
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
<p>Free public feed of automated risk scores for new token and contract launches on <strong>Robinhood Chain</strong> and <strong>Base</strong>. Every score is the sum of fixed rules, every rule links to its public source, and checks that could not run are shown as <em>unknown</em>. Higher score = more risk signals. <a href="methodology.html">How scoring works</a>. Teams launching on Robinhood Chain: see <a href="robinhood-chain-security.html">what Launch Watch covers vs a real audit</a>, and how to order a Verified badge or 24h review. Machine-readable feeds: <a href="embed.html">Embed</a>.</p>
<p class="legend"><span class="pill low">0-24 <small>LOW</small></span> <span class="pill mod">25-49 <small>MODERATE</small></span> <span class="pill elev">50-74 <small>ELEVATED</small></span> <span class="pill high">75-100 <small>HIGH</small></span></p>
<p class="small">{stats['included']} launches listed · {stats['chains']} · last updated {fmt_pt(stats['built'])} · scans run about every 30 minutes · <a href="feed.xml">Atom feed</a></p>
</section>
{featured_html}
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
<li>Privileged-function points are multiplied by {RF} when owner() is provably 0x0 or dead (renounced), because other roles may still call them. When there is no owner (or it is renounced), the contract uses OpenZeppelin AccessControl, no account holds a role (RoleGranted/RoleRevoked logs since deployment, hasRole on the deployer) and no initializer is still open, the functions are shown as informational with 0 points.</li>
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
<li>The bytecode-only function scan matches {64} exact common signatures; renamed functions can be missed. Beyond the role-holder check above, access control on those functions is not analysed.</li>
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
    site = ctx["SITE"]
    body = f"""
<h1>Get reviewed: pricing</h1>
<p class="lead">Launching on Robinhood Chain or Base? Every launch we see is scored for free. Need a scan right now, of any contract? Order a $5 instant scan and get the report in a GitHub issue within minutes of 1 confirmation. Teams who want more can order a dated Verified badge or a 24-hour manual review here, without waiting for anyone to contact them. Paid in BTC.</p>
<div class="cards">
<div class="card">
<h2>Scored</h2>
<p class="price">Free</p>
<ul>
<li>Automatic: new launches on Robinhood Chain and Base get a public page with every check, its weight and its evidence link.</li>
<li>Free re-scans and corrections when something changes or a signal looks wrong.</li>
<li>Run the open-source scanner yourself: <a href="{SCAN_REPO}">token-launch-scan</a> (MIT).</li>
<li>No badge. Link to your launch page.</li>
</ul>
<p><a class="btn" href="{ISSUE_NEW}">Request a re-scan</a></p>
</div>
<div class="card" id="instant-scan">
<h2>Instant scan</h2>
<p class="price">$5 <small>{SCAN_SATS:,} sats, BTC</small></p>
<ul>
<li>Any Robinhood Chain or Base contract, on demand: the full Launch Watch scanner (score, band, every check and its evidence link) posted as a comment on your GitHub issue.</li>
<li>Fully automatic: a bot checks your payment on mempool.space and runs the scan after <strong>1 confirmation</strong> (re-checked every 15 minutes), then closes the issue.</li>
<li><strong>Scan pack:</strong> {PACK_SATS:,} sats ($20) for 5 scans; one transaction ID works for 5 requests from the same GitHub account.</li>
<li>Fixed rate of ${SCAN_RATE_USD:,}/BTC; payments from {SCAN_MIN_SATS:,} sats are accepted. The report is public and automated: not an audit or a guarantee.</li>
<li>Payments via mixers/CoinJoin and launches in excluded hosting clusters are declined and refunded manually.</li>
</ul>
<p><a class="btn" href="{SCAN_FORM}">Order an instant scan</a></p>
</div>
<div class="card">
<h2>Verified badge</h2>
<p class="price">$50 <small>flat, paid in BTC</small></p>
<ul>
<li>A fresh scanner run pinned to a block, plus a person reading your verified source and live owner, admin and role setup.</li>
<li>A dated SVG badge (chain, short address, review date) linked to a public report page listing what was checked, at which address, block and commit.</li>
<li><strong>30-day change cover:</strong> if in-scope contracts change (upgrade, new implementation, owner or role change, a redeploy you tell us about), one free re-check; the badge is reissued or withdrawn.</li>
<li><strong>Eligibility:</strong> a LOW score (0-24), or MODERATE (25-49) when every flagged signal is market or hosting data (thin liquidity, a young domain, a fresh deployer), not contract logic. Anything else is pointed to the review.</li>
<li><strong>If the re-check finds an unresolved high- or medium-severity item</strong>, no badge is issued; you get the written findings and choose a full refund or a $50 credit toward the review.</li>
<li>Delivered within 24 hours of 1 confirmation.</li>
</ul>
<p><a class="btn" href="{ISSUE_NEW}">Order the Verified badge</a></p>
</div>
<div class="card featured">
<h2>24h Review</h2>
<p class="price">$150 <small>flat, paid in BTC</small></p>
<ul>
<li>A person reads your verified source, owner/admin and roles, privileged functions, liquidity and lock setup, and launch wallets.</li>
<li>Written findings within 24 hours of 1 confirmation, with evidence links for every point. Sensitive findings are shared privately, never in a public issue.</li>
<li>Teams with no unresolved high-severity items may display the <strong>Reviewed by Metatron</strong> badge, linked to their review page.</li>
</ul>
<p><a class="btn" href="{ISSUE_NEW}">Order the 24h review</a></p>
</div>
</div>

<h2 id="order">How to order</h2>
<p><strong>Instant scan ($5):</strong> pay {SCAN_SATS:,} sats to the address below, then <a href="{SCAN_FORM}">open a "Paid instant scan" issue</a> with the chain, contract address and transaction ID. Everything else is automatic (<a href="{SCAN_REPO_URL}">how it works</a>). The steps below are for the badge and the review.</p>
<ol>
<li><strong>Open a request:</strong> <a href="{ISSUE_NEW}">open a GitHub issue with the "Request a review or re-scan" form</a> (chain, addresses, which tier). If your launch already has a page here, use the <em>Is this your project?</em> button on it; the form comes prefilled. Issues are public: include public information only.<br>
Prefer email? Write to <a href="mailto:{CONTACT}?subject=Review%20request">{CONTACT}</a> with the chain, addresses and tier. The inbox can reply to anyone who writes to it first.</li>
<li><strong>Pay</strong> the USD amount in BTC (at the mempool.space price when you send) to the address below.</li>
<li><strong>Match the payment:</strong> post the transaction ID in your issue, or email it with your issue number (for example <code>launch-watch #12</code>), so we can match it.</li>
<li><strong>Work starts after 1 confirmation.</strong> Results arrive in your issue (or by email if you wrote in), within 24 hours.</li>
</ol>

<h2 id="payment">Payment address (BTC)</h2>
<div class="note"><p><code class="btc">{BTC_ADDR}</code></p>
<p class="small">Bitcoin mainnet, native SegWit. This is the only payment address. It is the same on this page, in the repository <a href="{REPO_URL}#ordering">README</a> and in <a href="{REPO_URL}/blob/main/SECURITY.md">SECURITY.md</a>; if an address you were given anywhere else differs, do not pay it. Check incoming payments yourself on <a href="https://mempool.space/address/{BTC_ADDR}" rel="nofollow noopener">mempool.space</a>. Under- or overpayments of more than 5% are settled in the issue thread; refunds go to a BTC address you give us there.</p></div>

<h2>What the badge means</h2>
<p><img src="badge.svg" alt="Reviewed by Metatron" width="220" height="40"></p>
<blockquote><p><strong>What this badge means:</strong> On the date shown, Metatron Launch Watch re-ran its automated checks and a person read the verified source code and live owner/admin setup of the contract address(es) listed on this page, pinned to the block (and source commit, if provided) listed here. The findings are on this page with links to public evidence.</p>
<p><strong>What it does not mean:</strong> It is not an audit, not a guarantee, not a statement that the project or token is free of risk, and not investment advice. It says nothing about the team's intentions, future behaviour, off-chain systems, other contracts, or market conditions. On-chain state can change after the review; check the date and the current status on this page.</p></blockquote>
<p class="small">Short form, used under every embedded badge: <em>Reviewed at the listed address and block on the date shown. Not an audit or a guarantee.</em></p>
<p><strong>Badge states.</strong> <em>Current</em>: within 30 days of the review date with no in-scope change detected. <em>Expired</em>: more than 30 days after the review date; the report stays up, marked "Verified as of &lt;date&gt;; not re-checked since". <em>Withdrawn</em>: an in-scope contract changed in a way that adds risk, or the team declined the re-check; the report says so, with the date and reason.</p>
<p><strong>Scope</strong> is limited to the addresses listed on the report: the token plus any directly related contracts you name (staking, hook, locker, vesting). Off-chain systems, websites, bridges and third-party protocols are out of scope.</p>
<pre><code>&lt;a href="{esc(site)}get-reviewed.html" title="Reviewed at the listed address and block on the date shown. Not an audit or a guarantee."&gt;&lt;img src="{esc(site)}badge.svg" alt="Reviewed by Metatron" width="220" height="40"&gt;&lt;/a&gt;</code></pre>
<p class="small">Paid badges link to the project's own report page instead of this page.</p>

<h2>Found a vulnerability in a listed contract, or in this site?</h2>
<p>Do not open a public issue. Use <a href="{REPO_URL}/security/advisories/new">GitHub private vulnerability reporting</a>, or email <a href="mailto:{CONTACT}">{CONTACT}</a>. See <a href="{REPO_URL}/blob/main/SECURITY.md">SECURITY.md</a>.</p>
"""
    return page("Get reviewed: pricing | Metatron Launch Watch", body, 0,
                desc="Scored (free), $5 instant scan, Launch Watch Verified badge ($50 BTC) or a 24-hour manual smart-contract review ($150 BTC) for Robinhood Chain and Base launches. Not an audit or a guarantee.",
                canonical=f"{site}get-reviewed.html")

def load_featured():
    try: return json.load(open(os.path.join(HERE, "data", "featured.json")))
    except FileNotFoundError: return {"featured": [], "skipped": []}

def render_featured(f, ctx, by_key):
    """Hand-written featured read (data/featured.json). Public, non-exploitable facts + questions only."""
    CH = ctx["CHAINS"]; c = CH.get(f["chain"], {}); exp = c.get("explorer", "")
    url = f"{ctx['SITE']}featured/{f['slug']}.html"
    first_addr = f["contracts"][0][1] if f.get("contracts") else None
    rows = "".join(
        f'<tr><td>{esc(lbl)}</td><td><a href="{esc(exp)}/address/{esc(a)}" rel="nofollow noopener"><code>{esc(short(a))}</code></a></td>'
        f'<td>{esc(ver)}</td><td>{esc(px)}</td></tr>' for lbl, a, ver, px in f.get("contracts", []))
    scored = []
    for ch, a, lbl in f.get("scored", []):
        r = by_key.get(f"{ch}:{a.lower()}")
        if r: scored.append(f'<li><a href="../launch/{slug(r)}.html">{esc(lbl)}</a>: {band_pill(r["score"], r["band"])} <small>scanned {fmt_pt(r["scanned_at_utc"])}</small></li>')
        else: scored.append(f'<li>{esc(lbl)}: <code>{esc(short(a))}</code> (score page pending the next scan)</li>')
    facts = "".join(f"<li>{x}</li>" for x in f.get("facts", []))
    qs = "".join(f"<li>{x}</li>" for x in f.get("questions", []))
    priv = ('<p class="note"><strong>Private notes available on request.</strong> We do not publish anything that could put users at risk. '
            f'The team can get them via <a href="{REPO_URL}/security/advisories/new">private vulnerability reporting</a> or by writing to '
            f'<a href="mailto:{CONTACT}">{CONTACT}</a> first.</p>') if f.get("private_notes") else ""
    claim = issue_url(f["name"], c.get("name", f["chain"]), first_addr, url)
    body = f"""
<p class="small"><a href="../index.html#featured">Featured reads</a> / {esc(f['name'])}</p>
<h1>{esc(f['name'])} <small class="mut">on {esc(c.get('name', f['chain']))}</small></h1>
<p>{esc(f['tagline'])}</p>
<p class="small">Team: {esc(f.get('team',''))} · Site: <a href="{esc(f['site'])}" rel="nofollow noopener">{esc(f['site'])}</a> · Checked {esc(f['checked'])} · {esc(f.get('audit',''))}</p>
<div class="owner"><strong>Is this your project?</strong> <a class="btn" href="{esc(claim)}">Claim this page: request a re-scan, badge or review</a>
<span class="small">Free corrections · <a href="../get-reviewed.html">Verified badge $50 / 24h review $150</a> · or email <a href="mailto:{CONTACT}">{CONTACT}</a></span></div>
<h2>Automated scores</h2>
<ul>{''.join(scored)}</ul>
<h2>Contracts checked</h2>
<div class="tablewrap"><table><thead><tr><th>Contract</th><th>Address</th><th>Source</th><th>Upgradeable?</th></tr></thead><tbody>{rows}</tbody></table></div>
<h2>What we read on-chain</h2>
<ul>{facts}</ul>
<h2>Open questions for the team</h2>
<ul>{qs}</ul>
{priv}
<p class="small">A manual read of public source and on-chain state, plus our automated score. This is not an audit or a guarantee, and risk signals are not evidence of wrongdoing. Anything that looks like a vulnerability is never published here. It goes to the team privately. Corrections are welcome via the claim button above.</p>
"""
    return page(f"{f['name']}: featured read | Metatron Launch Watch", body, 1,
                desc=f"{f['name']} on {c.get('name', f['chain'])}: verified contracts, admin roles, proxy status and open questions. {f['tagline']}"[:300],
                canonical=url)

def render_featured_index(feat, by_key):
    if not feat.get("featured"): return ""
    cards = []
    for f in feat["featured"]:
        sc = [by_key.get(f"{ch}:{a.lower()}") for ch, a, _ in f.get("scored", [])]
        pills = " ".join(band_pill(r["score"], r["band"]) for r in sc if r)
        cards.append(f'<li><a href="featured/{esc(f["slug"])}.html"><strong>{esc(f["name"])}</strong></a> {pills}<br><span class="small">{esc(f["tagline"])}</span></li>')
    return f"""<section id="featured" class="featured"><h2>Featured reads</h2>
<p class="small">Manual reads of active, unaudited projects: verified source, admin keys, proxy status and open questions. Not audits. <a href="get-reviewed.html">Want one for your project?</a></p>
<ul class="feat">{''.join(cards)}</ul></section>
"""



def render_rh_security(ctx):
    """SEO write-up: Launch Watch vs audit for Robinhood Chain teams."""
    site = ctx["SITE"]
    body = f"""
<h1>Robinhood Chain launch security: scores, reviews, and what &ldquo;not audited&rdquo; means</h1>
<p class="lead">Many Robinhood Chain (chain ID 4663) projects ship while contracts are still marked <em>not audited</em>. That is common early on. This page explains what Metatron Launch Watch already publishes for free, how that differs from a full security audit, and how a team can order a Verified badge ($50 BTC) or a 24-hour manual review ($150 BTC) without waiting for outreach.</p>

<div class="note"><p><strong>Short disclaimer:</strong> Reviewed at the listed address and block on the date shown. <strong>Not an audit or a guarantee.</strong> Automated on-chain checks and manual reads of verified source are risk signals, not evidence of wrongdoing, not investment advice, and not a statement that a project or token is free of risk.</p></div>

<h2 id="search">If you searched for a Robinhood Chain audit</h2>
<p>Search results for &ldquo;Robinhood Chain audit&rdquo;, &ldquo;RH Chain not audited&rdquo;, or &ldquo;Robinhood Chain launch security&rdquo; often mix three different things:</p>
<ul>
<li><strong>A third-party security audit</strong> &mdash; a firm reads design and code in depth, often over weeks, and publishes a report. Launch Watch does not sell that.</li>
<li><strong>Automated launch scoring</strong> &mdash; what this site does for free: fixed on-chain and hosting checks with evidence links for new Robinhood Chain and Base launches.</li>
<li><strong>A dated manual review of listed addresses</strong> &mdash; what you can order here ($50 Verified badge or $150 24h Review): a person reads verified source and live owner/admin/role setup at a pinned block, with written findings.</li>
</ul>
<p>If your site or docs already say the contracts are <strong>not audited</strong>, that statement can stay accurate while you still get a public Launch Watch score page, and optionally a dated badge or review report that links only to the addresses and block that were checked.</p>

<h2 id="vs-audit">What Launch Watch scores vs a real audit</h2>
<table class="weights">
<thead><tr><th></th><th>Launch Watch (this site)</th><th>Typical third-party audit</th></tr></thead>
<tbody>
<tr><td>Coverage</td><td>Token/contract launches we discover on Robinhood Chain and Base; fixed rule set</td><td>Agreed scope, often the whole protocol and design</td></tr>
<tr><td>Method</td><td>Automated checks + optional manual read of verified source and live roles</td><td>Deep manual review, often with tooling and formal process</td></tr>
<tr><td>Output</td><td>Public score page; optional dated badge or written findings</td><td>Audit report; sometimes a fix review</td></tr>
<tr><td>Speed / price</td><td>Scores are free and continuous; badge $50 / 24h review $150 (BTC)</td><td>Usually days to weeks; much higher cost</td></tr>
<tr><td>Claim</td><td>Risk signals at an address and block. Not an audit or guarantee</td><td>Defined by the auditor&rsquo;s own terms</td></tr>
</tbody>
</table>
<p class="small">Full rule weights and data sources: <a href="methodology.html">methodology</a>. Pricing and ordering: <a href="get-reviewed.html">Get reviewed</a>.</p>

<h2 id="tiers">What teams can get</h2>
<div class="cards">
<div class="card">
<h3>Scored (free)</h3>
<p>Every launch we see gets a public page: score 0&ndash;100, band (LOW / MODERATE / ELEVATED / HIGH), each check with evidence. Free re-scans and corrections. No badge.</p>
</div>
<div class="card">
<h3>Verified badge &mdash; $50 BTC</h3>
<p>Block-pinned re-scan plus a person reading verified source and live owner/admin/roles. Dated SVG badge linked to a public report. 30-day change cover. For LOW scores, or MODERATE when flags are market/hosting-only. If an unresolved high/medium item is found: no badge; full refund or $50 credit toward the review.</p>
</div>
<div class="card featured">
<h3>24h Review &mdash; $150 BTC</h3>
<p>Manual review of source, owner/admin and roles, privileged functions, liquidity/lock setup, launch wallets. Written findings with evidence within 24 hours of 1 confirmation. Teams with no unresolved high-severity items may display the Reviewed by Metatron badge.</p>
</div>
</div>

<h2 id="order">How to order (no cold outreach required)</h2>
<ol>
<li><strong>Open a request</strong> with the <a href="{ISSUE_NEW}">Request a review or re-scan</a> GitHub form (chain, addresses, tier). If you already have a launch page here, use <em>Is this your project?</em> so the form is prefilled. Issues are public: include public information only. Or email <a href="mailto:{CONTACT}?subject=Review%20request">{CONTACT}</a> first; the inbox can reply to anyone who writes in.</li>
<li><strong>Pay</strong> the USD amount in BTC (mempool.space price when you send) to:<br>
<code class="btc">{BTC_ADDR}</code><br>
Same address on <a href="get-reviewed.html#payment">Get reviewed</a>, the README, and SECURITY.md. If you are given any other address, do not pay it. Confirm on <a href="https://mempool.space/address/{BTC_ADDR}" rel="nofollow noopener">mempool.space</a>.</li>
<li><strong>Match the payment</strong> by posting the transaction ID in the issue (or emailing it with the issue number).</li>
<li><strong>Work starts after 1 confirmation.</strong> Results within 24 hours.</li>
</ol>
<p><a class="btn" href="get-reviewed.html">Full pricing and badge meaning</a> &nbsp; <a class="btn" href="{ISSUE_NEW}">Open a review request</a></p>

<h2 id="badge-means">What a badge does and does not mean</h2>
<blockquote>
<p><strong>What this badge means:</strong> On the date shown, Metatron Launch Watch re-ran its automated checks and a person read the verified source code and live owner/admin setup of the contract address(es) listed on this page, pinned to the block (and source commit, if provided) listed here. The findings are on this page with links to public evidence.</p>
<p><strong>What it does not mean:</strong> It is not an audit, not a guarantee, not a statement that the project or token is free of risk, and not investment advice. It says nothing about the team&rsquo;s intentions, future behaviour, off-chain systems, other contracts, or market conditions. On-chain state can change after the review; check the date and the current status on this page.</p>
</blockquote>
<p class="small">We do not use the words safe, secure, audited, certified, approved, or trusted for Launch Watch badges. Scope is the addresses on the report only.</p>

<h2 id="feeds">Feeds for builders and bots</h2>
<p>Machine-readable indexes (attribution required): <a href="embed.html">Embed / feeds</a> &mdash; Atom <a href="feed.xml">feed.xml</a>, <a href="data/launches.json">data/launches.json</a>, <a href="data/high-risk.json">data/high-risk.json</a> (ELEVATED and HIGH bands).</p>

<p class="small">Automated on-chain checks, not financial advice. Risk signals are not evidence of wrongdoing. Corrections: <a href="mailto:{CONTACT}">{CONTACT}</a>.</p>
"""
    return page(
        "Robinhood Chain launch security & reviews | Metatron Launch Watch",
        body, 0,
        desc=("Robinhood Chain launch security: how Metatron Launch Watch scores unaudited RH Chain contracts, "
              "what that is not (a full audit), and how to order a $50 Verified badge or $150 24h review in BTC."),
        canonical=f"{site}robinhood-chain-security.html",
    )


def render_embed(ctx):
    """Docs for Atom/JSON embeds with required attribution."""
    site = ctx["SITE"]
    body = f"""
<h1>Embed Launch Watch feeds</h1>
<p class="lead">Use these static feeds to show new scores or high-band launches in your own bots, dashboards, or docs. No API key. Files are regenerated when the site rebuilds.</p>

<div class="note"><p><strong>Wording:</strong> These are <em>risk signals</em>, not accusations. Do not label a named project a scam or a rug based only on a Launch Watch score. Higher score = more risk signals under our fixed rules. Not financial advice. Not an audit or a guarantee.</p></div>

<h2>Endpoints</h2>
<div class="tablewrap"><table>
<thead><tr><th>URL</th><th>Format</th><th>Contents</th></tr></thead>
<tbody>
<tr><td><a href="feed.xml"><code>{esc(site)}feed.xml</code></a></td><td>Atom</td><td>Newest scores (up to 100 entries), with band and summary</td></tr>
<tr><td><a href="data/launches.json"><code>{esc(site)}data/launches.json</code></a></td><td>JSON</td><td>All listed launches: chain, address, name, score, band, page path, scan time</td></tr>
<tr><td><a href="data/high-risk.json"><code>{esc(site)}data/high-risk.json</code></a></td><td>JSON</td><td>Subset with band <code>ELEVATED</code> or <code>HIGH</code> (score &ge; 50)</td></tr>
</tbody></table></div>

<h2>high-risk.json shape</h2>
<pre><code>{{
  "generated_utc": "…",
  "definition": "band ELEVATED or HIGH (score &gt;= 50)",
  "count": 27,
  "launches": [
    {{
      "chain": "robinhood",
      "address": "0x…",
      "name": "…",
      "score": 59,
      "band": "ELEVATED",
      "top_reason": "…",
      "scanned_at_utc": "…",
      "page": "launch/robinhood-0x….html",
      "url": "{esc(site)}launch/robinhood-0x….html"
    }}
  ]
}}</code></pre>

<h2>Required attribution</h2>
<p>If you embed, republish, or derive a public view from these feeds, include visible credit and a link back:</p>
<ul>
<li>Credit text such as: <em>Scores by Metatron Launch Watch</em></li>
<li>Link to <a href="index.html">{esc(site)}</a> (or the specific launch page you show)</li>
<li>For team-facing CTAs, also link <a href="get-reviewed.html">Get reviewed &amp; pricing</a></li>
</ul>
<pre><code>&lt;p&gt;Scores by &lt;a href="{esc(site)}"&gt;Metatron Launch Watch&lt;/a&gt;
 · &lt;a href="{esc(site)}get-reviewed.html"&gt;Get reviewed&lt;/a&gt;.
 Risk signals are not evidence of wrongdoing. Not an audit or a guarantee.&lt;/p&gt;</code></pre>

<h2>Suggested use</h2>
<ul>
<li>Bot digests of new ELEVATED/HIGH scores (poll <code>high-risk.json</code> or the Atom feed)</li>
<li>Dashboards that deep-link to each launch page for evidence</li>
<li>Internal watchlists for Robinhood Chain / Base launches</li>
</ul>
<p class="small">Do not strip evidence links or present a score as a safety certificate. Methodology: <a href="methodology.html">methodology.html</a>. Robinhood Chain context: <a href="robinhood-chain-security.html">robinhood-chain-security.html</a>.</p>
"""
    return page(
        "Embed feeds | Metatron Launch Watch",
        body, 0,
        desc="Atom and JSON feeds for Metatron Launch Watch scores, including high-risk (ELEVATED/HIGH) launches. Attribution required. Risk signals are not evidence of wrongdoing.",
        canonical=f"{site}embed.html",
    )



def sitemap(latest, ctx, built, feat=None):
    site = ctx["SITE"]; day = built.strftime("%Y-%m-%d")
    urls = [(site, day, "hourly", "1.0"),
            (f"{site}get-reviewed.html", day, "weekly", "0.9"),
            (f"{site}robinhood-chain-security.html", day, "weekly", "0.9"),
            (f"{site}embed.html", day, "weekly", "0.7"),
            (f"{site}methodology.html", day, "weekly", "0.6"),
            (f"{site}data/launches.json", day, "hourly", "0.5"),
            (f"{site}data/high-risk.json", day, "hourly", "0.5"),
            (f"{site}feed.xml", day, "hourly", "0.5")]
    for r in latest:
        urls.append((f"{site}launch/{slug(r)}.html", r["scanned_at_utc"][:10], "daily", "0.5"))
    for f in (feat or {}).get("featured", []):
        urls.append((f"{site}featured/{f['slug']}.html", day, "daily", "0.8"))
    body = "".join(f"<url><loc>{esc(u)}</loc><lastmod>{m}</lastmod><changefreq>{f}</changefreq><priority>{p}</priority></url>\n" for u, m, f, p in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{body}</urlset>\n'

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

CSS = """.featured{margin:1.2em 0}.feat{list-style:none;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:.8em}.feat li{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:.7em .9em}.mut{color:var(--mut);font-weight:400}
:root{--bg:#0b1020;--panel:#121a30;--line:#22304f;--txt:#e5ecf6;--mut:#93a4c0;--acc:#38bdf8;
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
blockquote{margin:12px 0;padding:6px 16px;border-left:4px solid var(--acc);background:var(--panel);border-radius:8px}
.btc{font-size:16px;font-weight:700;color:#f8fafc}.owner .btn{margin:6px 0}
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
    global SITE_URL; SITE_URL = site
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
    feat = load_featured()
    by_key_latest = {f"{r['chain']}:{r['address'].lower()}": r for r in latest}
    W("index.html", render_index(latest, ctx, stats, render_featured_index(feat, by_key_latest)))
    os.makedirs(os.path.join(out, "featured"), exist_ok=True)
    for f in feat.get("featured", []):
        W(f"featured/{f['slug']}.html", render_featured(f, ctx, by_key_latest))
    W("methodology.html", render_methodology(ctx, stats))
    W("get-reviewed.html", render_reviewed(ctx))
    W("robinhood-chain-security.html", render_rh_security(ctx))
    W("embed.html", render_embed(ctx))
    W("feed.xml", atom(latest, ctx, built))
    W("sitemap.xml", sitemap(latest, ctx, built, feat))
    W("robots.txt", f"User-agent: *\nAllow: /\nSitemap: {site}sitemap.xml\n")
    W(f"{INDEXNOW_KEY}.txt", INDEXNOW_KEY)
    if os.path.exists(os.path.join(HERE, "preview-index.png")):
        shutil.copy2(os.path.join(HERE, "preview-index.png"), os.path.join(out, "preview-index.png"))
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
    high_risk = []
    for row in index_json:
        if row["band"] in ("ELEVATED", "HIGH") or (isinstance(row.get("score"), (int, float)) and row["score"] >= 50):
            high_risk.append({**row, "url": site + row["page"]})
    W("data/high-risk.json", json.dumps({
        "generated_utc": stats["built"],
        "definition": "band ELEVATED or HIGH (score >= 50)",
        "count": len(high_risk),
        "launches": high_risk,
        "attribution": {
            "name": "Metatron Launch Watch",
            "site": site,
            "get_reviewed": site + "get-reviewed.html",
            "required": "Credit Metatron Launch Watch and link back. Risk signals are not evidence of wrongdoing. Not an audit or a guarantee.",
        },
    }, indent=1))
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

"""
CLOUD COPY -- runs inside the vcp-dashboard repo's own GitHub Actions
workflow (.github/workflows/refresh.yml), triggered by the dashboard's
public "Refresh scan" button (a repository_dispatch event) or manually via
workflow_dispatch.

This is a deliberately-duplicated adaptation of
tools/vcp_scanner_telegram.py from the Obsidian vault (private, not in this
repo) -- the core scan/enrich/render logic is identical, only the
config-loading and output-writing plumbing differs (env vars/secrets
instead of local JSON config files; writes directly into the current
checkout instead of cloning a separate one, since this already *is* the
target repo when running in Actions). If you fix a bug in the scan logic in
one copy, fix it in the other -- see wiki/strategies/vcp-screening-tools.md
in the vault for the full write-up of what's been fixed and why.

Config (all via GitHub Actions repo secrets, see .github/workflows/refresh.yml):
- TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Non-secret identifiers are hardcoded below (they're already public via the
dashboard's own client-side JS, so an env var buys no extra privacy here).

A cooldown check at the top of main() skips the run entirely if the last
scan was too recent -- this is what actually stops a spam-clicked Refresh
button (or a burst of repository_dispatch events) from flooding Chartink/
Yahoo/screener.in with a pile of near-simultaneous scans; the client-side
button disable is just a UX nicety, not real protection.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import request, parse, error

# GitHub Actions runners run in UTC -- time.strftime(..."IST") used to just
# label the runner's UTC wall clock as IST without converting it, so every
# cloud-generated timestamp was off by 5:30. This does the actual conversion.
_IST = timezone(timedelta(hours=5, minutes=30))


def _ist_timestamp():
    return datetime.now(timezone.utc).astimezone(_IST).strftime("%Y-%m-%d %H:%M IST")


REPO_ROOT = Path(__file__).parent
CHARTINK_URL = "https://chartink.com/screener/process"
DASHBOARD_PATH = REPO_ROOT / "index.html"
PREVIOUS_SCAN_PATH = REPO_ROOT / "previous_scan.json"
COOLDOWN_SECONDS = 120

GITHUB_REPO_OWNER = "saketspec-ship-it"
GITHUB_REPO_NAME = "vcp-dashboard"
GOATCOUNTER_SITE = "vcpdash"

# See tools/vcp_scanner_telegram.py's PUBLIC_BASE_URL for why exported
# files (CSV/Excel) need an absolute link here rather than the relative
# "details/<TICKER>.html" used on-page.
PUBLIC_BASE_URL = f"https://{GITHUB_REPO_OWNER}.github.io/{GITHUB_REPO_NAME}/"

# The Refresh button used to embed a GitHub PAT directly and call GitHub's
# dispatches API from the browser -- that token got auto-revoked by GitHub's
# secret-scanning within minutes of going public (see
# wiki/strategies/vcp-screening-tools.md). Now routed through a Cloudflare
# Worker that holds the real token server-side; nothing secret ends up here.
REFRESH_WORKER_URL = "https://vcp-refresh-proxy.saket-spec.workers.dev/"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Validated live against Chartink's scan endpoint on 2026-07-05 (54 matches).
# Mirrors the Trend Template + volume-dryup + volatility-contraction criteria
# from wiki/strategies/vcp-volatility-contraction-pattern.md. Known gap: no
# relative-strength-vs-market term (Chartink has no direct equivalent to the
# RS>=70 rating used by the US-market tools) -- see vcp-screening-tools.md.
#
# Fixed 2026-07-06: the 52-week-high condition was "close <= 1.25*high" (not
# more than 25% ABOVE the high), which is almost always true and so was a
# silent no-op, not an actual filter. Correct condition for "within 25% of
# the 52-week high" is "close >= 0.75*high" (not more than 25% BELOW it).
# Found via a stock (SILVERTUC) that matched the old query while genuinely
# trading 40%+ below its 200-day MA -- see vcp-screening-tools.md.
SCAN_CLAUSE = (
    "( {cash} ( latest close > 30 and latest volume > 100000 "
    "and latest close > latest sma(latest close,50) "
    "and latest sma(latest close,50) > latest sma(latest close,150) "
    "and latest sma(latest close,150) > latest sma(latest close,200) "
    "and latest close >= 0.75 * max(250,high) "
    "and latest close >= 1.3 * min(250,low) "
    "and sma(volume,5) < sma(volume,30) "
    "and ( latest upper bollinger band(20,2) - latest lower bollinger band(20,2) ) "
    "< ( 20 days ago upper bollinger band(20,2) - 20 days ago lower bollinger band(20,2) ) ) )"
)


# ---------------------------------------------------------------- Chartink --

def fetch_vcp_matches():
    """Fetch CSRF token + session cookie, then POST the scan. Returns list of dicts."""
    req = request.Request(CHARTINK_URL, headers={"User-Agent": "Mozilla/5.0"})
    with request.urlopen(req) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
        # Chartink sets two cookies (XSRF-TOKEN, ci_session); both are required
        # on the follow-up POST. get_all (not get) is needed to see both.
        raw_cookies = resp.headers.get_all("Set-Cookie") or []
        cookie = "; ".join(c.split(";")[0] for c in raw_cookies)

    token_match = re.search(r'name="csrf-token" content="([^"]*)"', html)
    if not token_match:
        raise RuntimeError("Could not find CSRF token on Chartink page")
    token = token_match.group(1)

    body = parse.urlencode({"scan_clause": SCAN_CLAUSE}).encode()
    req = request.Request(
        CHARTINK_URL,
        data=body,
        headers={
            "User-Agent": "Mozilla/5.0",
            "X-CSRF-TOKEN": token,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Cookie": cookie,
        },
    )
    with request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if "scan_error" in data:
        raise RuntimeError(f"Chartink scan error: {data['scan_error']}")
    return data.get("data", [])


# ------------------------------------------------------------- Screener.in --
# robots.txt (checked 2026-07-06) disallows /user/*, some query-param
# patterns, and /company/source/quarter/* -- plain /company/<TICKER>/... pages
# used here aren't disallowed. Still fetched politely: one request per stock,
# a real browser UA, a short delay between stocks (see enrich_all).

def _screener_get(url):
    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except error.HTTPError:
        return None


def _screener_num(text):
    if text is None:
        return None
    text = text.replace(",", "").replace("%", "").replace("₹", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def _screener_ratio(html, label):
    """Reads one of the top ratio-grid values, e.g. Stock P/E, ROCE, ROE --
    each is `<li ...><span class="name">LABEL</span>...<span class="number">
    VALUE</span>...</li>`."""
    m = re.search(
        r'<span class="name">\s*' + re.escape(label) + r'\s*</span>.*?'
        r'<span class="number">\s*([^<]+?)\s*</span>',
        html, re.S,
    )
    return _screener_num(m.group(1)) if m else None


def _screener_section(html, header):
    """Returns the HTML between <h2>header</h2> and the next <h2>, i.e. one
    of the named report sections (Quarterly Results, Profit & Loss, Balance
    Sheet, Cash Flows, Ratios, Shareholding Pattern)."""
    headers = [(m.start(), m.group(1).strip()) for m in re.finditer(r"<h2[^>]*>([^<]+)</h2>", html)]
    for i, (start, name) in enumerate(headers):
        if name == header:
            end = headers[i + 1][0] if i + 1 < len(headers) else len(html)
            return html[start:end]
    return None


def _screener_row_values(section_html, label):
    """Returns all <td> values (as text) from the row whose first cell
    contains `label` -- works for the schedule tables (Quarterly Results,
    P&L, Balance Sheet, Cash Flows, Ratios, Shareholding Pattern), which all
    share the same markup shape."""
    if section_html is None:
        return None
    idx = section_html.find(label)
    if idx == -1:
        return None
    tr_start = section_html.rfind("<tr", 0, idx)
    tr_end = section_html.find("</tr>", idx) + len("</tr>")
    row = section_html[tr_start:tr_end]
    tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
    return [" ".join(re.sub(r"<[^>]+>", " ", t).split()) for t in tds]


def _screener_page_has_financials(html):
    """True if the Quarterly Results table actually has numeric data (not
    just an empty shell with only row labels) -- see the fallback comment
    in fetch_screener_data for why this check exists."""
    if html is None:
        return False
    section = _screener_section(html, "Quarterly Results")
    row = _screener_row_values(section, "Net Profit")
    if not row or len(row) < 2:
        return False
    return any(_screener_num(v) is not None for v in row[1:])


def fetch_screener_data(nsecode):
    """Scrapes sector, valuation, profitability, quarterly/annual financials,
    and shareholding pattern from screener.in. Every field is independently
    best-effort (None on failure) -- these pages aren't a stable API and
    screener.in's own layout differs a bit for banks/NBFCs, so missing
    fields for some stocks is expected, not a bug to chase."""
    data = {
        "sector": None, "stock_pe": None, "roce": None, "roe": None,
        "net_profit_qtr": [], "net_profit_year": [], "opm_pct_year": [],
        "reserves_year": [], "cash_from_ops_year": [], "debtor_days": None,
        "shareholding_promoter": None, "shareholding_fii": None,
        "shareholding_dii": None, "shareholding_public": None,
    }

    html = _screener_get(f"https://www.screener.in/company/{nsecode}/consolidated/")
    if html is None or not _screener_page_has_financials(html):
        # Not every company publishes meaningful consolidated financials --
        # banks in particular (found via BANDHANBNK) serve a page that LOOKS
        # complete (100KB+, all the right section headers) but whose
        # Quarterly Results/P&L/etc. tables are empty shells (a <thead> with
        # no period columns, every row just the label cell, no numbers at
        # all) because they report standalone only. A page-length check
        # alone doesn't catch this -- checking for an actual number in the
        # Quarterly Results table does.
        fallback = _screener_get(f"https://www.screener.in/company/{nsecode}/")
        if fallback and _screener_page_has_financials(fallback):
            html = fallback
    if html is None:
        return data

    try:
        m = re.search(r'title="Sector">([^<]+)</a>', html)
        data["sector"] = m.group(1) if m else None
    except Exception:
        pass

    for key, label in [("stock_pe", "Stock P/E"), ("roce", "ROCE"), ("roe", "ROE")]:
        try:
            data[key] = _screener_ratio(html, label)
        except Exception:
            pass

    try:
        quarterly = _screener_section(html, "Quarterly Results")
        row = _screener_row_values(quarterly, "Net Profit")
        if row:
            vals = [v for v in (_screener_num(x) for x in row[1:]) if v is not None]
            data["net_profit_qtr"] = vals[-4:]
    except Exception:
        pass

    try:
        pl = _screener_section(html, "Profit & Loss")
        np_row = _screener_row_values(pl, "Net Profit")
        if np_row:
            vals = [v for v in (_screener_num(x) for x in np_row[1:]) if v is not None]
            data["net_profit_year"] = vals[-4:]
        opm_row = _screener_row_values(pl, "OPM")
        if opm_row:
            vals = [v for v in (_screener_num(x) for x in opm_row[1:]) if v is not None]
            data["opm_pct_year"] = vals[-4:]
    except Exception:
        pass

    try:
        bs = _screener_section(html, "Balance Sheet")
        res_row = _screener_row_values(bs, "Reserves")
        if res_row:
            vals = [v for v in (_screener_num(x) for x in res_row[1:]) if v is not None]
            data["reserves_year"] = vals[-4:]
    except Exception:
        pass

    try:
        cf = _screener_section(html, "Cash Flows")
        cf_row = _screener_row_values(cf, "Cash from Operating Activity")
        if cf_row:
            vals = [v for v in (_screener_num(x) for x in cf_row[1:]) if v is not None]
            data["cash_from_ops_year"] = vals[-4:]
    except Exception:
        pass

    try:
        ratios = _screener_section(html, "Ratios")
        dd_row = _screener_row_values(ratios, "Debtor Days")
        if dd_row:
            vals = [v for v in (_screener_num(x) for x in dd_row[1:]) if v is not None]
            if vals:
                data["debtor_days"] = vals[-1]
    except Exception:
        pass

    try:
        sh = _screener_section(html, "Shareholding Pattern")
        for key, label in [
            ("shareholding_promoter", "Promoter"), ("shareholding_fii", "FII"),
            ("shareholding_dii", "DII"), ("shareholding_public", "Public"),
        ]:
            row = _screener_row_values(sh, label)
            if row:
                vals = [v for v in (_screener_num(x) for x in row[1:]) if v is not None]
                if vals:
                    data[key] = vals[-1]
    except Exception:
        pass

    return data


# ------------------------------------------------------------ Yahoo Finance --

def _yahoo_get(url):
    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _split_adjust(timestamps, values, splits):
    """Adjusts historical prices for any stock splits so old and new prices
    are on the same (current) share-count basis. Without this, a stock that
    split N:1 shows a fake ~Nx-too-high "52-week high"/"all-time high" and a
    single-day RSI-breaking fake -100*(1-1/N)% "crash" on the split date --
    found via SILVERTUC, which split 2:1 on 2026-03-06 and was showing a
    52-week high of 1117 (really ~558 in current terms) against a ~182 price.
    `splits` is Yahoo's events.splits dict: {ts: {numerator, denominator}}."""
    if not splits:
        return values
    split_events = sorted(
        (int(ts), s["numerator"] / s["denominator"]) for ts, s in splits.items()
    )
    adjusted = []
    for t, v in zip(timestamps, values):
        if v is None:
            adjusted.append(None)
            continue
        factor = 1.0
        for split_ts, ratio in split_events:
            if split_ts > t:
                factor /= ratio
        adjusted.append(v * factor)
    return adjusted


def _extract_ohlc(chart_result):
    """Pulls timestamp/close/high/low as aligned lists (dropping only rows
    where the whole bar is missing) and applies split adjustment."""
    timestamps = chart_result["timestamp"]
    quote = chart_result["indicators"]["quote"][0]
    splits = chart_result.get("events", {}).get("splits", {})
    closes = _split_adjust(timestamps, quote["close"], splits)
    highs = _split_adjust(timestamps, quote["high"], splits)
    lows = _split_adjust(timestamps, quote["low"], splits)
    rows = [(c, h, l) for c, h, l in zip(closes, highs, lows) if c is not None]
    return ([c for c, h, l in rows], [h for c, h, l in rows], [l for c, h, l in rows])


def _buy_sell_flag(closes):
    """3-tier moving-average flag: below the 50-day MA is a broken
    intermediate-term trend ("Away"/red); above the 50-day but below the
    10-day MA is a short-term pullback within an intact trend ("Watch"/
    amber); above the 10-day MA is short-term strength ("Trend"/green).
    Checked in this order so it works regardless of whether the 10-day MA
    happens to sit above or below the 50-day MA on a given day."""
    if len(closes) < 50:
        return None
    price = closes[-1]
    sma10 = sum(closes[-10:]) / 10
    sma50 = sum(closes[-50:]) / 50
    if price < sma50:
        return {"flag": "Away", "css": "flag-red", "priority": 2}
    if price < sma10:
        return {"flag": "Watch", "css": "flag-amber", "priority": 1}
    return {"flag": "Trend", "css": "flag-green", "priority": 0}


def enrich_symbol(nsecode, nifty_return_pct):
    """Returns dict with prev_close, rsi14, all_time_high, all_time_low,
    trend_template_score ("x/y" string), listing_age_days, buy_sell_flag
    (from Yahoo Finance) plus a screener.in fundamentals block (see
    fetch_screener_data). Missing/failed fields are None rather than
    raising -- one bad symbol shouldn't kill the whole run."""
    yf_symbol = f"{nsecode}.NS"
    result = {
        "prev_close": None, "rsi14": None, "all_time_high": None, "all_time_low": None,
        "trend_template_score": None, "trend_template_criteria": None, "listing_age_days": None,
        "buy_sell_flag": None,
    }

    try:
        # 2y of daily data covers everything the 8-criteria check needs: 200-day
        # MA (needs 200 points), its 1-month-ago value (needs 220), and the
        # 52-week high/low (needs 252) -- with margin for holidays.
        body = _yahoo_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
            "?interval=1d&range=2y&events=split"
        )
        r = json.loads(body)["chart"]["result"][0]
        closes, highs, lows = _extract_ohlc(r)

        # meta.chartPreviousClose is the close *before the requested range*
        # started, not "yesterday's close" -- the second-to-last entry in the
        # actual daily series is the real previous trading day's close.
        if len(closes) >= 2:
            result["prev_close"] = closes[-2]
        result["rsi14"] = _rsi(closes, 14)
        tt = _trend_template_score(closes, highs, lows, nifty_return_pct)
        result["trend_template_score"] = tt["score"]
        result["trend_template_criteria"] = tt["criteria"]
        result["buy_sell_flag"] = _buy_sell_flag(closes)

        first_trade = r["meta"].get("firstTradeDate")
        if first_trade:
            result["listing_age_days"] = int(time.time() - first_trade) // 86400
    except Exception:
        pass

    try:
        body = _yahoo_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
            "?interval=1mo&range=max&events=split"
        )
        r = json.loads(body)["chart"]["result"][0]
        _, highs, lows = _extract_ohlc(r)
        result["all_time_high"] = max(highs) if highs else None
        result["all_time_low"] = min(lows) if lows else None
    except Exception:
        pass

    result["screener"] = fetch_screener_data(nsecode)
    return result


def get_nifty_return_pct(period=63):
    """Nifty 50's own trailing return over `period` trading days (~3 months),
    used as the benchmark for the relative-strength criterion below."""
    body = _yahoo_get(
        "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=1d&range=1y"
    )
    r = json.loads(body)["chart"]["result"][0]
    closes = [c for c in r["indicators"]["quote"][0]["close"] if c is not None]
    if len(closes) < period + 1:
        return None
    return (closes[-1] / closes[-1 - period] - 1) * 100


def _trend_template_score(closes, highs, lows, nifty_return_pct, period=63):
    """Scores the 8 Trend Template criteria the user asked for. Returns
    {"score": "7/8", "criteria": [...]} where each criterion has num/desc/
    applicable/passed/reading -- the human-readable "reading" (e.g. "217.35 >
    203.88 (150MA) and > 188.44 (200MA)") is what powers the per-stock detail
    page. If a criterion couldn't be evaluated due to insufficient price
    history (e.g. a stock listed less than a year ago), it's marked
    inapplicable (shrinks the denominator) rather than counted as a fail.

    Criterion 8 (RS Rating >= 70) has no direct equivalent available here --
    IBD-style RS Rating is a percentile rank across the whole market, which
    none of our data sources expose. It's approximated as "stock's trailing
    ~3-month return beats Nifty 50's" -- a real but much cruder relative-
    strength signal than a true percentile rating. Flagged in the wiki so
    this isn't mistaken for the real thing.
    """
    price = closes[-1]
    criteria = []

    def add(num, desc, applicable, passed, reading):
        criteria.append({"num": num, "desc": desc, "applicable": applicable,
                          "passed": passed, "reading": reading})

    have150 = len(closes) >= 150
    have200 = len(closes) >= 200
    have220 = len(closes) >= 220
    have50 = len(closes) >= 50
    have252 = len(closes) >= 252 and len(highs) >= 252 and len(lows) >= 252

    sma200 = sum(closes[-200:]) / 200 if have200 else None
    sma150 = sum(closes[-150:]) / 150 if have150 else None
    sma50 = sum(closes[-50:]) / 50 if have50 else None

    if sma150 is not None and sma200 is not None:
        passed = price > sma150 and price > sma200
        cmp150, cmp200 = (">" if price > sma150 else "<="), (">" if price > sma200 else "<=")
        add(1, "Price > 150 MA & 200 MA", True, passed,
            f"{price:.2f} {cmp150} {sma150:.2f} (150MA) and {cmp200} {sma200:.2f} (200MA)")
    else:
        add(1, "Price > 150 MA & 200 MA", False, None, "Not enough price history (need ~150-200 days)")

    if sma150 is not None and sma200 is not None:
        passed = sma150 > sma200
        add(2, "150 MA > 200 MA", True, passed,
            f"{sma150:.2f} {'>' if passed else '<='} {sma200:.2f}")
    else:
        add(2, "150 MA > 200 MA", False, None, "Not enough price history (need ~150-200 days)")

    if have220:
        sma200_1mo_ago = sum(closes[-220:-20]) / 200
        passed = sma200 > sma200_1mo_ago
        add(3, "200 MA trending up >= 1 month", True, passed,
            f"200 MA now {sma200:.2f} vs {sma200_1mo_ago:.2f} ~1 month ago "
            f"({'rising' if passed else 'not rising'})")
    else:
        add(3, "200 MA trending up >= 1 month", False, None, "Not enough price history (need ~220 days)")

    if sma50 is not None and sma150 is not None and sma200 is not None:
        passed = sma50 > sma150 and sma50 > sma200
        add(4, "50 MA > 150 MA & 200 MA", True, passed,
            f"{sma50:.2f} (50MA) {'>' if passed else 'not consistently >'} "
            f"150MA {sma150:.2f} & 200MA {sma200:.2f}")
    else:
        add(4, "50 MA > 150 MA & 200 MA", False, None, "Not enough price history")

    if sma50 is not None:
        passed = price > sma50
        add(5, "Price > 50 MA", True, passed, f"{price:.2f} {'>' if passed else '<='} {sma50:.2f}")
    else:
        add(5, "Price > 50 MA", False, None, "Not enough price history (need 50 days)")

    if have252:
        low52 = min(lows[-252:])
        pct_above_low = (price / low52 - 1) * 100
        passed = price >= 1.3 * low52
        add(6, "Price >= 30% above 52-week low", True, passed,
            f"52-week low ~{low52:.2f}; price is {pct_above_low:+.1f}% above it")
    else:
        add(6, "Price >= 30% above 52-week low", False, None, "Not enough price history (need 252 days)")

    # "price <= 1.25*high" was the original, wrong version: that's almost
    # always true since price rarely exceeds its own recent high by 25%, so
    # it was a silent no-op rather than an actual filter. Found via SILVERTUC
    # scoring 4/8 despite having passed the Chartink scan supposedly checking
    # this same condition. Correct: price >= 0.75*high (not more than 25% below it).
    if have252:
        high52 = max(highs[-252:])
        pct_below_high = (1 - price / high52) * 100
        passed = price >= 0.75 * high52
        add(7, "Price within 25% of 52-week high", True, passed,
            f"52-week high ~{high52:.2f}; price is {pct_below_high:.1f}% below it")
    else:
        add(7, "Price within 25% of 52-week high", False, None, "Not enough price history (need 252 days)")

    if nifty_return_pct is not None and len(closes) >= period + 1:
        stock_return_pct = (closes[-1] / closes[-1 - period] - 1) * 100
        passed = stock_return_pct > nifty_return_pct
        add(8, "RS Rating >= 70 (proxy)", True, passed,
            f"stock ~3mo return {stock_return_pct:+.1f}% vs Nifty 50 {nifty_return_pct:+.1f}% "
            "(proxy, not a true percentile RS rating)")
    else:
        add(8, "RS Rating >= 70 (proxy)", False, None, "Not enough data for the proxy calculation")

    passed_count = sum(1 for c in criteria if c["applicable"] and c["passed"])
    applicable_count = sum(1 for c in criteria if c["applicable"])
    return {"score": f"{passed_count}/{applicable_count}", "criteria": criteria}


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def enrich_all(matches):
    nifty_return_pct = get_nifty_return_pct()
    enriched = []
    for m in matches:
        data = enrich_symbol(m["nsecode"], nifty_return_pct)
        enriched.append({**m, **data})
        time.sleep(0.5)  # be polite to Yahoo's + screener.in's unauthenticated endpoints
    return enriched


# -------------------------------------------------------------- Dashboard --

def _fmt(v, suffix=""):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.2f}{suffix}"
    return f"{v:,}{suffix}"


def _fmt_pair(a, b, suffix=""):
    """Renders two related values in one cell, e.g. "1,117.45 | 52.75" for
    all-time high | low, per the user's requested combined-cell format."""
    return f"{_fmt(a, suffix)} | {_fmt(b, suffix)}"


def _fmt_series(values, suffix=""):
    """Renders up to the last 4 periods (oldest to newest) in one cell,
    e.g. "72.00 | 129.00 | 5.41 | 7.52" -- fewer than 4 shows whatever's
    actually available (e.g. a recently-listed stock without 4 years of
    annual reports yet) rather than padding with placeholders."""
    if not values:
        return "-"
    return " | ".join(_fmt(v, suffix) for v in values)


def _fmt_age(days):
    if days is None:
        return "-"
    years, rem_days = divmod(days, 365)
    months = rem_days // 30
    if years == 0:
        return f"{months}m"
    return f"{years}y {months}m"


def _trend_template_commentary(r):
    """Templated Minervini-VCP-flavored commentary, driven by which criteria
    actually passed/failed plus RSI and all-time-high context. Not a
    substitute for actually looking at the chart -- see the caveat this
    function always appends."""
    criteria = r.get("trend_template_criteria") or []
    by_num = {c["num"]: c for c in criteria}
    notes = []

    ma_stack_fails = [n for n in (1, 2, 4, 5) if by_num.get(n) and by_num[n]["applicable"] and not by_num[n]["passed"]]
    if ma_stack_fails:
        notes.append(
            "The moving-average stack itself is broken (criteria "
            f"{', '.join(str(n) for n in ma_stack_fails)}) -- per Minervini, this stock isn't "
            "confirmed to be in a Stage 2 uptrend right now, so it doesn't qualify as a VCP "
            "candidate regardless of any base pattern on the chart."
        )
    else:
        notes.append(
            "The MA stack (50 > 150 > 200, price above all three) is intact -- the basic "
            "precondition for a Stage 2 uptrend that Minervini's system requires before even "
            "looking for a VCP base."
        )

    c3 = by_num.get(3)
    if c3 and c3["applicable"] and not c3["passed"]:
        notes.append(
            "The 200-day MA isn't rising over the past month -- a flattening/declining "
            "long-term trend undercuts the 'institutions are accumulating' thesis the Trend "
            "Template is meant to detect."
        )

    c6 = by_num.get(6)
    if c6 and c6["applicable"] and not c6["passed"]:
        notes.append(
            "Price isn't meaningfully above its 52-week low -- in Minervini's words, this risks "
            "being 'dead money' rather than a stock building real strength off a bottom."
        )

    c7 = by_num.get(7)
    if c7 and c7["applicable"] and not c7["passed"]:
        notes.append(
            "Price is more than 25% below its 52-week high -- not currently acting like a "
            "market leader, which is what SEPA-style setups are meant to identify."
        )
    elif c7 and c7["applicable"] and c7["passed"]:
        notes.append(
            "Price is within 25% of its 52-week high -- consistent with leadership behavior "
            "(bases should form near highs, not deep in a drawdown)."
        )

    c8 = by_num.get(8)
    if c8 and c8["applicable"] and not c8["passed"]:
        notes.append(
            "Relative strength (proxy) is lagging Nifty 50 over the trailing ~3 months -- "
            "weaker than the 'outperforming the market' quality SEPA calls for. Treat this "
            "column skeptically either way -- it's a crude return-comparison, not a true "
            "percentile RS rating."
        )

    rsi = r.get("rsi14")
    if rsi is not None:
        if rsi >= 80:
            notes.append(
                f"RSI(14) is {rsi:.1f} -- quite extended. Chasing here risks buying into the "
                "top of a move rather than a proper low-volatility pivot; better to wait for a "
                "tightening pullback than to buy strength alone."
            )
        elif rsi < 45:
            notes.append(
                f"RSI(14) is {rsi:.1f} -- momentum is soft for a textbook VCP breakout candidate."
            )

    ath = r.get("all_time_high")
    close = r.get("close")
    if ath and close and close < ath * 0.5:
        notes.append(
            f"Current price ({close:,.2f}) is still less than half its all-time high "
            f"({ath:,.2f}) -- if a base is genuinely forming here, it's a recovery/turnaround "
            "setup off a depressed level, not a breakout to fresh all-time highs. Both are "
            "tradeable under this system, but they carry different risk profiles."
        )

    notes.append(
        "None of the above confirms an actual Volatility Contraction Pattern is present -- "
        "the Trend Template only checks trend/strength context. Look at the real chart for "
        "the base itself: 2-4 progressively tighter pullbacks with drying volume, culminating "
        "in a pivot breakout on rising volume (see wiki/strategies/vcp-volatility-contraction-pattern.md)."
    )
    return notes


def build_stock_detail_html(r):
    ticker = r["nsecode"]
    criteria = r.get("trend_template_criteria") or []

    rows = []
    for c in criteria:
        if not c["applicable"]:
            icon, css = "?", "na"
        elif c["passed"]:
            icon, css = "✅", "pos"
        else:
            icon, css = "❌", "neg"
        rows.append(f"""
        <tr>
          <td>{c['num']}</td>
          <td>{c['desc']}</td>
          <td>{c['reading']}</td>
          <td class="{css}" style="text-align:center">{icon}</td>
        </tr>""")

    commentary_items = "".join(f"<li>{n}</li>" for n in _trend_template_commentary(r))

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Trend Template Assessment - {ticker}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; background: #0f1117; color: #e6e6e6; padding: 24px; max-width: 900px; margin: 0 auto; }}
  h1 {{ font-size: 18px; }}
  a.back {{ color: #8ab4f8; text-decoration: none; font-size: 13px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin: 16px 0 24px; }}
  th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #2a2d34; vertical-align: top; }}
  th {{ background: #1a1d24; }}
  .pos {{ color: #4caf50; }}
  .neg {{ color: #f44336; }}
  .na {{ color: #9aa0a6; }}
  h2 {{ font-size: 15px; margin-top: 24px; }}
  li {{ margin-bottom: 8px; line-height: 1.4; }}
</style></head>
<body>
  <a class="back" href="../index.html">&larr; back to dashboard</a>
  <h1>Trend Template Assessment &mdash; {ticker}</h1>
  <div style="color:#9aa0a6;font-size:13px;margin-bottom:16px;">
    {r.get('name','')} &middot; Score: {r.get('trend_template_score') or '-'} &middot;
    generated {_ist_timestamp()}
  </div>
  <table>
    <thead><tr><th>#</th><th>Criterion</th><th>Reading</th><th>Result</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
  <h2>Commentary (Minervini VCP context)</h2>
  <ul>{commentary_items}</ul>
</body></html>"""


def _ticker_snapshot(r):
    """Slim per-ticker record persisted to previous_scan.json -- enough for
    both the changes-since-last-run section and the on-demand watchlist
    generator (tools/vcp_watchlist.py) without either needing a fresh scan."""
    s = r.get("screener") or {}
    flag = r.get("buy_sell_flag") or {}
    return {
        "name": r.get("name", ""),
        "sector": s.get("sector"),
        "close": r.get("close"),
        "flag": flag.get("flag"),
        "trend_template_score": r.get("trend_template_score"),
    }


def compute_scan_changes(enriched_matches, previous):
    """None on the very first run (nothing to compare against). Otherwise a
    dict with the previous run's timestamp plus which tickers were newly
    added or dropped since then -- the scan is re-run fresh each time, so the
    match list naturally shifts as price action changes day to day."""
    if previous is None:
        return None
    current = {r["nsecode"]: _ticker_snapshot(r) for r in enriched_matches}
    prev = previous.get("tickers", {})
    added = sorted(current.keys() - prev.keys())
    removed = sorted(prev.keys() - current.keys())
    return {
        "previous_run_time": previous.get("run_time", "unknown"),
        "added": [(t, current[t]["name"]) for t in added],
        "removed": [(t, prev[t]["name"] if isinstance(prev[t], dict) else prev[t]) for t in removed],
    }


def build_changes_html(changes):
    if changes is None:
        return '<div class="note">First run -- no previous scan to compare against yet.</div>'

    added, removed = changes["added"], changes["removed"]
    if not added and not removed:
        return (f'<div class="note">No changes since the last run '
                 f'({changes["previous_run_time"]}) -- same list of matches.</div>')

    def item_list(pairs, cls):
        if not pairs:
            return '<span class="note">none</span>'
        return ", ".join(
            f'<a class="{cls}" href="https://www.screener.in/company/{t}/consolidated/" target="_blank">{t}</a>'
            f' ({n})' for t, n in pairs
        )

    return f"""
    <div class="changes">
      <h2>Changes since last run ({changes['previous_run_time']})</h2>
      <div><strong class="pos">Added ({len(added)}):</strong> {item_list(added, 'pos')}</div>
      <div><strong class="neg">Removed ({len(removed)}):</strong> {item_list(removed, 'neg')}</div>
    </div>"""


def _link_cell(value, url):
    """A cell that carries a hyperlink -- rendered as plain text in the CSV
    export (no hyperlink support in that format) but a real clickable link
    in the Excel export."""
    return {"v": value, "url": url}


def _export_row(r):
    """One flat row shared by both the client-side CSV and Excel exports --
    raw numeric values (not the comma-formatted/combined display strings
    used in the HTML table) so a spreadsheet can actually sort/filter on
    them, plus real hyperlinks on Symbol/Name/Trend Template (Excel only)."""
    s = r.get("screener") or {}
    flag = r.get("buy_sell_flag") or {}
    ticker = r["nsecode"]
    chartink_url = f"https://chartink.com/stocks/{ticker.lower()}.html"
    screener_url = f"https://www.screener.in/company/{ticker}/consolidated/"
    detail_url = f"{PUBLIC_BASE_URL}details/{ticker}.html"

    def series(values):
        if not values:
            return ""
        return " | ".join(str(v) for v in values)

    return {
        "Symbol": _link_cell(ticker, chartink_url),
        "Name": _link_cell(r.get("name", ""), screener_url),
        "Sector": s.get("sector") or "",
        "Close": r.get("close"),
        "ATH": r.get("all_time_high"),
        "ATL": r.get("all_time_low"),
        "Stock P/E": s.get("stock_pe"),
        "Flag": flag.get("flag", ""),
        "ROCE %": s.get("roce"),
        "ROE %": s.get("roe"),
        "RSI(14)": r.get("rsi14"),
        "Net Profit Qtr last 4 (Cr)": series(s.get("net_profit_qtr")),
        "Net Profit Year last 4 (Cr)": series(s.get("net_profit_year")),
        "OPM % Year last 4": series(s.get("opm_pct_year")),
        "Reserves Year last 4 (Cr)": series(s.get("reserves_year")),
        "CFO Year last 4 (Cr)": series(s.get("cash_from_ops_year")),
        "Debtor Days": s.get("debtor_days"),
        "Promoter %": s.get("shareholding_promoter"),
        "FII %": s.get("shareholding_fii"),
        "DII %": s.get("shareholding_dii"),
        "Public %": s.get("shareholding_public"),
        "Trend Template": _link_cell(r.get("trend_template_score") or "", detail_url),
        "Listing Age (days)": r.get("listing_age_days"),
    }


def build_dashboard_html(enriched_matches, changes=None):
    rows = []
    for r in enriched_matches:
        s = r.get("screener") or {}
        shareholding = " / ".join(
            f"{label} {_fmt(val, '%')}" for label, val in [
                ("P", s.get("shareholding_promoter")), ("FII", s.get("shareholding_fii")),
                ("DII", s.get("shareholding_dii")), ("Pub", s.get("shareholding_public")),
            ] if val is not None
        ) or "-"

        rows.append(f"""
        <tr>
          <td class="sticky-col"><a href="https://chartink.com/stocks/{r['nsecode'].lower()}.html" target="_blank">{r['nsecode']}</a></td>
          <td><a href="https://www.screener.in/company/{r['nsecode']}/consolidated/" target="_blank">{r.get('name', '')}</a></td>
          <td>{s.get('sector') or '-'}</td>
          <td class="num">{_fmt(r.get('close'))}</td>
          <td class="num">{_fmt_pair(r.get('all_time_high'), r.get('all_time_low'))}</td>
          <td class="num">{_fmt(s.get('stock_pe'))}</td>
          <td class="flag-cell">{f'<span class="flag {r["buy_sell_flag"]["css"]}">{r["buy_sell_flag"]["flag"]}</span>' if r.get('buy_sell_flag') else '-'}</td>
          <td class="num">{_fmt(s.get('roce'), '%')}</td>
          <td class="num">{_fmt(s.get('roe'), '%')}</td>
          <td class="num">{_fmt(r.get('rsi14'))}</td>
          <td class="num">{_fmt_series(s.get('net_profit_qtr'))}</td>
          <td class="num">{_fmt_series(s.get('net_profit_year'))}</td>
          <td class="num">{_fmt_series(s.get('opm_pct_year'), '%')}</td>
          <td class="num">{_fmt_series(s.get('reserves_year'))}</td>
          <td class="num">{_fmt_series(s.get('cash_from_ops_year'))}</td>
          <td class="num">{_fmt(s.get('debtor_days'))}</td>
          <td>{shareholding}</td>
          <td class="num"><a href="details/{r['nsecode']}.html" target="_blank" title="Full criterion-by-criterion breakdown + commentary">{r.get('trend_template_score') or '-'}</a></td>
          <td class="num">{_fmt_age(r.get('listing_age_days'))}</td>
        </tr>""")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>VCP Scan Dashboard</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; background: #0f1117; color: #e6e6e6; padding: 24px; }}
  h1 {{ font-size: 20px; }}
  .meta {{ color: #9aa0a6; font-size: 13px; margin-bottom: 8px; }}
  .note {{ color: #9aa0a6; font-size: 12px; margin-bottom: 16px; max-width: 1000px; }}
  .table-wrap {{ overflow-x: auto; border: 1px solid #2a2d34; border-radius: 6px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 12px; white-space: nowrap; }}
  th, td {{ padding: 6px 10px; text-align: left; border-bottom: 1px solid #2a2d34; }}
  th {{ background: #1a1d24; position: sticky; top: 0; cursor: default; font-weight: 600; }}
  thead tr.group-row th {{ text-align: center; font-size: 11px; color: #9aa0a6; background: #12141a;
                            border-bottom: 1px solid #2a2d34; top: 0; }}
  thead tr.col-row th {{ top: 21px; }}
  .sticky-col {{ position: sticky; left: 0; background: #0f1117; z-index: 1; }}
  th.sticky-col {{ z-index: 3; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .pos {{ color: #4caf50; }}
  .neg {{ color: #f44336; }}
  a {{ color: #8ab4f8; text-decoration: none; }}
  tr:hover td {{ background: #15181f; }}
  .flag-cell {{ text-align: center; }}
  .flag {{ display: inline-block; padding: 2px 10px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
  .flag-green {{ background: #1b3a24; color: #4caf50; }}
  .flag-amber {{ background: #3a2f14; color: #ffb300; }}
  .flag-red {{ background: #3a1a1a; color: #f44336; }}
  .changes {{ background: #171a21; border: 1px solid #2a2d34; border-radius: 6px;
              padding: 12px 16px; margin-bottom: 16px; font-size: 13px; }}
  .changes h2 {{ font-size: 14px; margin: 0 0 8px; }}
  .changes div {{ margin-bottom: 4px; }}
  .footer {{ margin-top: 20px; padding-top: 16px; border-top: 1px solid #2a2d34;
             color: #9aa0a6; font-size: 12px; max-width: 1000px; }}
  .footer p {{ margin: 0 0 8px; }}
  .top-bar {{ display: flex; justify-content: space-between; align-items: flex-start;
              flex-wrap: wrap; gap: 12px; margin-bottom: 8px; }}
  .top-right {{ text-align: right; font-size: 12px; color: #9aa0a6; }}
  #refresh-btn, #csv-btn, #excel-btn {{ background: #1a1d24; color: #e6e6e6; border: 1px solid #2a2d34;
                  border-radius: 4px; padding: 6px 14px; font-size: 13px; cursor: pointer; }}
  #refresh-btn:hover:not(:disabled), #csv-btn:hover, #excel-btn:hover {{ background: #22262f; }}
  #refresh-btn:disabled {{ opacity: 0.5; cursor: default; }}
  #refresh-status {{ display: block; margin-top: 4px; max-width: 260px; }}
  #visitor-counts {{ margin-top: 8px; }}
</style></head>
<body>
  <div class="top-bar">
    <h1 style="margin:0">VCP Scan Dashboard</h1>
    <div class="top-right">
      <button id="refresh-btn" onclick="triggerRefresh()">&#8635; Refresh scan</button>
      <button id="csv-btn" onclick="downloadCsv()">&#8681; CSV</button>
      <button id="excel-btn" onclick="downloadExcel()">&#8681; Excel</button>
      <span id="refresh-status"></span>
      <div id="visitor-counts" title="Counter data can take up to 4 hours to update -- a GoatCounter free-tier caching limit, not a bug">Visitors today: <span id="vc-today">-</span> &middot; All-time: <span id="vc-total">-</span> <span style="opacity:0.6">(may lag up to 4h)</span></div>
    </div>
  </div>
  <div class="meta">{len(enriched_matches)} matches &middot; generated {_ist_timestamp()} &middot;
    scan logic: <a href="https://github.com/" target="_blank">wiki/strategies/vcp-screening-tools.md</a></div>
  {build_changes_html(changes)}
  <div class="note">Sorted by Flag (green &gt; amber &gt; red), then youngest-to-oldest by
    listing date within each flag group. Fundamentals (Sector, P/E, ROCE, ROE, financials, shareholding) are
    scraped from screener.in and are best-effort -- some fields may show "-" for banks/NBFCs or newer
    listings whose report layout differs. Sector P/E was dropped (not just shown as "-") -- screener.in
    loads its peer/sector comparison table via JavaScript after page load, not reachable without full
    browser automation, and no working public endpoint was found for it either.
    <strong>Flag</strong>: Trend (green) = price above the 10-day MA (short-term strength); Watch (amber) =
    price between the 50-day and 10-day MA (pullback within an intact trend); Away (red) = price below the
    50-day MA (intermediate trend broken) -- a fast, mechanical read, not a substitute for the Trend
    Template/VCP read next to it. Qtr/annual financial columns show up to the last 4 available periods,
    oldest to newest, separated by "|" -- fewer than 4 for recently-listed stocks without that much
    history. "Trend Template" = how many of the 8 criteria (price/50/150/200-day MA stack, 200-MA
    uptrend, 52-week high/low proximity, relative strength) the stock passes -- click a score for the full
    breakdown. Criterion 8 there is a crude proxy (beats Nifty 50's 3-month return), not a true percentile
    RS rating, and not the same thing as the RSI(14) column here.</div>
  <div class="table-wrap">
  <table>
    <thead>
      <tr class="group-row">
        <th colspan="3">Identity</th>
        <th colspan="2">Price (&#8377;)</th>
        <th colspan="2">Valuation / Signal</th>
        <th colspan="3">Quality</th>
        <th colspan="1">Profit, Qtr (&#8377;Cr)</th>
        <th colspan="4">Profit &amp; Financials, Annual (&#8377;Cr)</th>
        <th colspan="1">Efficiency</th>
        <th colspan="1">Shareholding %</th>
        <th colspan="2">VCP</th>
      </tr>
      <tr class="col-row">
        <th class="sticky-col">Symbol</th><th>Name</th><th>Sector</th>
        <th>Close</th><th>ATH | ATL</th>
        <th>Stock P/E</th><th>Flag</th>
        <th>ROCE</th><th>ROE</th><th>RSI(14)</th>
        <th>Net Profit (last 4 Qtr)</th>
        <th>Net Profit (last 4 Yr)</th><th>OPM % (last 4 Yr)</th><th>Reserves (last 4 Yr)</th><th>CFO (last 4 Yr)</th>
        <th>Debtor Days</th>
        <th>Promoter / FII / DII / Public</th>
        <th>Trend Template</th><th>Listed</th>
      </tr>
    </thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
  </div>
  <div class="footer">
    <p><a href="https://github.com/saketspec-ship-it/vcp-dashboard/blob/main/BUILD.md" target="_blank">How this dashboard was built</a> -- a step-by-step writeup of the full pipeline (Chartink scan, Yahoo Finance/screener.in enrichment, GitHub Pages hosting, Telegram bot, GitHub Actions + Cloudflare Worker for the public Refresh button, GoatCounter visitor count).</p>
    <p><strong>Disclaimer:</strong> This dashboard is for educational and informational purposes only and demonstrates a
    rule-based stock screening methodology. The stocks displayed are not investment recommendations or buy/sell advice.
    Please do your own research or consult a SEBI-registered professional before investing. Investing in securities is
    subject to market risks.</p>
  </div>
  <script type="application/json" id="scan-data">{json.dumps([_export_row(r) for r in enriched_matches])}</script>
  <script>
  // Cells that carry a hyperlink are objects {{v, url}} (see _link_cell in
  // the Python build script) -- the CSV export only ever shows the plain
  // value (no hyperlink support in that format); the Excel export below
  // turns the url into a real clickable link.
  function cellValue(v) {{
    return (v && typeof v === 'object') ? v.v : v;
  }}

  function triggerDownload(blob, filename) {{
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }}

  function downloadCsv() {{
    var data = JSON.parse(document.getElementById('scan-data').textContent);
    if (!data.length) return;
    var headers = Object.keys(data[0]);
    function escapeCell(v) {{
      v = cellValue(v);
      if (v === null || v === undefined) v = '';
      return '"' + String(v).replace(/"/g, '""') + '"';
    }}
    var lines = [headers.map(escapeCell).join(',')];
    data.forEach(function(row) {{
      lines.push(headers.map(function(h) {{ return escapeCell(row[h]); }}).join(','));
    }});
    var blob = new Blob([lines.join('\\r\\n')], {{type: 'text/csv;charset=utf-8;'}});
    triggerDownload(blob, 'vcp_scan_' + new Date().toISOString().slice(0, 10) + '.csv');
  }}

  // Genuine Excel "SpreadsheetML" 2003 XML format -- no external library
  // needed (unlike a real .xlsx, which is a zip archive), opens cleanly in
  // Excel/Google Sheets with no format-mismatch warning, and (unlike CSV)
  // supports real per-cell hyperlinks via ss:HRef.
  function downloadExcel() {{
    var data = JSON.parse(document.getElementById('scan-data').textContent);
    if (!data.length) return;
    var headers = Object.keys(data[0]);
    function escapeXml(s) {{
      return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&apos;');
    }}
    function cellXml(raw) {{
      var url = (raw && typeof raw === 'object') ? raw.url : null;
      var v = cellValue(raw);
      var isNum = typeof v === 'number';
      var type = isNum ? 'Number' : 'String';
      var text = (v === null || v === undefined) ? '' : v;
      var hrefAttr = url ? ' ss:HRef="' + escapeXml(url) + '"' : '';
      return '<Cell' + hrefAttr + '><Data ss:Type="' + type + '">' + escapeXml(text) + '</Data></Cell>';
    }}
    var headerRow = '<Row>' + headers.map(function(h) {{
      return '<Cell><Data ss:Type="String">' + escapeXml(h) + '</Data></Cell>';
    }}).join('') + '</Row>';
    var dataRows = data.map(function(row) {{
      return '<Row>' + headers.map(function(h) {{ return cellXml(row[h]); }}).join('') + '</Row>';
    }}).join('');
    var xml = '<?xml version="1.0"?>' +
      '<?mso-application progid="Excel.Sheet"?>' +
      '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" ' +
      'xmlns:o="urn:schemas-microsoft-com:office:office" ' +
      'xmlns:x="urn:schemas-microsoft-com:office:excel" ' +
      'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">' +
      '<Worksheet ss:Name="VCP Scan"><Table>' + headerRow + dataRows + '</Table></Worksheet>' +
      '</Workbook>';
    var blob = new Blob([xml], {{type: 'application/vnd.ms-excel;charset=utf-8;'}});
    triggerDownload(blob, 'vcp_scan_' + new Date().toISOString().slice(0, 10) + '.xls');
  }}

  function triggerRefresh() {{
    var btn = document.getElementById('refresh-btn');
    var status = document.getElementById('refresh-status');
    btn.disabled = true;
    status.textContent = 'Triggering a full re-scan...';
    // Routed through a small Cloudflare Worker that holds the real GitHub
    // token server-side -- nothing secret is embedded in this page.
    fetch('{REFRESH_WORKER_URL}', {{ method: 'POST' }})
      .then(function(r) {{ return r.text().then(function(t) {{ return {{ok: r.ok, text: t}}; }}); }})
      .then(function(result) {{
        if (result.ok) {{
          status.textContent = 'Triggered. Takes ~60-90s -- reload the page shortly.';
        }} else {{
          status.textContent = 'Trigger failed: ' + result.text;
          btn.disabled = false;
        }}
      }}).catch(function() {{
        status.textContent = 'Trigger failed (network error). Try again in a bit.';
        btn.disabled = false;
      }});
    setTimeout(function() {{ btn.disabled = false; }}, 90000);
  }}

  // GoatCounter: records + displays today's and lifetime unique visitor
  // counts. Two separate tracked "paths" -- a fixed one for lifetime, and
  // one keyed by today's date -- so the daily figure naturally resets each
  // day without needing a private API key client-side.
  (function() {{
    var goatBase = 'https://{GOATCOUNTER_SITE}.goatcounter.com';
    var today = new Date().toISOString().slice(0, 10);
    function record(path) {{
      var img = new Image();
      img.src = goatBase + '/count?p=' + encodeURIComponent(path) + '&t=' + encodeURIComponent(document.title);
    }}
    function showCount(path, elId) {{
      fetch(goatBase + '/counter/' + encodeURIComponent(path) + '.json')
        .then(function(r) {{ return r.json(); }})
        .then(function(d) {{ document.getElementById(elId).textContent = d.count || '0'; }})
        .catch(function() {{ document.getElementById(elId).textContent = '?'; }});
    }}
    record('/lifetime');
    record('/daily/' + today);
    showCount('/lifetime', 'vc-total');
    showCount('/daily/' + today, 'vc-today');
  }})();
  </script>
</body></html>"""


# ---------------------------------------------------------------- Telegram --

def send_telegram_text(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[not sent] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID secrets not set.", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    with request.urlopen(request.Request(url, data=body)) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram send failed: {result}")


# --------------------------------------------------------------------- main --

def main():
    """Writes index.html + details/*.html directly into the current checkout
    (the workflow's own git steps handle add/commit/push -- this script only
    needs to produce the files, not push them, since it's already running
    inside the target repo in Actions). Includes a cooldown check so a burst
    of Refresh-button clicks / repository_dispatch events can't flood scans."""
    if PREVIOUS_SCAN_PATH.exists():
        previous = json.loads(PREVIOUS_SCAN_PATH.read_text())
        last_run = previous.get("run_epoch", 0)
        if time.time() - last_run < COOLDOWN_SECONDS:
            print(f"Skipped: last scan was <{COOLDOWN_SECONDS}s ago (cooldown).")
            return
    else:
        previous = None

    matches = fetch_vcp_matches()
    print(f"Chartink: {len(matches)} matches")

    enriched = enrich_all(matches)
    # Primary: Flag (green/Trend=most actionable first, then amber/Watch, then
    # red/Away; unknown flag sorts last). Secondary, within each flag group:
    # youngest-to-oldest by IPO listing date (unknown age sorts last within
    # its flag group rather than being wrongly treated as youngest/oldest).
    def sort_key(r):
        flag = r.get("buy_sell_flag")
        flag_priority = flag["priority"] if flag else 3
        age = r.get("listing_age_days")
        return (flag_priority, age is None, age)
    enriched.sort(key=sort_key)

    changes = compute_scan_changes(enriched, previous)
    html = build_dashboard_html(enriched, changes)
    DASHBOARD_PATH.write_text(html, encoding="utf-8")
    (REPO_ROOT / ".nojekyll").touch()

    save_data = {
        "run_time": _ist_timestamp(),
        "run_epoch": time.time(),
        "tickers": {r["nsecode"]: _ticker_snapshot(r) for r in enriched},
    }
    PREVIOUS_SCAN_PATH.write_text(json.dumps(save_data, indent=2), encoding="utf-8")

    details_dir = REPO_ROOT / "details"
    details_dir.mkdir(exist_ok=True)
    for r in enriched:
        (details_dir / f"{r['nsecode']}.html").write_text(build_stock_detail_html(r), encoding="utf-8")
    print(f"Dashboard + {len(enriched)} detail pages written.")

    pages_url = f"https://{GITHUB_REPO_OWNER}.github.io/{GITHUB_REPO_NAME}/"
    # Cache-busting query param -- see tools/vcp_scanner_telegram.py's
    # run_scan_and_notify for why: GitHub Pages sets Cache-Control:
    # max-age=600 on the same URL every scan, so without this a fresh
    # Telegram link can still show a browser-cached, stale page.
    fresh_url = f"{pages_url}?t={int(time.time())}"
    # GitHub Actions sets GITHUB_EVENT_NAME automatically -- label the
    # notification by what actually triggered this run (cron schedule,
    # someone clicking the public Refresh button, or a manual
    # workflow_dispatch) instead of always claiming it was the button.
    trigger_label = {
        "schedule": "scheduled run",
        "repository_dispatch": "triggered via Refresh button",
        "workflow_dispatch": "manual trigger",
    }.get(os.environ.get("GITHUB_EVENT_NAME"), "cloud run")
    send_telegram_text(f"VCP scan ({trigger_label}) - {len(enriched)} matches.\n{fresh_url}")
    print(f"Notified Telegram: {fresh_url}")


if __name__ == "__main__":
    main()

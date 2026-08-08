"""
US MARKET dashboard -- runs inside the vcp-dashboard repo's own GitHub
Actions workflow (.github/workflows/refresh_us.yml), triggered by the US
dashboard's public "Refresh scan" button (a repository_dispatch event of
type `refresh_us`), its own cron schedule, or manually via workflow_dispatch.
Publishes to the `us/` subpath of the same GitHub Pages site as the Indian
dashboard (which is served from the repo root by cloud_scan.py).

This is the US-market sibling of cloud_scan.py. The Yahoo-Finance technical
enrichment, the Trend Template re-check, the per-stock detail pages, and the
whole dashboard HTML/CSS/JS (sortable + Excel-style filterable columns,
CSV/Excel export, changes-since-last-run) are the same proven code as the
Indian pipeline -- so **if you fix a bug in the shared scan/enrich/render
logic, fix it in cloud_scan.py and tools/vcp_scanner_telegram.py too**. What
differs for the US:
  - The universe/scan + fundamentals come from TradingView's public scanner
    API (scanner.tradingview.com/america/scan) instead of Chartink (scan) +
    screener.in (fundamentals). One unauthenticated POST returns both the
    Trend-Template-filtered universe AND the fundamentals (P/E, market cap,
    sector, ROIC, ROE, net-income/revenue history arrays, debt/equity, etc.).
  - Yahoo symbols are the bare ticker ("AAPL"), no ".NS" suffix.
  - The relative-strength benchmark is the S&P 500 (^GSPC), not the Nifty 50.
  - Columns are US-appropriate: ROCE->ROIC, Net Profit->Net Income,
    Reserves->Revenue, and the India-only Debtor Days + Promoter/FII/DII/Public
    shareholding block -> Debt/Equity, Current Ratio, Gross Margin, P/B.
  - Prices are in USD ($), market cap / financials in USD millions.

Config (via GitHub Actions repo secrets): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
A cooldown check at the top of main() skips a run whose predecessor was too
recent, so a spam-clicked Refresh button can't flood TradingView/Yahoo.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import request, parse, error

# GitHub Actions runners run in UTC -- convert to IST for display so the
# "generated" timestamp matches the Indian dashboard's clock (the user is in
# India even though these are US stocks).
_IST = timezone(timedelta(hours=5, minutes=30))


def _ist_timestamp():
    return datetime.now(timezone.utc).astimezone(_IST).strftime("%Y-%m-%d %H:%M IST")


REPO_ROOT = Path(__file__).parent
US_DIR = REPO_ROOT / "us"
TRADINGVIEW_URL = "https://scanner.tradingview.com/america/scan"
DASHBOARD_PATH = US_DIR / "index.html"
PREVIOUS_SCAN_PATH = US_DIR / "previous_scan.json"
COOLDOWN_SECONDS = 120

GITHUB_REPO_OWNER = "saketspec-ship-it"
GITHUB_REPO_NAME = "vcp-dashboard"
GOATCOUNTER_SITE = "vcpdash"

# Absolute base for links embedded in exported CSV/Excel files (the on-page
# detail links are relative). Points at the US subpath.
PUBLIC_BASE_URL = f"https://{GITHUB_REPO_OWNER}.github.io/{GITHUB_REPO_NAME}/us/"

# Same Cloudflare Worker as the Indian dashboard, but the US Refresh button
# calls it with ?market=us so the Worker dispatches a `refresh_us` event
# (the Indian button omits the param and gets the default `refresh`).
REFRESH_WORKER_URL = "https://vcp-refresh-proxy.saket-spec.workers.dev/?market=us"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# How many matches to keep after the Trend-Template filter (sorted by market
# cap desc). Keeps the page + Yahoo enrichment time bounded, like the ~68 the
# Indian scan returns.
MAX_MATCHES = 80

# TradingView scanner server-side pre-filter: liquidity + the Minervini
# 50/150/200 moving-average stack + price above the 50-day. The 52-week-high
# proximity (within 25%) and 52-week-low distance (30%+ above) are applied in
# Python afterwards (TradingView's simple filter can't express field*constant
# comparisons), and the full 8-criterion Trend Template is independently
# re-checked from Yahoo history per stock, same as the Indian dashboard.
TV_FILTER = [
    {"left": "close", "operation": "greater", "right": 10},
    {"left": "average_volume_90d_calc", "operation": "greater", "right": 1000000},
    {"left": "close", "operation": "greater", "right": "SMA50"},
    {"left": "SMA50", "operation": "greater", "right": "SMA150"},
    {"left": "SMA150", "operation": "greater", "right": "SMA200"},
]
# Column order requested from TradingView; parsed positionally in fetch_us_matches.
TV_COLUMNS = [
    "name", "description", "sector", "close", "price_52_week_high", "price_52_week_low",
    "price_earnings_ttm", "market_cap_basic", "return_on_invested_capital", "return_on_equity",
    "net_income_fq_h", "net_income_fy_h", "total_revenue_fy_h", "operating_margin_ttm",
    "cash_f_operating_activities_ttm", "debt_to_equity", "current_ratio", "gross_margin_ttm",
    "price_book_fq",
]


# ------------------------------------------------------------- TradingView --

def _tv_series(arr, n=4):
    """TradingView returns fundamental history newest-first (e.g. the latest
    reported period at index 0). Take the most recent n, drop Nones, and
    reverse to oldest->newest so it renders left-to-right like the Indian
    dashboard's last-4 columns."""
    if not isinstance(arr, list):
        return []
    recent = [v for v in arr[:n] if v is not None]
    return list(reversed(recent))


def _cr(v):
    """USD -> millions, for market cap / financials shown in $M. TradingView
    returns these as raw dollars."""
    return None if v is None else v / 1e6


def fetch_us_matches():
    """One POST to TradingView's public scanner returns the Trend-Template-
    filtered US universe *and* its fundamentals. Each returned row already
    carries everything the dashboard's fundamentals columns need, so (unlike
    the Indian pipeline) there's no per-stock fundamentals scrape -- only the
    Yahoo technical enrichment per stock afterwards.

    Applies the 52-week-high-proximity (within 25%) and 52-week-low-distance
    (30%+ above) checks in Python here, sorts by market cap, and caps to
    MAX_MATCHES. Returns dicts shaped like the Indian matches (nsecode/name/
    close plus a nested `screener` fundamentals block) so the shared row/
    export/snapshot builders work unchanged."""
    payload = {
        "filter": TV_FILTER,
        "columns": TV_COLUMNS,
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
        "range": [0, 600],
    }
    req = request.Request(
        TRADINGVIEW_URL,
        data=json.dumps(payload).encode(),
        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
    )
    with request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    idx = {c: i for i, c in enumerate(TV_COLUMNS)}
    matches = []
    for row in data.get("data", []):
        d = row["d"]
        def col(name):
            return d[idx[name]] if idx.get(name) is not None else None
        close = col("close")
        hi52, lo52 = col("price_52_week_high"), col("price_52_week_low")
        if close is None or hi52 is None or lo52 is None:
            continue
        # Minervini: within 25% of the 52-week high AND at least 30% above the
        # 52-week low.
        if not (close >= 0.75 * hi52 and close >= 1.3 * lo52):
            continue
        ticker = col("name")
        exchange = row["s"].split(":")[0] if ":" in row["s"] else ""
        matches.append({
            "nsecode": ticker,          # bare ticker; key name kept for shared code
            "exchange": exchange,
            "name": col("description") or ticker,
            "close": close,
            "screener": {
                "sector": col("sector"),
                "stock_pe": col("price_earnings_ttm"),
                "market_cap": _cr(col("market_cap_basic")),
                "roce": col("return_on_invested_capital"),   # ROIC in the ROCE slot
                "roe": col("return_on_equity"),
                "net_profit_qtr": [_cr(v) for v in _tv_series(col("net_income_fq_h"))],
                "net_profit_year": [_cr(v) for v in _tv_series(col("net_income_fy_h"))],
                "revenue_year": [_cr(v) for v in _tv_series(col("total_revenue_fy_h"))],
                "opm_ttm": col("operating_margin_ttm"),
                "cfo_ttm": _cr(col("cash_f_operating_activities_ttm")),
                "debt_to_equity": col("debt_to_equity"),
                "current_ratio": col("current_ratio"),
                "gross_margin": col("gross_margin_ttm"),
                "price_book": col("price_book_fq"),
            },
        })

    matches.sort(key=lambda m: (m["screener"]["market_cap"] or 0), reverse=True)
    return matches[:MAX_MATCHES]


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
    """Yahoo-Finance technical enrichment for one US ticker: prev_close,
    rsi14, all_time_high/low, the 8-criterion Trend Template re-check (+ its
    per-criterion detail for the stock page), listing age, buy/sell flag,
    listing price. Fundamentals are NOT fetched here for the US dashboard --
    they already came from TradingView in fetch_us_matches and are merged in
    enrich_all. Missing/failed fields stay None; one bad symbol shouldn't
    kill the run."""
    yf_symbol = nsecode  # US tickers are bare on Yahoo, no ".NS" suffix
    result = {
        "prev_close": None, "rsi14": None, "all_time_high": None, "all_time_low": None,
        "trend_template_score": None, "trend_template_criteria": None, "listing_age_days": None,
        "buy_sell_flag": None, "listing_price": None,
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
        closes, highs, lows = _extract_ohlc(r)
        result["all_time_high"] = max(highs) if highs else None
        result["all_time_low"] = min(lows) if lows else None
        # Earliest available monthly close as a listing-price proxy -- Yahoo's
        # history for a stock only starts around its listing date, so the
        # first bar's close is a reasonable stand-in for the actual IPO price.
        result["listing_price"] = closes[0] if closes else None
    except Exception:
        pass

    return result


def get_nifty_return_pct(period=63):
    """S&P 500's own trailing return over `period` trading days (~3 months),
    the relative-strength benchmark for the US Trend Template (the Indian
    dashboard uses the Nifty 50; kept the function name for shared code)."""
    body = _yahoo_get(
        "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?interval=1d&range=1y"
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
    # Each match already carries its TradingView `screener` fundamentals; the
    # spread keeps that block (enrich_symbol only adds Yahoo technicals).
    benchmark_return_pct = get_nifty_return_pct()
    enriched = []
    for m in matches:
        data = enrich_symbol(m["nsecode"], benchmark_return_pct)
        enriched.append({**m, **data})
        time.sleep(0.5)  # be polite to Yahoo's unauthenticated endpoint
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


def _last(series):
    """Most recent value of a last-4-periods series -- what column sorting
    keys off of, since sorting a pipe-joined multi-value cell only makes
    sense against its latest figure."""
    return series[-1] if series else None


def _sort_attr(value):
    """HTML attribute value for a data-sort cell -- empty string (not "None")
    so the client-side sort JS can detect and always push missing values to
    the end regardless of sort direction."""
    return "" if value is None else value


def _tt_score_num(r):
    score = r.get("trend_template_score")
    if not score:
        return None
    try:
        return int(score.split("/")[0])
    except (ValueError, IndexError):
        return None


# Single source of truth for the column list, in display order. Drives both
# the sortable/filterable header cells (_build_col_headers) and keeps the
# header aligned with the <td> order in build_dashboard_html's row loop --
# if you add/reorder a column, change it here AND in that row loop.
#   sort_numeric  -- sort by parseFloat(data-sort) vs. string localeCompare
#   filter_numeric -- filter box accepts >N/<N/N-M numeric operators vs. a
#                     plain substring match (Flag/Listed sort numerically but
#                     read more naturally as a text filter: "Trend", "2y").
COLUMNS = [
    ("Symbol", False, False),
    ("Name", False, False),
    ("Sector", False, False),
    ("Close ($)", True, True),
    ("ATH | ATL", True, True),
    ("Stock P/E", True, True),
    ("Market Cap ($M)", True, True),
    ("Flag", True, False),
    ("ROIC", True, True),
    ("ROE", True, True),
    ("RSI(14)", True, True),
    ("Net Income (last 4 Qtr, $M)", True, True),
    ("Net Income (last 4 Yr, $M)", True, True),
    ("Revenue (last 4 Yr, $M)", True, True),
    ("Op Margin % (TTM)", True, True),
    ("CFO (TTM, $M)", True, True),
    ("Debt / Equity", True, True),
    ("Current Ratio", True, True),
    ("Gross Margin %", True, True),
    ("Price / Book", True, True),
    ("Trend Template", True, True),
    ("Listed", True, False),
    ("Listing Price", True, True),
]


def _build_col_headers():
    """Renders the col-row header cells: each is a clickable sort label plus
    an Excel-style filter button (opens a searchable checklist dropdown of
    the column's distinct values). Generated (not hand-written) so the 24
    columns stay consistent and the sort indices can't drift."""
    cells = []
    for i, (label, sort_num, filt_num) in enumerate(COLUMNS):
        cls = ' class="sticky-col"' if i == 0 else ""
        cells.append(
            f'<th{cls}><div class="th-inner">'
            f'<span class="th-label" data-label="{label}" onclick="sortTable({i},{str(sort_num).lower()})">{label}</span>'
            f'<button class="filter-btn" data-col="{i}" data-num="{1 if filt_num else 0}" '
            f'onclick="openFilter(event,this)" title="Filter">&#9662;</button></div></th>'
        )
    return "\n        ".join(cells)


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


WATCHLIST_FLAG_PRIORITY = {"Trend": 0, "Watch": 1, "Away": 2}


def build_watchlist_md(enriched, run_time):
    """Mirrors the vault's tools/vcp_watchlist.py update_watchlist_md table
    format (kept in sync manually, same relationship as this whole file has
    to vcp_scanner_telegram.py). Written to watchlist_export.md in this
    checkout -- deliberately NOT part of the `git add` list for this repo,
    so it never gets committed/pushed here; the workflow's own steps copy it
    into the private trading-wiki-cloud repo instead, since a personal
    trading watchlist has no business in the public dashboard repo."""
    ordered = sorted(
        enriched,
        key=lambda r: (WATCHLIST_FLAG_PRIORITY.get((r.get("buy_sell_flag") or {}).get("flag"), 3), r["nsecode"]),
    )
    rows = []
    for r in ordered:
        s = r.get("screener") or {}
        flag = (r.get("buy_sell_flag") or {}).get("flag") or "-"
        score = r.get("trend_template_score") or "-"
        why = f"VCP scan match -- Flag: {flag}, Trend Template {score}"
        rows.append(f"| {r['nsecode']} | {s.get('sector') or '-'} | {why} | - |")

    return (
        "---\n"
        "tags: [watchlist]\n"
        f"last_updated: {run_time[:10]}\n"
        "---\n\n"
        "# Watchlist\n\n"
        f"Auto-generated by the cloud scan's own schedule ({run_time}) -- names being tracked, "
        "not currently held. See [[portfolio]] for actual holdings.\n\n"
        "| Ticker | Sector | Why watching | Stock page |\n"
        "|---|---|---|---|\n"
        + "\n".join(rows) + "\n"
    )


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
            f'<a class="{cls}" href="https://finviz.com/quote.ashx?t={t}" target="_blank">{t}</a>'
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
    finviz_url = f"https://finviz.com/quote.ashx?t={ticker}"
    tv_url = f"https://www.tradingview.com/symbols/{r.get('exchange','')}-{ticker}/"
    detail_url = f"{PUBLIC_BASE_URL}details/{ticker}.html"

    def series(values):
        if not values:
            return ""
        return " | ".join(str(v) for v in values)

    return {
        "Symbol": _link_cell(ticker, finviz_url),
        "Name": _link_cell(r.get("name", ""), tv_url),
        "Sector": s.get("sector") or "",
        "Close": r.get("close"),
        "ATH": r.get("all_time_high"),
        "ATL": r.get("all_time_low"),
        "Stock P/E": s.get("stock_pe"),
        "Market Cap ($M)": s.get("market_cap"),
        "Flag": flag.get("flag", ""),
        "ROIC %": s.get("roce"),
        "ROE %": s.get("roe"),
        "RSI(14)": r.get("rsi14"),
        "Net Income Qtr last 4 ($M)": series(s.get("net_profit_qtr")),
        "Net Income Year last 4 ($M)": series(s.get("net_profit_year")),
        "Revenue Year last 4 ($M)": series(s.get("revenue_year")),
        "Op Margin % (TTM)": s.get("opm_ttm"),
        "CFO (TTM, $M)": s.get("cfo_ttm"),
        "Debt / Equity": s.get("debt_to_equity"),
        "Current Ratio": s.get("current_ratio"),
        "Gross Margin %": s.get("gross_margin"),
        "Price / Book": s.get("price_book"),
        "Trend Template": _link_cell(r.get("trend_template_score") or "", detail_url),
        "Listing Age (days)": r.get("listing_age_days"),
        "Listing Price": r.get("listing_price"),
    }


def build_dashboard_html(enriched_matches, changes=None):
    rows = []
    for r in enriched_matches:
        s = r.get("screener") or {}
        flag = r.get("buy_sell_flag")

        rows.append(f"""
        <tr>
          <td class="sticky-col" data-sort="{r['nsecode']}"><a href="https://finviz.com/quote.ashx?t={r['nsecode']}" target="_blank">{r['nsecode']}</a></td>
          <td data-sort="{r.get('name', '')}"><a href="https://www.tradingview.com/symbols/{r.get('exchange','')}-{r['nsecode']}/" target="_blank">{r.get('name', '')}</a></td>
          <td data-sort="{s.get('sector') or ''}">{s.get('sector') or '-'}</td>
          <td class="num" data-sort="{_sort_attr(r.get('close'))}">{_fmt(r.get('close'))}</td>
          <td class="num" data-sort="{_sort_attr(r.get('all_time_high'))}">{_fmt_pair(r.get('all_time_high'), r.get('all_time_low'))}</td>
          <td class="num" data-sort="{_sort_attr(s.get('stock_pe'))}">{_fmt(s.get('stock_pe'))}</td>
          <td class="num" data-sort="{_sort_attr(s.get('market_cap'))}">{_fmt(s.get('market_cap'))}</td>
          <td class="flag-cell" data-sort="{_sort_attr(flag['priority'] if flag else None)}">{f'<span class="flag {flag["css"]}">{flag["flag"]}</span>' if flag else '-'}</td>
          <td class="num" data-sort="{_sort_attr(s.get('roce'))}">{_fmt(s.get('roce'), '%')}</td>
          <td class="num" data-sort="{_sort_attr(s.get('roe'))}">{_fmt(s.get('roe'), '%')}</td>
          <td class="num" data-sort="{_sort_attr(r.get('rsi14'))}">{_fmt(r.get('rsi14'))}</td>
          <td class="num" data-sort="{_sort_attr(_last(s.get('net_profit_qtr')))}">{_fmt_series(s.get('net_profit_qtr'))}</td>
          <td class="num" data-sort="{_sort_attr(_last(s.get('net_profit_year')))}">{_fmt_series(s.get('net_profit_year'))}</td>
          <td class="num" data-sort="{_sort_attr(_last(s.get('revenue_year')))}">{_fmt_series(s.get('revenue_year'))}</td>
          <td class="num" data-sort="{_sort_attr(s.get('opm_ttm'))}">{_fmt(s.get('opm_ttm'), '%')}</td>
          <td class="num" data-sort="{_sort_attr(s.get('cfo_ttm'))}">{_fmt(s.get('cfo_ttm'))}</td>
          <td class="num" data-sort="{_sort_attr(s.get('debt_to_equity'))}">{_fmt(s.get('debt_to_equity'))}</td>
          <td class="num" data-sort="{_sort_attr(s.get('current_ratio'))}">{_fmt(s.get('current_ratio'))}</td>
          <td class="num" data-sort="{_sort_attr(s.get('gross_margin'))}">{_fmt(s.get('gross_margin'), '%')}</td>
          <td class="num" data-sort="{_sort_attr(s.get('price_book'))}">{_fmt(s.get('price_book'))}</td>
          <td class="num" data-sort="{_sort_attr(_tt_score_num(r))}"><a href="details/{r['nsecode']}.html" target="_blank" title="Full criterion-by-criterion breakdown + commentary">{r.get('trend_template_score') or '-'}</a></td>
          <td class="num" data-sort="{_sort_attr(r.get('listing_age_days'))}">{_fmt_age(r.get('listing_age_days'))}</td>
          <td class="num" data-sort="{_sort_attr(r.get('listing_price'))}">{_fmt(r.get('listing_price'))}</td>
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
  thead tr.col-row th {{ top: 21px; vertical-align: top; }}
  .th-inner {{ display: flex; align-items: center; gap: 6px; justify-content: space-between; }}
  .th-label {{ cursor: pointer; user-select: none; }}
  .th-label:hover {{ color: #fff; }}
  .filter-btn {{ background: none; border: 1px solid #2a2d34; color: #9aa0a6; border-radius: 3px;
                 cursor: pointer; font-size: 9px; padding: 0 4px; line-height: 16px; font-weight: 400; flex: 0 0 auto; }}
  .filter-btn:hover {{ background: #22262f; color: #e6e6e6; }}
  .filter-btn.active {{ background: #1b3a24; color: #4caf50; border-color: #2e5a3a; }}
  #filter-pop {{ display: none; position: fixed; z-index: 50; background: #1a1d24; border: 1px solid #2a2d34;
                 border-radius: 6px; padding: 8px; width: 230px; box-shadow: 0 6px 24px rgba(0,0,0,0.5);
                 font-weight: 400; font-size: 12px; }}
  #filter-pop input[type=text] {{ width: 100%; box-sizing: border-box; background: #0f1117; color: #e6e6e6;
                 border: 1px solid #2a2d34; border-radius: 3px; padding: 3px 6px; font-size: 12px; margin-bottom: 6px; }}
  #filter-pop input[type=text]:focus {{ outline: none; border-color: #8ab4f8; }}
  .fp-hint {{ font-size: 10px; color: #9aa0a6; margin: -2px 0 8px; }}
  #fp-list {{ max-height: 200px; overflow-y: auto; margin: 4px 0; border-top: 1px solid #2a2d34;
              border-bottom: 1px solid #2a2d34; padding: 4px 0; }}
  .fp-item, .fp-all {{ display: flex; align-items: center; gap: 6px; padding: 2px; cursor: pointer; white-space: nowrap; }}
  .fp-item:hover {{ background: #22262f; }}
  .fp-all {{ font-weight: 600; }}
  .fp-actions {{ display: flex; gap: 6px; margin-top: 6px; }}
  .fp-actions button {{ flex: 1; background: #0f1117; color: #e6e6e6; border: 1px solid #2a2d34;
                        border-radius: 3px; padding: 4px; font-size: 12px; cursor: pointer; }}
  .fp-actions button:hover {{ background: #22262f; }}
  #filter-status {{ display: none; font-size: 12px; color: #9aa0a6; margin-bottom: 8px; }}
  #filter-status button {{ background: #1a1d24; color: #e6e6e6; border: 1px solid #2a2d34;
                           border-radius: 4px; padding: 2px 10px; font-size: 12px; cursor: pointer; margin-left: 8px; }}
  #filter-status button:hover {{ background: #22262f; }}
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
    <h1 style="margin:0">VCP Scan Dashboard &mdash; US</h1>
    <div class="top-right">
      <a href="../" style="margin-right:10px">&larr; India dashboard</a>
      <button id="refresh-btn" onclick="triggerRefresh()">&#8635; Refresh scan</button>
      <button id="csv-btn" onclick="downloadCsv()">&#8681; CSV</button>
      <button id="excel-btn" onclick="downloadExcel()">&#8681; Excel</button>
      <span id="refresh-status"></span>
      <div id="visitor-counts" title="Counter data can take up to 4 hours to update -- a GoatCounter free-tier caching limit, not a bug">Visitors today: <span id="vc-today">-</span> &middot; All-time: <span id="vc-total">-</span> &middot; Downloads: <span id="vc-downloads">-</span> <span style="opacity:0.6">(may lag up to 4h)</span></div>
    </div>
  </div>
  <div class="meta">{len(enriched_matches)} US matches &middot; generated {_ist_timestamp()} &middot;
    Minervini VCP / Trend Template screen (NYSE/Nasdaq)</div>
  {build_changes_html(changes)}
  <div class="note">Sorted by Flag (green &gt; amber &gt; red), then youngest-to-oldest by listing date
    within each flag group. The universe + fundamentals (Sector, P/E, Market Cap, ROIC, ROE, Net Income /
    Revenue history, Debt/Equity, Current Ratio, Gross Margin, P/B) come from TradingView's public scanner;
    technicals (RSI, all-time high/low, the independent 8-criterion Trend Template re-check, Flag, listing
    age/price) come from Yahoo Finance. Best-effort -- a field may show "-" where the source doesn't report
    it.
    <strong>Flag</strong>: Trend (green) = price above the 10-day MA (short-term strength); Watch (amber) =
    price between the 50-day and 10-day MA (pullback within an intact trend); Away (red) = price below the
    50-day MA (intermediate trend broken) -- a fast, mechanical read, not a substitute for the Trend
    Template/VCP read next to it. Net Income / Revenue columns show up to the last 4 reported periods,
    oldest to newest, separated by "|". "Trend Template" = how many of the 8 criteria (price/50/150/200-day
    MA stack, 200-MA uptrend, 52-week high/low proximity, relative strength) the stock passes -- click a
    score for the full breakdown. Criterion 8 there is a crude proxy (beats the S&amp;P 500's 3-month
    return), not a true percentile RS rating, and not the same thing as the RSI(14) column here. "Listing
    Price" is the earliest available monthly close from Yahoo Finance -- a proxy for the IPO price, not the
    exact day-1 figure. Market Cap / Net Income / Revenue / CFO are in USD millions ($M). Click a column
    heading's label to sort (click again to reverse). Click the <strong>&#9662;</strong> button on any
    heading for an Excel-style filter: a searchable checklist of that column's values (tick only the ones
    you want); numeric columns also offer a "Number filter" box for conditions like <code>&gt;100</code> or
    a range <code>50-200</code>. Filters on different columns combine (a row must pass all of them).</div>
  <div id="filter-status"><span id="filter-count"></span><button onclick="clearFilters()">clear all filters</button></div>
  <div class="table-wrap">
  <table>
    <thead>
      <tr class="group-row">
        <th colspan="3">Identity</th>
        <th colspan="2">Price ($)</th>
        <th colspan="3">Valuation / Signal</th>
        <th colspan="3">Quality</th>
        <th colspan="1">Profit, Qtr ($M)</th>
        <th colspan="4">Profit &amp; Financials, Annual</th>
        <th colspan="4">Financial Health</th>
        <th colspan="3">VCP</th>
      </tr>
      <tr class="col-row">
        {_build_col_headers()}
      </tr>
    </thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
  </div>
  <div id="filter-pop">
    <div id="fp-num" style="display:none">
      <input type="text" id="fp-expr" placeholder="Number filter, e.g. &gt;100 or 50-200">
      <div class="fp-hint">Leave blank to use the checklist below.</div>
    </div>
    <input type="text" id="fp-search" placeholder="Search values&hellip;" oninput="fpRenderList()">
    <label class="fp-all"><input type="checkbox" id="fp-all" onchange="fpToggleAll()"> (Select all)</label>
    <div id="fp-list"></div>
    <div class="fp-actions">
      <button onclick="fpApply()">OK</button>
      <button onclick="fpClearColumn()">Clear</button>
      <button onclick="fpClose()">Cancel</button>
    </div>
  </div>
  <div class="footer">
    <p><a href="https://github.com/saketspec-ship-it/vcp-dashboard/blob/main/BUILD.md" target="_blank">How this dashboard was built</a> -- a step-by-step writeup of the pipeline. The US edition sources its universe + fundamentals from TradingView's public scanner and technicals from Yahoo Finance; everything else (hosting, Telegram, the Cloudflare-worker Refresh button, GoatCounter) is shared with the Indian dashboard.</p>
    <p><strong>Disclaimer:</strong> This dashboard is for educational and informational purposes only and demonstrates a
    rule-based stock screening methodology. The stocks displayed are not investment recommendations or buy/sell advice.
    Please do your own research or consult a licensed financial professional before investing. Investing in securities is
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

  // Same GoatCounter account as the visitor counter below, just a distinct
  // tracked path -- lets the "Downloads" figure count CSV and Excel clicks
  // together without a second analytics account.
  function recordDownload() {{
    var img = new Image();
    img.src = 'https://{GOATCOUNTER_SITE}.goatcounter.com/count?p=' + encodeURIComponent('/us/download') +
      '&t=' + encodeURIComponent(document.title + ' download');
  }}

  function downloadCsv() {{
    recordDownload();
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
    recordDownload();
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

  // Click a column heading to sort the table by it (client-side only, reads
  // the data-sort attribute set on each td at build time rather than
  // parsing the displayed/formatted text -- see _sort_attr in the Python
  // build script). Click the same heading again to reverse direction.
  // Missing values (empty data-sort) always sort last regardless of
  // direction, not just whichever end the comparator would otherwise put
  // them at.
  var _sortState = {{ col: -1, dir: 1 }};
  function sortTable(colIndex, isNumeric) {{
    var tbody = document.querySelector('.table-wrap table tbody');
    var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
    _sortState.dir = (_sortState.col === colIndex) ? -_sortState.dir : 1;
    _sortState.col = colIndex;
    rows.sort(function(a, b) {{
      var av = a.children[colIndex].getAttribute('data-sort') || '';
      var bv = b.children[colIndex].getAttribute('data-sort') || '';
      if (av === '' && bv === '') return 0;
      if (av === '') return 1;
      if (bv === '') return -1;
      if (isNumeric) {{ return (parseFloat(av) - parseFloat(bv)) * _sortState.dir; }}
      return av.localeCompare(bv) * _sortState.dir;
    }});
    // Reorder the rows, but only update the arrow on the label span -- the
    // filter <input> lives in the same header cell and must not be wiped.
    rows.forEach(function(row) {{ tbody.appendChild(row); }});
    document.querySelectorAll('.col-row .th-label').forEach(function(lbl, i) {{
      var label = lbl.getAttribute('data-label');
      lbl.textContent = label + (i === colIndex ? (_sortState.dir === 1 ? ' ▲' : ' ▼') : '');
    }});
  }}

  // Excel-style per-column filtering. Each column heading has a ▾ button
  // that opens a dropdown: a searchable checklist of the column's distinct
  // displayed values (tick the ones to keep), plus -- for numeric columns --
  // a "Number filter" box that accepts >N / >=N / <N / <=N / =N or an N-M
  // range and, when filled, overrides the checklist for that column. Active
  // column filters combine with AND. Everything is client-side; sorting and
  // filtering are independent (sorting reorders, filtering only hides rows).
  var columnFilters = {{}};  // colIndex(int) -> {{type:'set', values:[...]}} | {{type:'expr', q}}
  var fpCol = -1, fpNum = false, fpValues = [], fpChecked = {{}};

  function matchExpr(query, raw) {{
    if (raw === '') return false;
    var range = query.match(/^(-?\\d+\\.?\\d*)\\s*-\\s*(-?\\d+\\.?\\d*)$/);
    if (range) {{ var x = parseFloat(raw); return x >= parseFloat(range[1]) && x <= parseFloat(range[2]); }}
    var cmp = query.match(/^(>=|<=|>|<|=)\\s*(-?\\d+\\.?\\d*)$/);
    if (cmp) {{
      var v = parseFloat(raw), n = parseFloat(cmp[2]);
      if (cmp[1] === '>') return v > n;
      if (cmp[1] === '<') return v < n;
      if (cmp[1] === '>=') return v >= n;
      if (cmp[1] === '<=') return v <= n;
      return v === n;
    }}
    return false;  // unparseable expr -> matches nothing, so the box visibly does something
  }}

  function distinctValues(col) {{
    var seen = {{}}, out = [];
    document.querySelectorAll('.table-wrap table tbody tr').forEach(function(r) {{
      var t = r.children[col].textContent.trim();
      if (!(t in seen)) {{ seen[t] = 1; out.push(t); }}
    }});
    return out;
  }}

  function fpLabel(v) {{ return (v === '-' || v === '') ? '(Blanks)' : v; }}

  function openFilter(ev, btn) {{
    ev.stopPropagation();
    fpCol = parseInt(btn.getAttribute('data-col'), 10);
    fpNum = btn.getAttribute('data-num') === '1';
    fpValues = distinctValues(fpCol);
    if (fpNum) {{
      fpValues.sort(function(a, b) {{
        var na = parseFloat(a.replace(/,/g, '').replace(/[^0-9.\\-].*$/, ''));
        var nb = parseFloat(b.replace(/,/g, '').replace(/[^0-9.\\-].*$/, ''));
        if (isNaN(na)) return 1; if (isNaN(nb)) return -1;
        return na - nb;
      }});
    }} else {{
      fpValues.sort(function(a, b) {{ return a.localeCompare(b); }});
    }}
    var existing = columnFilters[fpCol];
    fpChecked = {{}};
    if (existing && existing.type === 'set') {{
      existing.values.forEach(function(v) {{ fpChecked[v] = 1; }});
    }} else {{
      fpValues.forEach(function(v) {{ fpChecked[v] = 1; }});
    }}
    document.getElementById('fp-num').style.display = fpNum ? 'block' : 'none';
    document.getElementById('fp-expr').value = (existing && existing.type === 'expr') ? existing.q : '';
    document.getElementById('fp-search').value = '';
    fpRenderList();
    var pop = document.getElementById('filter-pop');
    pop.style.display = 'block';
    var rect = btn.getBoundingClientRect();
    var left = Math.min(rect.left, window.innerWidth - pop.offsetWidth - 12);
    pop.style.left = Math.max(8, left) + 'px';
    pop.style.top = Math.min(rect.bottom + 2, window.innerHeight - pop.offsetHeight - 12) + 'px';
  }}

  function fpVisibleValues() {{
    var q = document.getElementById('fp-search').value.toLowerCase();
    return fpValues.filter(function(v) {{ return fpLabel(v).toLowerCase().indexOf(q) !== -1; }});
  }}

  function fpRenderList() {{
    var list = document.getElementById('fp-list');
    list.innerHTML = '';
    fpVisibleValues().forEach(function(v) {{
      var lab = document.createElement('label');
      lab.className = 'fp-item';
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = !!fpChecked[v];
      cb.onchange = function() {{ if (cb.checked) fpChecked[v] = 1; else delete fpChecked[v]; fpSyncAll(); }};
      var span = document.createElement('span');
      span.textContent = fpLabel(v);
      lab.appendChild(cb); lab.appendChild(span);
      list.appendChild(lab);
    }});
    fpSyncAll();
  }}

  function fpSyncAll() {{
    var visible = fpVisibleValues();
    var checkedCount = visible.filter(function(v) {{ return fpChecked[v]; }}).length;
    var all = document.getElementById('fp-all');
    all.checked = visible.length > 0 && checkedCount === visible.length;
    all.indeterminate = checkedCount > 0 && checkedCount < visible.length;
  }}

  function fpToggleAll() {{
    var check = document.getElementById('fp-all').checked;
    fpVisibleValues().forEach(function(v) {{ if (check) fpChecked[v] = 1; else delete fpChecked[v]; }});
    fpRenderList();
  }}

  function fpApply() {{
    var expr = document.getElementById('fp-expr').value.trim();
    if (fpNum && expr) {{
      columnFilters[fpCol] = {{ type: 'expr', q: expr }};
    }} else {{
      var checked = fpValues.filter(function(v) {{ return fpChecked[v]; }});
      if (checked.length >= fpValues.length) {{ delete columnFilters[fpCol]; }}
      else {{ columnFilters[fpCol] = {{ type: 'set', values: checked }}; }}
    }}
    applyAllFilters();
    fpClose();
  }}

  function fpClearColumn() {{
    delete columnFilters[fpCol];
    applyAllFilters();
    fpClose();
  }}

  function fpClose() {{ document.getElementById('filter-pop').style.display = 'none'; fpCol = -1; }}

  function applyAllFilters() {{
    var rows = document.querySelectorAll('.table-wrap table tbody tr');
    var cols = Object.keys(columnFilters);
    var shown = 0;
    rows.forEach(function(row) {{
      var visible = cols.every(function(c) {{
        var f = columnFilters[c];
        var cell = row.children[parseInt(c, 10)];
        if (f.type === 'expr') return matchExpr(f.q, cell.getAttribute('data-sort') || '');
        return f.values.indexOf(cell.textContent.trim()) !== -1;
      }});
      row.style.display = visible ? '' : 'none';
      if (visible) shown++;
    }});
    document.querySelectorAll('.filter-btn').forEach(function(b) {{
      if (columnFilters[b.getAttribute('data-col')]) b.classList.add('active');
      else b.classList.remove('active');
    }});
    var status = document.getElementById('filter-status');
    if (cols.length) {{
      status.style.display = 'block';
      document.getElementById('filter-count').textContent = 'Showing ' + shown + ' of ' + rows.length + ' -- ';
    }} else {{
      status.style.display = 'none';
    }}
  }}

  function clearFilters() {{ columnFilters = {{}}; applyAllFilters(); }}

  // Close the dropdown when clicking elsewhere, scrolling the table, or resizing.
  document.addEventListener('click', function(e) {{
    var pop = document.getElementById('filter-pop');
    if (pop.style.display === 'block' && !pop.contains(e.target)) fpClose();
  }});
  window.addEventListener('resize', fpClose);
  document.addEventListener('DOMContentLoaded', function() {{
    var wrap = document.querySelector('.table-wrap');
    if (wrap) wrap.addEventListener('scroll', fpClose);
  }});

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
    // '/us' prefix keeps the US dashboard's counts separate from the Indian
    // dashboard's on the same GoatCounter site.
    record('/us/lifetime');
    record('/us/daily/' + today);
    showCount('/us/lifetime', 'vc-total');
    showCount('/us/daily/' + today, 'vc-today');
    showCount('/us/download', 'vc-downloads');
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

    matches = fetch_us_matches()
    print(f"TradingView: {len(matches)} matches after Trend Template filter")

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
    US_DIR.mkdir(exist_ok=True)
    DASHBOARD_PATH.write_text(html, encoding="utf-8")
    (REPO_ROOT / ".nojekyll").touch()

    save_data = {
        "run_time": _ist_timestamp(),
        "run_epoch": time.time(),
        "tickers": {r["nsecode"]: _ticker_snapshot(r) for r in enriched},
    }
    PREVIOUS_SCAN_PATH.write_text(json.dumps(save_data, indent=2), encoding="utf-8")

    details_dir = US_DIR / "details"
    details_dir.mkdir(exist_ok=True)
    for r in enriched:
        (details_dir / f"{r['nsecode']}.html").write_text(build_stock_detail_html(r), encoding="utf-8")
    print(f"US dashboard + {len(enriched)} detail pages written to {US_DIR}.")

    pages_url = f"https://{GITHUB_REPO_OWNER}.github.io/{GITHUB_REPO_NAME}/us/"
    # Cache-busting query param -- GitHub Pages sets Cache-Control: max-age=600
    # on the same URL every scan, so without this a fresh Telegram link can
    # still show a browser-cached, stale page.
    fresh_url = f"{pages_url}?t={int(time.time())}"
    trigger_label = {
        "schedule": "scheduled run",
        "repository_dispatch": "triggered via Refresh button",
        "workflow_dispatch": "manual trigger",
    }.get(os.environ.get("GITHUB_EVENT_NAME"), "cloud run")
    send_telegram_text(f"US VCP scan ({trigger_label}) - {len(enriched)} matches.\n{fresh_url}")
    print(f"Notified Telegram: {fresh_url}")


if __name__ == "__main__":
    main()

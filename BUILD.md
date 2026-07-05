# How This Dashboard Was Built — Step by Step

A ground-up writeup of the pipeline behind this dashboard
(https://saketspec-ship-it.github.io/vcp-dashboard/), a live scan for Mark Minervini
"Volatility Contraction Pattern" (VCP) setups on the Indian stock market (NSE).

**Disclaimer:** This dashboard and this writeup are for educational and informational
purposes only, demonstrating a rule-based stock screening methodology. The stocks
displayed are not investment recommendations or buy/sell advice. Please do your own
research or consult a SEBI-registered professional before investing. Investing in
securities is subject to market risks.

## Architecture at a glance
```
Chartink scan (NSE)  ->  Yahoo Finance + screener.in enrichment  ->  HTML dashboard
        |                                                                |
        |                                                     GitHub Pages (static host)
        |                                                                |
   Telegram bot  <---------------------------------------------  Telegram notify
        ^
        |
Scheduled runs (9:10am / 4pm daily, + on-demand "scan" message listener)

Public "Refresh" button on the dashboard -> Cloudflare Worker (holds a GitHub token
privately) -> GitHub repository_dispatch -> GitHub Actions workflow -> same pipeline,
runs in the cloud, commits the result -> Pages redeploys
```

## Step 1 — The Chartink scan
[Chartink](https://chartink.com) has no official public API, but its scanner's own
endpoint (`https://chartink.com/screener/process`) is reachable with just a CSRF token
+ session cookie fetched from a prior GET — no login required. The scan_clause used
here encodes Mark Minervini's "Trend Template" (price above a rising 50/150/200-day
moving-average stack, price within 25% of its 52-week high and at least 30% above its
52-week low) plus a volume-dryup and Bollinger-Band-contraction check as a proxy for
the VCP itself:
```
( {cash} ( latest close > 30 and latest volume > 100000
and latest close > latest sma(latest close,50)
and latest sma(latest close,50) > latest sma(latest close,150)
and latest sma(latest close,150) > latest sma(latest close,200)
and latest close >= 0.75 * max(250,high)
and latest close >= 1.3 * min(250,low)
and sma(volume,5) < sma(volume,30)
and ( latest upper bollinger band(20,2) - latest lower bollinger band(20,2) )
    < ( 20 days ago upper bollinger band(20,2) - 20 days ago lower bollinger band(20,2) ) ) )
```
Built by iterating candidate queries directly against the live endpoint and checking
result counts/errors — not by guessing at syntax from docs (there aren't any public
ones). One real bug was found and fixed after the fact: the 52-week-high condition was
originally backwards (`close <= 1.25*high`, almost always true, so effectively a
no-op) — the correct form is `close >= 0.75*high`.

## Step 2 — Enrichment (Yahoo Finance)
Chartink's own response only has ticker/name/close/%change/volume. Everything else
technical comes from Yahoo Finance's public (no-login) chart endpoint,
`query1.finance.yahoo.com/v8/finance/chart/<SYM>.NS`:
- Previous close, RSI(14)
- All-time high/low (from a `range=max&interval=1mo` request)
- An independent re-check of all 8 Trend Template criteria (not just trusting the
  Chartink filter), shown per-stock with the actual computed numbers
- Listing age (from `meta.firstTradeDate`)
- A 3-tier "Flag" (Trend/Watch/Away) based on price vs. the 10-day and 50-day MA

Two real data-quality bugs were found and fixed here:
- **Yahoo's raw price history isn't split-adjusted.** A stock that recently split N:1
  shows a fake ~Nx-too-high all-time-high/52-week-high, and a fake single-day RSI-
  breaking "crash" on the split date. Fixed by fetching `events=split` and dividing
  every pre-split price by the split ratio before any downstream calculation.
- **`meta.chartPreviousClose` is not "yesterday's close"** — it's the close *before
  the requested date range started*. Fixed by using the second-to-last entry in the
  actual daily series instead.

## Step 3 — Fundamentals (screener.in)
Sector, Stock P/E, ROCE, ROE, the last 4 quarters/years of Net Profit, OPM%, Reserves,
Cash from Operating Activity, Debtor Days, and the Promoter/FII/DII/Public shareholding
split are scraped from [screener.in](https://www.screener.in)'s server-rendered (not a
JS single-page app) company pages — `robots.txt` was checked first to confirm this is
within their stated crawling policy (only `/user/*` and some query-param patterns are
disallowed).

One field, **Sector P/E, was tried and dropped** — screener.in loads its peer/sector
comparison table via JavaScript/AJAX after page load, not reachable with a plain HTTP
fetch, and no working public endpoint for it was found after a few reasonable attempts.

One real bug was found testing against a bank (a stock trading under a different
report structure than a typical manufacturer): its **consolidated** page looked
complete (100KB+, every expected section present) but its financial tables were empty
shells, because Indian banks report standalone financials only. Fixed by checking that
the Quarterly Results table actually resolves to real numbers before trusting a page,
falling back to the standalone URL otherwise.

## Step 4 — Building the dashboard
A single Python script (stdlib only, no dependencies to install) does steps 1-3, sorts
the results (Flag first, then youngest-to-oldest by listing date within each flag
group), and renders one HTML file: a grouped two-row header, a sticky first column, a
"changes since last run" section (which tickers were added/removed vs. the previous
scan), combined cells for related value pairs, and a per-stock detail page for every
match — a criterion-by-criterion breakdown of the Trend Template score plus templated
commentary, linked from each score in the main table.

## Step 5 — Hosting (GitHub Pages)
A dedicated public GitHub repo with GitHub Pages enabled (Settings -> Pages -> Deploy
from branch: main). Two things had to be fixed to make this reliable:
- **`.nojekyll`** — without it, GitHub's default Jekyll build started silently failing
  once the `details/` folder had dozens of plain static files in it.
- **An explicit rebuild trigger** — a plain `git push` sometimes left Pages stuck
  "building" against a stale commit for several minutes; explicitly calling
  `POST /repos/{owner}/{repo}/pages/builds` after every push reliably unstuck it.

## Step 6 — Telegram bot
A bot created via @BotFather (free, ~2 minutes) receives the dashboard link after every
run. A companion script polls Telegram's `getUpdates` for a message saying
`scan`/`update` and re-runs the pipeline on demand, without needing a persistent server.

## Step 7 — Scheduling
Three scheduled tasks: a morning run, an afternoon run, and a lightweight listener
(every couple of minutes) that only runs the full ~70-second pipeline if it actually
finds a trigger message from Telegram.

## Step 8 — Public Refresh button (the hardest part)
The button any visitor can click to trigger a fresh full re-scan needed real
architecture, since GitHub Pages is a static host with no server of its own to call.

**First attempt, and the most important lesson here:** embedding a scoped-down GitHub
token directly in the button's client-side JS, calling GitHub's `repository_dispatch`
API straight from the browser. This token was **auto-revoked by GitHub within minutes**
of the page going public — GitHub proactively revokes tokens it detects exposed in
public content, a separate and non-overridable safety net from push-protection's
"allow secret" gate. **Any token embedded in a public page is fundamentally not
viable**, regardless of how tightly it's scoped.

Fixed with a small free **Cloudflare Worker** that holds a fresh GitHub token as a
private environment secret, invisible to visitors. The button calls the Worker's
public URL (which does nothing by itself without the token behind it); the Worker
makes the authenticated GitHub call server-side, triggering a **GitHub Actions
workflow** that runs the same pipeline in the cloud and commits the result directly. A
120-second cooldown check inside that cloud script is what actually prevents a
spam-clicked button from flooding the data sources with simultaneous scans — the
button's client-side disable is just a UX nicety, not real protection.

Smaller bugs hit and fixed along the way: Cloudflare Workers restrict manually setting
a `User-Agent` header in some configurations (caused a crash); removing it entirely
then broke things differently since **GitHub's REST API requires a `User-Agent` on
every request** (fixed with a different valid value, not by omitting it); and a manual
paste into GitHub's web-based file editor (needed because pushing changes to
`.github/workflows/*.yml` requires a `workflow`-scoped token) introduced a stray-
indentation bug that silently broke the whole workflow file.

## Step 9 — Visitor counter
A free [GoatCounter](https://www.goatcounter.com) account, with "allow using the
visitor counter" enabled in its settings (off by default). Two tracked paths per page
load — a fixed "lifetime" path and one keyed by today's date — so the daily figure
resets naturally with no private API key needed client-side. Worth knowing:
GoatCounter's public counter *read* endpoint is cached for **up to 4 hours** on their
end, even though the visit itself is recorded within ~10 seconds — the counter can look
"stuck" for a while after a real visit; that's expected behavior on their free tier,
not a bug here.

## Known gaps
- **Sector P/E** is not available (see Step 3).
- **RS Rating** (one of the 8 Trend Template criteria) is approximated as "beats Nifty
  50's trailing ~3-month return" — a much cruder signal than a true percentile RS
  rating, and shown separately from the RSI(14) column so the two aren't confused.
- Some fundamentals (OPM%, Debtor Days) don't apply to banks/NBFCs and are correctly
  left blank rather than filled with a guess.
- The Trend Template only checks trend/strength context — it does **not**
  algorithmically confirm the actual VCP contraction structure (2-4 progressively
  tighter pullbacks with drying volume) is present on the chart. Always look at the
  real chart before acting on anything shown here.

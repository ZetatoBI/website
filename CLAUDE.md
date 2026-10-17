# ZetatoBI website (zetatobi.com)

Static site on GitHub Pages, deployed from the `main` branch root. The homepage is `index.html`.
`CNAME` holds the custom domain: never delete or edit it.

## Zetato Insights (zetatobi.com/insights/)
A market-data page rebuilt every weekday after the US close by `.github/workflows/insights.yml`,
which runs `pipeline/build_insights.py`.

- **Edit** `insights/template.html` (layout and page code), `insights/content.json` (positions, notes)
  and `pipeline/build_insights.py` (data).
- **Never edit by hand:** `insights/index.html` (overwritten every run), `sitemap.xml`, and
  everything under `insights/data/` (the daily bot commits there: track record, price and screen archive).
  The archive is a deliberately kept, irreplaceable record. Never delete or rewrite it.
- **Version guard:** `TEMPLATE_VERSION` in the pipeline must match the `zetato-insights-template vN`
  comment on line 2 of `template.html`. If you change the data format, bump both in the same change.
  A mismatch keeps the old page live and turns the run red on purpose.
- `robots.txt` blocks `/pipeline/` and `/insights/data/` from search engines.
- Canada support is built but off: `MARKETS = ("us",)` in the pipeline. Turning it on is a deliberate decision.
- **Valuation** (`pipeline/valuation.py`): the Buffett indicator from FRED (Fed Z.1 `NCBEILQ027S` / BEA `GDP`),
  plus a daily estimate labelled as such. **Legends** (`pipeline/legends.py`): SEC 13F holdings for the funds in
  `LEGENDS` (or `content.json` "legends"). Each CIK is checked against EDGAR's company name before use.
  Filings are cached in `insights/data/legends/` (immutable, safe to keep; deleting forces a full re-download).
  Avg purchase price is an estimate and must stay labelled as one. A fund can span several CIKs (`ciks` list,
  e.g. Pershing Square moved to Pershing Square Inc., 2026053). Every run checks each filing's totals against
  its own summary page, checks each ticker against the filing's quarter-end prices (mismatch: prices hidden),
  and flags funds missing the latest quarter after the 45-day deadline. Results go to the Actions run summary.
  `content.json` "reportedCost" holds costs a fund itself publishes (Berkshire's annual report), labelled
  "reported". The consensus board ("Across N legends") counts one vote per fund; a move counts only if it
  changes the position by >= MIN_POS_CHG % and is worth >= MIN_PORT_CHG % of the portfolio (smaller = held);
  a consensus label needs >= 3 movers (two-thirds rule). Share classes of one company are grouped by the
  CUSIP's first six characters. Context beside a row is facts only, never a guessed reason.
  Data fixes that must stay: per-filing unit detection (`fix_units`: some filers report thousands after 2023),
  combining two filers for the same quarter (`combine`: Pershing Square), and showing a ticker only when it
  passes the filing-price check (never fall back to an unverified ticker).
- **Data status:** `data_status()` builds the "as of" line and section stamps (green/amber/red), and the page
  itself turns red if it has not been rebuilt for 3+ weekdays. The run summary lists every source's status and
  any Buffett indicator download errors. FRED is tried by CSV, then plain text, then the API if the optional
  `FRED_API_KEY` repository secret is set; the last good download is kept in `insights/data/valuation-cache.json`.
- **Performance > Valuation tab:** sector and industry medians (forward P/E, EV/EBITDA, P/S, yields) of the
  ~120 large companies in Rebounder's screen, with each sector's range over the archived screens.
  Each sector lists its companies, linked to Rebounder. The page shows the 6 largest legends, with "Show all".
  Index managers (Vanguard, BlackRock, State Street) are deliberately excluded: their 13Fs mirror the market. Berkshire's cash comes from `content.json`
  "berkshireCash" (update it from each quarterly report). "reported" holds self-reported moves (e.g. from an
  investor's own posts) with a date and source link; they must stay badged as not an SEC filing.
- **Strategy signals:** no longer shown on Insights (Rebounder shows them), but the pipeline still records them. The pipeline reads `https://rebounder.zetatobi.com/data/signals.json` (built by the
  `ZetatoBI/Rebounder` repo with each strategy's default settings) and keeps a forward-only record in
  `insights/data/signal-history.json`. That file is the track record: never edit, rebuild, back-fill or delete
  it, and never drop losing trades from it. If signals are unavailable, the page shows the last saved record.
- The old Value watch cohort file (`insights/data/watch-history.json`) is retired from the page but kept as a record.

## Data and wording rules
- Every number on the page is calculated from real data. Never invent or fill in sample values.
- The page is educational, not advice: keep the disclaimer and keep the Value watch worded as screening
  criteria, never as recommendations.
- Hypothetical dollar figures must say they are illustrations, not real trades.
- Data comes from Yahoo Finance through yfinance. It is fine for now but not licensed for commercial
  redistribution. Do not add features that republish raw price data.

## Working here
- Work on a branch and open a pull request. Do not push to `main` directly. Merging a PR that touches
  `insights/` or `pipeline/` starts the refresh workflow, then the Pages deploy.
- Test the pipeline without network access by stubbing `yfinance`. Do not hit Yahoo in a loop.
- Brand: navy `#031C3A`, blue `#1F7AE0`, sky `#8EC1FF`, ice `#EFF4FB`; Hanken Grotesk and Newsreader.
- Owner preference: honest, structured analysis; concise wording on the public site.

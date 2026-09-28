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

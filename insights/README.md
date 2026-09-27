# Zetato Insights

The page at zetatobi.com/insights/ is rebuilt automatically each weekday after the US close by
`.github/workflows/insights.yml`, which runs `pipeline/build_insights.py`.

**Never edit `insights/index.html`.** It is overwritten on every run. Change the layout in
`insights/template.html` and your own content in `insights/content.json`.

## What updates by itself
- Index levels and returns (S&P 500, Nasdaq 100, Russell 2000, S&P/TSX), 10-year yield, VIX
- The written market summary at the top (generated from the numbers)
- Sector performance (the 11 Select Sector SPDR funds)
- Value watch, read from Rebounder's latest screen, and its track record
  (`insights/data/watch-history.json`, kept by the workflow; don't delete it)
- `sitemap.xml`

## Publishing positions (optional)
Add holdings to `content.json`. The Tracked portfolio section appears once there is at least one.

```json
{
  "startingCash": null,
  "holdings": [
    { "ticker": "ADBE", "name": "Adobe", "sector": "Software", "bought": "2026-03-10",
      "weight": 10, "thesis": "Why you own it.", "exit": "What would make you sell." },
    { "ticker": "XOM", "name": "Exxon Mobil", "sector": "Energy", "bought": "2026-01-15",
      "weight": 8, "sold": "2026-06-02" }
  ],
  "notes": [
    { "date": "2026-10-01", "title": "Your note title", "summary": "One or two sentences.",
      "link": "/insights/notes/your-note.html" }
  ]
}
```

- `weight`: percent of the portfolio at entry. Use `"shares"` instead if you prefer; only
  percentages are ever shown on the page, never dollar amounts.
- `cost` is optional. Without it, the close on the `bought` date is used.
- To close a position add `"sold": "YYYY-MM-DD"` (and optionally `"soldPrice"`). Keep the row:
  closed positions stay on record.

## Run it by hand
Actions > Refresh Insights > Run workflow. Or locally:
```
pip install -r pipeline/requirements.txt
python pipeline/build_insights.py
```

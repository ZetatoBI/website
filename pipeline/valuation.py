"""Buffett indicator: US stock market value relative to the economy.

Official reading: the Federal Reserve's market value of US nonfinancial corporate equities (Financial
Accounts Z.1, FRED series NCBEILQ027S, millions of dollars) divided by nominal GDP (BEA, FRED series GDP,
billions, annual rate). Both are public-domain US government data. It is quarterly and published with a
lag of a few months.

Daily estimate: the latest official reading moved by the change in a total US market fund (VTI, price only)
since that quarter ended, with GDP held at its latest value. It is labelled as an estimate on the page.
"""
import csv
import io
import sys

import pandas as pd

from legends import http_get

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
MARKET_PROXY = "VTI"


def fred(series_id):
    raw = http_get(FRED.format(series_id), headers={"User-Agent": "Mozilla/5.0 (Zetato Insights)"}).decode("utf-8")
    rows = list(csv.reader(io.StringIO(raw)))
    out = {}
    for r in rows[1:]:
        if len(r) >= 2 and r[1] not in ("", "."):
            try:
                out[pd.Timestamp(r[0])] = float(r[1])
            except ValueError:
                pass
    if not out:
        raise ValueError(f"FRED {series_id}: no data")
    return pd.Series(out).sort_index()


def build(price_series):
    """price_series: price-only daily closes of MARKET_PROXY (or None)."""
    try:
        eq, gdp = fred("NCBEILQ027S"), fred("GDP")
    except Exception as e:
        print(f"Buffett indicator unavailable: {e}", file=sys.stderr)
        return None
    both = pd.concat([eq / 1000.0, gdp], axis=1, join="inner").dropna()
    both.columns = ["mkt", "gdp"]
    ratio = (both["mkt"] / both["gdp"] * 100).round(1)
    q_start = ratio.index[-1]
    q_end = (q_start + pd.offsets.QuarterEnd(0)).normalize()
    last = float(ratio.iloc[-1])
    hist = ratio.tolist()
    pctile = round(sum(1 for v in hist if v <= last) / len(hist) * 100)
    est = None
    if price_series is not None and len(price_series):
        base = price_series[price_series.index <= q_end]
        if len(base) and price_series.index[-1] > q_end:
            est = {"value": round(last * float(price_series.iloc[-1]) / float(base.iloc[-1]), 1),
                   "asOf": price_series.index[-1].strftime("%Y-%m-%d")}
    recent = ratio[ratio.index >= ratio.index[-1] - pd.DateOffset(years=30)]
    return {
        "value": last, "quarterEnd": q_end.strftime("%Y-%m-%d"), "quarter": f"Q{q_start.quarter} {q_start.year}",
        "estimate": est, "percentile": pctile,
        "avgAll": round(float(ratio.mean()), 1), "avg30": round(float(recent.mean()), 1),
        "max": round(float(ratio.max()), 1), "maxQuarter": f"Q{ratio.idxmax().quarter} {ratio.idxmax().year}",
        "since": ratio.index[0].strftime("%Y"),
        "series": [[d.strftime("%Y-%m-%d"), v] for d, v in ratio.items()],
        "mktT": round(float(both["mkt"].iloc[-1]) / 1000, 1), "gdpT": round(float(both["gdp"].iloc[-1]) / 1000, 1),
    }

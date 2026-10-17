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
import json
import os
import sys

import pandas as pd

from legends import http_get

MARKET_PROXY = "VTI"
ERRORS = []     # why a fetch failed, for the run summary


def _parse_csv(raw):
    out = {}
    for r in list(csv.reader(io.StringIO(raw)))[1:]:
        if len(r) >= 2 and r[1] not in ("", "."):
            try:
                out[pd.Timestamp(r[0])] = float(r[1])
            except ValueError:
                pass
    return out


def _parse_txt(raw):
    out, on = {}, False
    for line in raw.splitlines():
        parts = line.split()
        if parts[:2] == ["DATE", "VALUE"]:
            on = True
            continue
        if on and len(parts) == 2 and parts[1] != ".":
            try:
                out[pd.Timestamp(parts[0])] = float(parts[1])
            except ValueError:
                pass
    return out


def fred(series_id):
    """FRED's CSV download, then its plain-text download, then (if a FRED_API_KEY secret is set) the API."""
    hdr = {"User-Agent": "Mozilla/5.0 (Zetato Insights; Hello@ZetatoBI.com)"}
    tries = [("csv", f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}", _parse_csv),
             ("txt", f"https://fred.stlouisfed.org/data/{series_id}.txt", _parse_txt)]
    key = os.environ.get("FRED_API_KEY", "").strip()
    if key:
        tries.append(("api", f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={key}&file_type=json",
                      lambda raw: {pd.Timestamp(o["date"]): float(o["value"]) for o in json.loads(raw)["observations"] if o["value"] != "."}))
    for name, url, parse in tries:
        try:
            out = parse(http_get(url, headers=hdr, timeout=45, tries=2).decode("utf-8", "ignore"))
            if out:
                return pd.Series(out).sort_index()
            ERRORS.append(f"{series_id} via {name}: no data in the response")
        except Exception as e:
            ERRORS.append(f"{series_id} via {name}: {type(e).__name__}: {str(e)[:120]}")
    raise ValueError(f"FRED {series_id} unavailable")


def build(price_series, cache_path=None):
    """price_series: price-only daily closes of MARKET_PROXY (or None). The last good download is kept in
    cache_path, so a temporary FRED outage shows the last reading (flagged) instead of hiding the section."""
    cached = False
    try:
        eq, gdp = fred("NCBEILQ027S"), fred("GDP")
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({"eq": {d.strftime("%Y-%m-%d"): v for d, v in eq.items()},
                                              "gdp": {d.strftime("%Y-%m-%d"): v for d, v in gdp.items()}}), encoding="utf-8")
    except Exception as e:
        print(f"Buffett indicator download failed: {e}; " + " | ".join(ERRORS), file=sys.stderr)
        if not (cache_path and cache_path.exists()):
            return None
        c = json.loads(cache_path.read_text())
        eq = pd.Series({pd.Timestamp(k): v for k, v in c["eq"].items()}).sort_index()
        gdp = pd.Series({pd.Timestamp(k): v for k, v in c["gdp"].items()}).sort_index()
        cached = True
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
        "cached": cached,
    }

"""Zetato Insights builder.

Runs once each weekday after the US close (GitHub Actions). It:
  1. Pulls daily prices for the major indices and the 11 S&P sector funds (yfinance).
  2. Reads Rebounder's latest screen (rebounder.zetatobi.com/data/screen.json) and picks
     large caps trading below their industry peers with analyst upside ("Value watch").
  3. Keeps a running record of every stock the Value watch has flagged, so results since
     the flag date are measured against the S&P 500.
  4. Values the tracked portfolio in insights/content.json with real closing prices.
  5. Writes insights/index.html from insights/template.html with the data baked in,
     plus sitemap.xml, so search engines see real content without running JavaScript.

Usage:  python pipeline/build_insights.py
"""
import html
import json
import math
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
INS = ROOT / "insights"
TEMPLATE = INS / "template.html"
OUT = INS / "index.html"
CONTENT = INS / "content.json"
HISTORY = INS / "data" / "watch-history.json"
SITEMAP = ROOT / "sitemap.xml"
SITE = "https://zetatobi.com"
REBOUNDER = "https://rebounder.zetatobi.com/data/screen.json"
BENCH = "^GSPC"

INDICES = [
    ("^GSPC", "S&P 500"),
    ("^NDX", "Nasdaq 100"),
    ("^RUT", "Russell 2000"),
    ("^GSPTSE", "S&P/TSX Composite"),
]
RATES = [("^TNX", "US 10-year yield"), ("^VIX", "VIX volatility")]
SECTORS = [
    ("XLK", "Technology"), ("XLC", "Communication services"), ("XLY", "Consumer discretionary"),
    ("XLF", "Financials"), ("XLV", "Health care"), ("XLI", "Industrials"), ("XLE", "Energy"),
    ("XLB", "Materials"), ("XLP", "Consumer staples"), ("XLU", "Utilities"), ("XLRE", "Real estate"),
]
LOOKBACK = {"1d": 1, "1w": 5, "1m": 21, "3m": 63, "1y": 252}

# Value watch rules (same spirit as Rebounder's defaults, a little stricter for a public list)
WATCH = {"min_cap": 10e9, "min_pe_discount": 15, "min_off_high": 15, "min_upside": 15, "max_rows": 8}


def num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def rnd(v, d=2):
    return None if v is None else round(v, d)


def load_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------- prices
def download(tickers, period="2y"):
    """Adjusted daily closes, one column per ticker."""
    raw = yf.download(sorted(set(tickers)), period=period, interval="1d", auto_adjust=True,
                      group_by="ticker", progress=False, threads=True)
    closes = {}
    for t in set(tickers):
        try:
            s = raw[t]["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw["Close"]
            s = s.dropna()
            if len(s):
                s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
                closes[t] = s
        except (KeyError, TypeError):
            pass
    missing = set(tickers) - set(closes)
    if missing:
        print("No prices for:", ", ".join(sorted(missing)), file=sys.stderr)
    return closes


def ret(s, n):
    if len(s) <= n:
        return None
    return (s.iloc[-1] / s.iloc[-1 - n] - 1) * 100


def ytd(s):
    prior = s[s.index.year < s.index[-1].year]
    return None if prior.empty else (s.iloc[-1] / prior.iloc[-1] - 1) * 100


def since(s, d):
    """Return from the close on or after date d to the latest close."""
    after = s[s.index >= pd.Timestamp(d)]
    return None if after.empty else (s.iloc[-1] / after.iloc[0] - 1) * 100


def returns(s):
    r = {k: rnd(ret(s, n)) for k, n in LOOKBACK.items()}
    r["ytd"] = rnd(ytd(s))
    return r


def spark(s, points=66):
    tail = s.iloc[-252:]
    step = max(1, len(tail) // points)
    pts = tail.iloc[::step].tolist()
    if pts[-1] != tail.iloc[-1]:
        pts.append(tail.iloc[-1])
    return [round(v, 2) for v in pts]


def ma_gap(s, n=200):
    if len(s) < n:
        return None
    return (s.iloc[-1] / s.iloc[-n:].mean() - 1) * 100


# ---------------------------------------------------------------- market block
def market_block(closes):
    idx = []
    for t, name in INDICES:
        s = closes.get(t)
        if s is None:
            continue
        idx.append({"ticker": t, "name": name, "last": rnd(s.iloc[-1]), "ret": returns(s),
                    "ma200": rnd(ma_gap(s), 1), "spark": spark(s)})
    rates = []
    for t, name in RATES:
        s = closes.get(t)
        if s is None or len(s) < 22:
            continue
        if t == "^TNX" and s.iloc[-1] > 20:  # older feeds quote the yield x10
            s = s / 10
        rates.append({"ticker": t, "name": name, "last": rnd(s.iloc[-1]),
                      "chg1w": rnd(s.iloc[-1] - s.iloc[-6]), "chg1m": rnd(s.iloc[-1] - s.iloc[-22]),
                      "unit": "%" if t == "^TNX" else "", "spark": spark(s)})
    sectors = []
    for t, name in SECTORS:
        s = closes.get(t)
        if s is None:
            continue
        sectors.append({"ticker": t, "name": name, "ret": returns(s), "ma200": rnd(ma_gap(s), 1)})
    return idx, rates, sectors


def market_summary(idx, rates, sectors):
    """Plain-language summary built from the numbers. Rendered into the HTML for readers and search."""
    by = {i["ticker"]: i for i in idx}
    out = []
    sp = by.get("^GSPC")
    if sp and sp["ret"]["1w"] is not None:
        wk = sp["ret"]["1w"]
        verb = "gained" if wk > 0.05 else "lost" if wk < -0.05 else "was flat"
        amt = f" {abs(wk):.1f}%" if verb != "was flat" else ""
        s = f"The S&P 500 {verb}{amt} over the past week"
        if sp["ret"]["ytd"] is not None:
            s += f" and is {'up' if sp['ret']['ytd'] >= 0 else 'down'} {abs(sp['ret']['ytd']):.1f}% year to date"
        s += "."
        if sp["ma200"] is not None:
            s += (f" It sits {abs(sp['ma200']):.1f}% {'above' if sp['ma200'] >= 0 else 'below'} its 200-day average, "
                  f"{'a long-term uptrend' if sp['ma200'] >= 0 else 'a long-term downtrend'} by that measure.")
        out.append(s)
    nd, rt = by.get("^NDX"), by.get("^RUT")
    if sp and nd and rt and all(x["ret"]["1m"] is not None for x in (sp, nd, rt)):
        ranked = sorted((sp, nd, rt), key=lambda x: x["ret"]["1m"], reverse=True)
        lead, lag = ranked[0], ranked[-1]
        out.append(f"Over the past month the {lead['name']} led the major US indices at "
                   f"{lead['ret']['1m']:+.1f}%, while the {lag['name']} trailed at {lag['ret']['1m']:+.1f}%.")
    sec = [x for x in sectors if x["ret"]["1m"] is not None]
    if len(sec) >= 3:
        sec.sort(key=lambda x: x["ret"]["1m"], reverse=True)
        out.append(f"By sector, {sec[0]['name'].lower()} ({sec[0]['ret']['1m']:+.1f}%) and "
                   f"{sec[1]['name'].lower()} ({sec[1]['ret']['1m']:+.1f}%) led over one month, while "
                   f"{sec[-1]['name'].lower()} ({sec[-1]['ret']['1m']:+.1f}%) lagged.")
    r = {x["ticker"]: x for x in rates}
    if "^TNX" in r and "^VIX" in r:
        t, v = r["^TNX"], r["^VIX"]
        bps = round(t["chg1m"] * 100)
        out.append(f"The US 10-year yield is {t['last']:.2f}%, {'up' if bps >= 0 else 'down'} {abs(bps)} basis points "
                   f"in a month, and the VIX is at {v['last']:.1f}.")
    return out


# ---------------------------------------------------------------- value watch (from Rebounder)
def fetch_rebounder():
    try:
        req = urllib.request.Request(REBOUNDER, headers={"User-Agent": "zetato-insights"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"Rebounder screen unavailable: {e}", file=sys.stderr)
        return []
    if (data.get("meta") or {}).get("demo"):
        print("Rebounder screen is demo data: skipping value watch.", file=sys.stderr)
        return []
    stocks = data.get("stocks", data.get("universe", []))
    if isinstance(stocks, dict):
        stocks = [dict(v, ticker=v.get("ticker", k)) for k, v in stocks.items()]
    return [s for s in stocks if isinstance(s, dict) and s.get("ticker")]


def watch_metrics(s):
    price = num(s.get("price"))
    pe, peer = num(s.get("fwdPE")), num((s.get("peers") or {}).get("fwdPE"))
    tgt = s.get("target") or {}
    target = num(tgt.get("weighted")) or num(tgt.get("mean"))
    return {
        "ticker": s["ticker"],
        "name": s.get("name") or s.get("shortName") or s["ticker"],
        "sector": s.get("sector") or "",
        "industry": s.get("industry") or "",
        "price": price,
        "cap": num(s.get("marketCap")),
        "pe": pe, "peerPe": peer,
        "peDiscount": (1 - pe / peer) * 100 if pe and pe > 0 and peer and peer > 0 else None,
        "offHigh": num(s.get("offHighPct")),
        "upside": (target / price - 1) * 100 if target and price else None,
    }


def value_watch(stocks):
    w = WATCH
    rows = []
    for s in stocks:
        m = watch_metrics(s)
        if not (m["cap"] and m["cap"] >= w["min_cap"]):
            continue
        if not all(m[k] is not None for k in ("peDiscount", "offHigh", "upside")):
            continue
        if m["peDiscount"] >= w["min_pe_discount"] and m["offHigh"] >= w["min_off_high"] and m["upside"] >= w["min_upside"]:
            m["score"] = m["peDiscount"] * 0.4 + m["upside"] * 0.4 + m["offHigh"] * 0.2
            rows.append(m)
    rows.sort(key=lambda m: m["score"], reverse=True)
    return rows[: w["max_rows"]]


def update_history(watch, prices_now, bench, today):
    """Record first-flag date and price for every stock the watch shows; measure since then."""
    hist = load_json(HISTORY, {"started": today, "flags": {}})
    flags = hist["flags"]
    current = {m["ticker"] for m in watch}
    for m in watch:
        f = flags.get(m["ticker"])
        if not f or not f.get("active"):
            flags[m["ticker"]] = {"name": m["name"], "flagged": today, "price": m["price"],
                                  "bench": rnd(bench.iloc[-1]), "active": True}
    for t, f in flags.items():
        if f.get("active") and t not in current:
            f["active"] = False
            f["unflagged"] = today
            f["exitPrice"] = prices_now.get(t, f.get("lastPrice"))
        if f.get("active") and prices_now.get(t):
            f["lastPrice"] = prices_now[t]
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(hist, indent=1), encoding="utf-8")

    rows = []
    for t, f in flags.items():
        end = f.get("lastPrice") if f.get("active") else f.get("exitPrice")
        if not (f.get("price") and end):
            continue
        b_end = bench.iloc[-1] if f.get("active") else bench[bench.index <= pd.Timestamp(f["unflagged"])].iloc[-1]
        r = (end / f["price"] - 1) * 100
        br = (b_end / f["bench"] - 1) * 100 if f.get("bench") else None
        rows.append({"ticker": t, "name": f["name"], "flagged": f["flagged"], "unflagged": f.get("unflagged"),
                     "active": f.get("active", False), "ret": rnd(r, 1), "bench": rnd(br, 1)})
    rows.sort(key=lambda x: x["flagged"], reverse=True)
    done = [x for x in rows if x["bench"] is not None and x["flagged"] < today]  # skip flags made today
    stats = None
    if done:
        stats = {"count": len(done),
                 "avg": rnd(sum(x["ret"] for x in done) / len(done), 1),
                 "avgBench": rnd(sum(x["bench"] for x in done) / len(done), 1),
                 "beat": sum(1 for x in done if x["ret"] > x["bench"])}
    return {"started": hist["started"], "rows": rows[:24], "stats": stats}


# ---------------------------------------------------------------- tracked portfolio
def portfolio_block(content, closes):
    holds = [h for h in content.get("holdings", []) if h.get("ticker") and h.get("bought")]
    if not holds:
        return None
    bench = closes[BENCH]
    start = min(pd.Timestamp(h["bought"]) for h in holds)
    days = bench.index[bench.index >= start]
    if len(days) < 2:
        return None

    def px(t, d):
        s = closes.get(t)
        if s is None:
            return None
        s = s[s.index <= d]
        return None if s.empty else float(s.iloc[-1])

    lots = []
    for h in holds:
        t, s = h["ticker"], closes.get(h["ticker"])
        if s is None:
            continue
        b = pd.Timestamp(h["bought"])
        cost = num(h.get("cost")) or float(s[s.index >= b].iloc[0])
        shares = num(h.get("shares")) or (num(h.get("weight")) or 10) / cost  # weight mode: $ per % of portfolio
        sold = pd.Timestamp(h["sold"]) if h.get("sold") else None
        sold_px = num(h.get("soldPrice")) or (px(t, sold) if sold is not None else None)
        lots.append(dict(h=h, t=t, b=b, cost=cost, shares=shares, sold=sold, sold_px=sold_px))
    if not lots:
        return None
    invested = sum(l["cost"] * l["shares"] for l in lots)
    cash0 = num(content.get("startingCash")) or invested

    series = []
    for d in days:
        cash, val = cash0, 0.0
        for l in lots:
            if d < l["b"]:
                continue
            cash -= l["cost"] * l["shares"]
            if l["sold"] is not None and d >= l["sold"]:
                cash += l["sold_px"] * l["shares"]
            else:
                p = px(l["t"], d)
                val += (p or l["cost"]) * l["shares"]
        series.append(cash + val)
    port = pd.Series(series, index=days)
    base_p, base_b = port.iloc[0], bench[days].iloc[0]
    step = max(1, len(days) // 160)
    pick = list(range(0, len(days), step)) + ([len(days) - 1] if (len(days) - 1) % step else [])
    perf = [{"d": days[i].strftime("%Y-%m-%d"), "p": round((port.iloc[i] / base_p - 1) * 100, 2),
             "b": round((bench[days[i]] / base_b - 1) * 100, 2)} for i in pick]

    total_now = port.iloc[-1]
    rows = []
    for l in lots:
        open_ = l["sold"] is None
        last = px(l["t"], days[-1]) if open_ else l["sold_px"]
        r = (last / l["cost"] - 1) * 100
        end = days[-1] if open_ else l["sold"]
        br = (px(BENCH, end) / float(bench[bench.index >= l["b"]].iloc[0]) - 1) * 100
        h = l["h"]
        rows.append({"ticker": l["t"], "name": h.get("name") or l["t"], "sector": h.get("sector", ""),
                     "bought": h["bought"], "sold": h.get("sold"), "open": open_,
                     "weight": rnd(l["shares"] * last / total_now * 100, 1) if open_ else None,
                     "cost": rnd(l["cost"]), "last": rnd(last), "ret": rnd(r, 1), "bench": rnd(br, 1),
                     "thesis": h.get("thesis", ""), "exit": h.get("exit", "")})
    held = sum(r["weight"] or 0 for r in rows if r["open"])
    return {"inception": days[0].strftime("%Y-%m-%d"), "perf": perf,
            "cash": rnd(max(0.0, 100 - held), 1), "holdings": rows}


# ---------------------------------------------------------------- output
def fmt_long(d):
    return f"{d:%B} {d.day}, {d.year}"


def build(template, data, summary):
    last = datetime.strptime(data["asOf"], "%Y-%m-%d")
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    summ = "".join(f"<p>{html.escape(p)}</p>" for p in summary) or "<p>Market data is being refreshed.</p>"
    desc = summary[0] if summary else "Daily market data, sector performance and screened large caps."
    return (template.replace("{{DATA}}", blob)
            .replace("{{SUMMARY}}", summ)
            .replace("{{DESCRIPTION}}", html.escape(desc[:155]))
            .replace("{{AS_OF}}", fmt_long(last))
            .replace("{{AS_OF_ISO}}", data["asOf"])
            .replace("{{BUILT_ISO}}", data["built"]))


def write_sitemap(as_of):
    SITEMAP.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f'  <url><loc>{SITE}/</loc></url>\n'
        f'  <url><loc>{SITE}/insights/</loc><lastmod>{as_of}</lastmod></url>\n'
        '</urlset>\n', encoding="utf-8")


def main():
    content = load_json(CONTENT, {})
    rebounder = fetch_rebounder()
    watch = value_watch(rebounder)

    tickers = [t for t, _ in INDICES + RATES + SECTORS]
    tickers += [h["ticker"] for h in content.get("holdings", []) if h.get("ticker")]
    hist = load_json(HISTORY, {"flags": {}})
    tickers += [m["ticker"] for m in watch] + list(hist.get("flags", {}).keys())
    closes = download(tickers)
    if BENCH not in closes:
        sys.exit("S&P 500 prices could not be loaded; leaving the published page unchanged.")

    as_of = closes[BENCH].index[-1].strftime("%Y-%m-%d")
    idx, rates, sectors = market_block(closes)
    summary = market_summary(idx, rates, sectors)

    prices_now = {t: rnd(float(s.iloc[-1])) for t, s in closes.items()}
    for m in watch:  # prefer today's close over the screen's price
        m["price"] = prices_now.get(m["ticker"], m["price"])
        for k in ("peDiscount", "offHigh", "upside", "pe", "peerPe"):
            m[k] = rnd(m[k], 1)
        m.pop("score", None)
    track = update_history(watch, prices_now, closes[BENCH], as_of) if watch else None

    data = {
        "asOf": as_of,
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "indices": idx, "rates": rates, "sectors": sectors,
        "watch": watch, "watchRules": WATCH, "track": track,
        "portfolio": portfolio_block(content, closes),
        "notes": content.get("notes", []),
    }
    OUT.write_text(build(TEMPLATE.read_text(encoding="utf-8"), data, summary), encoding="utf-8")
    write_sitemap(as_of)
    print(f"Built insights for {as_of}: {len(idx)} indices, {len(sectors)} sectors, "
          f"{len(watch)} on value watch, portfolio {'on' if data['portfolio'] else 'off'}.")


if __name__ == "__main__":
    main()

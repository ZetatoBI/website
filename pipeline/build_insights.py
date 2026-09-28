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
import gzip
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
# Must match the marker at the top of insights/template.html. Bump both together whenever the data
# format changes, so a half-finished upload can never publish a broken page.
TEMPLATE_VERSION = "v5"
REBOUNDER = "https://rebounder.zetatobi.com/data/screen.json"
BENCH = "^GSPC"

FX = "CAD=X"  # Canadian dollars per US dollar
# Canada is built but switched off for now. To bring it back: MARKETS = ("us", "ca").
MARKETS = ("us",)
CCYS = ("USD", "CAD") if "ca" in MARKETS else ("USD",)

# Overview tiles per market: (ticker, label, kind). kind: index | yield | level | fx
TILES = {
    "us": [("^GSPC", "S&P 500", "index"), ("^NDX", "Nasdaq 100", "index"), ("^RUT", "Russell 2000", "index"),
           ("^DJI", "Dow Jones Industrial Average", "index"),
           ("^TNX", "US 10-year yield", "yield"), ("^VIX", "VIX volatility", "level")],
    "ca": [("^GSPTSE", "S&P/TSX Composite", "index"), ("XIU.TO", "S&P/TSX 60 (XIU)", "index"),
           ("XCS.TO", "TSX small caps (XCS)", "index"), ("CL=F", "WTI crude oil", "index"),
           ("GC=F", "Gold", "index"), (FX, "USD/CAD", "fx")],
}
BENCHMARKS = {"us": ("SPY", "S&P 500 (total return)"), "ca": ("^GSPTSE", "S&P/TSX Composite")}

COMMODITIES = [("CL=F", "WTI crude oil"), ("BZ=F", "Brent crude oil"), ("NG=F", "Natural gas"), ("GC=F", "Gold"),
               ("SI=F", "Silver"), ("HG=F", "Copper"), ("LBR=F", "Lumber"), ("ZW=F", "Wheat"), ("ZC=F", "Corn")]
GLOBAL = [("SPY", "United States"), ("EWC", "Canada"), ("EZU", "Eurozone"), ("EWU", "United Kingdom"),
          ("EWJ", "Japan"), ("MCHI", "China"), ("INDA", "India"), ("EWZ", "Brazil"), ("EEM", "Emerging markets")]
# Performance groups per market: (id, tab label, description, [(ticker, name)])
GROUPS = {
    "us": [
        ("sectors", "Sectors", "The eleven S&P 500 sectors, measured with the Select Sector SPDR funds.", [
            ("XLK", "Technology"), ("XLC", "Communication services"), ("XLY", "Consumer discretionary"),
            ("XLF", "Financials"), ("XLV", "Health care"), ("XLI", "Industrials"), ("XLE", "Energy"),
            ("XLB", "Materials"), ("XLP", "Consumer staples"), ("XLU", "Utilities"), ("XLRE", "Real estate")]),
        ("styles", "Styles", "Size and investment styles. When equal weight trails the S&P 500, gains are concentrated in the largest companies.", [
            ("SPY", "S&P 500"), ("RSP", "S&P 500 equal weight"), ("IVW", "Growth"), ("IVE", "Value"),
            ("IWM", "Small caps"), ("MTUM", "Momentum"), ("QUAL", "Quality"), ("USMV", "Low volatility"),
            ("SCHD", "Dividend")]),
        ("commodities", "Commodities", "Front-month futures prices. Futures roll monthly, so long-period figures are approximate.", COMMODITIES),
        ("bonds", "Bonds", "US bond market total return, from short Treasuries to high yield.", [
            ("SHY", "Treasuries, 1 to 3 years"), ("IEF", "Treasuries, 7 to 10 years"), ("TLT", "Treasuries, 20+ years"),
            ("TIP", "Inflation-protected"), ("LQD", "Investment-grade corporate"), ("HYG", "High-yield corporate")]),
        ("global", "Global", "Country and regional stock markets, measured with US-listed funds.", GLOBAL),
    ],
    "ca": [
        ("sectors", "Sectors", "Canadian sectors, measured with TSX-listed sector funds.", [
            ("XFN.TO", "Financials"), ("ZEB.TO", "Banks"), ("XEG.TO", "Energy"), ("XMA.TO", "Materials"),
            ("XGD.TO", "Gold miners"), ("ZIN.TO", "Industrials"), ("XIT.TO", "Technology"),
            ("XRE.TO", "Real estate (REITs)"), ("XUT.TO", "Utilities"), ("XST.TO", "Consumer staples")]),
        ("styles", "Styles", "Canadian stocks by size and style.", [
            ("XIC.TO", "S&P/TSX Capped Composite"), ("XIU.TO", "S&P/TSX 60"), ("XMD.TO", "Mid caps"),
            ("XCS.TO", "Small caps"), ("XEI.TO", "High dividend"), ("ZLB.TO", "Low volatility")]),
        ("commodities", "Commodities", "Front-month futures prices. Futures roll monthly, so long-period figures are approximate.", COMMODITIES),
        ("bonds", "Bonds", "Canadian bond market total return.", [
            ("XSB.TO", "Short-term bonds"), ("XBB.TO", "Broad bond market"), ("XLB.TO", "Long-term bonds"),
            ("XCB.TO", "Corporate bonds"), ("XRB.TO", "Real return bonds")]),
        ("global", "Global", "Country and regional stock markets, measured with US-listed funds.", GLOBAL),
    ],
}
for _m in GROUPS:  # keep Styles as the right-most tab
    GROUPS[_m].sort(key=lambda g: g[0] == "styles")

# Plain-language notes for the Styles deep dive. Written by hand: edit here, not in the page.
STYLE_INFO = {
    "SPY": {"fund": "SPDR S&P 500 ETF Trust",
            "what": "The 500 largest US companies, each weighted by its market value, so the biggest companies have the biggest say.",
            "tends": "The reference point for everything else in this tab. Because the largest companies dominate, its result can come down to a handful of stocks."},
    "RSP": {"fund": "Invesco S&P 500 Equal Weight ETF",
            "what": "The same 500 companies as the S&P 500, but each is held in roughly equal amounts and rebalanced regularly.",
            "tends": "Leads when gains spread across many companies and trails when a few giants do the heavy lifting. Set beside the S&P 500, it shows how broad a rally is."},
    "IVW": {"fund": "iShares S&P 500 Growth ETF",
            "what": "The S&P 500 companies that score highest on growth: sales and earnings growth and price momentum. Technology and other fast growers are typically the heaviest.",
            "tends": "Tends to lead when investors pay up for growth and interest rates ease, and to lag when they prefer cheaper, steadier companies."},
    "IVE": {"fund": "iShares S&P 500 Value ETF",
            "what": "The S&P 500 companies that look cheapest against their book value, earnings and sales. Financials, health care and industrials are typically the heaviest.",
            "tends": "Tends to lead when the economy or higher rates favour older, cheaper businesses, and to lag when growth stocks run."},
    "IWM": {"fund": "iShares Russell 2000 ETF",
            "what": "Roughly 2,000 smaller US companies. Many depend more on the domestic economy and on borrowing costs than the large multinationals do.",
            "tends": "Tends to lead early in economic recoveries and when rates fall, and to lag when credit tightens or investors want safety."},
    "MTUM": {"fund": "iShares MSCI USA Momentum Factor ETF",
             "what": "US stocks with the strongest price gains over roughly the past six to twelve months. The portfolio is reset a couple of times a year, so its holdings can change a lot.",
             "tends": "Tends to do well in steady trends and to struggle when market leadership abruptly reverses."},
    "QUAL": {"fund": "iShares MSCI USA Quality Factor ETF",
             "what": "US companies with high profitability, steady earnings and modest debt.",
             "tends": "Tends to hold up better when the economy weakens and to lag in sharp rallies led by weaker, more speculative companies."},
    "USMV": {"fund": "iShares MSCI USA Min Vol Factor ETF",
             "what": "US stocks chosen and weighted to make the whole portfolio less volatile than the market, within limits on how far it can lean toward any one sector.",
             "tends": "Tends to fall less in sell-offs and to lag in strong rallies."},
    "SCHD": {"fund": "Schwab U.S. Dividend Equity ETF",
             "what": "About 100 US companies with long dividend records, screened for cash flow, balance-sheet strength, profitability and dividend growth.",
             "tends": "Tends to hold up better when investors favour income and stability, and to lag when growth stocks lead."},
}

LOOKBACK = {"1d": 1, "1w": 5, "1m": 21, "3m": 63, "1y": 252}
ANNUALIZED = {"3y": 3, "5y": 5}      # shown as per-year returns
CHART_INDICES = [("^GSPC", "S&P 500"), ("^DJI", "Dow Jones Industrial Average"), ("^NDX", "Nasdaq 100")]
CHART_BARS = 252                      # one year of daily candles; the page can show 3M, 6M or 1Y of them
CALENDAR_YEARS = 5                    # the last five full calendar years

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
def download(tickers, period="7y"):
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


def candle_block():
    """Daily open/high/low/close plus 50 and 200-day simple moving averages for the chart indices.
    The averages are calculated on the full history first, so they are correct from the first bar shown."""
    out = {}
    try:
        raw = yf.download([t for t, _ in CHART_INDICES], period="3y", interval="1d", auto_adjust=True,
                          group_by="ticker", progress=False, threads=True)
    except Exception as e:
        print(f"Candles unavailable: {e}", file=sys.stderr)
        return out
    for t, name in CHART_INDICES:
        try:
            df = (raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw)[["Open", "High", "Low", "Close"]].dropna()
            df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
            if len(df) < 200 + 30:
                continue
            df["s50"] = df["Close"].rolling(50).mean()
            df["s200"] = df["Close"].rolling(200).mean()
            df = df.tail(CHART_BARS)
            col = lambda c: [rnd(num(v)) for v in df[c]]
            out[t] = {"name": name, "d": [d.strftime("%Y-%m-%d") for d in df.index],
                      "o": col("Open"), "h": col("High"), "l": col("Low"), "c": col("Close"),
                      "s50": col("s50"), "s200": col("s200")}
        except (KeyError, TypeError, ValueError):
            print(f"No candles for {t}", file=sys.stderr)
    return out


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


def cal_year(s, y):
    a, b = s[s.index.year < y], s[s.index.year <= y]
    if a.empty or b.empty or b.index[-1].year != y or (b.index[-1].month < 12):
        return None
    return (b.iloc[-1] / a.iloc[-1] - 1) * 100


def returns(s):
    """Display returns (rounded) plus 'tot': exact total returns per period, used for dollar illustrations.
    3y and 5y display as per-year averages; their totals live in 'tot'."""
    r = {k: rnd(ret(s, n)) for k, n in LOOKBACK.items()}
    r["ytd"] = rnd(ytd(s))
    tot = {k: rnd(ret(s, LOOKBACK[k]), 3) for k in ("1m", "3m", "1y")}
    tot["ytd"] = rnd(ytd(s), 3)
    for k, yrs in ANNUALIZED.items():
        n = 252 * yrs
        if len(s) <= n:
            r[k], tot[k] = None, None
        else:
            total = s.iloc[-1] / s.iloc[-1 - n]
            r[k] = rnd((total ** (1 / yrs) - 1) * 100)
            tot[k] = rnd((total - 1) * 100, 3)
    last = s.index[-1].year
    for y in range(last - CALENDAR_YEARS, last):
        v = cal_year(s, y)
        r[f"cy{y}"], tot[f"cy{y}"] = rnd(v), rnd(v, 3)
    r["tot"] = tot
    return r


def period_dates(s):
    """The start and end close behind each period key, so the page can say when a hypothetical trade was bought and sold."""
    idx, out = s.index, {}
    end = idx[-1]
    f = lambda d: d.strftime("%Y-%m-%d")
    for k, n in list(LOOKBACK.items()) + [(k, 252 * y) for k, y in ANNUALIZED.items()]:
        if len(idx) > n:
            out[k] = [f(idx[-1 - n]), f(end)]
    prior = idx[idx.year < end.year]
    if len(prior):
        out["ytd"] = [f(prior[-1]), f(end)]
    for y in range(end.year - CALENDAR_YEARS, end.year):
        a, b = idx[idx.year < y], idx[idx.year <= y]
        if len(a) and len(b) and b[-1].year == y and b[-1].month == 12:
            out[f"cy{y}"] = [f(a[-1]), f(b[-1])]
    return out


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
def native(t):
    return "CAD" if t.endswith(".TO") or t == "^GSPTSE" else "USD"


def convert(s, t, target, fx):
    """Price series of ticker t expressed in the target currency."""
    if native(t) == target or fx is None:
        return s
    f = fx.reindex(s.index).ffill().bfill()
    return s * f if target == "CAD" else s / f


def rets_all(s, t, fx):
    return {c: returns(convert(s, t, c, fx)) for c in CCYS}


HOLDINGS_FILE = INS / "data" / "style-holdings.json"
HOLDINGS_MAX_AGE_DAYS = 7
TOP_N = 5


def _top_holdings(fund):
    df = yf.Ticker(fund).funds_data.top_holdings
    if df is None or len(df) == 0:
        return []
    name_col = next((c for c in df.columns if "name" in str(c).lower()), None)
    w_col = next((c for c in df.columns if "percent" in str(c).lower() or "weight" in str(c).lower()), df.columns[-1])
    total = float(pd.to_numeric(df[w_col], errors="coerce").fillna(0).sum())
    scale = 100 if total <= 1.0 else 1      # Yahoo reports fractions; ten holdings can never sum to under 1% as a percent
    rows = []
    for sym, r in df.iterrows():
        w, sym = num(r[w_col]), str(sym).strip().upper()
        if w is not None and sym:
            rows.append({"t": sym, "name": str(r[name_col]) if name_col else sym, "w": w * scale})
    rows.sort(key=lambda x: x["w"], reverse=True)
    return rows[:TOP_N]


def style_holdings():
    """Largest holdings of each Styles fund. Refreshed weekly (holdings move slowly), kept in the repo,
    and archived by date. If Yahoo fails, the last good copy is used and the page still works."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache = load_json(HOLDINGS_FILE, {})
    try:
        age = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(cache.get("asOf", ""), "%Y-%m-%d")).days
    except ValueError:
        age = None
    if age is not None and age < HOLDINGS_MAX_AGE_DAYS and cache.get("funds"):
        return cache["funds"], cache["asOf"]
    fresh = {}
    for fund in STYLE_INFO:
        try:
            rows = _top_holdings(fund)
            if rows:
                fresh[fund] = rows
        except Exception as e:
            print(f"Holdings unavailable for {fund}: {e}", file=sys.stderr)
    if not fresh:
        return cache.get("funds", {}), cache.get("asOf")
    funds = dict(cache.get("funds", {}))
    funds.update(fresh)                      # a fund that failed today keeps its previous rows
    HOLDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOLDINGS_FILE.write_text(json.dumps({"asOf": today, "funds": funds}, indent=1), encoding="utf-8")
    _gz_write(ARCHIVE / "holdings" / today[:4] / f"{today}.json.gz", fresh)
    return funds, today


def market_block(closes, holdings=None):
    fx = closes.get(FX)
    tiles, groups, bench = {}, {}, {}
    for m, spec in ((m, TILES[m]) for m in MARKETS):
        out = []
        for t, name, kind in spec:
            s = closes.get(t)
            if s is None or len(s) < 22:
                continue
            if kind == "index":
                out.append({"t": t, "name": name, "kind": kind, "last": rnd(s.iloc[-1]), "ccy": native(t),
                            "ret": rets_all(s, t, fx), "spark": spark(s)})
            else:
                if t == "^TNX" and s.iloc[-1] > 20:  # some feeds quote the yield x10
                    s = s / 10
                out.append({"t": t, "name": name, "kind": kind, "last": rnd(s.iloc[-1], 4 if kind == "fx" else 2),
                            "chg1w": rnd(s.iloc[-1] - s.iloc[-6], 4), "chg1m": rnd(s.iloc[-1] - s.iloc[-22], 4),
                            "spark": spark(s)})
        tiles[m] = out
    for m, spec in ((m, GROUPS[m]) for m in MARKETS):
        gs = []
        for gid, label, desc, items in spec:
            rows = []
            for t, n in items:
                if t not in closes:
                    continue
                row = {"t": t, "name": n, "ret": rets_all(closes[t], t, fx)}
                if gid == "styles" and t in STYLE_INFO:
                    row["about"] = STYLE_INFO[t]
                    tops = []
                    for h in (holdings or {}).get(t, []):
                        hs = closes.get(h["t"])
                        if hs is not None and len(hs) > 60:
                            tops.append({"t": h["t"], "name": h["name"], "w": rnd(h["w"], 1), "ret": rets_all(hs, h["t"], fx)})
                    if tops:
                        row["top"] = tops
                rows.append(row)
            if rows:
                gs.append({"id": gid, "label": label, "desc": desc, "items": rows})
        groups[m] = gs
        bt, bn = BENCHMARKS[m]
        if bt in closes:
            bench[m] = {"name": bn, "ret": rets_all(closes[bt], bt, fx)}
    return tiles, groups, bench


def _move(v):
    return "gained" if v > 0.05 else "lost" if v < -0.05 else "was flat"


def _lead_lag(group, ccy, label_fmt):
    rows = [x for x in group["items"] if x["ret"][ccy]["1m"] is not None]
    if len(rows) < 3:
        return None
    rows.sort(key=lambda x: x["ret"][ccy]["1m"], reverse=True)
    f = lambda x: f"{x['name'].lower()} ({x['ret'][ccy]['1m']:+.1f}%)"
    return label_fmt.format(a=f(rows[0]), b=f(rows[1]), z=f(rows[-1]))


def index_sentence(closes, t, name, ccy_word):
    s = closes.get(t)
    if s is None or len(s) < 210:
        return None
    wk, y, gap = ret(s, 5), ytd(s), ma_gap(s)
    verb = _move(wk)
    txt = f"The {name} {verb}{'' if verb == 'was flat' else f' {abs(wk):.1f}%'} over the past week"
    if y is not None:
        txt += f" and is {'up' if y >= 0 else 'down'} {abs(y):.1f}% year to date{ccy_word}"
    txt += "."
    if gap is not None:
        txt += (f" It sits {abs(gap):.1f}% {'above' if gap >= 0 else 'below'} its 200-day average, "
                f"{'a long-term uptrend' if gap >= 0 else 'a long-term downtrend'} by that measure.")
    return txt


def market_summary(closes, groups):
    by = lambda m, gid: next((g for g in groups.get(m, []) if g["id"] == gid), None)
    fx = closes.get(FX)
    us, ca = [], []
    x = index_sentence(closes, "^GSPC", "S&P 500", "")
    if x: us.append(x)
    trio = [(t, n, closes[t]) for t, n in (("^GSPC", "S&P 500"), ("^NDX", "Nasdaq 100"), ("^RUT", "Russell 2000")) if t in closes]
    if len(trio) == 3 and all(ret(s, 21) is not None for _, _, s in trio):
        r = sorted(trio, key=lambda z: ret(z[2], 21), reverse=True)
        us.append(f"Over the past month the {r[0][1]} led the major US indices at {ret(r[0][2], 21):+.1f}%, "
                  f"while the {r[-1][1]} trailed at {ret(r[-1][2], 21):+.1f}%.")
    g = by("us", "sectors")
    x = g and _lead_lag(g, "USD", "By sector, {a} and {b} led over one month, while {z} lagged.")
    if x: us.append(x)
    tnx, vix = closes.get("^TNX"), closes.get("^VIX")
    if tnx is not None and vix is not None and len(tnx) > 22:
        if tnx.iloc[-1] > 20: tnx = tnx / 10
        bps = round((tnx.iloc[-1] - tnx.iloc[-22]) * 100)
        us.append(f"The US 10-year yield is {tnx.iloc[-1]:.2f}%, {'up' if bps >= 0 else 'down'} {abs(bps)} basis points "
                  f"in a month, and the VIX is at {vix.iloc[-1]:.1f}.")

    if "ca" not in MARKETS:
        return {"us": us, "ca": []}
    x = index_sentence(closes, "^GSPTSE", "S&P/TSX Composite", " in Canadian dollars")
    if x: ca.append(x)
    g = by("ca", "sectors")
    x = g and _lead_lag(g, "CAD", "Among Canadian sectors, {a} and {b} led over one month, while {z} lagged.")
    if x: ca.append(x)
    oil, gold = closes.get("CL=F"), closes.get("GC=F")
    if oil is not None and gold is not None and len(oil) > 22 and len(gold) > 22:
        ca.append(f"WTI crude is at US${oil.iloc[-1]:.2f} a barrel ({ret(oil, 21):+.1f}% in a month) and gold at "
                  f"US${gold.iloc[-1]:,.0f} an ounce ({ret(gold, 21):+.1f}%), two of the biggest drivers of the TSX.")
    if fx is not None and len(fx) > 22:
        chg = ret(fx, 21)
        ca.append(f"One US dollar buys C${fx.iloc[-1]:.4f}. The Canadian dollar "
                  f"{'weakened' if chg > 0 else 'strengthened'} {abs(chg):.1f}% against it over the past month, "
                  f"which {'adds to' if chg > 0 else 'reduces'} US returns for Canadian investors.")
    return {"us": us, "ca": ca}


# ---------------------------------------------------------------- value watch (from Rebounder)
def fetch_rebounder():
    """Returns (stock list, raw screen JSON)."""
    try:
        req = urllib.request.Request(REBOUNDER, headers={"User-Agent": "zetato-insights"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"Rebounder screen unavailable: {e}", file=sys.stderr)
        return [], None
    if (data.get("meta") or {}).get("demo"):
        print("Rebounder screen is demo data: skipping value watch.", file=sys.stderr)
        return [], None
    stocks = data.get("stocks", data.get("universe", []))
    if isinstance(stocks, dict):
        stocks = [dict(v, ticker=v.get("ticker", k)) for k, v in stocks.items()]
    return [s for s in stocks if isinstance(s, dict) and s.get("ticker")], data


ARCHIVE = INS / "data" / "archive"
DATE_KEYS = ("asOf", "as_of", "date", "generated", "updated", "built", "timestamp")


def _gz_write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))


def screen_date(meta, fallback):
    for k in DATE_KEYS:
        v = str((meta or {}).get(k) or "")[:10]
        if len(v) == 10 and v[4] == "-" and v[7] == "-":
            return v
    return fallback


def archive_screen(raw, as_of):
    """Keep Rebounder's full screen once per day, exactly as published: valuations, peer medians
    and analyst targets. This history can't be downloaded later from any free source."""
    if not raw or (raw.get("meta") or {}).get("demo"):
        return
    d = screen_date(raw.get("meta"), as_of)
    out = ARCHIVE / "screens" / d[:4] / f"{d}.json.gz"
    if not out.exists():
        _gz_write(out, raw)


def archive_prices(tickers, as_of):
    """Raw daily bars (open, high, low, close, adjusted close, volume) for every stock in the screen's
    universe plus everything on this page. Kept from today on, so companies that are later acquired or
    delisted stay in the record."""
    out = ARCHIVE / "prices" / as_of[:4] / f"{as_of}.json.gz"
    if out.exists() or not tickers:
        return
    try:
        raw = yf.download(sorted(set(tickers)), period="5d", interval="1d", auto_adjust=False,
                          group_by="ticker", progress=False, threads=True)
    except Exception as e:
        print(f"Price archive skipped: {e}", file=sys.stderr)
        return
    rows = {}
    for t in set(tickers):
        try:
            df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            df = df.dropna(how="all")
            df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
            if df.empty or df.index[-1].strftime("%Y-%m-%d") != as_of:
                continue
            r = df.iloc[-1]
            rows[t] = [num(r.get("Open")), num(r.get("High")), num(r.get("Low")), num(r.get("Close")),
                       num(r.get("Adj Close")), int(num(r.get("Volume")) or 0)]
        except (KeyError, TypeError, ValueError):
            pass
    if rows:
        _gz_write(out, {"date": as_of, "fields": ["open", "high", "low", "close", "adjClose", "volume"], "bars": rows})
        print(f"Archived {len(rows)} daily bars for {as_of}.")


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

    # Daily groups: every list the screen has published, keyed by date.
    cohorts = hist.setdefault("cohorts", {})
    if not cohorts:  # first run after upgrade: rebuild groups from first-flag dates
        for t, f in flags.items():
            if f.get("price"):
                cohorts.setdefault(f["flagged"], []).append({"t": t, "price": f["price"], "bench": f.get("bench")})
    if watch and today not in cohorts:
        cohorts[today] = [{"t": m["ticker"], "price": m["price"], "bench": rnd(bench.iloc[-1])} for m in watch]
    HISTORY.write_text(json.dumps(hist, indent=1), encoding="utf-8")

    out = []
    for d in sorted(cohorts):
        rows, b0 = [], None
        for c in cohorts[d]:
            now = prices_now.get(c["t"])
            if not (now and c.get("price")):
                continue
            b0 = b0 or c.get("bench")
            rows.append({"ticker": c["t"], "name": flags.get(c["t"], {}).get("name", c["t"]),
                         "ret": rnd((now / c["price"] - 1) * 100, 1), "on": c["t"] in current})
        if not rows:
            continue
        rows.sort(key=lambda x: x["ret"], reverse=True)
        out.append({"date": d, "bench": rnd((bench.iloc[-1] / b0 - 1) * 100, 1) if b0 else None, "rows": rows})
    return {"started": hist["started"], "cohorts": out}


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
    def block(m, hidden):
        body = "".join(f"<p>{html.escape(p)}</p>" for p in summary[m]) or "<p>Market data is being refreshed.</p>"
        return f'<div class="summary" data-market="{m}"{" hidden" if hidden else ""}>{body}</div>'
    desc = (summary["us"] or ["Daily US and Canadian market data, sector and commodity performance, and screened large caps."])[0]
    return (template.replace("{{DATA}}", blob)
            .replace("{{SUMMARY}}", block("us", False) + block("ca", True))
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
    rebounder, raw_screen = fetch_rebounder()
    watch = value_watch(rebounder)

    tickers = {BENCH} | ({FX} if "ca" in MARKETS else set())
    tickers |= {t for m in MARKETS for t, _, _ in TILES[m]}
    tickers |= {t for m in MARKETS for g in GROUPS[m] for t, _ in g[3]}
    tickers |= {BENCHMARKS[m][0] for m in MARKETS}
    tickers = list(tickers)
    tickers += [h["ticker"] for h in content.get("holdings", []) if h.get("ticker")]
    hist = load_json(HISTORY, {"flags": {}})
    tickers += [m["ticker"] for m in watch] + list(hist.get("flags", {}).keys())
    holdings, holdings_asof = style_holdings()
    tickers += [h["t"] for hs in holdings.values() for h in hs]
    closes = download(tickers)
    if BENCH not in closes:
        sys.exit("S&P 500 prices could not be loaded; leaving the published page unchanged.")

    as_of = closes[BENCH].index[-1].strftime("%Y-%m-%d")
    tiles, groups, bench = market_block(closes, holdings)
    summary = market_summary(closes, groups)

    archive_screen(raw_screen, as_of)
    archive_prices([s["ticker"] for s in rebounder] + tickers, as_of)
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
        "tiles": tiles, "groups": groups, "bench": bench,
        "ccys": list(CCYS),
        "charts": candle_block(),
        "periods": period_dates(closes[BENCH]),
        "holdingsAsOf": holdings_asof,
        "watch": watch, "watchRules": WATCH, "track": track,
        "portfolio": portfolio_block(content, closes),
        "notes": content.get("notes", []),
    }
    template = TEMPLATE.read_text(encoding="utf-8")
    marker = f"zetato-insights-template {TEMPLATE_VERSION}"
    if marker not in template:
        # Data and archives above are already saved and will still be committed. The live page is left as is.
        print(f"::error::insights/template.html is not {TEMPLATE_VERSION}. Upload the matching template.html "
              f"into the insights folder. The live page was left unchanged.")
        Path(ROOT / ".template-mismatch").write_text(TEMPLATE_VERSION)
        return
    OUT.write_text(build(template, data, summary), encoding="utf-8")
    write_sitemap(as_of)
    print(f"Built insights for {as_of}: {sum(len(g['items']) for m in groups.values() for g in m)} performance rows, "
          f"{len(watch)} on value watch, portfolio {'on' if data['portfolio'] else 'off'}.")


if __name__ == "__main__":
    main()

"""What the legends are doing: holdings from SEC 13F filings, kept current automatically, with checks.

Each run checks EDGAR for new 13F filings from the investors in LEGENDS (or insights/content.json "legends").
New filings are downloaded once, parsed and cached in insights/data/legends/<cik>/ (filings never change).

What comes straight from the filings: holdings, shares, values, weights, filing dates and quarter-to-quarter
changes. What is estimated, and labelled as such on the page:
  - Average purchase price: rebuilt from quarter-to-quarter share increases, each priced at that quarter's
    average close. Sales do not change it. It answers "what did they pay, on average, for the shares they
    bought", which is not the same as the tax cost of the shares they still hold.
  - Dollar size of a move: the change in shares times that quarter's average close.
Where a fund itself publishes its cost (Berkshire's annual report does), content.json "reportedCost" is used
instead and labelled "reported".

Checks on every run (results go to the GitHub Actions run summary):
  - Each filing's parsed total and line count must match the totals the filing declares on its summary page.
  - Each ticker must agree with the filing: value / shares at quarter end must be within 8% of the market
    close that day. A mismatch means a wrong ticker or a split problem, so price-based figures are hidden.
  - Freshness: once the 13F deadline for a quarter has passed (45 days), a fund without that quarter is
    flagged on the page.

Track record (estimated, labelled as such): the return of each fund's disclosed US stocks, held from one
quarter end to the next at the weights filed, dividends reinvested, against the S&P 500 (SPY) over the same
quarters. Only positions whose ticker passes the price check count; the share of value covered is shown.
It is not the fund's own return: no shorts, options, cash, foreign shares, trading within a quarter or fees.
Only the derived quarterly returns are saved (insights/data/legends/track-cache.json), never prices.

13F limits: filed up to 45 days after quarter end; US-listed long positions and listed options only (no
shorts, cash, bonds or most foreign shares); options are reported at the notional value of the underlying.
"""
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

UA = "Zetato Insights Hello@ZetatoBI.com"   # the SEC asks automated clients to identify themselves
SEC_PAUSE = 0.15                            # stay well under the SEC's 10 requests a second
HISTORY_START = "2013-06-30"                # information tables are XML from mid-2013 onward
TOP_N = 15
MIN_POS_CHG = 10.0      # a change smaller than this % of the position counts as held, not bought or sold
MIN_PORT_CHG = 0.1      # ...and so does a change worth less than this % of the fund's portfolio
BOARD_ROWS = 25         # rows kept per board tab
MOVE_FILINGS = 4
PRICE_TOLERANCE = 0.08
FILING_DEADLINE_DAYS = 45

# ciks: every SEC filer ID the fund has used, oldest first (funds sometimes change filer, e.g. after a listing).
# match: words that must appear in EDGAR's name for each CIK (a list allows a renamed filer), which guards against typos.
LEGENDS = [
    {"ciks": [1067983], "name": "Berkshire Hathaway", "people": "Greg Abel, CEO; Warren Buffett, chairman", "match": "BERKSHIRE HATHAWAY"},
    {"ciks": [1336528, 2026053], "name": "Pershing Square", "people": "Bill Ackman", "match": "PERSHING SQUARE"},
    {"ciks": [1061768], "name": "Baupost Group", "people": "Seth Klarman", "match": "BAUPOST"},
    {"ciks": [1656456], "name": "Appaloosa", "people": "David Tepper", "match": "APPALOOSA"},
    {"ciks": [1536411], "name": "Duquesne Family Office", "people": "Stanley Druckenmiller", "match": "DUQUESNE"},
    {"ciks": [1096343], "name": "Markel", "people": "Tom Gayner", "match": "MARKEL"},
    {"ciks": [1709323], "name": "Himalaya Capital", "people": "Li Lu", "match": "HIMALAYA"},
    {"ciks": [1040273], "name": "Third Point", "people": "Dan Loeb", "match": "THIRD POINT"},
    {"ciks": [921669], "name": "Icahn", "people": "Carl Icahn", "match": "ICAHN"},
    {"ciks": [1079114, 1489933], "name": "Greenlight Capital", "people": "David Einhorn", "match": ["GREENLIGHT", "DME CAPITAL"]},
    {"ciks": [1167483], "name": "Tiger Global", "people": "Chase Coleman", "match": "TIGER GLOBAL"},
    {"ciks": [1135730], "name": "Coatue", "people": "Philippe Laffont", "match": "COATUE"},
    {"ciks": [1112520], "name": "Akre Capital", "people": "Chuck Akre", "match": "AKRE"},
]

AUDIT = []      # (fund, check, ok, detail) rows for the run summary


def audit(fund, check, ok, detail=""):
    AUDIT.append((fund, check, ok, detail))
    if not ok:
        print(f"::warning::{fund}: {check}: {detail}")


def http_get(url, data=None, headers=None, timeout=60, tries=3):
    """GET (or POST when data is given). Retries politely; returns bytes."""
    h = {"User-Agent": UA, "Accept-Encoding": "identity"}
    h.update(headers or {})
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def _json(url, **kw):
    return json.loads(http_get(url, **kw).decode("utf-8"))


def _strip(tag):
    return tag.rsplit("}", 1)[-1]


# ---------------------------------------------------------------- EDGAR
def list_13f(cik, match):
    """All 13F-HR and 13F-HR/A filings for a CIK after HISTORY_START, oldest first."""
    sub = _json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    names = [match] if isinstance(match, str) else list(match)
    if not any(m.upper() in (sub.get("name") or "").upper() for m in names):
        raise ValueError(f"CIK {cik} is '{sub.get('name')}', not {' or '.join(names)}: check the CIK")
    blocks = [sub["filings"]["recent"]]
    for f in sub["filings"].get("files", []):
        time.sleep(SEC_PAUSE)
        blocks.append(_json(f"https://data.sec.gov/submissions/{f['name']}"))
    out = []
    for b in blocks:
        for i, form in enumerate(b.get("form", [])):
            if form in ("13F-HR", "13F-HR/A") and (b["reportDate"][i] or "") >= HISTORY_START:
                out.append({"form": form, "acc": b["accessionNumber"][i], "filed": b["filingDate"][i],
                            "period": b["reportDate"][i], "cik": cik})
    out.sort(key=lambda x: (x["period"], x["filed"], x["acc"]))
    return sub.get("name"), out


def parse_info_table(xml_bytes, filed):
    """Rows of a 13F information table, folded by CUSIP and option type. Values in dollars."""
    root = ET.fromstring(xml_bytes)
    scale = 1000 if filed < "2023-01-03" else 1     # values were reported in thousands before 2023
    rows, lines = {}, 0
    for it in root.iter():
        if _strip(it.tag) != "infoTable":
            continue
        lines += 1
        f = {_strip(el.tag): el.text.strip() for el in it.iter() if el.text and el.text.strip()}
        cusip = (f.get("cusip") or "").upper()
        if not cusip:
            continue
        put_call = (f.get("putCall") or "").title() or None
        r = rows.setdefault(f"{cusip}|{put_call or ''}", {"cusip": cusip, "issuer": f.get("nameOfIssuer", ""),
                                                           "cls": f.get("titleOfClass", ""), "putCall": put_call,
                                                           "value": 0.0, "shares": 0.0})
        r["value"] += float(f.get("value", 0) or 0) * scale
        if (f.get("sshPrnamtType") or "SH").upper() == "SH":
            r["shares"] += float(f.get("sshPrnamt", 0) or 0)
    return list(rows.values()), lines


def parse_cover(xml_text, filed):
    """Amendment type and the totals the filing declares on its summary page."""
    def tag(name):
        m = re.search(rf"<(?:\w+:)?{name}>\s*([^<]+?)\s*<", xml_text)
        return m.group(1) if m else None
    total, entries = tag("tableValueTotal"), tag("tableEntryTotal")
    scale = 1000 if filed < "2023-01-03" else 1
    return {"amendment": (tag("amendmentType") or "").upper() or None,
            "declTotal": float(total) * scale if total else None,
            "declEntries": int(float(entries)) if entries else None}


def _base(cik, acc):
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}"


def fetch_cover(cik, acc, filed):
    idx = _json(f"{_base(cik, acc)}/index.json")
    files = [i["name"] for i in idx.get("directory", {}).get("item", [])]
    doc = next((n for n in files if n.lower() == "primary_doc.xml"), None)
    cover = {"amendment": None, "declTotal": None, "declEntries": None}
    if doc:
        time.sleep(SEC_PAUSE)
        cover = parse_cover(http_get(f"{_base(cik, acc)}/{doc}").decode("utf-8", "ignore"), filed)
    return files, cover


def fetch_filing(f):
    files, cover = fetch_cover(f["cik"], f["acc"], f["filed"])
    xmls = [n for n in files if n.lower().endswith(".xml") and n.lower() != "primary_doc.xml"]
    if not xmls:
        raise ValueError(f"no information table in {f['acc']}")
    pick = next((n for n in xmls if "info" in n.lower()), xmls[0])
    time.sleep(SEC_PAUSE)
    rows, lines = parse_info_table(http_get(f"{_base(f['cik'], f['acc'])}/{pick}"), f["filed"])
    if f["form"] == "13F-HR":
        cover["amendment"] = None
    elif not cover["amendment"]:
        cover["amendment"] = "RESTATEMENT"
    return {**f, **cover, "lines": lines, "rows": rows}


TOTALS_RECENT = "2024-06-30"   # filings before this are checked but only summarised, not flagged one by one


def check_totals(name, f):
    """The parsed table must add up to the value the filing itself declares (line counts are only a note:
    some filers count their lines differently)."""
    if f.get("declTotal") is None or f["form"] != "13F-HR" or not f["declTotal"]:
        return True
    parsed = sum(r["value"] for r in f["rows"])
    ok = abs(parsed - f["declTotal"]) <= max(5000.0, 0.005 * f["declTotal"])
    if not ok and f["period"] >= TOTALS_RECENT:
        audit(name, f"totals {f['period']}", False,
              f"parsed ${parsed:,.0f}; the filing declares ${f['declTotal']:,.0f}")
    return ok


def fix_units(f):
    """Some filers keep reporting values in thousands after the 2023 switch to dollars (and a few did the
    reverse before it). Each filing's own implied share prices tell which unit was used. Returns the factor."""
    px = sorted(r["value"] / r["shares"] for r in f["rows"] if r["shares"] > 0 and r["value"] > 0 and not r["putCall"])
    if not px:
        return 1
    med = px[len(px) // 2]
    k = 1000 if f["filed"] >= "2023-01-03" and med < 1.0 else 0.001 if f["filed"] < "2023-01-03" and med > 5000 else 1
    if k != 1:
        for r in f["rows"]:
            r["value"] *= k
        if f.get("declTotal") is not None:
            f["declTotal"] *= k
    return k


def combine(a, b):
    """Two filers reporting the same quarter for one fund (e.g. a manager and its listed parent) cover separate
    accounts, so their holdings add up. A line with identical share counts in both is the same holding reported
    twice and is counted once."""
    out = {k: dict(r) for k, r in a.items()}
    for k, r in b.items():
        if k not in out:
            out[k] = dict(r)
        elif abs(out[k]["shares"] - r["shares"]) > 0.5:
            out[k]["shares"] += r["shares"]
            out[k]["value"] += r["value"]
    return out


def quarters(L, cache_root):
    """Holdings per reporting quarter for one fund, across all its CIKs, from cache plus any new filings."""
    names, filings = [], []
    for cik in L["ciks"]:
        try:
            n, fs = list_13f(int(cik), L.get("match", L["name"]))
            names.append(n)
            filings += fs
        except Exception as e:
            audit(L["name"], f"CIK {cik}", False, str(e))
    if not names:
        raise ValueError("no usable CIK")
    for f in filings:
        d = cache_root / str(f["cik"])
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{f['acc']}.json"
        try:
            if not path.exists():
                time.sleep(SEC_PAUSE)
                path.write_text(json.dumps(fetch_filing(f), separators=(",", ":")), encoding="utf-8")
                print(f"  {L['name']}: cached {f['form']} for {f['period']} ({f['acc']})")
            else:
                c = json.loads(path.read_text())
                if "declTotal" not in c:      # filings cached before the totals check: add the cover once
                    time.sleep(SEC_PAUSE)
                    _, cover = fetch_cover(f["cik"], f["acc"], f["filed"])
                    c.update({k: v for k, v in cover.items() if k != "amendment" or c.get("form") != "13F-HR"})
                    c.setdefault("cik", f["cik"])
                    path.write_text(json.dumps(c, separators=(",", ":")), encoding="utf-8")
        except Exception as e:
            audit(L["name"], f"filing {f['acc']}", False, f"skipped: {e}")
    cached = []
    for cik in L["ciks"]:
        cached += [json.loads(p.read_text()) for p in (cache_root / str(cik)).glob("*.json")]
    by_q, bad, old_bad, fixed, joined = {}, 0, 0, 0, 0
    # per quarter and filer: the original (or a full restatement) replaces earlier versions; "new holdings"
    # amendments add positions first kept confidential
    per = {}
    for f in sorted(cached, key=lambda x: (x["period"], x["filed"])):
        fixed += fix_units(f) != 1
        if not check_totals(L["name"], f):
            bad += 1
            old_bad += f["period"] < TOTALS_RECENT
        rows = {f"{r['cusip']}|{r['putCall'] or ''}": r for r in f["rows"]}
        key = (f["period"], f.get("cik"))
        q = per.get(key)
        if f["form"] == "13F-HR" or q is None or f.get("amendment") == "RESTATEMENT":
            per[key] = {"period": f["period"], "filed": f["filed"], "acc": f["acc"], "cik": f.get("cik"), "rows": rows}
        elif f.get("amendment") == "NEW HOLDINGS":
            for k, r in rows.items():
                q["rows"].setdefault(k, r)
            q["amended"] = f["filed"]
    for (period, cik), q in sorted(per.items(), key=lambda kv: (kv[0][0], kv[1]["filed"])):
        if period not in by_q:
            by_q[period] = q
        else:      # a second filer for the same quarter
            prev = by_q[period]
            by_q[period] = {**prev, "rows": combine(prev["rows"], q["rows"]), "filed": max(prev["filed"], q["filed"]),
                            "acc": q["acc"] if q["filed"] >= prev["filed"] else prev["acc"],
                            "cik": q["cik"] if q["filed"] >= prev["filed"] else prev["cik"], "combined": True}
            joined += 1
    audit(L["name"], "totals match each filing", bad == old_bad,
          f"{bad - old_bad} recent filings differ" if bad > old_bad else
          f"{len(cached)} filings" + (f"; {old_bad} older than {TOTALS_RECENT} differ slightly" if old_bad else ""))
    if fixed:
        audit(L["name"], "value units", True, f"{fixed} filings reported values in thousands instead of dollars: corrected")
    if joined:
        audit(L["name"], "filers combined", True, f"{joined} quarters combine filings from more than one SEC filer")
    return names[-1], [by_q[k] for k in sorted(by_q)]


def expected_period(today=None):
    """Latest quarter whose 13F deadline has passed."""
    today = today or date.today()
    q = pd.Timestamp(today) - pd.offsets.QuarterEnd(1)
    while (q + pd.Timedelta(days=FILING_DEADLINE_DAYS)).date() >= today:
        q = q - pd.offsets.QuarterEnd(1)
    return q.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- CUSIP to ticker
def _norm(name):
    s = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper())
    drop = {"INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "PLC", "LP", "NV", "SA", "AG", "HLDGS", "HOLDINGS",
            "THE", "CL", "CLASS", "A", "B", "COM", "NEW", "DEL", "GROUP", "GRP", "INTL", "INTERNATIONAL"}
    return " ".join(w for w in s.split() if w not in drop)


_NOT_COMMON = (" DEP", "PFD", "PREF", "NOTE", "DBCV", "SDCV", "BOND", " WT", "WARR", "RIGHT", " RT ", "UNIT", "%")


def _looks_common(cls):
    c = f" {(cls or '').upper()} "
    return not any(x in c for x in _NOT_COMMON)


def map_cusips(issuers, cache_path):
    """issuers: {cusip: (issuer name, title of class)}. OpenFIGI first; SEC's own ticker list by exact normalized
    name as a fallback, for common-stock lines only (a company's notes or depositary shares share its name but not
    its ticker). Every mapping is later checked against the filing's own prices, so a wrong match is not shown."""
    issuers = {c: (v if isinstance(v, tuple) else (v, "")) for c, v in issuers.items()}
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    todo = [c for c in issuers if c not in cache or (cache[c] or {}).get("figiPending")]   # pending: OpenFIGI never answered
    for i in range(0, len(todo), 10):          # 10 per request and 25 requests a minute without an API key
        batch = todo[i:i + 10]
        try:
            res = _json("https://api.openfigi.com/v3/mapping", headers={"Content-Type": "application/json"},
                        data=json.dumps([{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in batch]).encode())
        except Exception as e:
            print(f"OpenFIGI unavailable: {e}", file=sys.stderr)
            break
        for c, r in zip(batch, res):
            if r.get("error") and "rate" in str(r.get("error")).lower():
                continue
            d = (r.get("data") or [{}])[0]
            cache[c] = {"t": (d.get("ticker") or "").replace("/", "-") or None, "name": d.get("name"), "via": "openfigi"}
        time.sleep(2.6)
    missing = [c for c in issuers
               if not (cache.get(c) or {}).get("v2") and (not (cache.get(c) or {}).get("t") or (cache.get(c) or {}).get("via") == "sec-name")]
    if missing:
        try:
            sec = _json("https://www.sec.gov/files/company_tickers.json")
            by_name = {}
            for v in sec.values():
                by_name.setdefault(_norm(v["title"]), v["ticker"])
            for c in missing:
                name, cls = issuers[c]
                t = by_name.get(_norm(name)) if _looks_common(cls) else None
                pending = (cache.get(c) or {}).get("via") != "openfigi" and (c not in cache or (cache[c] or {}).get("figiPending"))
                cache[c] = {"t": t.replace(".", "-") if t else None, "name": name, "cls": cls,
                            "via": "sec-name" if t else None, "triedSec": True, "v2": True}
                if pending:
                    cache[c]["figiPending"] = True
        except Exception as e:
            print(f"SEC ticker list unavailable: {e}", file=sys.stderr)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")
    return cache


# ---------------------------------------------------------------- analysis
def _qavg(px, period):
    """Average close over the calendar quarter that ends on `period`."""
    if px is None:
        return None
    end = pd.Timestamp(period)
    start = (end - pd.offsets.QuarterBegin(startingMonth=1)).normalize()
    s = px[(px.index >= start) & (px.index <= end)]
    return float(s.mean()) if len(s) else None


def _close_on(px, period):
    if px is None:
        return None
    s = px[px.index <= pd.Timestamp(period)]
    return float(s.iloc[-1]) if len(s) else None


def _split_factor(splits, period):
    """How many of today's shares one share at `period` became."""
    f = 1.0
    if splits is not None and len(splits):
        for d, r in splits.items():
            if pd.Timestamp(d) > pd.Timestamp(period) and r and r > 0:
                f *= float(r)
    return f


def price_agrees(row, period, px, splits):
    """Value / shares in the filing should match the market close at quarter end (in that day's share basis)."""
    close = _close_on(px, period)
    if not close or not row["shares"]:
        return None
    implied = row["value"] / row["shares"]
    market = close * _split_factor(splits, period)
    return abs(implied / market - 1) <= PRICE_TOLERANCE


def position_history(qs, key, px, splits):
    """Estimated average purchase price (today's share basis) and when the current holding began."""
    shares = cost = 0.0
    since, before = None, False
    first_q = qs[0]["period"]
    for q in qs:
        r = q["rows"].get(key)
        new = (r["shares"] if r else 0.0) * _split_factor(splits, q["period"])
        if new <= 0:
            shares = cost = 0.0
            since, before = None, False
            continue
        avg = _qavg(px, q["period"])
        if shares <= 0:
            since, before = q["period"], q["period"] == first_q
        if new > shares and avg:
            cost += (new - shares) * avg
        elif new < shares and shares > 0:
            cost *= new / shares
        shares = new
    return {"shares": shares, "avgCost": (cost / shares) if shares and cost else None, "since": since, "before": before}


def moves(prev, cur, adj=None):
    """New, added, trimmed and sold positions between two quarters (stock positions only), in today's share basis.
    amtQE: dollar size of the change at the filing's own quarter-end price (needs no ticker, so always available)."""
    adj = adj or (lambda c, p: 1.0)
    out = []
    keys = {k for k in list(prev["rows"]) + list(cur["rows"]) if not k.endswith("|Put") and not k.endswith("|Call")}
    for k in keys:
        a, b = prev["rows"].get(k), cur["rows"].get(k)
        r0 = b or a
        c = r0["cusip"]
        sa = (a or {}).get("shares", 0.0) * adj(c, prev["period"])
        sb = (b or {}).get("shares", 0.0) * adj(c, cur["period"])
        if abs(sa - sb) < 1e-6:
            continue
        if sa == 0:
            act, ch = "New", None
        elif sb == 0:
            act, ch = "Sold out", -100.0
        else:
            ch = (sb / sa - 1) * 100
            if abs(ch) < 0.5:
                continue
            act = "Added" if ch > 0 else "Trimmed"
        if b and b["shares"]:
            pq = b["value"] / b["shares"] / adj(c, cur["period"])
        elif a and a["shares"]:
            pq = a["value"] / a["shares"] / adj(c, prev["period"])
        else:
            pq = 0.0
        out.append({"cusip": c, "issuer": r0["issuer"], "cls": r0.get("cls", ""), "act": act,
                    "chg": None if ch is None else round(ch, 1), "dShares": sb - sa, "shares": sb,
                    "valueAfter": (b or {}).get("value", 0.0), "amtQE": abs(sb - sa) * pq})
    return out


def stock_total(q):
    return sum(r["value"] for r in q["rows"].values() if not r["putCall"])


def mark_material(ms, total):
    """A move counts as buying or selling only if it changes the position by at least MIN_POS_CHG % and is worth
    at least MIN_PORT_CHG % of the portfolio. Smaller ones are rebalancing noise and count as held."""
    for m in ms:
        pct = m["amtQE"] / total * 100 if total else 0.0
        m["material"] = (m["act"] in ("New", "Sold out") or abs(m["chg"] or 0) >= MIN_POS_CHG) and pct >= MIN_PORT_CHG
    return ms


def _title(s):
    s = (s or "").strip()
    return s.title() if s.isupper() else s


BUY = ("New", "Added")


def _company(cusip, cls):
    """Board key: a company's share classes (e.g. Alphabet's GOOGL and GOOG) share the CUSIP's first six characters."""
    return cusip[:6] if _looks_common(cls) else cusip


def board(current, verify, priced):
    """Consensus board across funds' latest filings, one row per company, one vote per fund.
    current: [{name, total, q, moves}] with priced moves."""
    by = {}
    for f in current:
        for r in f["q"]["rows"].values():
            if r["putCall"] or r["value"] <= 0:
                continue
            k = _company(r["cusip"], r.get("cls"))
            e = by.setdefault(k, {"issuer": r["issuer"], "lines": {}, "hold": {}, "mv": {}})
            e["lines"].setdefault(r["cusip"], [0.0, f["q"], r])[0] += r["value"]
            h = e["hold"].setdefault(f["name"], {"w": 0.0, "value": 0.0})
            h["value"] += r["value"]
            h["w"] += r["value"] / f["total"] * 100
        for m in f["moves"]:
            k = _company(m["cusip"], m.get("cls"))
            e = by.setdefault(k, {"issuer": m["issuer"], "lines": {}, "hold": {}, "mv": {}})
            e["mv"].setdefault(f["name"], []).append(m)
    rows = []
    for e in by.values():
        mv, small = [], []
        for fund, parts in e["mv"].items():
            mat = [m for m in parts if m["material"]]
            if not mat:
                small.append(fund)
                continue
            net = sum((m["amount"] or 0) * (1 if m["act"] in BUY else -1) for m in mat)
            if len(mat) == 1:
                act, chg = mat[0]["act"], mat[0]["chg"]
            else:     # several share classes moved: the net dollar change decides the direction
                acts = {m["act"] for m in mat}
                act = acts.pop() if len(acts) == 1 else ("Added" if net > 0 else "Trimmed")
                chg = None
            mv.append({"fund": fund, "act": act, "chg": chg, "amount": round(abs(net)),
                       "pct": round(sum(m["pct"] or 0 for m in mat if (m["act"] in BUY) == (act in BUY)), 2),
                       "w": round(e["hold"].get(fund, {}).get("w", 0), 2)})
        add = [m for m in mv if m["act"] in BUY]
        red = [m for m in mv if m["act"] not in BUY]
        n = len(mv)
        cons = None if n < 3 else "buy" if len(add) * 3 >= n * 2 else "sell" if len(red) * 3 >= n * 2 else "split"
        moved = {m["fund"] for m in mv}
        lines = sorted(e["lines"].values(), key=lambda x: -x[0])
        rows.append({"issuer": _title(e["issuer"]), "ref": (lines[0][1], lines[0][2]) if lines else None,
                     "classes": len(lines), "holders": len(e["hold"]), "adders": len(add), "reducers": len(red),
                     "unchanged": sum(1 for k in e["hold"] if k not in moved), "cons": cons,
                     "value": round(sum(h["value"] for h in e["hold"].values())),
                     "convBuy": round(sum(m["pct"] for m in add), 2), "convSell": round(sum(m["pct"] for m in red), 2),
                     "net": round(sum(m["amount"] * (1 if m["act"] in BUY else -1) for m in mv)),
                     "movers": sorted(mv, key=lambda x: -(x["pct"] or 0)),
                     "held": sorted(({"fund": k, "w": round(v["w"], 2), "small": k in small}
                                     for k, v in e["hold"].items() if k not in moved), key=lambda x: -x["w"])})
    tabs = {
        "bought": sorted((r for r in rows if r["adders"]), key=lambda r: (-r["adders"], r["reducers"], -r["convBuy"]))[:BOARD_ROWS],
        "sold": sorted((r for r in rows if r["reducers"]), key=lambda r: (-r["reducers"], r["adders"], -r["convSell"]))[:BOARD_ROWS],
        "held": sorted(rows, key=lambda r: (-r["holders"], -r["value"]))[:BOARD_ROWS],
    }
    for lst in tabs.values():
        for r in lst:
            if "ref" in r:
                ref = r.pop("ref")
                r["t"] = verify(None, *ref)[0] if ref else None
                r["cusip"] = ref[1]["cusip"] if ref else None
    return tabs


# ---------------------------------------------------------------- track record
TRACK_V = 1             # bump to recompute every cached quarter
TRACK_SHARE = 0.95      # positions priced each quarter: the largest ones making up this share of the portfolio
TRACK_SHOW_COV = 75.0   # average % of reported value with verified prices needed to show a record
TRACK_RETRY_COV = 0.6   # quarters covered less than this are recomputed on later runs
TRACK_MIN_YEARS = 3


def _qe(p):
    return (pd.Timestamp(p) + pd.offsets.QuarterEnd(0)).normalize()


def track_rows(q):
    """The largest stock positions making up TRACK_SHARE of the filing, and the filing's stock total."""
    tot = stock_total(q) or 1.0
    rows = sorted((r for r in q["rows"].values() if not r["putCall"] and r["value"] > 0 and r["shares"] > 0),
                  key=lambda r: -r["value"])
    out, acc = [], 0.0
    for r in rows:
        if acc >= TRACK_SHARE * tot:
            break
        out.append(r)
        acc += r["value"]
    return out, tot


def track_plan(qs, last_qe):
    """[(start, end, filing held)] for each quarter from the first filing to last_qe. A filing is carried forward
    one quarter at most (a late or missing filing); a longer gap restarts the record."""
    if not qs:
        return []
    plan, i, Q = [], 0, _qe(qs[0]["period"])
    while Q < last_qe:
        Qn = (Q + pd.offsets.QuarterEnd(1)).normalize()
        while i + 1 < len(qs) and _qe(qs[i + 1]["period"]) <= Q:
            i += 1
        h = qs[i]
        if (Q - _qe(h["period"])).days > 100:
            plan = []
        else:
            plan.append((Q, Qn, h))
        Q = Qn
    return plan


def _at(df, col, d):
    try:
        v = float(df.at[d, col])
    except (KeyError, TypeError, ValueError):
        return None
    return v if v == v and v > 0 else None


def quarter_return(h, Q, Qn, tick, monthly, splits):
    """Value-weighted total return of the filing's priced positions from Q to Qn, and the share of the filing's
    stock value it covers. A position counts only if the filing's own price agrees with the ticker's close."""
    rows, tot = track_rows(h)
    w = acc = 0.0
    for r in rows:
        t = tick(r["cusip"])
        df = monthly.get(t) if t else None
        if df is None:
            continue
        close, a0, a1 = _at(df, "close", _qe(h["period"])), _at(df, "adj", Q), _at(df, "adj", Qn)
        if not (close and a0 and a1):
            continue
        market = close * _split_factor(splits.get(t), h["period"])
        if abs(r["value"] / r["shares"] / market - 1) > PRICE_TOLERANCE:
            continue
        ret = a1 / a0 - 1
        if ret > 4:           # a five-fold quarter is far more likely a data glitch than a real move
            continue
        w += r["value"]
        acc += r["value"] * ret
    return (acc / w if w else None), w / tot


def _cagr(rs):
    g = 1.0
    for r in rs:
        g *= 1 + r
    return (g ** (4 / len(rs)) - 1) * 100 if rs else None


def track_records(funds, root, monthly_loader, tick, today=None):
    """{fund name: record}. Cached quarters are reused; only new or weakly covered quarters need prices."""
    path = root / "track-cache.json"
    try:
        cache = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    if cache.get("v") != TRACK_V:
        cache = {"v": TRACK_V, "spy": {}, "funds": {}}
    last_qe = (pd.Timestamp(today or date.today()) - pd.offsets.QuarterEnd(1)).normalize()
    plans = {L["name"]: track_plan(qs, last_qe) for L, _, qs, _ in funds}
    key = lambda d: d.strftime("%Y-%m-%d")
    retry = pd.Timestamp(today or date.today()).weekday() == 0     # weakly covered quarters: retried on Mondays
    todo, need = [], set()
    for name, plan in plans.items():
        fc = cache["funds"].get(name, {})
        for Q, Qn, h in plan:
            c = fc.get(key(Q))
            if c and c[2] == h["acc"] and (c[1] >= TRACK_RETRY_COV or not retry):
                continue
            todo.append((name, Q, Qn, h))
            need |= {tick(r["cusip"]) for r in track_rows(h)[0] if tick(r["cusip"])}
    spy_todo = sorted({Q for plan in plans.values() for Q, _, _ in plan if key(Q) not in cache["spy"]})
    monthly, splits = ({}, {})
    if todo or spy_todo:
        try:
            monthly, splits = monthly_loader(sorted(need | {"SPY"}))
        except Exception as e:
            audit("Track record", "prices", False, f"monthly prices unavailable: {e}")
    spy = monthly.get("SPY")
    for Q in spy_todo:
        if spy is not None:
            a0, a1 = _at(spy, "adj", Q), _at(spy, "adj", (Q + pd.offsets.QuarterEnd(1)).normalize())
            if a0 and a1:
                cache["spy"][key(Q)] = round(a1 / a0 - 1, 6)
    for name, Q, Qn, h in todo:
        if not monthly:
            break
        r, cov = quarter_return(h, Q, Qn, tick, monthly, splits)
        old = cache["funds"].get(name, {}).get(key(Q))
        if not old or old[2] != h["acc"] or cov >= old[1]:
            cache["funds"].setdefault(name, {})[key(Q)] = [None if r is None else round(r, 6), round(cov, 4), h["acc"]]
    path.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True), encoding="utf-8")

    out = {}
    for name, plan in plans.items():
        fc, run = cache["funds"].get(name, {}), []
        for Q, Qn, h in plan:            # the latest unbroken run of quarters with both returns
            c, s = fc.get(key(Q)), cache["spy"].get(key(Q))
            if c is None or c[0] is None or s is None or c[2] != h["acc"]:
                run = []
                continue
            run.append((Q, Qn, c[0], s, c[1]))
        if not run:
            audit(name, "track record", False, "no quarters could be priced")
            continue
        fr, sr, cov = [x[2] for x in run], [x[3] for x in run], [x[4] * 100 for x in run]
        years = len(run) / 4
        growth, gf, gs = [[key(run[0][0]), 1.0, 1.0]], 1.0, 1.0
        for Q, Qn, a, b, _ in run:
            gf, gs = gf * (1 + a), gs * (1 + b)
            growth.append([key(Qn), round(gf, 4), round(gs, 4)])
        span = lambda n: None if len(run) < n else {"ret": round(_cagr(fr[-n:]), 1), "spy": round(_cagr(sr[-n:]), 1)}
        avg_cov = sum(cov) / len(cov)
        rec = {"start": key(run[0][0]), "end": key(run[-1][1]), "years": years, "quarters": len(run),
               "ret": round(_cagr(fr), 1), "spy": round(_cagr(sr), 1), "y5": span(20), "y10": span(40),
               "best": round(max(fr) * 100, 1), "worst": round(min(fr) * 100, 1),
               "cov": round(avg_cov, 0), "covMin": round(min(cov), 0),
               "show": years >= TRACK_MIN_YEARS and avg_cov >= TRACK_SHOW_COV, "growth": growth}
        out[name] = rec
        audit(name, "track record", rec["show"],
              f"{years:g} years from {rec['start']}: {rec['ret']}%/yr vs S&P 500 {rec['spy']}%/yr; "
              f"{rec['cov']:.0f}% of value priced on average (lowest {rec['covMin']:.0f}%)"
              + ("" if rec["show"] else "; hidden: too little priced or too short"))
    return out


def build(content, data_dir, price_loader, monthly_loader=None):
    """Returns the page block. price_loader(tickers) -> (closes: dict[ticker, Series], splits: dict)."""
    AUDIT.clear()
    cfg = content.get("legends") or LEGENDS
    root = data_dir / "legends"
    want = expected_period()
    funds = []
    for L in cfg:
        L = {**L, "ciks": L.get("ciks") or [L["cik"]]}
        try:
            edgar_name, qs = quarters(L, root)
        except Exception as e:
            audit(L["name"], "available", False, str(e))
            continue
        if qs:
            stale = qs[-1]["period"] < want
            audit(L["name"], "latest filing", not stale,
                  f"latest {qs[-1]['period']}, expected {want}" if stale else f"{qs[-1]['period']} filed {qs[-1]['filed']}")
            funds.append((L, edgar_name, qs, stale))
    if not funds:
        return None

    # securities that need a ticker: top holdings, options, recent moves, and board candidates
    issuers = {}
    def need(r):
        issuers[r["cusip"]] = (r["issuer"], r.get("cls", ""))
    pre = []
    for L, _, qs, stale in funds:
        cur = qs[-1]["rows"]
        stock = sorted((r for r in cur.values() if not r["putCall"]), key=lambda r: -r["value"])
        for r in stock[:TOP_N] + [r for r in cur.values() if r["putCall"]]:
            need(r)
        for i in range(max(1, len(qs) - MOVE_FILINGS), len(qs)):
            for m in moves(qs[i - 1], qs[i]):
                need(m)
        if len(qs) > 1 and not stale:
            ms = mark_material(moves(qs[-2], qs[-1]), stock_total(qs[-1]))
            for m in ms:
                m["amount"], m["pct"] = m["amtQE"], m["amtQE"] / (stock_total(qs[-1]) or 1) * 100
            pre.append({"name": L["name"], "total": stock_total(qs[-1]) or 1, "q": qs[-1], "moves": ms})
    for lst in board(pre, lambda L, q, r: (None, None), {}).values():
        for r in lst:
            if r.get("cusip"):
                issuers.setdefault(r["cusip"], (r["issuer"], ""))
    hist_issuers = {}
    if monthly_loader:                   # the track record prices each filing's largest positions (monthly only)
        for _, _, qs, _ in funds:
            for q in qs:
                for r in track_rows(q)[0]:
                    if r["cusip"] not in issuers:
                        hist_issuers.setdefault(r["cusip"], (r["issuer"], r.get("cls", "")))
    figi = map_cusips({**hist_issuers, **issuers}, root / "cusip-map.json")
    tick = lambda c: (figi.get(c) or {}).get("t")
    via = lambda c: (figi.get(c) or {}).get("via")
    closes, splits = price_loader(sorted({tick(c) for c in issuers if tick(c)}))
    reported = content.get("reportedCost", {})

    # A ticker is shown only when the filing's own quarter-end prices agree with it. Prices are used only then.
    checked = {}
    def verify(L, q, r):
        """-> (ticker to show, ticker trusted for price figures)"""
        c = r["cusip"]
        t = tick(c)
        if not t:
            return None, None
        if t not in closes:
            return (t if via(c) != "sec-name" else None), None
        key = (c, q["period"])
        if key not in checked:
            checked[key] = price_agrees(r, q["period"], closes[t], splits.get(t))
            if checked[key] is False:
                audit(L["name"] if L else "Board", "ticker check", False,
                      f"{_title(r['issuer'])} ({t}) price disagrees with the filing at {q['period']}: ticker and prices hidden")
        if checked[key] is False:
            return None, None
        if checked[key] is None and via(c) == "sec-name":
            return None, None
        return t, t

    def price_moves(L, ms, q, prev, total):
        """Dollar size of each move: shares changed x that quarter's average close when the ticker is verified,
        otherwise x the filing's own quarter-end price. Both are estimates and labelled so."""
        for m in ms:
            row, rq = q["rows"].get(f"{m['cusip']}|"), q
            if not row:                   # sold out: check the ticker against the previous filing instead
                row, rq = prev["rows"].get(f"{m['cusip']}|"), prev
            show, t = verify(L, rq, row) if row else (None, None)
            est = _qavg(closes.get(t), q["period"]) if t else None
            m["t"] = show
            m["estPrice"] = None if est is None else round(est, 2)
            m["amount"] = round(abs(m["dShares"]) * est) if est else round(m["amtQE"])
            m["pct"] = round(m["amount"] / total * 100, 2) if total else None
        return ms

    track = {}
    if monthly_loader:
        try:
            track = track_records(funds, root, monthly_loader, tick)
        except Exception as e:
            audit("Track record", "built", False, f"skipped: {e}")

    out, current = [], []
    for L, edgar_name, qs, stale in funds:
        cur = qs[-1]
        stock = sorted([(k, r) for k, r in cur["rows"].items() if not r["putCall"]], key=lambda kr: -kr[1]["value"])
        total = sum(r["value"] for _, r in stock) or 1.0
        rep = reported.get(str(L["ciks"][0])) or reported.get(L["name"]) or {}
        holdings, mapped = [], 0
        for k, r in stock[:TOP_N]:
            show, t = verify(L, cur, r)
            mapped += 1 if show else 0
            px = closes.get(t) if t else None
            h = position_history(qs, k, px, splits.get(t) if t else None)
            last = float(px.iloc[-1]) if px is not None and len(px) else None
            rc = rep.get(t) if t else None
            cost = rc["perShare"] if rc else (None if h["before"] else h["avgCost"])
            gain = (last / cost - 1) * 100 if last and cost else None
            shares_now = h["shares"] or r["shares"]
            holdings.append({"t": show, "name": _title(r["issuer"]), "w": round(r["value"] / total * 100, 2),
                             "value": round(r["value"]), "valueNow": round(shares_now * last) if last else None,
                             "shares": round(shares_now), "cost": None if cost is None else round(cost, 2),
                             "costKind": "reported" if rc else ("estimate" if cost else None),
                             "costSource": rc.get("source") if rc else None,
                             "last": None if last is None else round(last, 2), "gain": None if gain is None else round(gain, 1),
                             "since": h["since"], "before": h["before"], "checked": bool(t)})
        audit(L["name"], "tickers verified", mapped == len(holdings), f"{mapped} of {len(holdings)} top holdings")
        options = [{"t": verify(L, cur, r)[0], "name": _title(r["issuer"]), "type": r["putCall"], "value": round(r["value"])}
                   for k, r in sorted(cur["rows"].items(), key=lambda kr: -kr[1]["value"]) if r["putCall"]]
        hist = []
        adj = lambda c, p: _split_factor(splits.get(tick(c)) if tick(c) else None, p)
        for i in range(len(qs) - 1, max(0, len(qs) - 1 - MOVE_FILINGS), -1):
            if i < 1:
                break
            tot_i = stock_total(qs[i])
            ms = price_moves(L, mark_material(moves(qs[i - 1], qs[i], adj), tot_i), qs[i], qs[i - 1], tot_i)
            if i == len(qs) - 1 and not stale:
                current.append({"name": L["name"], "total": tot_i or 1, "q": qs[i], "moves": ms})
            shown = sorted(ms, key=lambda m: (not m["material"], -(m["amount"] or 0)))
            hist.append({"period": qs[i]["period"], "filed": qs[i]["filed"], "acc": qs[i]["acc"],
                         "counts": {a: sum(1 for m in ms if m["act"] == a and m["material"]) for a in ("New", "Added", "Trimmed", "Sold out")},
                         "small": sum(1 for m in ms if not m["material"]),
                         "bought": round(sum(m["amount"] or 0 for m in ms if m["act"] in BUY)),
                         "sold": round(sum(m["amount"] or 0 for m in ms if m["act"] not in BUY)),
                         "moves": [{k: (round(v) if k in ("dShares", "shares", "valueAfter") else v) for k, v in m.items()
                                    if k not in ("cusip", "cls", "amtQE")} for m in shown[:25]],
                         "more": max(0, len(ms) - 25)})
        cash = None
        if L["ciks"][0] == 1067983 and content.get("berkshireCash"):
            c = sorted(content["berkshireCash"], key=lambda x: x["date"])[-1]
            cash = {"usd": c["usdB"] * 1e9, "date": c["date"], "share": round(c["usdB"] * 1e9 / (c["usdB"] * 1e9 + total) * 100, 1)}
        latest_cik = cur.get("cik") or L["ciks"][-1]
        out.append({"cik": int(latest_cik), "name": L["name"], "people": L.get("people", ""), "edgarName": edgar_name,
                    "period": cur["period"], "filed": cur["filed"], "acc": cur["acc"], "amended": cur.get("amended"),
                    "combined": bool(cur.get("combined")), "stale": stale, "expected": want if stale else None,
                    "total": round(total), "positions": len(stock), "options": options, "cash": cash,
                    "holdings": holdings, "moves": hist, "firstPeriod": qs[0]["period"], "track": track.get(L["name"]),
                    "secUrl": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={int(latest_cik)}&type=13F-HR"})
    out.sort(key=lambda f: -f["total"])
    tabs = board(current, lambda L, q, r: verify(L, q, r), {})
    return {"funds": out, "board": {**tabs, "funds": len(current), "excluded": [f["name"] for f in out if f["stale"]],
                                    "rules": {"minPos": MIN_POS_CHG, "minPort": MIN_PORT_CHG}},
            "reported": content.get("reported", []), "berkshireCash": content.get("berkshireCash", []),
            "expected": want, "built": datetime.utcnow().strftime("%Y-%m-%d")}


def summary_markdown():
    """Audit table for the GitHub Actions run summary."""
    lines = ["## Legends data checks", "", "| Fund | Check | Result | Detail |", "|---|---|---|---|"]
    for fund, check, ok, detail in AUDIT:
        lines.append(f"| {fund} | {check} | {'Pass' if ok else '**Check**'} | {detail} |")
    return "\n".join(lines) + "\n"

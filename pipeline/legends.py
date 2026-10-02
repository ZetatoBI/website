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
MOVE_FILINGS = 4
PRICE_TOLERANCE = 0.08
FILING_DEADLINE_DAYS = 45

# ciks: every SEC filer ID the fund has used, oldest first (funds sometimes change filer, e.g. after a listing).
# match: words that must appear in EDGAR's name for each CIK, which guards against typos.
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
    {"ciks": [1079114], "name": "Greenlight Capital", "people": "David Einhorn", "match": "GREENLIGHT"},
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
    if match.upper() not in (sub.get("name") or "").upper():
        raise ValueError(f"CIK {cik} is '{sub.get('name')}', not {match}: check the CIK")
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


def check_totals(name, f):
    """The parsed table must add up to what the filing itself declares."""
    if f.get("declTotal") is None or f["form"] != "13F-HR":
        return True
    parsed = sum(r["value"] for r in f["rows"])
    ok_val = abs(parsed - f["declTotal"]) <= max(1000.0 * (1000 if f["filed"] < "2023-01-03" else 1), 0.005 * f["declTotal"])
    ok_n = f.get("declEntries") is None or f.get("lines") is None or f["lines"] == f["declEntries"]
    if not (ok_val and ok_n):
        audit(name, f"totals {f['period']}", False,
              f"parsed ${parsed:,.0f} in {f.get('lines')} lines; filing declares ${f['declTotal']:,.0f} in {f.get('declEntries')}")
    return ok_val and ok_n


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
    by_q, bad = {}, 0
    for f in sorted(cached, key=lambda x: (x["period"], x["filed"])):
        if not check_totals(L["name"], f):
            bad += 1
        q = by_q.get(f["period"])
        if f["form"] == "13F-HR" or q is None or f.get("amendment") == "RESTATEMENT":
            by_q[f["period"]] = {"period": f["period"], "filed": f["filed"], "acc": f["acc"], "cik": f.get("cik"),
                                 "rows": {f"{r['cusip']}|{r['putCall'] or ''}": r for r in f["rows"]}}
        elif f.get("amendment") == "NEW HOLDINGS":
            for r in f["rows"]:
                q["rows"].setdefault(f"{r['cusip']}|{r['putCall'] or ''}", r)
            q["amended"] = f["filed"]
    audit(L["name"], "totals match each filing", bad == 0, f"{bad} filings differ" if bad else f"{len(cached)} filings")
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


def map_cusips(issuers, cache_path):
    """issuers: {cusip: issuer name}. OpenFIGI first; SEC's own ticker list by exact normalized name as fallback.
    Every mapping is later checked against the filing's own prices, so a wrong match is caught, not shown."""
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    todo = [c for c in issuers if c not in cache]
    for i in range(0, len(todo), 10):          # 10 per request and 25 requests a minute without an API key
        batch = todo[i:i + 10]
        try:
            res = _json("https://api.openfigi.com/v3/mapping", headers={"Content-Type": "application/json"},
                        data=json.dumps([{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in batch]).encode())
        except Exception as e:
            print(f"OpenFIGI unavailable: {e}", file=sys.stderr)
            break
        for c, r in zip(batch, res):
            d = (r.get("data") or [{}])[0]
            cache[c] = {"t": (d.get("ticker") or "").replace("/", "-") or None, "name": d.get("name"), "via": "openfigi"}
        time.sleep(2.6)
    missing = [c for c in issuers if not (cache.get(c) or {}).get("t") and not (cache.get(c) or {}).get("triedSec")]
    if missing:
        try:
            sec = _json("https://www.sec.gov/files/company_tickers.json")
            by_name = {}
            for v in sec.values():
                by_name.setdefault(_norm(v["title"]), v["ticker"])
            for c in missing:
                t = by_name.get(_norm(issuers[c]))
                cache[c] = {"t": t.replace(".", "-") if t else None, "name": issuers[c], "via": "sec-name" if t else None, "triedSec": True}
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
    """New, added, trimmed and sold positions between two quarters (stock positions only), in today's share basis."""
    adj = adj or (lambda c, p: 1.0)
    out = []
    keys = {k for k in list(prev["rows"]) + list(cur["rows"]) if not k.endswith("|Put") and not k.endswith("|Call")}
    for k in keys:
        a, b = prev["rows"].get(k), cur["rows"].get(k)
        r0 = b or a
        sa = (a or {}).get("shares", 0.0) * adj(r0["cusip"], prev["period"])
        sb = (b or {}).get("shares", 0.0) * adj(r0["cusip"], cur["period"])
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
        out.append({"cusip": r0["cusip"], "issuer": r0["issuer"], "act": act, "chg": None if ch is None else round(ch, 1),
                    "dShares": sb - sa, "shares": sb, "valueAfter": (b or {}).get("value", 0.0),
                    "valueBefore": (a or {}).get("value", 0.0)})
    return out


def _title(s):
    s = (s or "").strip()
    return s.title() if s.isupper() else s


def build(content, data_dir, price_loader):
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

    issuers = {}
    for L, _, qs, _ in funds:
        cur = qs[-1]["rows"]
        stock = sorted((r for r in cur.values() if not r["putCall"]), key=lambda r: -r["value"])
        for r in stock[:TOP_N] + [r for r in cur.values() if r["putCall"]]:
            issuers[r["cusip"]] = r["issuer"]
        for i in range(max(1, len(qs) - MOVE_FILINGS), len(qs)):
            for m in moves(qs[i - 1], qs[i]):
                issuers[m["cusip"]] = m["issuer"]
    figi = map_cusips(issuers, root / "cusip-map.json")
    tick = lambda c: (figi.get(c) or {}).get("t")
    closes, splits = price_loader(sorted({tick(c) for c in issuers if tick(c)}))
    reported = content.get("reportedCost", {})

    # a ticker is trusted only if the filing's own quarter-end prices agree with it
    trusted = {}
    def ok_ticker(L, q, r):
        t = tick(r["cusip"])
        if not t or t not in closes:
            return None
        key = (t, q["period"])
        if key not in trusted:
            trusted[key] = price_agrees(r, q["period"], closes[t], splits.get(t))
            if trusted[key] is False:
                audit(L["name"], "ticker check", False, f"{_title(r['issuer'])} ({t}) price disagrees with the filing at {q['period']}: price figures hidden")
        return t if trusted[key] is not False else None

    out = []
    for L, edgar_name, qs, stale in funds:
        cur = qs[-1]
        stock = sorted([(k, r) for k, r in cur["rows"].items() if not r["putCall"]], key=lambda kr: -kr[1]["value"])
        total = sum(r["value"] for _, r in stock) or 1.0
        rep = reported.get(str(L["ciks"][0])) or reported.get(L["name"]) or {}
        holdings, mapped = [], 0
        for k, r in stock[:TOP_N]:
            t = ok_ticker(L, cur, r)
            mapped += 1 if t else 0
            px = closes.get(t) if t else None
            h = position_history(qs, k, px, splits.get(t) if t else None)
            last = float(px.iloc[-1]) if px is not None and len(px) else None
            rc = rep.get(t) if t else None
            cost = rc["perShare"] if rc else (None if h["before"] else h["avgCost"])
            gain = (last / cost - 1) * 100 if last and cost else None
            shares_now = h["shares"] or r["shares"]
            holdings.append({"t": t or tick(r["cusip"]), "name": _title(r["issuer"]), "w": round(r["value"] / total * 100, 2),
                             "value": round(r["value"]), "valueNow": round(shares_now * last) if last else None,
                             "shares": round(shares_now), "cost": None if cost is None else round(cost, 2),
                             "costKind": "reported" if rc else ("estimate" if cost else None),
                             "costSource": rc.get("source") if rc else None,
                             "last": None if last is None else round(last, 2), "gain": None if gain is None else round(gain, 1),
                             "since": h["since"], "before": h["before"], "checked": bool(t)})
        audit(L["name"], "tickers verified", mapped == len(holdings), f"{mapped} of {len(holdings)} top holdings")
        options = [{"t": tick(r["cusip"]), "name": _title(r["issuer"]), "type": r["putCall"], "value": round(r["value"])}
                   for k, r in sorted(cur["rows"].items(), key=lambda kr: -kr[1]["value"]) if r["putCall"]]
        hist = []
        for i in range(len(qs) - 1, max(0, len(qs) - 1 - MOVE_FILINGS), -1):
            if i < 1:
                break
            adj = lambda c, p: _split_factor(splits.get(tick(c)) if tick(c) else None, p)
            ms = moves(qs[i - 1], qs[i], adj)
            for m in ms:
                row = qs[i]["rows"].get(f"{m['cusip']}|") or qs[i - 1]["rows"].get(f"{m['cusip']}|")
                t = ok_ticker(L, qs[i] if f"{m['cusip']}|" in qs[i]["rows"] else qs[i - 1], row) if row else None
                est = _qavg(closes.get(t), qs[i]["period"]) if t else None
                m.update({"t": t or tick(m["cusip"]), "issuer": _title(m["issuer"]),
                          "estPrice": None if est is None else round(est, 2),
                          "estAmount": None if est is None else round(abs(m["dShares"]) * est),
                          "dShares": round(m["dShares"]), "shares": round(m["shares"]),
                          "valueAfter": round(m["valueAfter"])})
                m.pop("cusip", None)
                m.pop("valueBefore", None)
            ms.sort(key=lambda m: -(m["estAmount"] or abs(m["valueAfter"]) or 0))
            hist.append({"period": qs[i]["period"], "filed": qs[i]["filed"], "acc": qs[i]["acc"],
                         "counts": {a: sum(1 for m in ms if m["act"] == a) for a in ("New", "Added", "Trimmed", "Sold out")},
                         "bought": round(sum(m["estAmount"] or 0 for m in ms if m["act"] in ("New", "Added"))),
                         "sold": round(sum(m["estAmount"] or 0 for m in ms if m["act"] in ("Trimmed", "Sold out"))),
                         "moves": ms[:25], "more": max(0, len(ms) - 25)})
        cash = None
        if L["ciks"][0] == 1067983 and content.get("berkshireCash"):
            c = sorted(content["berkshireCash"], key=lambda x: x["date"])[-1]
            cash = {"usd": c["usdB"] * 1e9, "date": c["date"], "share": round(c["usdB"] * 1e9 / (c["usdB"] * 1e9 + total) * 100, 1)}
        latest_cik = cur.get("cik") or L["ciks"][-1]
        out.append({"cik": int(latest_cik), "name": L["name"], "people": L.get("people", ""), "edgarName": edgar_name,
                    "period": cur["period"], "filed": cur["filed"], "acc": cur["acc"], "amended": cur.get("amended"),
                    "stale": stale, "expected": want if stale else None,
                    "total": round(total), "positions": len(stock), "options": options, "cash": cash,
                    "holdings": holdings, "moves": hist, "firstPeriod": qs[0]["period"],
                    "secUrl": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={int(latest_cik)}&type=13F-HR"})
    out.sort(key=lambda f: -f["total"])
    return {"funds": out, "shared": shared_moves(out), "reported": content.get("reported", []), "berkshireCash": content.get("berkshireCash", []),
            "expected": want, "built": datetime.utcnow().strftime("%Y-%m-%d")}


def shared_moves(funds):
    """Stocks that two or more funds moved in their latest filing: all buying, all selling, or split."""
    by = {}
    for f in funds:
        if not f["moves"]:
            continue
        g = f["moves"][0]
        for m in g["moves"]:
            k = m.get("t") or m["issuer"]
            e = by.setdefault(k, {"t": m.get("t"), "name": m["issuer"], "funds": []})
            e["funds"].append({"fund": f["name"], "act": m["act"], "chg": m["chg"], "estAmount": m.get("estAmount"),
                               "estPrice": m.get("estPrice"), "period": g["period"]})
    out = []
    for e in by.values():
        if len(e["funds"]) < 2:
            continue
        buys = sum(1 for x in e["funds"] if x["act"] in ("New", "Added"))
        e["kind"] = "buy" if buys == len(e["funds"]) else "sell" if buys == 0 else "split"
        e["total"] = sum(x["estAmount"] or 0 for x in e["funds"])
        out.append(e)
    out.sort(key=lambda e: (-len(e["funds"]), {"buy": 0, "sell": 1, "split": 2}[e["kind"]], -e["total"]))
    return out[:12]


def summary_markdown():
    """Audit table for the GitHub Actions run summary."""
    lines = ["## Legends data checks", "", "| Fund | Check | Result | Detail |", "|---|---|---|---|"]
    for fund, check, ok, detail in AUDIT:
        lines.append(f"| {fund} | {check} | {'Pass' if ok else '**Check**'} | {detail} |")
    return "\n".join(lines) + "\n"

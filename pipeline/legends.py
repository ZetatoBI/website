"""What the legends are doing: holdings from SEC 13F filings, kept current automatically.

Each run checks EDGAR for new 13F filings from the investors in LEGENDS (or insights/content.json "legends").
New filings are downloaded once, parsed, and cached in insights/data/legends/<cik>/ (filings never change),
so a daily run makes only a handful of requests. Everything shown is from the filings, except two figures
that are clearly labelled as estimates:

  - Estimated average cost: 13F filings do not report what an investor paid. It is rebuilt from
    quarter-to-quarter share changes, priced at the average closing price of the quarter of each change.
  - Positions already held in the first quarter of the available history are marked "held before"
    that quarter, because their cost cannot be estimated.

13F limits worth knowing: filed up to 45 days after quarter end; US-listed long positions and listed
options only (no shorts, cash, bonds or most foreign shares); options are reported at the notional value
of the underlying shares.
"""
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pandas as pd

UA = "Zetato Insights Hello@ZetatoBI.com"   # the SEC asks automated clients to identify themselves
SEC_PAUSE = 0.15                            # stay well under the SEC's 10 requests a second
HISTORY_START = "2013-06-30"                # information tables are XML from mid-2013 onward
TOP_N = 15
MOVE_FILINGS = 4

# name: what the page shows. match: words that must appear in EDGAR's name for that CIK (guards typos).
LEGENDS = [
    {"cik": 1067983, "name": "Berkshire Hathaway", "people": "Greg Abel, CEO; Warren Buffett, chairman", "match": "BERKSHIRE HATHAWAY"},
    {"cik": 1336528, "name": "Pershing Square", "people": "Bill Ackman", "match": "PERSHING SQUARE"},
    {"cik": 1061768, "name": "Baupost Group", "people": "Seth Klarman", "match": "BAUPOST"},
    {"cik": 1656456, "name": "Appaloosa", "people": "David Tepper", "match": "APPALOOSA"},
    {"cik": 1536411, "name": "Duquesne Family Office", "people": "Stanley Druckenmiller", "match": "DUQUESNE"},
]


def http_get(url, data=None, headers=None, timeout=60, tries=3):
    """GET (or POST when data is given). Retries politely; returns bytes."""
    h = {"User-Agent": UA, "Accept-Encoding": "identity"}
    h.update(headers or {})
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def _json(url, **kw):
    return json.loads(http_get(url, **kw).decode("utf-8"))


# ---------------------------------------------------------------- EDGAR
def list_13f(cik, match):
    """All 13F-HR and 13F-HR/A filings for a CIK, oldest first, after HISTORY_START."""
    sub = _json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    name = (sub.get("name") or "").upper()
    if match.upper() not in name:
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
                            "period": b["reportDate"][i], "doc": b["primaryDocument"][i]})
    out.sort(key=lambda x: (x["period"], x["filed"], x["acc"]))
    return sub.get("name"), out


def _strip(tag):
    return tag.rsplit("}", 1)[-1]


def parse_info_table(xml_bytes, filed):
    """Rows of a 13F information table, folded by CUSIP and option type. Values in dollars."""
    root = ET.fromstring(xml_bytes)
    scale = 1000 if filed < "2023-01-03" else 1     # values were reported in thousands before 2023
    rows = {}
    for it in root.iter():
        if _strip(it.tag) != "infoTable":
            continue
        f = {}
        for el in it.iter():
            t = _strip(el.tag)
            if el.text and el.text.strip():
                f[t] = el.text.strip()
        cusip = (f.get("cusip") or "").upper()
        if not cusip:
            continue
        put_call = (f.get("putCall") or "").title() or None
        key = f"{cusip}|{put_call or ''}"
        r = rows.setdefault(key, {"cusip": cusip, "issuer": f.get("nameOfIssuer", ""), "cls": f.get("titleOfClass", ""),
                                  "putCall": put_call, "value": 0.0, "shares": 0.0})
        r["value"] += float(f.get("value", 0) or 0) * scale
        if (f.get("sshPrnamtType") or "SH").upper() == "SH":
            r["shares"] += float(f.get("sshPrnamt", 0) or 0)
    return list(rows.values())


def _filing_files(cik, acc):
    idx = _json(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/index.json")
    return [i["name"] for i in idx.get("directory", {}).get("item", [])]


def _amendment_type(cik, acc, files):
    doc = next((n for n in files if n.lower() == "primary_doc.xml"), None)
    if not doc:
        return "RESTATEMENT"
    time.sleep(SEC_PAUSE)
    x = http_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}").decode("utf-8", "ignore")
    m = re.search(r"<(?:\w+:)?amendmentType>\s*([^<]+?)\s*<", x)
    return (m.group(1).upper() if m else "RESTATEMENT")


def fetch_filing(cik, f):
    files = _filing_files(cik, f["acc"])
    xmls = [n for n in files if n.lower().endswith(".xml") and n.lower() != "primary_doc.xml"]
    if not xmls:
        raise ValueError(f"no information table in {f['acc']}")
    pick = next((n for n in xmls if "info" in n.lower()), xmls[0])
    time.sleep(SEC_PAUSE)
    rows = parse_info_table(http_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{f['acc'].replace('-', '')}/{pick}"), f["filed"])
    amend = _amendment_type(cik, f["acc"], files) if f["form"] == "13F-HR/A" else None
    return {**f, "amendment": amend, "rows": rows}


def quarters(cik, match, cache_dir):
    """Holdings per reporting quarter, built from cached filings plus any new ones."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    edgar_name, filings = list_13f(cik, match)
    for f in filings:
        path = cache_dir / f"{f['acc']}.json"
        if path.exists():
            continue
        time.sleep(SEC_PAUSE)
        try:
            path.write_text(json.dumps(fetch_filing(cik, f), separators=(",", ":")), encoding="utf-8")
            print(f"  {edgar_name}: cached {f['form']} for {f['period']} ({f['acc']})")
        except Exception as e:
            print(f"  {edgar_name}: skipped {f['acc']}: {e}", file=sys.stderr)
    by_q = {}
    for f in sorted((json.loads(p.read_text()) for p in cache_dir.glob("*.json")), key=lambda x: (x["period"], x["filed"])):
        q = by_q.get(f["period"])
        if f["form"] == "13F-HR" or q is None or f.get("amendment") == "RESTATEMENT":
            by_q[f["period"]] = {"period": f["period"], "filed": f["filed"], "acc": f["acc"],
                                 "rows": {f"{r['cusip']}|{r['putCall'] or ''}": r for r in f["rows"]}}
        elif f.get("amendment") == "NEW HOLDINGS":
            for r in f["rows"]:
                q["rows"].setdefault(f"{r['cusip']}|{r['putCall'] or ''}", r)
            q["amended"] = f["filed"]
    return edgar_name, [by_q[k] for k in sorted(by_q)]


# ---------------------------------------------------------------- CUSIP to ticker (OpenFIGI, cached)
def map_cusips(cusips, cache_path):
    cache = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
    todo = [c for c in dict.fromkeys(cusips) if c not in cache]
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
            cache[c] = {"t": (d.get("ticker") or "").replace("/", "-") or None, "name": d.get("name")}
        time.sleep(2.6)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")
    return cache


# ---------------------------------------------------------------- analysis
def _qavg(px, period):
    """Average close over the calendar quarter that ends on `period`."""
    end = pd.Timestamp(period)
    start = (end - pd.offsets.QuarterBegin(startingMonth=1)).normalize()
    s = px[(px.index > start - pd.Timedelta(days=1)) & (px.index <= end)]
    return float(s.mean()) if len(s) else None


def _split_factor(splits, period):
    """How many of today's shares one share at `period` became."""
    if splits is None or not len(splits):
        return 1.0
    f = 1.0
    for d, r in splits.items():
        if pd.Timestamp(d) > pd.Timestamp(period) and r and r > 0:
            f *= float(r)
    return f


def position_history(qs, key, px, splits):
    """Estimated average cost (today's share basis) and the quarter the current holding began."""
    shares = cost = 0.0
    since = None
    before = False
    first_q = qs[0]["period"]
    for q in qs:
        r = q["rows"].get(key)
        new = (r["shares"] if r else 0.0) * _split_factor(splits, q["period"])
        if new <= 0:
            shares = cost = 0.0
            since, before = None, False
            continue
        avg = _qavg(px, q["period"]) if px is not None else None
        if shares <= 0:
            since = q["period"]
            before = q["period"] == first_q
        if new > shares and avg:
            cost += (new - shares) * avg
        elif new < shares and shares > 0:
            cost *= new / shares
        shares = new
    return {"shares": shares, "avgCost": (cost / shares) if shares and cost else None, "since": since, "before": before}


def moves(prev, cur, names, adj=None):
    """New, added, trimmed and sold positions between two quarters (stock positions only).
    adj(cusip, period) gives the split factor, so a stock split is not mistaken for buying."""
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
        r = b or a
        out.append({"key": k, "cusip": r["cusip"], "issuer": names(r["cusip"], r["issuer"]), "act": act,
                    "chg": None if ch is None else round(ch, 1), "shares": sb, "prevShares": sa,
                    "value": (b or {}).get("value", 0.0)})
    rank = {"New": 0, "Sold out": 1, "Added": 2, "Trimmed": 3}
    out.sort(key=lambda m: (rank[m["act"]], -(m["value"] or 0)))
    return out


def _title(s):
    s = (s or "").strip()
    return s.title() if s.isupper() else s


def build(content, data_dir, price_loader):
    """Returns the page block. price_loader(tickers) -> (closes: dict[ticker, Series], splits: dict)."""
    cfg = content.get("legends") or LEGENDS
    legends_dir = data_dir / "legends"
    funds = []
    for L in cfg:
        try:
            edgar_name, qs = quarters(int(L["cik"]), L.get("match", L["name"]), legends_dir / str(L["cik"]))
        except Exception as e:
            print(f"Legend {L['name']} unavailable: {e}", file=sys.stderr)
            continue
        if not qs:
            continue
        funds.append((L, edgar_name, qs))
    if not funds:
        return None

    # tickers we need: latest top holdings plus everything that moved in the last few filings
    need = set()
    for L, _, qs in funds:
        cur = qs[-1]["rows"]
        stock = sorted((r for k, r in cur.items() if not r["putCall"]), key=lambda r: -r["value"])
        need |= {r["cusip"] for r in stock[:TOP_N]}
        need |= {r["cusip"] for r in cur.values() if r["putCall"]}
        for i in range(max(1, len(qs) - MOVE_FILINGS), len(qs)):
            need |= {m["cusip"] for m in moves(qs[i - 1], qs[i], lambda c, n: n)}
    figi = map_cusips(sorted(need), legends_dir / "cusip-map.json")
    tick = lambda c: (figi.get(c) or {}).get("t")
    closes, splits = price_loader(sorted({tick(c) for c in need if tick(c)}))

    out = []
    for L, edgar_name, qs in funds:
        cur, total = qs[-1], 0.0
        stock = [(k, r) for k, r in cur["rows"].items() if not r["putCall"]]
        total = sum(r["value"] for _, r in stock) or 1.0
        stock.sort(key=lambda kr: -kr[1]["value"])
        holdings = []
        for k, r in stock[:TOP_N]:
            t = tick(r["cusip"])
            px = closes.get(t) if t else None
            h = position_history(qs, k, px, splits.get(t) if t else None)
            last = float(px.iloc[-1]) if px is not None and len(px) else None
            gain = (last / h["avgCost"] - 1) * 100 if last and h["avgCost"] and not h["before"] else None
            holdings.append({"t": t, "name": _title(r["issuer"]), "cls": r["cls"], "w": round(r["value"] / total * 100, 2),
                             "value": round(r["value"]), "shares": round(h["shares"] or r["shares"]),
                             "avgCost": None if h["before"] or not h["avgCost"] else round(h["avgCost"], 2),
                             "last": None if last is None else round(last, 2),
                             "gain": None if gain is None else round(gain, 1),
                             "qtrChg": (round((last / closes[t][closes[t].index <= pd.Timestamp(cur["period"])].iloc[-1] - 1) * 100, 1)
                                        if last and t in closes and len(closes[t][closes[t].index <= pd.Timestamp(cur["period"])]) else None),
                             "since": h["since"], "before": h["before"]})
        options = [{"t": tick(r["cusip"]), "name": _title(r["issuer"]), "type": r["putCall"], "value": round(r["value"])}
                   for k, r in sorted(cur["rows"].items(), key=lambda kr: -kr[1]["value"]) if r["putCall"]]
        hist_moves = []
        for i in range(len(qs) - 1, max(0, len(qs) - 1 - MOVE_FILINGS), -1):
            if i < 1:
                break
            adj = lambda c, p: _split_factor(splits.get(tick(c)) if tick(c) else None, p)
            ms = moves(qs[i - 1], qs[i], lambda c, n: _title(n), adj)
            for m in ms:
                t = tick(m["cusip"])
                px = closes.get(t) if t else None
                m["t"] = t
                m["estPrice"] = round(_qavg(px, qs[i]["period"]), 2) if px is not None and _qavg(px, qs[i]["period"]) else None
                m.pop("key", None)
                m.pop("cusip", None)
            hist_moves.append({"period": qs[i]["period"], "filed": qs[i]["filed"], "acc": qs[i]["acc"],
                               "counts": {a: sum(1 for m in ms if m["act"] == a) for a in ("New", "Added", "Trimmed", "Sold out")},
                               "moves": ms[:25], "more": max(0, len(ms) - 25)})
        out.append({"cik": int(L["cik"]), "name": L["name"], "people": L.get("people", ""), "edgarName": edgar_name,
                    "period": cur["period"], "filed": cur["filed"], "acc": cur["acc"], "amended": cur.get("amended"),
                    "total": round(sum(r["value"] for _, r in stock)), "positions": len(stock), "options": options,
                    "holdings": holdings, "moves": hist_moves, "firstPeriod": qs[0]["period"],
                    "secUrl": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={int(L['cik'])}&type=13F-HR"})
    return {"funds": out, "reported": content.get("reported", []), "berkshireCash": content.get("berkshireCash", []),
            "built": datetime.utcnow().strftime("%Y-%m-%d")}

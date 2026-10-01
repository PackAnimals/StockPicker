#!/usr/bin/env python3
"""Build data.json for Quarterly Leaders.

Top 3 gainers per quarter (first trading day's open -> last trading day's close) for the
S&P 500, Dow 30, Nasdaq-100 and Russell 2000, using TODAY's index members.
Prices, rating changes and earnings come from Yahoo Finance via yfinance (split-adjusted).
"""
import datetime as dt
import io
import json
import math
import time

import pandas as pd
import requests
import yfinance as yf

N_QUARTERS = 20
INDEX_SYMBOLS = {"S": "^GSPC", "D": "^DJI", "N": "^NDX", "R": "^RUT"}
UA = {"User-Agent": "Mozilla/5.0 (compatible; StockPicker/1.0; +https://github.com/PackAnimals/StockPicker)"}


def norm(t):
    return str(t).strip().upper().replace(".", "-").replace("/", "-")


def get(url):
    for i in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=60)
            r.raise_for_status()
            return r.text
        except Exception as e:  # noqa: BLE001
            print("retry", url, e)
            time.sleep(5 * (i + 1))
    raise RuntimeError("failed to fetch " + url)


def wiki_table(url, col, lo, hi):
    for t in pd.read_html(io.StringIO(get(url))):
        if col in [str(c) for c in t.columns] and lo <= len(t) <= hi:
            return t
    raise RuntimeError(f"no table with column {col} at {url}")


def sp500():
    t = wiki_table("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol", 480, 520)
    return {norm(r["Symbol"]): str(r["Security"]) for _, r in t.iterrows()}


def ndx():
    t = wiki_table("https://en.wikipedia.org/wiki/Nasdaq-100", "Ticker", 95, 110)
    name = "Company" if "Company" in t.columns else t.columns[0]
    return {norm(r["Ticker"]): str(r[name]) for _, r in t.iterrows()}


def dow():
    t = wiki_table("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average", "Symbol", 29, 31)
    return {norm(r["Symbol"]): str(r["Company"]) for _, r in t.iterrows()}


def r2000():
    # iShares Russell 2000 ETF (IWM) holdings = current Russell 2000 members
    url = ("https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax"
           "?fileType=csv&fileName=IWM_holdings&dataType=fund")
    lines = get(url).splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("Ticker"))
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])), on_bad_lines="skip")
    df = df[df["Asset Class"] == "Equity"]
    out = {}
    for _, r in df.iterrows():
        t = r["Ticker"]
        if isinstance(t, str) and t.strip() not in ("", "-"):
            out[norm(t)] = str(r["Name"]).title()
    return out


def completed_quarters(today):
    y, m = today.year, 3 * ((today.month - 1) // 3) + 1  # current quarter start
    qs = []
    for _ in range(N_QUARTERS):
        m -= 3
        if m < 1:
            m += 12
            y -= 1
        s = dt.date(y, m, 1)
        ny, nm = (y + 1, m + 3 - 12) if m + 3 > 12 else (y, m + 3)
        qs.insert(0, (y, (m - 1) // 3 + 1, s, dt.date(ny, nm, 1) - dt.timedelta(days=1)))
    return qs


def download(tickers, start, end):
    O, C = [], []
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        df = None
        for attempt in range(3):
            try:
                df = yf.download(chunk, start=start, end=end, auto_adjust=False, actions=False,
                                 progress=False, threads=True, group_by="column")
                break
            except Exception as e:  # noqa: BLE001
                print("download retry", e)
                time.sleep(15 * (attempt + 1))
        if df is None or df.empty:
            continue
        o, c = df["Open"], df["Close"]
        if isinstance(o, pd.Series):
            o, c = o.to_frame(chunk[0]), c.to_frame(chunk[0])
        O.append(o)
        C.append(c)
        print(f"prices {i + len(chunk)}/{len(tickers)}")
        time.sleep(2)
    O, C = pd.concat(O, axis=1), pd.concat(C, axis=1)
    O = O.loc[:, ~O.columns.duplicated()].sort_index()
    C = C.loc[:, ~C.columns.duplicated()].sort_index()
    return O, C


def num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def rnd(x):
    x = num(x)
    if x is None:
        return None
    return round(x, 2) if abs(x) >= 10 else round(x, 4)


def analyst(t, since):
    out = []
    try:
        ud = yf.Ticker(t).upgrades_downgrades
        if ud is None or ud.empty:
            return out
        ud = ud.reset_index()
        for _, r in ud.iterrows():
            d = pd.Timestamp(r["GradeDate"]).date()
            if d < since:
                continue
            out.append([d.isoformat(), str(r.get("Firm") or ""), str(r.get("FromGrade") or ""),
                        str(r.get("ToGrade") or ""), str(r.get("Action") or ""), rnd(r.get("currentPriceTarget"))])
    except Exception as e:  # noqa: BLE001
        print("analyst", t, e)
    return sorted(out, key=lambda r: r[0])


def earnings(t):
    out = []
    try:
        ed = yf.Ticker(t).get_earnings_dates(limit=28)
        if ed is None or ed.empty:
            return out
        for idx, r in ed.iterrows():
            rep = num(r.get("Reported EPS"))
            if rep is None:
                continue
            out.append([pd.Timestamp(idx).date().isoformat(), num(r.get("EPS Estimate")), rep, num(r.get("Surprise(%)"))])
    except Exception as e:  # noqa: BLE001
        print("earnings", t, e)
    return sorted(out, key=lambda r: r[0])


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    return o


def main():
    lists = {"S": sp500(), "D": dow(), "N": ndx(), "R": r2000()}
    names, members = {}, {}
    for code, d in lists.items():
        print(code, len(d), "members")
        for t, n in d.items():
            names.setdefault(t, n)
            members[t] = members.get(t, "") + code

    quarters = completed_quarters(dt.date.today())
    start = quarters[0][2] - dt.timedelta(days=10)
    end = quarters[-1][3] + dt.timedelta(days=1)
    O, C = download(sorted(members) + list(INDEX_SYMBOLS.values()), start, end)
    cal = C["^GSPC"].dropna().index

    out_q, qfirst, tracked = [], [], []
    for y, q, s, e in quarters:
        days = cal[(cal >= pd.Timestamp(s)) & (cal <= pd.Timestamp(e))]
        first, last = days[0], days[-1]
        qfirst.append(first)
        op, cl = O.loc[first], C.loc[last]
        ret = pd.to_numeric(cl / op - 1, errors="coerce")
        ret = ret[(op > 0) & ret.notna() & (ret < 15)]
        entry = {"year": y, "q": q, "start": s.isoformat(), "end": e.isoformat(), "idx": {}}
        for code, sym in INDEX_SYMBOLS.items():
            mem = [t for t in members if code in members[t] and t in ret.index]
            top = ret[mem].nlargest(3)
            entry["idx"][code] = {
                "ret": num(ret.get(sym)),
                "top": [{"ticker": t, "name": names[t], "ret": round(float(v), 6)} for t, v in top.items()],
            }
            tracked += [t for t in top.index if t not in tracked]
        out_q.append(entry)
        print(f"Q{q} {y} done")

    dates = cal[(cal >= pd.Timestamp(quarters[0][2])) & (cal <= pd.Timestamp(quarters[-1][3]))]

    def series(t):
        return {"c": [rnd(v) for v in C[t].reindex(dates)], "o": [rnd(O[t].get(f)) for f in qfirst]}

    since = quarters[0][2] - dt.timedelta(days=400)
    an, ea = {}, {}
    for i, t in enumerate(tracked):
        an[t] = analyst(t, since)
        ea[t] = earnings(t)
        print(f"details {i + 1}/{len(tracked)} {t}")
        time.sleep(0.5)

    data = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "dates": [d.strftime("%Y-%m-%d") for d in dates],
        "quarters": out_q,
        "names": {t: names[t] for t in tracked},
        "members": {t: members[t] for t in tracked},
        "series": {t: series(t) for t in tracked},
        "index": {code: series(sym) for code, sym in INDEX_SYMBOLS.items()},
        "analyst": an,
        "earnings": ea,
    }
    with open("data.json", "w") as f:
        json.dump(clean(data), f, separators=(",", ":"), allow_nan=False)
    print("wrote data.json with", len(tracked), "tracked tickers")


if __name__ == "__main__":
    main()

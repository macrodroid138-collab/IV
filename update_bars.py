from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
import json
import os

import pandas as pd
import yfinance as yf

TICKER = "SPY"
ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.abspath(__file__))

ODTE_IV_CSV = os.path.join(ROOT, f"{TICKER}_0dte_iv_ticks.csv")
DTE1_IV_CSV = os.path.join(ROOT, f"{TICKER}_1dte_iv_ticks.csv")
ODTE_BARS = os.path.join(ROOT, f"{TICKER}_0dte_bars.json")
DTE1_BARS = os.path.join(ROOT, f"{TICKER}_1dte_bars.json")


def is_rth(now_et=None):
    now_et = now_et or datetime.now(ET)
    if now_et.weekday() >= 5:
        return False
    t = now_et.time()
    return dtime(9, 30) <= t < dtime(16, 0)


def get_atm_iv(ticker_obj, spot, expiration):
    chain = ticker_obj.option_chain(expiration)
    calls, puts = chain.calls.copy(), chain.puts.copy()
    if calls.empty or puts.empty:
        return None
    calls["dist"] = (calls["strike"] - spot).abs()
    puts["dist"] = (puts["strike"] - spot).abs()
    c = calls.loc[calls["dist"].idxmin()]
    p = puts.loc[puts["dist"].idxmin()]
    for leg in (c, p):
        bid = float(leg.get("bid") or 0)
        ask = float(leg.get("ask") or 0)
        iv = float(leg.get("impliedVolatility") or 0)
        if bid <= 0 or ask <= bid or iv < 0.01:
            return None
    return round(((float(c["impliedVolatility"]) + float(p["impliedVolatility"])) / 2.0) * 100, 2)


def filter_rth(df):
    if df.empty:
        return df
    idx = df.index
    et = idx.tz_localize(ET) if getattr(idx, "tz", None) is None else idx.tz_convert(ET)
    mask = (et.day_of_week < 5) & (
        ((et.hour > 9) | ((et.hour == 9) & (et.minute >= 30))) & (et.hour < 16)
    )
    out = df.loc[mask].copy()
    out.index = et[mask].tz_localize(None)
    return out


def to_lwc_bars(df):
    bars = []
    for ts, row in df.iterrows():
        et_ts = ts.replace(tzinfo=ET) if ts.tzinfo is None else ts.tz_convert(ET)
        bars.append({
            "time": int(et_ts.astimezone(ZoneInfo("UTC")).timestamp()),
            "open": round(float(row["Open"]), 4),
            "high": round(float(row["High"]), 4),
            "low": round(float(row["Low"]), 4),
            "close": round(float(row["Close"]), 4),
        })
    return bars


def price_10m(ticker):
    raw = yf.Ticker(ticker).history(period="60d", interval="5m")
    if raw.empty:
        return pd.DataFrame()
    raw.index = raw.index.tz_convert(ET).tz_localize(None)
    px = (
        raw.resample("10min")
        .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"})
        .dropna()
    )
    return filter_rth(px)


def iv_10m(csv_path):
    if not os.path.exists(csv_path):
        return pd.DataFrame()
    df = pd.read_csv(csv_path, parse_dates=["Timestamp"])
    if df.empty:
        return pd.DataFrame()
    df = df.set_index("Timestamp").sort_index()
    col = "ATM_IV" if "ATM_IV" in df.columns else "ATM_0DTE_IV"
    out = df[col].resample("10min").agg(["first", "max", "min", "last"]).dropna()
    out.columns = ["Open", "High", "Low", "Close"]
    return filter_rth(out)


def write_bars(ticker, book, expiration, iv_csv, out_json):
    px = price_10m(ticker)
    if px.empty:
        return False
    iv = iv_10m(iv_csv)
    payload = {
        "ticker": ticker,
        "book": book,
        "expiration": expiration,
        "interval": "10m",
        "updated": datetime.now(ET).isoformat(),
        "price": to_lwc_bars(px),
        "iv": to_lwc_bars(iv) if not iv.empty else [],
    }
    with open(out_json, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    return True


def append_iv(csv_path, price, atm_iv, expiration, book):
    row = pd.DataFrame([{
        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Price": round(price, 2),
        "ATM_IV": atm_iv,
        "Expiration": expiration,
        "Book": book,
    }])
    row.to_csv(csv_path, mode="a", header=not os.path.exists(csv_path), index=False)


def main():
    now_et = datetime.now(ET)
    rth = is_rth(now_et)
    tkr = yf.Ticker(TICKER)
    spot = float(tkr.fast_info["last_price"])
    exps = list(tkr.options)
    exp_0 = exps[0] if exps else None
    exp_1 = exps[1] if len(exps) > 1 else None

    print(f"{now_et} RTH={rth} {TICKER}=${spot:.2f} 0DTE={exp_0} 1DTE={exp_1}")

    if exp_0:
        if rth:
            iv0 = get_atm_iv(tkr, spot, exp_0)
            if iv0 is not None:
                append_iv(ODTE_IV_CSV, spot, iv0, exp_0, "0DTE")
                print(f"0DTE IV appended: {iv0}")
            else:
                print("0DTE IV skipped (stale quotes)")
        else:
            print("0DTE IV skipped (outside RTH)")
        write_bars(TICKER, "0DTE", exp_0, ODTE_IV_CSV, ODTE_BARS)

    if exp_1:
        iv1 = get_atm_iv(tkr, spot, exp_1)
        if iv1 is not None:
            append_iv(DTE1_IV_CSV, spot, iv1, exp_1, "1DTE")
            print(f"1DTE IV appended: {iv1}")
        else:
            print("1DTE IV skipped (stale quotes)")
        write_bars(TICKER, "1DTE", exp_1, DTE1_IV_CSV, DTE1_BARS)

    print("done")


if __name__ == "__main__":
    main()
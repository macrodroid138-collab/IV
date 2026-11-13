from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import json
import os

import pandas as pd
import yfinance as yf

TICKER = "SPY"
ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.abspath(__file__))
RETENTION_DAYS = 84

ODTE_IV_CSV = os.path.join(ROOT, f"{TICKER}_0dte_iv_ticks.csv")
DTE1_IV_CSV = os.path.join(ROOT, f"{TICKER}_1dte_iv_ticks.csv")
ODTE_BARS = os.path.join(ROOT, f"{TICKER}_0dte_bars.json")
DTE1_BARS = os.path.join(ROOT, f"{TICKER}_1dte_bars.json")


def is_rth(now_et=None):
    """Mon–Fri 09:30–16:00 ET only."""
    now_et = now_et or datetime.now(ET)
    if now_et.weekday() >= 5:
        return False
    t = now_et.time()
    return dtime(9, 30) <= t < dtime(16, 0)


def get_atm_iv(ticker_obj, spot, expiration, strict_quotes=True):
    """
    ATM call/put mid IV (%).
    strict_quotes=True  -> require live bid/ask (0DTE / RTH)
    strict_quotes=False -> accept Yahoo IV even if bid/ask flat (next expiry off-hours)
    """
    chain = ticker_obj.option_chain(expiration)
    calls, puts = chain.calls.copy(), chain.puts.copy()
    if calls.empty or puts.empty:
        return None

    calls["dist"] = (calls["strike"] - spot).abs()
    puts["dist"] = (puts["strike"] - spot).abs()
    c = calls.loc[calls["dist"].idxmin()]
    p = puts.loc[puts["dist"].idxmin()]

    iv_c = float(c.get("impliedVolatility") or 0)
    iv_p = float(p.get("impliedVolatility") or 0)
    if iv_c < 0.01 or iv_p < 0.01:
        return None

    if strict_quotes:
        for leg in (c, p):
            bid = float(leg.get("bid") or 0)
            ask = float(leg.get("ask") or 0)
            if bid <= 0 or ask <= bid:
                return None

    return round(((iv_c + iv_p) / 2.0) * 100, 2)


def cleanup_old(csv_path):
    if not os.path.exists(csv_path):
        return
    df = pd.read_csv(csv_path, parse_dates=["Timestamp"])
    if df.empty:
        return
    cutoff = datetime.now() - pd.Timedelta(days=RETENTION_DAYS)
    df[df["Timestamp"] >= cutoff].to_csv(csv_path, index=False)


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
        bars.append(
            {
                "time": int(et_ts.astimezone(ZoneInfo("UTC")).timestamp()),
                "open": round(float(row["Open"]), 4),
                "high": round(float(row["High"]), 4),
                "low": round(float(row["Low"]), 4),
                "close": round(float(row["Close"]), 4),
            }
        )
    return bars


def iv_10m_from_csv(csv_path, rth_only=False):
    if not os.path.exists(csv_path):
        return pd.DataFrame()
    df = pd.read_csv(csv_path, parse_dates=["Timestamp"])
    if df.empty:
        return pd.DataFrame()
    df = df.set_index("Timestamp").sort_index()
    col = "ATM_IV" if "ATM_IV" in df.columns else "ATM_0DTE_IV"
    out = df[col].resample("10min").agg(["first", "max", "min", "last"]).dropna()
    out.columns = ["Open", "High", "Low", "Close"]
    return filter_rth(out) if rth_only else out


def write_iv_bars(ticker, book, expiration, iv_csv, out_json, spot, rth_only=False):
    iv = iv_10m_from_csv(iv_csv, rth_only=rth_only)
    payload = {
        "ticker": ticker,
        "book": book,
        "expiration": expiration,
        "interval": "10m",
        "spot": round(float(spot), 2) if spot is not None else None,
        "updated": datetime.now(ET).isoformat(),
        "iv": to_lwc_bars(iv) if not iv.empty else [],
    }
    with open(out_json, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"{book}: wrote {len(payload['iv'])} IV 10m bars -> {os.path.basename(out_json)}")
    return len(payload["iv"])


def append_iv(csv_path, price, atm_iv, expiration, book):
    row = pd.DataFrame(
        [
            {
                "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Price": round(price, 2),
                "ATM_IV": atm_iv,
                "Expiration": expiration,
                "Book": book,
            }
        ]
    )
    row.to_csv(csv_path, mode="a", header=not os.path.exists(csv_path), index=False)


def main():
    now_et = datetime.now(ET)
    rth = is_rth(now_et)
    tkr = yf.Ticker(TICKER)
    spot = float(tkr.fast_info["last_price"])
    exps = list(tkr.options)

    # First two listed expiries: nearest + next
    exp_0 = exps[0] if exps else None
    exp_1 = exps[1] if len(exps) > 1 else None

    print(f"{now_et} RTH={rth} {TICKER}=${spot:.2f} nearest={exp_0} next={exp_1}")
    cleanup_old(ODTE_IV_CSV)
    cleanup_old(DTE1_IV_CSV)

    # ----- nearest / 0DTE: RTH ONLY -----
    if exp_0:
        if rth:
            iv0 = get_atm_iv(tkr, spot, exp_0, strict_quotes=True)
            if iv0 is not None:
                append_iv(ODTE_IV_CSV, spot, iv0, exp_0, "0DTE")
                print(f"0DTE IV appended: {iv0}%")
            else:
                print("0DTE IV skipped (stale quotes)")
        else:
            print("0DTE IV skipped (outside RTH)")
        write_iv_bars(
            TICKER, "0DTE", exp_0, ODTE_IV_CSV, ODTE_BARS, spot, rth_only=True
        )
    else:
        print("no nearest expiry")

    # ----- next expiry (1DTE / weekend-holiday equivalent): ALWAYS -----
    if exp_1:
        iv1 = get_atm_iv(tkr, spot, exp_1, strict_quotes=False)
        if iv1 is not None:
            append_iv(DTE1_IV_CSV, spot, iv1, exp_1, "1DTE")
            print(f"NEXT IV appended: {iv1}% (exp {exp_1})")
        else:
            print(f"NEXT IV skipped (no usable IV on exp {exp_1})")
        write_iv_bars(
            TICKER, "1DTE", exp_1, DTE1_IV_CSV, DTE1_BARS, spot, rth_only=False
        )
    else:
        print("no next expiry")

    print("done")


if __name__ == "__main__":
    main()

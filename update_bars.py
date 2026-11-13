from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import json
import math
import os

import pandas as pd
import yfinance as yf

TICKER = "SPY"
ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.abspath(__file__))
RETENTION_DAYS = 84
RISK_FREE = 0.05
MIN_IV = 0.01  # 1% absolute vol floor for usable Yahoo IV

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


def get_spot(ticker_obj):
    """Yahoo fast_info can flake; fall back to last daily close."""
    try:
        spot = ticker_obj.fast_info.get("last_price")
        if spot is not None and float(spot) > 0:
            return float(spot)
    except Exception:
        pass
    hist = ticker_obj.history(period="5d")
    if hist is None or hist.empty:
        return None
    return float(hist["Close"].dropna().iloc[-1])


def years_to_expiry(expiration, now_et=None):
    """Calendar years until 16:00 ET on the listed expiry date."""
    now_et = now_et or datetime.now(ET)
    exp_dt = datetime.fromisoformat(expiration).replace(
        hour=16, minute=0, second=0, microsecond=0, tzinfo=ET
    )
    secs = max((exp_dt - now_et).total_seconds(), 60.0)
    return secs / (365.25 * 24 * 3600)


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(spot, strike, t_years, rate, sigma, kind):
    if t_years <= 0 or sigma <= 0:
        intrinsic = max(0.0, spot - strike) if kind == "call" else max(0.0, strike - spot)
        return intrinsic
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years) / (
        sigma * sqrt_t
    )
    d2 = d1 - sigma * sqrt_t
    if kind == "call":
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * t_years) * _norm_cdf(d2)
    return strike * math.exp(-rate * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol_from_price(price, spot, strike, t_years, kind, rate=RISK_FREE):
    """Bisection BS IV. Returns decimal vol or None."""
    if price is None or price <= 0 or spot <= 0 or strike <= 0 or t_years <= 0:
        return None
    # Reject prices below intrinsic (stale/crossed last trades).
    disc_k = strike * math.exp(-rate * t_years)
    intrinsic = max(0.0, spot - disc_k) if kind == "call" else max(0.0, disc_k - spot)
    if price < intrinsic * 0.98:
        return None

    lo, hi = 1e-4, 5.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        model = _bs_price(spot, strike, t_years, rate, mid, kind)
        if model > price:
            hi = mid
        else:
            lo = mid
    iv = 0.5 * (lo + hi)
    if iv < MIN_IV or iv > 4.0:
        return None
    return iv


def _leg_quote_price(leg, strict_quotes):
    bid = float(leg.get("bid") or 0)
    ask = float(leg.get("ask") or 0)
    last = float(leg.get("lastPrice") or 0)
    live = bid > 0 and ask > bid
    if strict_quotes and not live:
        return None, False
    if live:
        return 0.5 * (bid + ask), True
    if last > 0:
        return last, False
    return None, False


def _leg_iv(leg, spot, t_years, kind, strict_quotes):
    yahoo_iv = float(leg.get("impliedVolatility") or 0)
    price, live = _leg_quote_price(leg, strict_quotes)
    if price is None:
        return None

    # Prefer Yahoo IV when it looks real AND we have a live quote (RTH),
    # or when loose mode and Yahoo IV is usable.
    if yahoo_iv >= MIN_IV:
        if (not strict_quotes) or live:
            return yahoo_iv

    # Workaround: Yahoo often posts ~0 ATM IV with flat bid/ask off-hours.
    # Back out IV from mid (preferred) or lastPrice via Black-Scholes.
    return implied_vol_from_price(
        price, spot, float(leg.get("strike") or 0), t_years, kind
    )


def get_atm_iv(ticker_obj, spot, expiration, strict_quotes=True):
    """
    ATM call/put mid IV (%).
    strict_quotes=True  -> require live bid/ask (0DTE / RTH)
    strict_quotes=False -> accept Yahoo IV or BS-from-last even if bid/ask flat
    """
    chain = ticker_obj.option_chain(expiration)
    calls, puts = chain.calls.copy(), chain.puts.copy()
    if calls.empty or puts.empty:
        return None

    calls["dist"] = (calls["strike"] - spot).abs()
    puts["dist"] = (puts["strike"] - spot).abs()
    c = calls.loc[calls["dist"].idxmin()]
    p = puts.loc[puts["dist"].idxmin()]

    t_years = years_to_expiry(expiration)
    iv_c = _leg_iv(c, spot, t_years, "call", strict_quotes)
    iv_p = _leg_iv(p, spot, t_years, "put", strict_quotes)
    if iv_c is None or iv_p is None:
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
    # Always store America/New_York wall-clock (Actions runners are UTC).
    row = pd.DataFrame(
        [
            {
                "Timestamp": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                "Price": round(price, 2),
                "ATM_IV": atm_iv,
                "Expiration": expiration,
                "Book": book,
            }
        ]
    )
    row.to_csv(csv_path, mode="a", header=not os.path.exists(csv_path), index=False)


TZ_MARKER = os.path.join(ROOT, ".iv_timestamps_et")


def migrate_csv_utc_wallclock_to_et(csv_path):
    """
    Older ticks used datetime.now() on UTC runners, so the naive Timestamp was
    UTC wall-clock. Chart code treats naive stamps as ET — that made 05:10 ET
    display as 10:10. Convert once, then new rows are written in ET.
    """
    if not os.path.exists(csv_path):
        return
    df = pd.read_csv(csv_path, parse_dates=["Timestamp"])
    if df.empty:
        return
    et = df["Timestamp"].dt.tz_localize("UTC").dt.tz_convert(ET)
    df["Timestamp"] = et.dt.strftime("%Y-%m-%d %H:%M:%S")
    df.to_csv(csv_path, index=False)
    print(f"migrated {os.path.basename(csv_path)} timestamps UTC→ET")


def ensure_et_timestamps():
    if os.path.exists(TZ_MARKER):
        return
    migrate_csv_utc_wallclock_to_et(ODTE_IV_CSV)
    migrate_csv_utc_wallclock_to_et(DTE1_IV_CSV)
    with open(TZ_MARKER, "w") as f:
        f.write("et\n")
    print("timestamp basis: America/New_York")


def main():
    now_et = datetime.now(ET)
    rth = is_rth(now_et)
    tkr = yf.Ticker(TICKER)
    spot = get_spot(tkr)
    if spot is None:
        print("no spot — abort")
        return
    exps = list(tkr.options)

    # First two listed expiries: nearest + next
    exp_0 = exps[0] if exps else None
    exp_1 = exps[1] if len(exps) > 1 else None

    print(f"{now_et} RTH={rth} {TICKER}=${spot:.2f} nearest={exp_0} next={exp_1}")
    ensure_et_timestamps()
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

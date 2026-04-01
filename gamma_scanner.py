#!/usr/bin/env python3
"""
S&P 500 Gamma Flip Scanner
===========================
Scans all S&P 500 tickers using yfinance, computes the gamma flip (gamma-neutral)
price level for each stock using Black-Scholes gamma estimation, and outputs a
JSON file that can be loaded into the web dashboard.

Usage:
    pip install yfinance pandas numpy scipy
    python gamma_scanner.py [--tickers AAPL MSFT ...] [--output results.json] [--workers 8]

Output JSON schema:
    {
      "generated": "2024-01-15T10:30:00",
      "spy_price": 478.5,
      "results": [
        {
          "ticker": "AAPL",
          "price": 185.2,
          "gamma_flip": 182.5,
          "net_gamma": 1234567.89,
          "distance_pct": 1.48,
          "above_flip": true,
          "sector": "Technology",
          "market_cap": 2.85e12,
          "gamma_profile": [
            {"strike": 180, "net_gamma_exp": 500000},
            ...
          ]
        },
        ...
      ]
    }
"""

import json
import math
import time
import argparse
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
RISK_FREE_RATE = 0.053          # ~current 3-month T-bill rate
MAX_EXPIRY_DAYS = 60            # only use options expiring within this window
MIN_OPEN_INTEREST = 10          # skip strikes with tiny OI
GAMMA_PROFILE_STRIKES = 40      # number of strike points in exposure chart

# S&P 500 sector map (abbreviated; expand as needed)
SECTOR_MAP = {
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "GOOGL": "Communication", "GOOG": "Communication", "META": "Communication",
    "AMZN": "Consumer Cyclical", "TSLA": "Consumer Cyclical",
    "BRK-B": "Financial", "JPM": "Financial", "V": "Financial", "MA": "Financial",
    "JNJ": "Healthcare", "UNH": "Healthcare", "LLY": "Healthcare",
    "XOM": "Energy", "CVX": "Energy",
    "PG": "Consumer Defensive", "KO": "Consumer Defensive", "WMT": "Consumer Defensive",
}


# ── Black-Scholes helpers ─────────────────────────────────────────────────────
def bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Compute Black-Scholes gamma for a single option."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return norm.pdf(d1) / (S * sigma * math.sqrt(T))


def compute_gamma_exposure(chain_calls: pd.DataFrame,
                           chain_puts: pd.DataFrame,
                           spot: float,
                           T: float) -> pd.DataFrame:
    """
    Compute net gamma exposure (GEX) per strike.
    GEX = gamma * OI * 100 * spot^2 * 0.01
    Calls add positive GEX; puts subtract.
    """
    rows = []
    all_strikes = sorted(set(chain_calls["strike"]).union(set(chain_puts["strike"])))

    for K in all_strikes:
        call_row = chain_calls[chain_calls["strike"] == K]
        put_row  = chain_puts[chain_puts["strike"] == K]

        call_iv = float(call_row["impliedVolatility"].values[0]) if not call_row.empty else 0
        put_iv  = float(put_row["impliedVolatility"].values[0])  if not put_row.empty  else 0
        call_oi = int(call_row["openInterest"].values[0])        if not call_row.empty else 0
        put_oi  = int(put_row["openInterest"].values[0])         if not put_row.empty  else 0

        if call_oi < MIN_OPEN_INTEREST and put_oi < MIN_OPEN_INTEREST:
            continue

        call_gamma = bs_gamma(spot, K, T, RISK_FREE_RATE, call_iv) if call_oi > 0 and call_iv > 0 else 0
        put_gamma  = bs_gamma(spot, K, T, RISK_FREE_RATE, put_iv)  if put_oi  > 0 and put_iv  > 0 else 0

        # Dollar gamma (per 1% move)
        dollar_gamma_scale = spot ** 2 * 0.01 * 100
        net_gex = (call_gamma * call_oi - put_gamma * put_oi) * dollar_gamma_scale

        rows.append({"strike": K, "net_gamma_exp": round(net_gex, 2)})

    return pd.DataFrame(rows)


def find_gamma_flip(gex_df: pd.DataFrame, spot: float) -> Optional[float]:
    """
    Interpolate the strike where cumulative gamma exposure crosses zero.
    Returns None if no crossing found.
    """
    if gex_df.empty:
        return None

    # Sort by strike, accumulate from highest strike downward (dealer perspective)
    df = gex_df.sort_values("strike", ascending=False).copy()
    df["cumulative"] = df["net_gamma_exp"].cumsum()

    # Find sign changes
    signs = np.sign(df["cumulative"].values)
    crossings = np.where(np.diff(signs))[0]

    if len(crossings) == 0:
        return None

    # Take the crossing closest to spot
    idx = crossings[np.argmin(abs(df["strike"].values[crossings] - spot))]
    s0, s1 = df["strike"].values[idx], df["strike"].values[idx + 1]
    g0, g1 = df["cumulative"].values[idx], df["cumulative"].values[idx + 1]

    # Linear interpolation
    if g1 == g0:
        return float((s0 + s1) / 2)
    flip = s0 + (0 - g0) * (s1 - s0) / (g1 - g0)
    return round(float(flip), 2)


# ── Per-ticker scanner ────────────────────────────────────────────────────────
def scan_ticker(ticker_sym: str) -> Optional[dict]:
    """Fetch options data and compute gamma flip for one ticker."""
    try:
        t = yf.Ticker(ticker_sym)
        hist = t.history(period="1d")
        if hist.empty:
            log.warning(f"{ticker_sym}: no price data")
            return None

        spot = float(hist["Close"].iloc[-1])
        expirations = t.options
        if not expirations:
            log.warning(f"{ticker_sym}: no options listed")
            return None

        # Filter to near-term expirations only
        today = datetime.today().date()
        cutoff = today + timedelta(days=MAX_EXPIRY_DAYS)
        valid_exp = [e for e in expirations if datetime.strptime(e, "%Y-%m-%d").date() <= cutoff]

        if not valid_exp:
            valid_exp = [expirations[0]]  # fallback to nearest

        all_gex = []
        for exp in valid_exp:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            T = max((exp_date - today).days / 365.0, 1 / 365)
            try:
                chain = t.option_chain(exp)
                gex = compute_gamma_exposure(chain.calls, chain.puts, spot, T)
                all_gex.append(gex)
                time.sleep(0.05)  # gentle rate-limiting
            except Exception as e:
                log.debug(f"{ticker_sym} {exp}: {e}")
                continue

        if not all_gex:
            return None

        combined = pd.concat(all_gex).groupby("strike", as_index=False)["net_gamma_exp"].sum()

        flip = find_gamma_flip(combined, spot)
        total_gex = float(combined["net_gamma_exp"].sum())
        above_flip = (spot > flip) if flip is not None else None
        distance_pct = round((spot - flip) / spot * 100, 2) if flip else None

        # Build gamma profile (subset of strikes near spot for the chart)
        lo = spot * 0.85
        hi = spot * 1.15
        profile_df = combined[(combined["strike"] >= lo) & (combined["strike"] <= hi)].copy()
        profile_df = profile_df.nlargest(GAMMA_PROFILE_STRIKES, "strike") \
                                .sort_values("strike")
        profile = profile_df.to_dict(orient="records")

        info = t.info
        market_cap = info.get("marketCap")
        sector = info.get("sector") or SECTOR_MAP.get(ticker_sym, "Unknown")

        return {
            "ticker": ticker_sym,
            "price": round(spot, 2),
            "gamma_flip": flip,
            "net_gamma": round(total_gex, 0),
            "distance_pct": distance_pct,
            "above_flip": above_flip,
            "sector": sector,
            "market_cap": market_cap,
            "gamma_profile": profile,
        }

    except Exception as e:
        log.error(f"{ticker_sym}: {e}")
        return None


# ── S&P 500 ticker list ───────────────────────────────────────────────────────
def get_sp500_tickers() -> list[str]:
    """Fetch current S&P 500 constituents from Wikipedia."""
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        df = tables[0]
        tickers = df["Symbol"].str.replace(".", "-", regex=False).tolist()
        log.info(f"Loaded {len(tickers)} S&P 500 tickers from Wikipedia")
        return tickers
    except Exception as e:
        log.warning(f"Could not fetch S&P 500 list: {e} — using fallback list")
        # Abbreviated fallback (top 50)
        return [
            "AAPL","MSFT","NVDA","GOOGL","AMZN","META","BRK-B","TSLA","AVGO","LLY",
            "JPM","V","UNH","XOM","MA","PG","JNJ","HD","ABBV","MRK","CRM","CVX",
            "COST","BAC","NFLX","AMD","KO","WMT","PEP","TMO","CSCO","MCD","ACN",
            "ABT","ORCL","LIN","TXN","DHR","NEE","PM","NKE","QCOM","UPS","RTX",
            "AMGN","HON","CAT","SPGI","IBM","GS",
        ]


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="S&P 500 Gamma Flip Scanner")
    parser.add_argument("--tickers", nargs="+", help="Override ticker list (e.g. AAPL MSFT)")
    parser.add_argument("--output", default="gamma_results.json", help="Output JSON file path")
    parser.add_argument("--workers", type=int, default=6, help="Parallel workers (default 6)")
    parser.add_argument("--limit", type=int, default=None, help="Limit to first N tickers (testing)")
    args = parser.parse_args()

    tickers = args.tickers or get_sp500_tickers()
    if args.limit:
        tickers = tickers[: args.limit]

    log.info(f"Scanning {len(tickers)} tickers with {args.workers} workers…")
    log.info("This may take 10–25 minutes for the full S&P 500. Ctrl+C to abort early.")

    spy = yf.Ticker("SPY")
    spy_hist = spy.history(period="1d")
    spy_price = float(spy_hist["Close"].iloc[-1]) if not spy_hist.empty else None

    results = []
    errors = 0
    start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(scan_ticker, sym): sym for sym in tickers}
        for i, fut in enumerate(as_completed(futures), 1):
            sym = futures[fut]
            result = fut.result()
            if result:
                results.append(result)
                status = "✓" if result["gamma_flip"] else "~"
            else:
                errors += 1
                status = "✗"
            elapsed = time.time() - start
            eta = elapsed / i * (len(tickers) - i)
            log.info(f"[{i}/{len(tickers)}] {status} {sym:8s}  "
                     f"elapsed={elapsed:.0f}s  ETA={eta:.0f}s  "
                     f"ok={len(results)}  err={errors}")

    output = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "spy_price": spy_price,
        "total_scanned": len(tickers),
        "successful": len(results),
        "results": sorted(results, key=lambda x: abs(x["distance_pct"] or 999)),
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    log.info(f"\n✅ Done! {len(results)}/{len(tickers)} tickers processed.")
    log.info(f"Results saved to: {args.output}")
    log.info(f"Load this file into the web dashboard to visualize.")


if __name__ == "__main__":
    main()

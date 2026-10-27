"""Point-in-time inputs for the three selected strategies.

Historical cap input: date,symbol,market_cap,market,security_type.
Every date must contain the FULL KOSPI ordinary-share universe, not just top3.
Trading calendar: ETF dates. No forward fill across missing cap dates.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd

from research_paths import CODE, resolve_input

ROOT = CODE


def make_universe(caps: pd.DataFrame, calendar: list[str]) -> pd.DataFrame:
    required = {"date", "symbol", "market_cap", "market", "security_type", "full_universe"}
    if not required.issubset(caps):
        raise ValueError(f"Historical full-universe cap input needs {sorted(required)}")
    caps = caps.copy()
    caps["date"] = pd.to_datetime(caps.date).dt.strftime("%Y-%m-%d")
    caps["symbol"] = caps.symbol.astype(str).str.zfill(6)
    if caps.duplicated(["date", "symbol"]).any():
        raise ValueError("Duplicate daily security cap records")
    rows = []
    for prior, day in zip(calendar[:-1], calendar[1:]):
        raw = caps[caps.date.eq(prior)]
        if raw.empty:
            continue  # Never reuse a stale universe.
        if not raw.full_universe.astype(str).str.lower().isin(["true", "1"]).all():
            raise ValueError("Cap input must certify full historical universe coverage")
        x = raw[raw.market.eq("KOSPI") & raw.security_type.eq("ordinary")].copy()
        x.market_cap = pd.to_numeric(x.market_cap, errors="raise")
        if len(x) < 3 or (x.market_cap <= 0).any():
            raise ValueError("Invalid full-market-cap values")
        x = x.sort_values(["market_cap", "symbol"], ascending=[False, True]).head(3)
        for rank, row in enumerate(x.itertuples(), 1):
            rows.append({"effective_date": day, "asof_date": prior, "rank": rank,
                         "symbol": row.symbol, "market_cap": row.market_cap,
                         "weight": row.market_cap / x.market_cap.sum()})
    return pd.DataFrame(rows, columns=["effective_date", "asof_date", "rank", "symbol", "market_cap", "weight"])


def investor_gate(flows: pd.DataFrame, calendar: list[str], lookback=252) -> pd.DataFrame:
    required = {"date", "foreign_net_amount", "institution_net_amount", "scope"}
    if not required.issubset(flows):
        raise ValueError(f"Investor input needs {sorted(required)}")
    if not flows.scope.eq("KOSPI200_SPOT").all():
        raise ValueError("KOSPI/ETF/futures flow cannot replace KOSPI200 spot cash flow")
    x = flows.copy()
    x["date"] = pd.to_datetime(x.date).dt.strftime("%Y-%m-%d")
    if x.date.duplicated().any():
        raise ValueError("Duplicate investor dates")
    x = x.set_index("date").reindex(sorted(set(calendar) | set(x.date)))
    result = []
    for i, day in enumerate(calendar):
        if i == 0:
            continue
        prior = calendar[i - 1]
        historical = x.loc[x.index < prior].tail(lookback)
        value = x.loc[prior] if prior in x.index else pd.Series(dtype=float)
        f, o = value.get("foreign_net_amount", np.nan), value.get("institution_net_amount", np.nan)
        usable = len(historical) == lookback and historical[["foreign_net_amount", "institution_net_amount"]].notna().all().all() and pd.notna(f) and pd.notna(o)
        fq = oq = np.nan
        if usable:
            a, b = historical.foreign_net_amount.to_numpy(), historical.institution_net_amount.to_numpy()
            fq = float(np.mean(a < f) + 0.5 * np.mean(a == f))
            oq = float(np.mean(b < o) + 0.5 * np.mean(b == o))
        result.append({"date": day, "flow_asof_date": prior, "foreign_percentile": fq,
                       "institution_percentile": oq, "usable": usable,
                       "investor_gate": bool(usable and f > 0 and o < 0 and fq >= .7 and oq <= .3)})
    return pd.DataFrame(result)


def aggregate_chart_ticks(ticks: pd.DataFrame) -> pd.DataFrame:
    """Second batches: chart data lack a guaranteed within-second trade order.

    Give a tick-rule sign only to single-price seconds. Multi-price seconds
    remain unknown rather than sorting fabricated microsecond timestamps.
    Use one collection batch per symbol; overlapping downloads are alternatives.
    """
    if ticks.batch_id.nunique() != 1:
        raise ValueError("Do not concatenate overlapping tick collection batches")
    x = ticks.copy()
    x["timestamp"] = pd.to_datetime(x.timestamp)
    x["pv"] = x.close * x.volume
    s = x.groupby("timestamp", sort=True).agg(open=("close", "first"), close=("close", "last"),
           high=("close", "max"), low=("close", "min"), volume=("volume", "sum"),
           pv=("pv", "sum"), price_count=("close", "nunique"))
    # Intra-second opening/closing order is unknowable for multi-price seconds.
    # Minute OHLC comes from chart records ordered by server row sequence,
    # but this aggregator is an explicit proxy and is not a fill-quality tape.
    s["reference_price"] = s.pv / s.volume.replace(0, np.nan)
    direction = np.sign(s.reference_price.diff())
    direction[s.index.normalize().to_series(index=s.index).ne(s.index.normalize().to_series(index=s.index).shift())] = 0
    direction = direction.replace(0, np.nan).groupby(s.index.date).ffill().fillna(0)
    direction[s.price_count.ne(1)] = 0
    s["signed_volume"] = s.volume * direction
    s["unknown_volume"] = s.volume.where(direction.eq(0), 0)
    m = s.resample("1min").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                               "volume": "sum", "pv": "sum", "signed_volume": "sum", "unknown_volume": "sum"})
    m = m.dropna(subset=["close"]).reset_index()
    m["pressure"] = m.signed_volume / m.volume.replace(0, np.nan)
    m["vwap"] = m.pv / m.volume.replace(0, np.nan)
    m["flow_source"] = "chart_tick_rule_proxy_second_batches"
    return m


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--caps")
    p.add_argument("--calendar", default="market_1m_data/KODEX200_069500_1m_toss.csv")
    p.add_argument("--out", default="kiwoom_research_data/daily_top3_universe.csv")
    p.add_argument("--assemble-members", action="store_true")
    a = p.parse_args()
    a.calendar = resolve_input(a.calendar)
    a.out = resolve_input(a.out)
    if a.caps:
        a.caps = resolve_input(a.caps)
    if a.assemble_members:
        u = pd.read_csv(a.out, dtype={"symbol": str})
        parts = []
        for symbol in sorted(u.symbol.unique()):
            path = resolve_input(f"kiwoom_research_data/{symbol}_1m_kiwoom.parquet")
            if not path.exists():
                raise FileNotFoundError(f"Missing historical member bars: {symbol}")
            x = pd.read_parquet(path)
            x = x[x.date.isin(u.loc[u.symbol.eq(symbol), "effective_date"])]
            parts.append(x)
        result = pd.concat(parts, ignore_index=True).sort_values(["symbol", "timestamp"])
        result.to_parquet(resolve_input("kiwoom_research_data/top3_member_bars.parquet"), index=False)
        print(json.dumps({"member_bars": len(result), "members": len(parts)}))
        return
    if not a.caps:
        p.error("--caps is required unless --assemble-members is specified")
    dates = sorted(pd.read_csv(a.calendar, usecols=["date"]).date.unique())
    u = make_universe(pd.read_csv(a.caps, dtype={"symbol": str}), dates)
    u.to_csv(a.out, index=False)
    print(json.dumps({"days": u.effective_date.nunique(), "historical_members": sorted(u.symbol.unique())}))


if __name__ == "__main__":
    main()

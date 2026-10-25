"""Expiry-day-only NIFTY option selling: naked vs hedged (lower margin) variants. Research only, no orders.

Reuses data + pricing from app.option_study. Every trade: enter 09:20 on weekly expiry day, exit 15:15.

Margin estimate (APPROXIMATE - confirm with the broker's margin calculator on the day):
  * naked short options: ~12% of notional per short leg on SPAN+exposure, straddle offset -> 15% of notional
  * hedged (defined risk): max loss = (wing width - net credit) x lot, plus ~1.5% notional exposure
  * plus SEBI expiry-day extra ELM: 2% of notional per short leg (applies on expiry day)
"""
import os
from datetime import datetime

import pandas as pd

from app import option_study as S

OUT = S.OUT


def margin_estimate(spot, legs_spec, credit_pts):
    notional = spot * S.LOT
    shorts = [l for l in legs_spec if l[2] == -1]
    longs = [l for l in legs_spec if l[2] == +1]
    elm = 0.02 * notional * len(shorts)
    if not longs:
        base = 0.15 * notional if len(shorts) == 2 else 0.12 * notional
        return base + elm
    width_pct = max(abs(l[1]) for l in longs) - min(abs(l[1]) for l in shorts)
    max_loss = max(0.0, width_pct * spot - credit_pts) * S.LOT
    return max_loss + 0.015 * notional + elm


VARIANTS = {
    "1_naked_straddle_SL40": [("C", 0, -1, 0.40), ("P", 0, -1, 0.40)],
    "2_ironfly_1pct_SL40": [("C", 0, -1, 0.40), ("P", 0, -1, 0.40), ("C", 0.01, +1, None), ("P", -0.01, +1, None)],
    "3_ironfly_1.5pct_SL40": [("C", 0, -1, 0.40), ("P", 0, -1, 0.40), ("C", 0.015, +1, None), ("P", -0.015, +1, None)],
    "4_ironfly_2pct_SL40": [("C", 0, -1, 0.40), ("P", 0, -1, 0.40), ("C", 0.02, +1, None), ("P", -0.02, +1, None)],
    "5_ironfly_1.5pct_noSL": [("C", 0, -1, None), ("P", 0, -1, None), ("C", 0.015, +1, None), ("P", -0.015, +1, None)],
    "6_ironcondor_0.5/1.5_SL50": [("C", 0.005, -1, 0.50), ("P", -0.005, -1, 0.50),
                                  ("C", 0.015, +1, None), ("P", -0.015, +1, None)],
}


def run(label, nifty, vix_prev):
    rows = []
    for day, bars in nifty.groupby("date"):
        if S.weekdays_between(day, S.expiry_for(day)) != 0:
            continue
        bars = bars.reset_index(drop=True)
        if len(bars) < 70 or bars.timestamp.iloc[0].time() != S.dtime(9, 15):
            continue
        prev = vix_prev[vix_prev.index <= day]
        if prev.empty or pd.isna(prev.iloc[-1]):
            continue
        iv = float(prev.iloc[-1]) / 100.0
        spot0 = float(bars.close.iloc[0])
        S.DAY["spot0"] = spot0
        row = {"date": day, "iv": round(iv * 100, 2), "spot": round(spot0, 1)}
        for name, spec in VARIANTS.items():
            pnl, credit = S.run_short(day, bars, iv, spec)
            row[name] = round(pnl, 2)
            row[name + "_margin"] = round(margin_estimate(spot0, spec, credit))
        rows.append(row)
    S.DAY["spot0"] = None
    df = pd.DataFrame(rows)
    df.to_csv(OUT / f"expiry_hedge_daily_{label}.csv", index=False)
    years = (pd.to_datetime(df.date.max()) - pd.to_datetime(df.date.min())).days / 365.25
    out = {}
    for name in VARIANTS:
        s = df[name]
        eq = s.cumsum()
        dd_pts = (eq - eq.cummax()).min()
        margin = df[name + "_margin"].mean()
        total_rs = s.sum() * S.LOT
        out[name] = {
            "trades": len(s), "win%": round(100 * (s > 0).mean(), 1),
            "total_pts": round(s.sum(), 0),
            "PF": round(s[s > 0].sum() / -s[s < 0].sum(), 2) if (s < 0).any() else float("inf"),
            "total_Rs_lot": round(total_rs), "avg_margin_Rs": round(margin),
            "yearly_return%": round(100 * total_rs / years / margin, 1),
            "maxDD_Rs": round(dd_pts * S.LOT), "maxDD%_of_margin": round(100 * dd_pts * S.LOT / margin, 1),
            "worst_day_Rs": round(s.min() * S.LOT),
        }
    df["year"] = df.date.astype(str).str[:4]
    by_year = (df.groupby("year")[list(VARIANTS)].sum() * S.LOT).round(0)
    return pd.DataFrame(out).T, by_year, years, len(df)


def main():
    OUT.mkdir(exist_ok=True)
    vix_prev = S.fetch_vix().shift(1)
    nifty, src = S.load_nifty()
    share = S.intraday_share(nifty)
    modes = {"naive": (1.0, 0.0), "realistic": (share, float(os.getenv("OPT_IV_SHOCK", "6"))), "harsh": (share, 12.0)}
    lines = [f"EXPIRY-DAY ONLY: naked vs hedged NIFTY option selling  ({datetime.now():%Y-%m-%d %H:%M} UTC)",
             f"Data: {src}. Entry 09:20, exit 15:15. Lot {S.LOT}. Cost {S.COST_PTS} pts per leg round-trip.",
             "Margin is an ESTIMATE (SPAN-like + SEBI 2% expiry-day ELM per short leg). Research only.", ""]
    for label, (sh, shock) in modes.items():
        S.MODEL.update({"intraday_share": sh, "iv_shock": shock})
        table, by_year, years, n = run(label, nifty, vix_prev)
        lines += [f"=== [{label}] intraday_share={sh:.2f} iv_shock={shock}  ({n} expiry days, {years:.1f} yrs) ===",
                  table.to_string(), "", f"Rs per lot by year [{label}]:", by_year.to_string(), ""]
    text = "\n".join(lines)
    (OUT / "expiry_hedge_summary.txt").write_text(text)
    print(text)


if __name__ == "__main__":
    main()

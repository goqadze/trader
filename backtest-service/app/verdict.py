"""A plain-words recommendation for one finished backtest: a short checklist of what a result needs before it is
worth paper trading, and a grade from it. The frontend shows it as is; a scan uses the grade to decide which
combinations sit the exam. A backtest can at best earn "paper-trade it next": past results on one symbol and
window never prove a strategy, so nothing here says "trade it with real money"."""

from .models import RotationConfig, RunConfig

MIN_TRADES = 10  # fewer closed trades and luck can explain the whole result (the frontend's FEW_TRADES)
MIN_PROFIT_FACTOR = 1.2  # every $1 lost has to bring back at least $1.20
# Less return than buy & hold only counts as better with a CLEARLY smoother ride: a Sharpe 0.05 higher is noise
# (SPY Momentum 2020-23: 0.63 vs 0.58 "passed", then made +14% while holding made +88%)
SHARPE_MARGIN = 0.2
WORST_DRAWDOWN_PCT = -20.0  # like the bots' default breaker
MIN_MONTHS = 6  # shorter tests see one market mood only

TITLES = {"paper": "Worth paper trading", "weak": "Not yet", "no": "Not recommended"}


def _signed(v: float) -> str:
    return f"{'+' if v > 0 else ''}{v:.1f}%"


def _months(cfg: RunConfig | RotationConfig) -> float:
    return (cfg.end - cfg.start).days / 30.44


def judge(r: dict, cfg: RunConfig | RotationConfig) -> dict:
    """{grade: paper | weak | no, title, summary, checks: [{ok, label, detail, must_have}]} for a result dict."""
    checks = []

    def check(ok: bool, label: str, detail: str, must_have: bool = False):
        checks.append({"ok": bool(ok), "label": label, "detail": detail, "must_have": must_have})

    n = r["num_trades"]
    check(n >= MIN_TRADES, "Enough trades",
          f"{n} closed trade{'' if n == 1 else 's'} (at least {MIN_TRADES} needed, 30+ is better; fewer and luck can explain everything)",
          must_have=True)

    avg = r.get("avg_trade_pct") or 0.0
    check(r["total_return_pct"] > 0 and avg > 0, "Made money",
          f"Return {_signed(r['total_return_pct'])}, average trade {_signed(avg)}", must_have=True)

    # profit_factor None with winning trades = no losing trade yet (infinite)
    pf = r.get("profit_factor")
    if pf is None:
        pf = float("inf") if n and r.get("win_rate_pct", 0) > 0 else 0.0
    check(pf >= MIN_PROFIT_FACTOR, "Wins outweigh losses",
          "No losing trade yet" if pf == float("inf")
          else f"Each $1 lost brought back ${pf:.2f} (at least ${MIN_PROFIT_FACTOR:.2f} wanted)")

    bh = r["buy_hold_return_pct"]
    sharpe, bh_sharpe = r.get("sharpe"), r.get("buy_hold_sharpe")
    compared = sharpe is not None and bh_sharpe is not None
    smoother = compared and sharpe >= bh_sharpe + SHARPE_MARGIN - 1e-9
    slightly = compared and not smoother and sharpe > bh_sharpe
    beat = bool(r.get("beat_buy_hold"))
    check(beat or smoother, "Better than just holding",
          f"{_signed(r['total_return_pct'] - bh)} ahead of buy & hold ({_signed(bh)})" if beat
          else f"Less return than buy & hold ({_signed(bh)}), but a clearly smoother ride (Sharpe {sharpe} vs {bh_sharpe})" if smoother
          else f"Less return than buy & hold ({_signed(bh)}) and only a slightly smoother ride (Sharpe {sharpe} vs "
               f"{bh_sharpe}; at least {SHARPE_MARGIN} higher needed)" if slightly
          else f"Buy & hold made {_signed(bh)} with a smoother or equal ride: simply holding was better")

    tripped = r.get("breaker_tripped_on")
    check(r["max_drawdown_pct"] > WORST_DRAWDOWN_PCT and not tripped, "Losses stayed bearable",
          f"The drawdown breaker tripped on {tripped}" if tripped
          else f"Worst drop {r['max_drawdown_pct']:.1f}% (shallower than {WORST_DRAWDOWN_PCT:.0f}% wanted)")

    months = _months(cfg)
    check(months >= MIN_MONTHS - 0.1, "Long enough test",
          f"{months:.0f} month{'' if round(months) == 1 else 's'} tested (at least {MIN_MONTHS} wanted)")

    errors = r.get("decision_errors") or 0
    check(errors == 0, "Clean run",
          f"{errors} decision day{'' if errors == 1 else 's'} failed and counted as holds" if errors else "Every decision day worked")

    failed_must = [c for c in checks if c["must_have"] and not c["ok"]]
    failed = [c for c in checks if not c["ok"]]
    if failed_must:
        grade = "no"
        summary = ("Too few trades to judge: this result could be pure luck. Test a longer window or other symbols."
                   if failed_must[0]["label"] == "Enough trades"
                   else "This setup lost money here. Don't trade it; try other settings or another strategy.")
    elif failed:
        grade = "weak"
        summary = (f"It made money, but missed {len(failed)} check{'' if len(failed) == 1 else 's'} "
                   f"({', '.join(c['label'].lower() for c in failed)}). "
                   "Improve the settings or compare other strategies before paper trading.")
    else:
        grade = "paper"
        summary = ("Passed every check in this test period. That's a reason to keep testing, not proof: try another "
                   "period or similar symbols, then paper-trade it before risking real money.")
    return {"grade": grade, "title": TITLES[grade], "summary": summary, "checks": checks}

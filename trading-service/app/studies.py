"""The Test results grid, worked out here instead of in the browser: a study can hold thousands of tested setups, so the
dashboard asks for one page at a time, filtered and sorted by any column, and for a summary (the counts, each
column's filter choices, and "what matters": each rule's like-for-like effect on the score).

Both come from a per-study index kept in memory: every test's sort and filter keys, built once from the database. The
endpoints that change a study's tests drop it (forget), and as a second guard it is built again when the tests' count or
newest id moves (SQLite reuses a deleted row's id, so that alone can miss a replaced test). A page then only reads its
own rows. A study's strategy (dip buyer or momentum rotation) decides which rules its grid has: RULES."""

import json
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer

from .models import DipStudy, DipTest

MAIN = ("practice", "exam")  # the score's windows: "made money in both", "beat buy & hold in both"
PARTIAL = {"bear"}  # windows only some tests have: not part of "All"


def _pct(f: float) -> str:
    """0.03 -> "3%", 0.0125 -> "1.25%" (like the dashboard)."""
    return f"{round(f * 100, 2):g}%"


@dataclass(frozen=True)
class Rule:
    """A rule the tests of a study can differ in: its number (to sort, filter and compare) and its words."""

    key: str
    value: Callable[[dict], float]
    text: Callable[[dict], str]


# The same rules, values and words as the grid's columns (frontend dip/TestResults.tsx DIP_RULES)
DIP_RULES = (
    Rule("drop", lambda c: 10 + c["drop_atr"] if c.get("drop_mode") == "volatility" else c["drop_pct"],
         lambda c: f"{round(c['drop_atr'], 2):g}× move" if c.get("drop_mode") == "volatility" else _pct(c["drop_pct"])),
    Rule("lookback", lambda c: c["lookback"], lambda c: f"{c['lookback']}d"),
    Rule("turn", lambda c: c["rebound_pct"] if c.get("rebound") else 0, lambda c: _pct(c["rebound_pct"]) if c.get("rebound") else "off"),
    Rule("rise", lambda c: c["rise_pct"] if c.get("target_mode") == "percent" else 0,
         lambda c: f"+{_pct(c['rise_pct'])}" if c.get("target_mode") == "percent" else "back"),
    Rule("stop", lambda c: c["stop_pct"], lambda c: _pct(c["stop_pct"])),
    Rule("reenable", lambda c: c.get("reenable_days") or 0, lambda c: f"{c.get('reenable_days') or 0}d"),
    Rule("hold", lambda c: c.get("max_hold_days") or 0, lambda c: f"{c['max_hold_days']}d" if c.get("max_hold_days") else "—"),
    Rule("trend", lambda c: 1 if c.get("trend_filter") else 0, lambda c: "up only" if c.get("trend_filter") else "—"),
    Rule("slots", lambda c: c["max_positions"], lambda c: str(c["max_positions"])),
    Rule("cash", lambda c: c.get("initial_cash") or 0, lambda c: f"${c['initial_cash']:g}" if c.get("initial_cash") else "$—"),
)
# A momentum rotation's (frontend rotation/kit.tsx ROTATION_RULES)
ROTATION_RULES = (
    Rule("top", lambda c: c["top_n"], lambda c: str(c["top_n"])),
    Rule("months", lambda c: c["lookback_months"], lambda c: f"{c['lookback_months']} mo"),
    Rule("skip", lambda c: c.get("skip_months") or 0, lambda c: f"{c['skip_months']} mo" if c.get("skip_months") else "—"),
    Rule("rising", lambda c: 1 if c.get("abs_filter") else 0, lambda c: "rising only" if c.get("abs_filter") else "any"),
    Rule("cash", lambda c: c.get("initial_cash") or 0, lambda c: f"${c['initial_cash']:g}" if c.get("initial_cash") else "$—"),
)
RULES = {"dip": DIP_RULES, "rotation": ROTATION_RULES}  # by DipStudy.strategy
NO_EXTRA = "—"  # the Filter column's value for a test without an extra filter


@dataclass
class Row:
    """One test's keys: what the grid sorts and filters by."""

    id: int
    pick: int | None
    code: str
    name: str
    watchlist: str
    extra: str
    score: float | None
    haystack: str  # what the search box looks in
    rules: dict[str, float]
    numbers: dict[str, float | None]  # "<period>-ret" / "-dd" / "-trades", "calm", "all", "quarters"
    profit_both: bool
    beat_both: bool


@dataclass
class Index:
    version: tuple
    rows: list[Row]
    rule_keys: set[str] = field(default_factory=set)  # its strategy's rules
    summary: dict = field(default_factory=dict)


_cache: dict[int, Index] = {}
_lock = threading.Lock()


def _num(x: dict | None, key: str) -> float | None:
    if not x or x.get("error"):
        return None
    v = x.get(key)
    return float(v) if isinstance(v, (int, float)) else None


def _row(t: DipTest, periods: list[str], rules: tuple[Rule, ...]) -> Row:
    c, res = t.config, t.results or {}
    numbers: dict[str, float | None] = {}
    for p in periods:
        x = res.get(p)
        numbers[f"{p}-ret"], numbers[f"{p}-dd"], numbers[f"{p}-trades"] = (
            _num(x, "total_return_pct"), _num(x, "max_drawdown_pct"), _num(x, "num_trades"))
    # Steadiness: return per 1% of the worst drop, the weaker of practice and exam
    calm = [r / max(1.0, -(d or 0)) for p in MAIN if (r := numbers.get(f"{p}-ret")) is not None
            for d in [numbers.get(f"{p}-dd")]]
    numbers["calm"] = min(calm) if len(calm) == len(MAIN) else None
    # Every full window back to back: what $1 grew to, minus 1, in %
    full = [p for p in periods if p not in PARTIAL]
    rets = [numbers.get(f"{p}-ret") for p in full]
    numbers["all"] = (math.prod(1 + r / 100 for r in rets) - 1) * 100 if full and None not in rets else None
    q = t.quarters
    numbers["quarters"] = q["won"] * 1000 + q["worst"] if q else None
    main = [res.get(p) or {} for p in MAIN if p in periods]
    return Row(
        id=t.id, pick=t.pick, code=t.code, name=t.name, watchlist=t.watchlist, extra=t.extra or NO_EXTRA, score=t.score,
        haystack=f"{t.code} {t.name} {t.watchlist} {' '.join(c.get('symbols', []))}".lower(),
        rules={r.key: r.value(c) for r in rules}, numbers=numbers,
        profit_both=all((x.get("total_return_pct") or -1) > 0 for x in main),
        beat_both=all((x.get("alpha_vs_buy_hold_pct") or -1) > 0 for x in main),
    )


def _effects(tests: list[DipTest], rule_set: tuple[Rule, ...]) -> list[dict]:
    """Each rule's values and how they change the score, like for like: among tests the same in every other rule
    (watchlist and filter included), how far each value's score sits from that group's average; the median of those
    gaps per value. Plain medians would flatter values that were only tried around the best setups."""
    rules: list[tuple[str, Callable[[DipTest], object], Callable[[DipTest], str]]] = [
        ("watchlist", lambda t: t.watchlist, lambda t: t.watchlist),
        ("extra", lambda t: t.extra or "none", lambda t: t.extra or "none"),
        *[(r.key, (lambda t, r=r: r.value(t.config)), (lambda t, r=r: r.text(t.config))) for r in rule_set],
    ]
    scored = [t for t in tests if t.score is not None]
    values = [[v(t) for _, v, _ in rules] for t in scored]
    out = []
    for i, (key, _, text) in enumerate(rules):
        groups: dict[tuple, list[int]] = {}
        for j, vs in enumerate(values):
            groups.setdefault(tuple(vs[:i] + vs[i + 1:]), []).append(j)
        gaps: dict[object, tuple[str, list[float]]] = {}
        for members in groups.values():
            if len({values[j][i] for j in members}) < 2:
                continue
            mean = sum(scored[j].score for j in members) / len(members)
            for j in members:
                gaps.setdefault(values[j][i], (text(scored[j]), []))[1].append(scored[j].score - mean)
        if len(gaps) < 2:
            continue
        cells = [{"label": label, "median": round(median(d), 2), "n": len(d)}
                 for _, (label, d) in sorted(gaps.items(), key=lambda kv: (isinstance(kv[0], str), kv[0]))]
        out.append({"key": key, "cells": cells})
    return out


def _summary(tests: list[DipTest], rows: list[Row], rules: tuple[Rule, ...]) -> dict:
    varying = {}
    for r in rules:
        seen = {}
        for t in tests:
            seen.setdefault(r.value(t.config), r.text(t.config))
        if len(seen) > 1:
            varying[r.key] = [{"value": v, "text": seen[v]} for v in sorted(seen)]
    return {
        "tests": len(rows),
        "made_money_both": sum(r.profit_both for r in rows),
        "beat_hold_both": sum(r.beat_both for r in rows),
        "watchlists": sorted({r.watchlist for r in rows}),
        "extras": sorted({r.extra for r in rows}) if any(r.extra != NO_EXTRA for r in rows) else [],
        "rules": varying,
        "has_quarters": any(r.numbers["quarters"] is not None for r in rows),
        "effects": _effects(tests, rules),
    }


def index(session: Session, study: DipStudy) -> Index:
    """The study's index, built again when its tests changed since."""
    version = tuple(session.execute(select(func.count(DipTest.id), func.max(DipTest.id))
                                    .where(DipTest.study_id == study.id)).one()) + (study.created_at, json.dumps(study.periods))
    cached = _cache.get(study.id)
    if cached and cached.version == version:
        return cached
    with _lock:
        cached = _cache.get(study.id)
        if cached and cached.version == version:
            return cached
        tests = list(session.scalars(select(DipTest).where(DipTest.study_id == study.id)
                                     .options(defer(DipTest.detail)).order_by(DipTest.id)))
        periods, rules = list(study.periods), RULES[study.strategy]
        rows = [_row(t, periods, rules) for t in tests]
        built = Index(version=version, rows=rows, rule_keys={r.key for r in rules}, summary=_summary(tests, rows, rules))
        _cache[study.id] = built
        return built


def forget(study_id: int) -> None:
    _cache.pop(study_id, None)


def sort_keys(periods: list[str], strategy: str = "dip") -> set[str]:
    """The columns a page can be sorted by."""
    return ({"pick", "name", "watchlist", "score", "calm", "all", "quarters"} | {r.key for r in RULES[strategy]}
            | {f"{p}-{n}" for p in periods for n in ("ret", "dd", "trades")})


def page(idx: Index, offset: int, limit: int, sort: str = "pick", order: str = "asc", q: str = "", picks: bool = False,
         profit_both: bool = False, beat_both: bool = False, filters: dict[str, list] | None = None) -> tuple[int, list[int]]:
    """The ids of one page of the grid, and how many tests the filters leave."""
    q, filters = q.strip().lower(), filters or {}
    watchlists, extras = set(filters.get("watchlist") or []), set(filters.get("extra") or [])
    rule_filters = {k: {float(v) for v in vs} for k, vs in filters.items() if k in idx.rule_keys and vs}

    def keep(r: Row) -> bool:
        return ((not picks or r.pick is not None) and (not profit_both or r.profit_both) and (not beat_both or r.beat_both)
                and (not q or q in r.haystack) and (not watchlists or r.watchlist in watchlists)
                and (not extras or r.extra in extras)
                and all(float(r.rules[k]) in vs for k, vs in rule_filters.items()))

    rows = [r for r in idx.rows if keep(r)]
    # Ties, and the default order: the picks first (1, 2, ...), then the best score, then the oldest test
    rows.sort(key=lambda r: r.id)
    rows.sort(key=lambda r: -r.score if r.score is not None else math.inf)
    rows.sort(key=lambda r: r.pick if r.pick is not None else math.inf)
    if sort != "pick" or order != "asc":
        value: Callable[[Row], object] = {
            "pick": lambda r: r.pick, "name": lambda r: r.code, "watchlist": lambda r: r.watchlist, "score": lambda r: r.score,
        }.get(sort) or ((lambda r: r.rules[sort]) if sort in idx.rule_keys else (lambda r: r.numbers.get(sort)))
        present = [r for r in rows if value(r) is not None]
        missing = [r for r in rows if value(r) is None]  # always last, whichever way
        present.sort(key=value, reverse=order == "desc")
        rows = present + missing
    return len(rows), [r.id for r in rows[offset:offset + limit]]

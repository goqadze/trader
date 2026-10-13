"""Momentum rotation setups and test batches: saved apart from the dip buyer's, with a grid of their own rules."""

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app import main

SECTORS = ["xlk", "XLF", "XLV", "XLE", "xlk"]
SETUP = {"symbols": SECTORS, "top_n": 2, "lookback_months": 12, "skip_months": 1, "abs_filter": True, "fractional": True,
         "initial_cash": 400, "practice": ["2020-10-09", "2023-10-08"], "exam": ["2023-10-09", "2026-10-09"], "period_months": 36}
DIP_SETUP = {"symbols": ["XLK", "XLF"], "interval": "1d", "drop_pct": 0.07, "lookback": 60, "stop_pct": 0.1}


@pytest.fixture
def api():
    with TestClient(main.app) as c:
        yield c


def test_rotation_setups_are_saved_apart_from_the_dip_buyers(api):
    c = api
    r = c.post("/rotation/presets", json={"name": " Sectors 12-1 ", "config": SETUP})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert (saved["name"], saved["strategy"]) == ("Sectors 12-1", "rotation")
    assert saved["config"]["symbols"] == ["XLK", "XLF", "XLV", "XLE"] and saved["config"]["period_months"] == 36
    dip = c.post("/dip/presets", json={"name": "Big dips", "config": DIP_SETUP}).json()
    assert [p["name"] for p in c.get("/rotation/presets").json()] == ["Sectors 12-1"]
    assert [p["name"] for p in c.get("/dip/presets").json()] == ["Big dips"]

    again = c.post("/rotation/presets", json={"name": "SECTORS 12-1", "config": {**SETUP, "top_n": 3}}).json()
    assert again["id"] == saved["id"] and again["config"]["top_n"] == 3  # the same name replaces it
    r = c.post("/dip/presets", json={"name": "sectors 12-1", "config": DIP_SETUP})
    assert r.status_code == 409 and "momentum rotation setup is called" in r.text  # never the other kind's
    r = c.post("/rotation/presets", json={"name": "big DIPS", "config": SETUP})
    assert r.status_code == 409 and "dip buyer setup" in r.text

    assert c.delete(f"/dip/presets/{saved['id']}").status_code == 404  # through its own kind's path only
    assert c.delete(f"/rotation/presets/{dip['id']}").status_code == 404
    assert c.delete(f"/rotation/presets/{saved['id']}").status_code == 204
    assert c.get("/rotation/presets").json() == [] and len(c.get("/dip/presets").json()) == 1


@pytest.mark.parametrize("config, words", [
    ({**SETUP, "symbols": ["XLK"]}, "at least 2"),
    ({**SETUP, "top_n": 5}, "no more symbols than the universe"),
    ({**SETUP, "lookback_months": 0}, "greater than or equal to 1"),
    ({**SETUP, "exam": ["2026-10-09", "2023-10-09"]}, "end after it starts"),
    ({**SETUP, "drop_pct": 0.05}, "Extra inputs are not permitted"),  # a dip buyer's rule
])
def test_a_rotation_setup_is_checked(api, config, words):
    r = api.post("/rotation/presets", json={"name": "x", "config": config})
    assert r.status_code == 422 and words in r.text


def rotation_test(code: str, watchlist: str, top: int, months: int, practice: float, exam: float, **kw) -> dict:
    window = lambda ret, alpha: {"total_return_pct": ret, "alpha_vs_buy_hold_pct": alpha, "max_drawdown_pct": -10.0,  # noqa: E731
                                 "num_trades": 30}
    return {"code": code, "name": f"{code} {watchlist} top {top} · {months} mo", "watchlist": watchlist,
            "config": {**SETUP, "top_n": top, "lookback_months": months}, "score": min(practice, exam),
            "results": {"practice": window(practice, 1.0), "exam": window(exam, -1.0)}, **kw}


def test_a_rotation_batch_lists_apart_and_has_its_own_columns(api):
    c = api
    dip_batch = c.put("/dip/studies", json={"name": "Dip sweep", "periods": {"practice": ["2025-01-02", "2025-09-30"]}}).json()
    r = c.put("/dip/studies", json={"name": "Rotation sweep", "strategy": "rotation",
                                    "periods": {"practice": SETUP["practice"], "exam": SETUP["exam"]}})
    assert r.status_code == 200, r.text
    batch = r.json()
    assert batch["strategy"] == "rotation" and dip_batch["strategy"] == "dip"
    assert [s["name"] for s in c.get("/dip/studies").json()] == ["Dip sweep"]  # the dip buyer's page
    assert [s["name"] for s in c.get("/dip/studies?strategy=rotation").json()] == ["Rotation sweep"]
    assert c.get("/dip/studies?strategy=futures").status_code == 422
    moved = c.put("/dip/studies", json={"name": "DIP SWEEP", "strategy": "rotation", "periods": {"practice": SETUP["practice"]}}).json()
    assert (moved["id"], moved["strategy"]) == (dip_batch["id"], "rotation")  # saved again under its name: the new kind
    assert c.get("/dip/studies").json() == [] and len(c.get("/dip/studies?strategy=rotation").json()) == 2
    c.delete(f"/dip/studies/{moved['id']}")
    dip_batch = c.put("/dip/studies", json={"name": "Dip sweep", "periods": {"practice": ["2025-01-02", "2025-09-30"]}}).json()

    tests = [rotation_test("R1", "Sectors", 2, 12, 30, 20), rotation_test("R2", "Sectors", 3, 12, 25, 25, pick=1),
             rotation_test("R3", "Sectors", 2, 6, 10, 5)]
    assert c.post(f"/dip/studies/{batch['id']}/tests", json=tests).status_code == 200
    r = c.post(f"/dip/studies/{batch['id']}/tests", json=[{**tests[0], "code": "D1", "config": DIP_SETUP}])
    assert r.status_code == 422 and "D1: not a momentum rotation setup" in r.text  # a dip setup in a rotation batch
    r = c.post(f"/dip/studies/{dip_batch['id']}/tests", json=[{**tests[0], "config": {"top_n": 2}}])
    assert r.status_code == 422 and "not a dip buyer setup" in r.text

    url = f"/dip/studies/{batch['id']}/tests"
    codes = lambda q="": [t["code"] for t in c.get(f"{url}?{q}").json()["items"]]  # noqa: E731
    assert codes() == ["R2", "R1", "R3"]  # the pick, then the best score
    assert codes("sort=months&order=asc") == ["R3", "R2", "R1"]
    assert codes("filters=" + quote('{"top": [2]}')) == ["R1", "R3"]
    assert c.get(f"{url}?sort=stop").status_code == 422  # a dip buyer's column
    s = c.get(f"/dip/studies/{batch['id']}/summary").json()
    assert s["rules"] == {"top": [{"value": 2, "text": "2"}, {"value": 3, "text": "3"}],
                          "months": [{"value": 6, "text": "6 mo"}, {"value": 12, "text": "12 mo"}]}
    # Like for like: R1 and R2 differ only in how many they hold (20 vs 25), R1 and R3 only in the months (20 vs 5)
    assert [(e["key"], [(x["label"], x["median"]) for x in e["cells"]]) for e in s["effects"]] == [
        ("top", [("2", -2.5), ("3", 2.5)]), ("months", [("6 mo", -7.5), ("12 mo", 7.5)])]

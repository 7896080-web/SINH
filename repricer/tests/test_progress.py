"""Шкала прогресса массовых действий (priceapp/progress.py, static/busy.js)."""
import re
from pathlib import Path

from priceapp import progress
from tests import factories as F


def test_without_token_everything_is_a_no_op():
    progress.stage("x", 10)
    progress.step()
    assert list(progress.each([1, 2], "y")) == [1, 2]


def test_tracked_handler_reports_stage_count_and_finish():
    class Req:
        query_params = {"_progress": "abcdef0123456789"}

    seen = []

    @progress.tracked
    def handler(request):
        for _ in progress.each(range(3), "Шаги", total=3):
            seen.append(progress.get("abcdef0123456789")["done"])
        progress.pin("Внешний", total=2)
        progress.stage("внутренний")                       # подпись, а не новый счёт
        progress.step()                                     # внутренний шаг шкалу не двигает
        progress.advance()
        s = progress.get("abcdef0123456789")
        return s

    s = handler(request=Req())
    assert seen == [0, 1, 2]
    assert (s["stage"], s["done"], s["total"], s["detail"]) == ("Внешний", 1, 2, "внутренний")
    assert progress.get("abcdef0123456789")["finished"] is True


def test_bad_token_is_ignored():
    class Req:
        query_params = {"_progress": "../../etc"}

    @progress.tracked
    def handler(request):
        progress.stage("x", 1)
        return "ok"

    assert handler(request=Req()) == "ok" and progress.get("../../etc") is None


def test_send_reports_progress_through_the_endpoint(client, db):
    F.manual_rate(db)
    acc = F.account(db, "wb", "ИП 1")
    F.rule(db, acc)
    F.sku(db, "u1", "A1", "S", barcodes=["b1"], cost_usd="10")
    F.item(db, acc, "b1", "A1", external_id="5:1", size="S")
    r = client.post("/sku-prices/send?_progress=0011223344556677", data={"scope": f"a{acc.id}", "all_filtered": "1"})
    assert r.status_code == 200
    s = client.get("/progress/0011223344556677").json()
    assert s["finished"] is True and s["stage"] == "Ставлю цены в очередь отправки" and s["total"] == 1


def test_progress_endpoint_requires_login():
    from fastapi.testclient import TestClient

    from priceapp.main import app
    with TestClient(app) as c:
        r = c.get("/progress/0011223344556677", follow_redirects=False)
    assert r.status_code == 303


def test_busy_js_sends_the_token_and_polls_only_for_post_forms():
    js = (Path(__file__).resolve().parent.parent / "priceapp" / "static" / "busy.js").read_text(encoding="utf-8")
    assert 'searchParams.set("_progress"' in js and 'fetch("/progress/"' in js
    assert re.search(r'method === "post"', js)


def test_every_mass_action_is_tracked():
    """Новая массовая кнопка без шкалы — снова «программа зависла?»: перечень закрыт тестом."""
    root = Path(__file__).resolve().parent.parent / "priceapp" / "routers"
    must = {"sku_prices.py": ["/sku-prices/coef", "/sku-prices/send", "/sku-prices/import"],
            "prices.py": ["/prices/bulk/{account_id}", "/prices/load-current", "/prices/import/{account_id}",
                          "/prices/changes-import", "/prices/recalculate", "/prices/approve", "/prices/reject",
                          "/prices/guard-run"],
            "etalon.py": ["/etalon/follow-now", "/etalon/import", "/etalon/set"]}
    for name, routes in must.items():
        text = (root / name).read_text(encoding="utf-8")
        for r in routes:
            assert f'@router.post("{r}")\n@progress.tracked\n' in text, (name, r)

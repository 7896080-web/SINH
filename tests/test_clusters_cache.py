"""Картина кластеров собирается не на каждый запрос.

Сборка читает ВЕСЬ каталог площадок и связанные с ним баркоды 1С и строит по ним
union-find. Замер 21.09 на боевом масштабе (16 127 карточек, 154 232 баркода):
3,5 с и 52 МБ на КАЖДЫЙ запрос. А запросов много: фильтры на странице живые, htmx
дёргает фрагмент через 400 мс после набора — поиск из шести букв означал
несколько полных пересборок подряд, и всё это время веб-служба занята ими, а она
же принимает заказы с площадок.

Минута безопасна, потому что каталог меняется не сам по себе, а загрузкой, и оба
пути загрузки кэш сбрасывают.
"""
from datetime import timedelta

import app.routers.platform_matching as pm
from app.models import Barcode, Platform, PlatformCatalogItem, Product
from tests.factories import make_account


def _catalog(db, count=3):
    account = make_account(db, Platform.wb)
    for i in range(count):
        db.add(Product(uid_1c=f"u{i}", article=f"A-{i}", name="Товар", stock_on_hand=1))
        db.add(Barcode(barcode=f"b{i}", uid_1c=f"u{i}"))
        db.add(PlatformCatalogItem(account_id=account.id, barcode=f"b{i}",
                                   external_id=f"100:{i}", article=f"A-{i}", name="Товар"))
    db.commit()
    return account


def test_the_second_request_does_not_rebuild(db, monkeypatch):
    _catalog(db)
    calls = []
    real = pm._build_clusters
    monkeypatch.setattr(pm, "_build_clusters", lambda d: calls.append(1) or real(d))

    pm._filtered(db, "", "", "")
    pm._filtered(db, "куртка", "", "")

    assert len(calls) == 1, "второй запрос обязан взять картину из кэша"


def test_a_stale_picture_is_rebuilt(db, monkeypatch):
    """Минута — предел для случая, когда каталог поменяли МИМО приложения."""
    _catalog(db)
    calls = []
    real = pm._build_clusters
    monkeypatch.setattr(pm, "_build_clusters", lambda d: calls.append(1) or real(d))

    pm._filtered(db, "", "", "")
    when, rows = pm._clusters_cache
    pm._clusters_cache = (when - timedelta(minutes=2), rows)
    pm._filtered(db, "", "", "")

    assert len(calls) == 2


def test_loading_a_catalogue_drops_the_picture(db):
    """Человек нажал «Загрузить каталог» ровно затем, чтобы увидеть новое.
    Минута ожидания тут была бы особенно обидной."""
    _catalog(db)
    pm._filtered(db, "", "", "")
    assert pm._clusters_cache is not None

    pm.clear_clusters_cache()

    assert pm._clusters_cache is None


def test_the_filters_still_work_on_a_cached_picture(db):
    """Кэшируется СБОРКА, а не результат отбора: фильтры обязаны и дальше
    применяться к каждому запросу отдельно."""
    _catalog(db, count=4)

    everything = pm._filtered(db, "", "", "")
    narrowed = pm._filtered(db, "A-2", "", "")

    assert len(everything) == 4
    assert len(narrowed) == 1

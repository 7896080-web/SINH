"""Что изменилось в каталоге кабинета между двумя выгрузками — только чтение.

    ... catalog_diff.py save КИТ C:\\sync_admin\\kit-before.json
    ... catalog_diff.py diff КИТ C:\\sync_admin\\kit-before.json

Зачем. Снимок каталога кабинета (`platform_catalog_items`) ПЕРЕЗАПИСЫВАЕТСЯ при
каждой выгрузке — истории нет. Значит вопрос «что натворила в кабинете правка
витрины/карточек» постфактум ответа не имеет: сравнивать не с чем. Снять копию
надо ЗАРАНЕЕ, и `save` ровно для этого.

Порядок: «Мэппинг» → загрузить каталог → `save` → сделать правку в кабинете →
загрузить каталог снова → `diff`.

Что здесь важно и почему сравниваем именно это.

**`external_id` — это КЛЮЧ ОТПРАВКИ.** У Kit это `variant_id`, у WB
`nmID:chrtID`, у Ozon идентификатор товара. Сменился он — остаток уходит по
несуществующему ключу, площадка отвечает «такого товара нет», и запись очереди
закрывается ТЕРМИНАЛЬНО: повтора не будет, следующая отправка случится, только
когда изменится остаток, а у медленного размера это месяцы. Поэтому смена
ключа — первая строка отчёта, а не одна из.

**Исчезнувший баркод** — второе по цене. По баркодам расчёт разносит продажи
площадки на товары 1С: пропал баркод — заказы по нему не проведутся, остаток у
нас останется завышенным и уедет наружу. Это оверселл, и он тихий.

Названия и артикулы показываем последней строкой и только числом: они ни на что
у нас не влияют, но по ним видно, трогали карточки вообще или нет.

Ничего не меняет и не коммитит.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal                                    # noqa: E402
from app.models import PlatformAccount, PlatformCatalogItem              # noqa: E402

SHOW = 25


def _account(db, needle: str):
    if needle.isdigit():
        found = db.query(PlatformAccount).filter(
            PlatformAccount.id == int(needle)).first()
        if found:
            return found
    return db.query(PlatformAccount).filter(
        PlatformAccount.name.ilike(f"%{needle}%")).order_by(PlatformAccount.id).first()


def _snapshot(db, account_id: int) -> dict:
    """Текущий снимок кабинета: баркод → что мы о нём знаем."""
    rows = db.query(PlatformCatalogItem).filter(
        PlatformCatalogItem.account_id == account_id).all()
    out = {}
    for r in rows:
        # Строка без баркода сравнению не поддаётся — сопоставлять её не с чем.
        # Считаем такие отдельно, чтобы их отсутствие в отчёте не читалось как
        # «их не было».
        key = r.barcode or ""
        if not key:
            continue
        out[key] = {"external_id": r.external_id, "article": r.article or "",
                    "name": r.name or "",
                    "fetched_at": r.fetched_at.isoformat() if r.fetched_at else None}
    return out


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if "items" not in data:
        raise SystemExit(f"{path}: это не снимок каталога (нет поля items)")
    return data


def save(db, account, path: str) -> int:
    items = _snapshot(db, account.id)
    stamps = sorted(v["fetched_at"] for v in items.values() if v["fetched_at"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"account_id": account.id, "account_name": account.name,
                   "platform": account.platform.value, "items": items},
                  f, ensure_ascii=False)
    print(f"снимок кабинета «{account.name}»: {len(items)} баркодов → {path}")
    if stamps:
        # Снимок в базе обновляет ТОЛЬКО выгрузка каталога. Сохранив старый, мы
        # сравнивали бы правку витрины с позавчерашним состоянием и приписали
        # ей чужие изменения.
        print(f"выгружен: с {stamps[0]} по {stamps[-1]} (UTC)")
        print("Если это не сегодняшняя выгрузка — сначала загрузите каталог "
              "на «Мэппинге», иначе сравнивать будете не с тем.")
    return 0


def diff(db, account, path: str) -> int:
    before = _load(path)
    if before.get("account_id") != account.id:
        # Сравнение кабинета с чужим снимком выглядело бы как «поменялось всё»,
        # и это самая правдоподобная ошибка при работе с несколькими файлами.
        raise SystemExit(f"файл снят с кабинета «{before.get('account_name')}» "
                         f"(id={before.get('account_id')}), а спрашиваете про "
                         f"«{account.name}» (id={account.id})")
    old, new = before["items"], _snapshot(db, account.id)

    gone = sorted(set(old) - set(new))
    added = sorted(set(new) - set(old))
    common = set(old) & set(new)
    rekeyed = sorted(b for b in common
                     if old[b]["external_id"] != new[b]["external_id"])
    renamed = sum(1 for b in common
                  if (old[b]["article"], old[b]["name"])
                  != (new[b]["article"], new[b]["name"]))

    print("=" * 78)
    print(f"КАБИНЕТ «{account.name}» ({account.platform.value})")
    print(f"было {len(old)} баркодов, стало {len(new)}")
    print("=" * 78)

    print(f"СМЕНИЛИ КЛЮЧ ОТПРАВКИ: {len(rekeyed)}")
    if rekeyed:
        print("  Остаток по ним уходит по ключу, которого на площадке больше нет:")
        print("  площадка ответит «товара нет», запись закроется терминально, и")
        print("  повтора не будет — следующая отправка только при смене остатка.")
        for b in rekeyed[:SHOW]:
            print(f"    {b}  {old[b]['external_id']}  →  {new[b]['external_id']}"
                  f"   {new[b]['article']}")
        if len(rekeyed) > SHOW:
            print(f"    … и ещё {len(rekeyed) - SHOW}")

    print(f"\nИСЧЕЗЛИ ИЗ КАТАЛОГА: {len(gone)}")
    if gone:
        print("  По этим баркодам расчёт больше не разнесёт продажи площадки на")
        print("  товары 1С: заказы не проведутся, остаток останется завышенным.")
        for b in gone[:SHOW]:
            print(f"    {b}  {old[b]['external_id']}  {old[b]['article']}")
        if len(gone) > SHOW:
            print(f"    … и ещё {len(gone) - SHOW}")

    print(f"\nПОЯВИЛИСЬ: {len(added)}")
    for b in added[:SHOW]:
        print(f"    {b}  {new[b]['external_id']}  {new[b]['article']}")
    if len(added) > SHOW:
        print(f"    … и ещё {len(added) - SHOW}")

    print(f"\nПЕРЕИМЕНОВАНЫ (артикул или название): {renamed}")
    print("  На отправку остатка не влияет — но по этому числу видно, трогали")
    print("  карточки вообще или нет.")

    print("=" * 78)
    if not (rekeyed or gone):
        # Говорим вслух: «пусто» и «не проверяли» выглядят одинаково, а по этому
        # ответу решают, надо ли переотправлять остаток.
        print("Ключи отправки и состав баркодов НЕ ИЗМЕНИЛИСЬ — для рассылки")
        print("правка прошла бесследно. Если числа на витрине всё равно сбились,")
        print("дело в остатках, а не в каталоге: верните их кнопкой")
        print("«Переотправить остаток».")
    return 0


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[1] not in ("save", "diff"):
        print(__doc__.strip().splitlines()[0])
        print("  catalog_diff.py save <кабинет> <файл>")
        print("  catalog_diff.py diff <кабинет> <файл>")
        return 1
    mode, needle, path = sys.argv[1], sys.argv[2].strip(), sys.argv[3]

    db = SessionLocal()
    try:
        account = _account(db, needle)
        if account is None:
            have = db.query(PlatformAccount).order_by(PlatformAccount.id).all()
            print(f"кабинет «{needle}» не найден. Есть: "
                  + ", ".join(f"{a.id}:{a.name}" for a in have))
            return 1
        return save(db, account, path) if mode == "save" else diff(db, account, path)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

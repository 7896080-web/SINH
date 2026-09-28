"""Копию моложе суток уборка не удаляет — сколько бы их за день ни сняли.

23.09 на бою этот дефект стоил ровно того, ради чего бэкап и заводился.
Массовая кнопка схлопнула пороги у 62 товаров в 15:10 UTC. Копия до этого
момента была одна — 14:31, снятая накатом. В 15:32 прошёл следующий накат, снял
свою копию и удалил 14:31: правило «одна на календарный день» оставляет САМУЮ
СВЕЖУЮ, то есть снятую уже ПОСЛЕ порчи. В 16:10 — третий накат, и то же самое.

Обе площадки потеряли файл одновременно, и это не сбой, а следствие: правило
уборки у локальной папки и у облака ОДНО, намеренно. Восстанавливать пришлось по
вчерашней копии.

Правило дня писалось под копии по расписанию и для них верно. Но копию снимает и
накат — первым делом, перед миграцией, — и накатов за день бывает несколько.
Сутки неприкосновенности покрывают ровно тот срок, пока живёт вопрос «отменить
то, что только что сделали».
"""

from datetime import datetime, timedelta

from app.backup import KEEP_ALL_WITHIN_HOURS, names_to_drop

NOW = datetime(2026, 9, 23, 16, 30, 0)


def _name(moment: datetime) -> str:
    return f"sync_admin-{moment:%Y%m%d-%H%M%S}.db"


# ------------------------------------------------- собственно случай 23.09

def test_three_deploys_in_one_day_keep_all_three_copies():
    """Дословно тот день: 14:31 (до порчи), 15:32 и 16:10 (после)."""
    names = [_name(datetime(2026, 9, 23, h, m, s))
             for h, m, s in ((14, 31, 42), (15, 32, 36), (16, 10, 11))]

    dropped = names_to_drop(names, now=NOW)

    assert dropped == [], "копия до порчи удалена — восстанавливать нечем"


def test_the_copy_taken_before_the_damage_outlives_the_next_deploy():
    """Главное следствие: снимок «до» переживает накат, снятый «после».

    Именно эта копия и нужна — та, что старше события, а не та, что моложе."""
    before = _name(datetime(2026, 9, 23, 14, 31, 42))
    after = _name(datetime(2026, 9, 23, 16, 10, 11))

    assert before not in names_to_drop([before, after], now=NOW)


# ------------------------------------------- старое правило не ослаблено

def test_yesterdays_copies_still_collapse_to_one_a_day():
    """Сутки прошли — день снова представлен одной копией.

    Иначе каждый день с накатами оставался бы в хранилище целиком, и обещание
    «четырнадцать дней назад» превратилось бы в несколько последних дней."""
    old_day = [_name(datetime(2026, 9, 21, h, 0, 0)) for h in (9, 12, 18)]

    # `keep_weekly=0` — чтобы проверять именно суточный слой: недельный иначе
    # придержит ещё одну копию той же недели, и это его законная работа.
    dropped = names_to_drop(old_day, keep_weekly=0, now=NOW)

    assert len(dropped) == 2
    assert _name(datetime(2026, 9, 21, 18, 0, 0)) not in dropped


def test_a_fresh_copy_does_not_eat_a_weekly_slot(monkeypatch):
    """Свежая копия не вытесняет ту, ради которой недельный слой и заведён.

    23.09 копия 15:32 уцелела не как «сегодняшняя» — то место заняла 16:10, — а
    как НЕДЕЛЬНАЯ. То есть свежий файл занял слот, предназначенный копии
    месячной давности."""
    fresh = [_name(datetime(2026, 9, 23, h, 0, 0)) for h in (14, 15, 16)]
    # По одной копии на каждую из прошлых недель — их и должен держать второй слой.
    weekly = [_name(NOW - timedelta(days=7 * n)) for n in range(1, 4)]

    dropped = names_to_drop(fresh + weekly, keep_daily=1, keep_weekly=3, now=NOW)

    assert dropped == [], "недельные копии вытеснены свежими того же дня"


def test_an_unreadable_name_is_never_dropped():
    """В каталоге может лежать копия, положенную человеком руками."""
    assert names_to_drop(["моя_копия_перед_импортом.db"], now=NOW) == []


# --------------------------------------------------- граница неприкосновенности

def test_the_window_is_counted_from_now_not_from_the_newest_copy():
    """Иначе давно не работавший сервер держал бы сутки вокруг старой копии.

    Копия, снятая ровно на границе, ещё жива; та, что чуть старше, подчиняется
    обычному правилу дня."""
    inside = _name(NOW - timedelta(hours=KEEP_ALL_WITHIN_HOURS) + timedelta(minutes=1))
    outside = _name(NOW - timedelta(hours=KEEP_ALL_WITHIN_HOURS) - timedelta(minutes=1))
    # Представитель того же дня, что и `outside`, — чтобы правило дня его вытеснило.
    same_day_newer = _name(NOW - timedelta(hours=KEEP_ALL_WITHIN_HOURS) + timedelta(minutes=2))

    dropped = names_to_drop([inside, outside, same_day_newer], keep_weekly=0, now=NOW)

    assert inside not in dropped
    assert outside in dropped

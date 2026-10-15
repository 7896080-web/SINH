"""Симуляция длительной работы «Репрайсера» — перед патчем, чтобы увидеть, что программа
делает за месяцы, а не за один тест.

Настоящие задания программы (опрос WB, курс, суточное обновление, очередь, копии) идут по
ускоренному календарю на ОТДЕЛЬНОЙ временной базе; площадки поддельные: WB ×3 с акциями (в
том числе глубокими, скидка 84–87%), Kit, Ламода. Курс ЦБ блуждает (на 40-й день +6%, на 70-й
скачок +25%, на 72-й −18%, на 55–56-й ЦБ недоступен), себестоимость 1С меняется (на 50-й день
опечатка ×10), оператор раз в неделю меняет эталоны. После каждого шага проверяется: нет ли
отправок ниже пола, скачков, упавших и протухших заданий; в конце — рост таблиц и файлов.

Боевые база, .env, папки обмена и копий НЕ трогаются: всё пишется во временную папку.

    python scripts/simulate.py ДНЕЙ [SEED]          (Linux: TZ=Europe/Moscow для местного дня)
"""
import datetime as _dt
import json
import os
import random
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 1
rnd = random.Random(SEED)

ROOT = Path(tempfile.mkdtemp(prefix="repricer-sim-"))
os.environ["DATABASE_URL"] = f"sqlite:///{ROOT / 'sim.db'}"
os.environ["REPRICER_SECRETS_KEY"] = "q0Vk1gYz3m0cZ8m4mYl3Hq0z3zZkq5c1j9p7n4Qm2wQ="
os.environ["REPRICER_SESSION_SECRET"] = "x" * 32
for k, sub in (("REPRICER_ONEC_TASKS_DIR", "sync/tasks"), ("REPRICER_ONEC_RESULTS_DIR", "sync/results/pricing"),
               ("REPRICER_ONEC_ARCHIVE_DIR", "sync/archive/pricing"), ("REPRICER_BACKUP_DIR", "backups"),
               ("REPRICER_ETALON_DIR", "etalon_history")):
    os.environ[k] = str(ROOT / sub)
os.environ["REPRICER_BACKGROUND"] = "0"
os.environ["REPRICER_ONEC_SFTP_HOST"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")      # консоль Windows — cp1251


# --- часы ---------------------------------------------------------------------------------
class Clock:
    t = _dt.datetime(2026, 10, 15, 0, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=3))).astimezone(_dt.timezone.utc)


class FakeDT(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return Clock.t.astimezone().replace(tzinfo=None)
        return Clock.t.astimezone(tz)


from priceapp import timeutils  # noqa: E402
timeutils.datetime = FakeDT
from priceapp import backup  # noqa: E402
backup.datetime = FakeDT

from priceapp import (accounts as acc_mod, dispatch, etalon, pricing, rates, reference,  # noqa: E402
                      settings)
from priceapp.database import DATABASE_URL, Base, SessionLocal, engine  # noqa: E402

assert str(ROOT) in DATABASE_URL, f"симуляция обязана идти на временной базе, а не на {DATABASE_URL}"
from priceapp.models import (Account, AuditLog, OnecBarcode, OnecCost, PlatformItem, PriceChange,  # noqa: E402
                             ProductPrice, ReferencePrice)
from priceapp.platforms import CatalogRow, CurrentPrice  # noqa: E402
from priceapp.settings import ensure_defaults  # noqa: E402
from priceapp.workers import heartbeat, jobs  # noqa: E402

# --- журнал находок -----------------------------------------------------------------------
FIND = defaultdict(list)


def find(kind, text):
    FIND[kind].append(f"{Clock.t.astimezone():%d.%m %H:%M} {text}")


# --- площадки -------------------------------------------------------------------------------
WRITES = []          # (время, платформа, кабинет, ключ, цена до скидки, цена со скидкой, кто)


class Market:
    def __init__(self):
        self.wb = {}      # кабинет -> nm -> dict(list, disc, promo, pre)
        self.kit = {}     # variant -> dict(price, sale)
        self.lam = {}     # parent -> dict(price, sale)
        self.rate = Decimal("81.50")
        self.cbr_down = False


M = Market()


class FakeWb:
    platform = "wb"
    last_truncated = False

    def __init__(self, acc_name, catalog):
        self.acc, self.catalog = acc_name, catalog

    def get_catalog(self):
        return self.catalog

    def price_key(self, item):
        return (item.external_id or "").split(":")[0]

    def get_prices(self):
        out = {}
        for nm, c in M.wb[self.acc].items():
            out[str(nm)] = CurrentPrice(c["list"], round(c["list"] * (100 - c["disc"]) / 100))
        return out

    def _post(self, url, body):
        assert "upload/task" in url, url
        for d in body["data"]:
            c = M.wb[self.acc][int(d["nmID"])]
            c["list"] = int(d["price"])
            if "discount" in d:
                c["disc"] = int(d["discount"])
            c["promo"] = False
            WRITES.append((Clock.t, "wb", self.acc, int(d["nmID"]), c["list"],
                           round(c["list"] * (100 - c["disc"]) / 100), "upload"))
        return {"data": {"id": 1}, "error": False}

    def push_prices(self, items):
        by = defaultdict(list)
        for it in items:
            by[int(it.external_id.split(":")[0])].append(it)
        ok, sent = [], {}
        for nm, g in by.items():
            p = max(i.price for i in g)
            c = M.wb[self.acc][nm]
            c["list"] = p
            WRITES.append((Clock.t, "wb", self.acc, nm, p, round(p * (100 - c["disc"]) / 100), "dispatch"))
            for i in g:
                ok.append(i.barcode)
                sent[i.barcode] = p
        return {"ok": ok, "errors": [], "sent_prices": sent}


class FakeKit:
    platform = "kit"
    last_truncated = False

    def __init__(self, catalog):
        self.catalog = catalog

    def get_catalog(self):
        return self.catalog

    def price_key(self, item):
        return item.external_id or ""

    def get_prices(self):
        return {v: CurrentPrice(c["price"], c["sale"]) for v, c in M.kit.items()}

    def push_prices(self, items):
        ok = []
        for it in items:
            c = M.kit[it.external_id]
            c["price"] = it.price
            c["sale"] = it.sale if it.sale else it.price
            WRITES.append((Clock.t, "kit", "КИТ", it.external_id, c["price"], c["sale"], "push"))
            ok.append(it.barcode)
        return {"ok": ok, "errors": [], "sent_prices": {i.barcode: i.price for i in items}}


class FakeLamoda:
    platform = "lamoda"
    last_truncated = False

    def __init__(self, catalog):
        self.catalog = catalog

    def get_catalog(self):
        return self.catalog

    def price_key(self, item):
        return (item.external_id or "").split(":")[0]

    def get_prices(self):
        return {p: CurrentPrice(c["price"], c["sale"]) for p, c in M.lam.items()}

    def _post(self, url, body):
        assert "nomenclatures-prices" in url, url
        for p in body["prices"]:
            c = M.lam[p["parentSku"]]
            c["price"] = p["price"]["amount"] // 100
            c["sale"] = p["salePrice"]["amount"] // 100
            WRITES.append((Clock.t, "lamoda", "Ламода", p["parentSku"], c["price"], c["sale"], "post"))
        return {"successCount": len(body["prices"]), "errorCount": 0, "errors": [], "fraudValidationResults": []}

    def push_prices(self, items):
        find("BUG", f"Ламода получила общую отправку: {[(i.external_id, i.price) for i in items][:3]}")
        return {"ok": [], "errors": [{"detail": "запрещено", "items": [i.barcode for i in items]}]}


CATALOGS = {}

# повторный запрос к ЦБ из reference.run идёт без сессии — в симуляции только поддельный ЦБ
_fetch = rates.fetch_cbr
rates.fetch_cbr = lambda db, session=None: _fetch(db, session or FakeCbr())



def factory(platform, creds):
    key = creds.get("token") or creds.get("client_id")
    if platform == "wb":
        return FakeWb(key, CATALOGS[key])
    if platform == "kit":
        return FakeKit(CATALOGS[key])
    if platform == "lamoda":
        return FakeLamoda(CATALOGS[key])
    raise RuntimeError(platform)


class FakeCbr:
    """requests-подобная сессия для курса ЦБ."""
    def get(self, url, timeout=0):
        if M.cbr_down:
            import requests
            raise requests.ConnectionError("ЦБ недоступен (сим)")
        d = Clock.t.astimezone()
        xml = (f'<ValCurs Date="{d:%d.%m.%Y}"><Valute><CharCode>USD</CharCode><Nominal>1</Nominal>'
               f'<Value>{str(M.rate).replace(".", ",")}</Value></Valute></ValCurs>').encode()

        class R:
            content = xml

            def raise_for_status(self):
                pass
        return R()


# --- начальные данные ---------------------------------------------------------------------
N_ART, SIZES = 40, ["S", "M", "L", "XL"]
WB_CABS = ["ИП Караман", "ИП Ребрик", "ИП Яворская"]


def setup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    ensure_defaults(db)
    settings.put(db, settings.RATE_MODE, "cbr")
    settings.put(db, settings.ETALON_FOLLOW_AUTO, "1")
    settings.put(db, settings.REF_MAX_RAISE_PERCENT, "200")
    db.commit()
    rates.fetch_cbr(db, FakeCbr())
    accs = {}
    for name in WB_CABS:
        a = Account(platform="wb", name=name)
        db.add(a)
        accs[name] = a
    kit = Account(platform="kit", name="КИТ")
    lam = Account(platform="lamoda", name="Ламода")
    db.add_all([kit, lam])
    db.commit()
    for p, com in (("wb", 25), ("kit", 10), ("lamoda", 40)):
        r = pricing.get_rule(db, p)
        r.commission_percent = Decimal(com)
        r.base_coef, r.min_markup_coef, r.max_change_percent = Decimal("2.5"), Decimal("1.3"), Decimal(20)
        r.round_step, r.round_minus = 10, 1
    db.commit()
    for a in list(accs.values()) + [kit, lam]:
        field = "client_id" if a.platform == "lamoda" else "token"
        acc_mod.set_credential(db, a, field, a.name)
        if a.platform == "lamoda":
            acc_mod.set_credential(db, a, "client_secret", "s")
            acc_mod.set_credential(db, a, "seller_id", "1")
    db.commit()
    cat = {n: [] for n in WB_CABS}
    cat["КИТ"], cat["Ламода"] = [], []
    for ai in range(N_ART):
        art = f"A{ai:03d}"
        cost = Decimal(rnd.choice([8, 10, 12, 15, 18, 22, 30]))
        for si, size in enumerate(SIZES):
            item = f"u{ai:03d}{size}"
            bc = f"2{ai:05d}{si}"
            c = cost + (Decimal(3) if size == "XL" else 0)      # дорогой размер
            db.add(OnecBarcode(barcode=bc, item_id=item, article=art, name=f"Товар {art}", size=size, color="X"))
            db.add(OnecCost(item_id=item, cost_usd=c))
            for ci, name in enumerate(WB_CABS):
                nm = 100000 * (ci + 1) + ai
                cat[name].append(CatalogRow(f"{nm}:{si}", bc, art, f"Товар {art}", size, "Свитшоты", "1"))
            cat["КИТ"].append(CatalogRow(f"v{ai:03d}{si}", bc, art, f"Товар {art}", size))
            cat["Ламода"].append(CatalogRow(f"MP{ai:03d}:{si}", bc, art, f"Товар {art}", size))
    db.commit()
    CATALOGS.update(cat)
    # витрина: WB — наценка ×(4..6) от себестоимости, скидка 35–50
    for ci, name in enumerate(WB_CABS):
        M.wb[name] = {}
        for ai in range(N_ART):
            cost = db.get(OnecCost, f"u{ai:03d}XL").cost_usd
            sale = int(Decimal(cost) * M.rate * Decimal(rnd.choice([3.2, 3.6, 4.0, 4.5])))
            disc = rnd.randint(35, 50)
            M.wb[name][100000 * (ci + 1) + ai] = {"list": int(sale * 100 / (100 - disc)), "disc": disc,
                                                    "promo": False, "pre": None}
    for ai in range(N_ART):
        for si in range(len(SIZES)):
            M.kit[f"v{ai:03d}{si}"] = {"price": 9000, "sale": 6000}
        M.lam[f"MP{ai:03d}"] = {"price": 9000, "sale": 4500}
    # загрузка каталогов и цен, эталон = то, что WB держит сейчас
    for a in db.query(Account):
        acc_mod.load_catalog(db, a, acc_mod.client_for(db, a, factory))
        acc_mod.load_prices(db, a, acc_mod.client_for(db, a, factory))
    from priceapp import mapping
    rule = pricing.get_rule(db, "wb")
    for a in db.query(Account).filter(Account.platform == "wb"):
        for item_id, rows in mapping.account_items(db, a.id).items():
            reference.record(db, a, item_id, rows[0].current_price, rows, rule, source="adopt")
    db.commit()
    # оператор: «Привести все площадки к эталону» — старт из согласованного состояния
    cards = [c for c in etalon.follower_plan(db) if not c.skip and not c.follows]
    etalon.send_followers(db, cards, factory, "оператор")
    for a in db.query(Account).filter(Account.platform != "wb"):
        acc_mod.load_prices(db, a, acc_mod.client_for(db, a, factory))
    db.close()


# --- проверки -------------------------------------------------------------------------------
def floor_ok_after_writes(since_idx):
    """Каждая НАША запись на площадку — не ниже пола у каждого размера ключа."""
    db = SessionLocal()
    try:
        rate = rates.current(db)
        if rate is None:
            return
        costs = {c.item_id: c.cost_usd for c in db.query(OnecCost)}
        item_of = {b.barcode: b.item_id for b in db.query(OnecBarcode)}
        acc_by_name = {a.name: a for a in db.query(Account)}
        rows_by_key = defaultdict(list)
        for it in db.query(PlatformItem):
            a = db.get(Account, it.account_id)
            key = (a.name, it.external_id.split(":")[0] if a.platform != "kit" else it.external_id)
            rows_by_key[key].append(it)
        for t, plat, accn, key, lst, sale, how in WRITES[since_idx:]:
            rule = pricing.get_rule(db, plat)
            rows = rows_by_key.get((accn, str(key)), [])
            disc = (Decimal(1) - Decimal(sale) / Decimal(lst)) if lst and sale and sale < lst else Decimal(0)
            facts = pricing.item_facts(rule, rows)
            for r in rows:
                base = pricing.base_price(costs.get(item_of.get(r.barcode)), rate.usd_rub)
                if base is None or facts.commission is None:
                    continue
                fl = pricing.floor_price(base, rule, facts.commission, disc)
                if lst < fl:
                    find("НИЖЕ ПОЛА", f"{plat} {accn} {key}: ушло {lst}/{sale} при поле {fl} ({how})")
                    break
    finally:
        db.close()


def check_jumps(since_idx, last_sale):
    for t, plat, accn, key, lst, sale, how in WRITES[since_idx:]:
        prev = last_sale.get((plat, accn, key))
        if prev and sale and abs(sale / prev - 1) > 0.6:
            find("СКАЧОК", f"{plat} {accn} {key}: {prev} -> {sale} ({how})")
        last_sale[(plat, accn, key)] = sale


def heartbeat_errors():
    from priceapp.models import WorkerHeartbeat
    db = SessionLocal()
    try:
        for hb in db.query(WorkerHeartbeat):
            if not hb.last_success:
                find("ЗАДАНИЕ УПАЛО", f"{hb.name}: {hb.last_error[:200]}")
        st = heartbeat.stale_workers(db)
        for s in st:
            find("ПРОТУХЛО", s)
    finally:
        db.close()


# --- рынок и оператор -----------------------------------------------------------------------
def market_midnight(day):
    """Акции WB стартуют в полночь; часть кончается."""
    for name in WB_CABS:
        for nm, c in M.wb[name].items():
            if c["promo"] and rnd.random() < 0.3:          # акция закончилась — WB возвращает прежнее
                c["list"], c["disc"] = c["pre"]
                c["promo"] = False
            elif not c["promo"]:
                r = rnd.random()
                if r < 0.06:                                # глубокая акция: скидка 84–87, цена до скидки ниже
                    c["pre"] = (c["list"], c["disc"])
                    c["list"] = int(c["list"] * rnd.uniform(0.6, 0.9))
                    c["disc"] = rnd.randint(84, 87)
                    c["promo"] = True
                elif r < 0.12:                              # мягкая: +3..+12 п.п. скидки
                    c["pre"] = (c["list"], c["disc"])
                    c["disc"] = min(c["disc"] + rnd.randint(3, 12), 70)
                    c["promo"] = True


def market_rate(day):
    step = Decimal(str(rnd.gauss(0, 0.006)))
    if day == 40:
        step = Decimal("0.06")                              # резкий рост
    if day == 70:
        step = Decimal("0.25")                              # скачок больше 20%
    if day == 72:
        step = Decimal("-0.18")
    M.rate = (M.rate * (1 + step)).quantize(Decimal("0.0001"))
    M.cbr_down = day in (55, 56)                            # ЦБ недоступен двое суток


def market_costs(day):
    if day % 30 == 15:
        db = SessionLocal()
        for c in db.query(OnecCost):
            if rnd.random() < 0.2:
                c.cost_usd = (Decimal(c.cost_usd) * Decimal(str(rnd.uniform(0.9, 1.15)))).quantize(Decimal("0.01"))
        db.commit()
        db.close()
    if day == 50:                                           # опечатка в 1С: себестоимость ×10
        db = SessionLocal()
        c = db.get(OnecCost, "u005M")
        c.cost_usd = Decimal(c.cost_usd) * 10
        db.commit()
        db.close()
        find("СОБЫТИЕ", "1С: себестоимость u005M ×10")


def operator(day):
    if day % 7 != 3:
        return
    db = SessionLocal()
    try:
        ets = etalon.etalons(db)
        arts = [a for a, e in ets.items() if e.sale]
        for art in rnd.sample(arts, 3):
            k = rnd.choice([0.85, 0.9, 1.1, 1.15, 0.55])
            new = int(ets[art].sale * k)
            rep = etalon.set_etalon(db, art, new, "оператор", client_factory=factory)
            bad = [r for r in rep if "не записан" in r.lower() or "не отправлено" in r]
            if bad:
                find("ОПЕРАТОР", f"{art} {ets[art].sale}->{new}: " + "; ".join(bad)[:200])
    finally:
        db.close()


# --- расписание ---------------------------------------------------------------------------
def tick(minutes):
    Clock.t += _dt.timedelta(minutes=minutes)


def sizes():
    db = SessionLocal()
    try:
        return {"price_changes": db.query(PriceChange).count(), "audit": db.query(AuditLog).count(),
                "approved": db.query(PriceChange).filter_by(status="approved").count(),
                "error": db.query(PriceChange).filter_by(status="error").count(),
                "db_kb": (ROOT / "sim.db").stat().st_size // 1024,
                "backups": len(list((ROOT / "backups").glob("*.db"))) if (ROOT / "backups").exists() else 0,
                "etalon_files": len(list((ROOT / "etalon_history").glob("*.xlsx"))) if (ROOT / "etalon_history").exists() else 0}
    finally:
        db.close()


def divergence():
    db = SessionLocal()
    try:
        plan = etalon.follower_plan(db)
        d = Counter()
        for c in plan:
            if c.skip:
                d[f"{c.platform}:skip:{c.skip[:25]}"] += 1
            elif not c.follows:
                d[f"{c.platform}:расходится"] += 1
        wb_below = 0
        for name in WB_CABS:
            for nm, c in M.wb[name].items():
                if c["promo"] and c["disc"] > 50:
                    wb_below += 1
        d["wb:в глубокой акции сейчас"] = wb_below
        return dict(d)
    finally:
        db.close()


def run():
    setup()
    t0 = time.time()
    last_sale = {}
    daily = []
    sends_per_key = Counter()
    for day in range(1, DAYS + 1):
        day_start_writes = len(WRITES)
        # 00:00 — акции, курс, себестоимость
        market_midnight(day)
        market_rate(day)
        market_costs(day)
        for minute in range(0, 24 * 60, 10):
            idx = len(WRITES)
            hhmm = minute
            if hhmm in (10, 6 * 60 + 10, 12 * 60 + 10, 18 * 60 + 10):
                jobs.job_wb_poll(factory)
            if hhmm % 360 == 30:
                jobs.job_rate(FakeCbr())
            if hhmm == 60:
                jobs.job_daily_refresh(factory)
            if hhmm == 180:
                jobs.job_backup()
            if hhmm == 10 * 60:
                operator(day)
            jobs.job_onec_exchange()
            jobs.job_price_dispatch(factory)
            if hhmm % 60 == 0:
                jobs.job_attention()
            if len(WRITES) > idx:
                floor_ok_after_writes(idx)
                check_jumps(idx, last_sale)
            tick(10)
        heartbeat_errors()
        for w in WRITES[day_start_writes:]:
            sends_per_key[(w[1], w[2], w[3])] += 1
        s = sizes()
        s.update({"day": day, "writes": len(WRITES) - day_start_writes, "rate": float(M.rate)})
        s.update(divergence())
        daily.append(s)
        print(json.dumps(s, ensure_ascii=False), flush=True)
    print(f"--- {DAYS} дней за {time.time() - t0:.0f} с, база {ROOT}")
    busiest = sends_per_key.most_common(8)
    print("Чаще всего переписывали:", busiest)
    for k, v in FIND.items():
        print(f"\n=== {k}: {len(v)}")
        for line in v[:15]:
            print("  ", line)
    json.dump({"daily": daily, "find": FIND}, open(ROOT / "result.json", "w"), ensure_ascii=False, default=str)


if __name__ == "__main__":
    run()

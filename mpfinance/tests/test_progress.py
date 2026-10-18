"""Шкала прогресса: кнопки (`static/progress.js`) и фоновые дела.

Проверяется то, на чём шкала держится на стороне сервера: ответ-переход для
фоновой отправки формы не съедает сообщение об итоге, операция сообщает свой
ход по номеру, а фоновые дела видны в `/progress/jobs`. И то, что в шаблонах
нет кнопок мимо шкалы."""
import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from finapp import progress
from finapp.finance import fetch
from finapp.models import OnecTask, OneCUploadStatus, ReportFetch
from finapp.timeutils import now_utc
from tests.finance_factories import make_account, make_product, make_report, wb_file, wb_row
from tests.test_finance_api_fetch import _cred, _wb_line, _wb_session

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "finapp" / "templates"


def test_background_submit_gets_the_redirect_as_json_and_keeps_the_message(client):
    data = {"FIN_TAX_MODE": "revenue", "FIN_TAX_RATE": "7"}
    response = client.post("/finance/settings", data=data, headers={"X-Progress": "1"},
                           follow_redirects=False)
    assert response.status_code == 200
    assert response.json() == {"location": "/finance/costs#settings"}
    # Сообщение об итоге ждёт страницу, на которую перейдёт кнопка.
    assert "Настройки расчёта сохранены" in client.get("/finance/costs").text


def test_plain_submit_still_redirects(client):
    response = client.post("/finance/settings", data={"FIN_TAX_MODE": "revenue", "FIN_TAX_RATE": "6"},
                           follow_redirects=False)
    assert response.status_code == 303


def test_expired_login_on_background_submit_leads_to_login(anon_client):
    response = anon_client.post("/finance/settings", data={}, headers={"X-Progress": "1"},
                                follow_redirects=False)
    assert response.json() == {"location": "/login"}


def test_upload_reports_its_progress_by_operation_id(client, db):
    make_product(db)
    op = "op12345678abc"
    client.post("/finance/reports/upload", data={"platform": "wb"},
                files={"file": ("отчёт.xlsx", wb_file([wb_row(), wb_row(srid="2")]))},
                headers={"X-Progress": "1", "X-Progress-Id": op})
    state = client.get(f"/progress/op/{op}").json()
    assert state["note"] and state["total"] == 2 and state["done"] == 2   # разбор строк дошёл до конца
    assert client.get("/progress/op/unknownid123").json() == {}
    assert client.get("/progress/op/bad!id").status_code == 400


def test_report_without_operation_is_silent():
    progress.report(1, 2, "никто не слушает")                    # не падает и не копит
    token = progress.begin("abcdefgh1")
    try:
        progress.report(3, 10, "строки")
        progress.report(note="запись")
        assert progress.get("abcdefgh1") == {"done": 3, "total": 10, "note": "запись"}
    finally:
        progress.end(token)
    progress.report(9, 9, "после конца")
    assert progress.get("abcdefgh1")["done"] == 3


def test_wb_fetch_counts_one_step_per_report_detail(db):
    make_product(db)
    account = make_account(db)
    _cred(db, account, "finance_token", "fin")
    job, _ = fetch.request_fetch(db, "wb_api", "admin", account.id, date(2026, 9, 1), date(2026, 9, 30))
    db.commit()
    fetch.run_pending(db, session=_wb_session([_wb_line()]), sleep=lambda s: None)
    db.refresh(job)
    assert (job.progress_done, job.progress_total) == (1, 1)
    assert "отчёт №307401554" in job.progress_note


def test_jobs_show_running_fetches_1c_tasks_and_finished_results(client, db):
    account = make_account(db, name="WB Яворская")
    running = ReportFetch(kind="wb_api", account_id=account.id, status="running", date_from=date(2026, 9, 1),
                          date_to=date(2026, 9, 30), progress_done=3, progress_total=12,
                          progress_note="отчёт №1 (4 из 12)")
    finished = ReportFetch(kind="wb_api", account_id=account.id, status="failed", message="нет токена",
                           finished_at=now_utc())
    old = ReportFetch(kind="wb_api", account_id=account.id, status="done", message="давно",
                      finished_at=now_utc() - timedelta(hours=1))
    db.add_all([running, finished, old,
                OnecTask(command="FIN_PING", key="FINPING-1", line="x", status="sent")])
    report = make_report(db, key="SR-1c-progress")
    report.onec_status = OneCUploadStatus.pending
    db.commit()
    body = client.get("/progress/jobs").json()
    active = {item["key"]: item for item in body["active"]}
    item = active[f"fetch:{running.id}"]
    assert (item["done"], item["total"]) == (3, 12) and "WB Яворская" in item["label"]
    assert item["eta_seconds"] == 9 * 61                           # минута на отчёт WB
    assert active["onec:1"]["done"] == 2 and "ждём ответ" in active["onec:1"]["note"]
    assert active[f"report1c:{report.id}"]["done"] == 1
    assert [f["key"] for f in body["finished"]] == [f"fetch:{finished.id}"]   # старый итог не показываем
    assert body["finished"][0]["note"] == "нет токена"


def test_jobs_need_login(anon_client):
    assert anon_client.get("/progress/jobs", follow_redirects=False).status_code == 303


# ------------------------------------------------------------ шаблоны и скрипт

def test_every_page_carries_the_progress_script_and_jobs_panel():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "progress.js" in base and 'id="jobs-panel"' in base
    assert "progress.js" in (TEMPLATES / "login.html").read_text(encoding="utf-8")


def test_no_template_submits_a_form_past_the_progress_bar():
    """`form.submit()` не порождает события submit — такая кнопка шла бы мимо
    шкалы. Нужен `requestSubmit()`."""
    offenders = [p.name for p in TEMPLATES.glob("*.html")
                 if re.search(r"\.submit\(\)", p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_every_download_link_is_marked():
    """Выгрузка без `data-download` открылась бы переходом: шкала повисла бы
    на «открываю страницу» — страница ведь не сменится."""
    missing = []
    for path in TEMPLATES.glob("*.html"):
        for tag in re.findall(r"<a\b[^>]*>", path.read_text(encoding="utf-8")):
            href = re.search(r'href="([^"]*)"', tag)
            if href and re.search(r"/(export|template)(\?|$)", href.group(1)) and "data-download" not in tag:
                missing.append(f"{path.name}: {tag}")
    assert missing == []


@pytest.mark.skipif(shutil.which("node") is None, reason="нет node — синтаксис скрипта не проверить")
def test_progress_script_parses():
    result = subprocess.run(["node", "--check", str(ROOT / "finapp" / "static" / "progress.js")],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stderr


# ------------------------------------------------------------ аудит 18.10: безопасность

def test_post_from_another_origin_is_refused(client):
    """SameSite=Lax порт не различает: форма со страницы 127.0.0.1:<другой порт>
    ушла бы с cookie входа. Origin порт несёт — по нему и отказ."""
    data = {"FIN_TAX_MODE": "revenue", "FIN_TAX_RATE": "9"}
    refused = client.post("/finance/settings", data=data, headers={"Origin": "http://127.0.0.1:8000"},
                          follow_redirects=False)
    assert refused.status_code == 403
    own = client.post("/finance/settings", data=data, headers={"Origin": "http://testserver"},
                      follow_redirects=False)
    assert own.status_code == 303


def test_warehouse_name_with_a_line_break_is_refused(client):
    response = client.post("/finance/settings", data={"FIN_TAX_MODE": "revenue", "FIN_TAX_RATE": "6",
                                                      "FIN_WAREHOUSE_KIT": "Сайт\nCANCEL_MOVEMENT|x"},
                           follow_redirects=True)
    assert "не сохранены" in response.text


def test_1c_task_line_never_carries_a_line_break(db, epf_ready):
    """Склад с переводом строки, попавший в базу мимо формы, не даёт 1С лишней
    строки-команды: отчёт получает отказ, а файл заданий — ни строки."""
    from finapp.finance import sales_1c, values as v
    from finapp.finance.settings import save_setting
    from tests.finance_factories import make_product, make_report
    make_product(db)
    save_setting(db, "FIN_WAREHOUSE_WB", "Склад\nCANCEL_MOVEMENT")
    report = make_report(db, key="SR-wb-inj", lines=[{"operation": v.SALE, "quantity": 1, "revenue": 1000,
                                                       "payout": 900, "barcode": "2000000000012"}])
    from finapp.finance.importer import resolve_uids
    resolve_uids(db, report)
    db.commit()
    assert sales_1c.task_lines(db, [report], "task_fin_x.txt") == []
    assert report.onec_status == OneCUploadStatus.failed


def test_excel_export_keeps_formula_like_text_as_text():
    import io
    import zipfile

    from finapp.excel_utils import build_xlsx_bytes
    data = build_xlsx_bytes(["Товар"], [["=HYPERLINK(\"http://x\",\"y\")"], ["обычный"]])
    sheet = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode()
    assert "<f>" not in sheet and "HYPERLINK" in sheet

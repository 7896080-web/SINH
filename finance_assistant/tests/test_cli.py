import io

from finance.cli import main


def run(db, *argv):
    out = io.StringIO()
    code = main(["--db", str(db), *argv], out=out)
    return code, out.getvalue()


def test_full_flow(tmp_path):
    db = tmp_path / "f.db"
    assert run(db, "account", "add", "Карта", "--balance", "1000")[0] == 0
    code, out = run(db, "expense", "250,5", "Продукты", "-a", "Карта", "-d", "2026-09-03", "-n", "магазин")
    assert code == 0 and "749,50" in out
    run(db, "income", "5000", "Зарплата", "-a", "Карта", "-d", "2026-09-05")
    run(db, "budget", "set", "Продукты", "200", "-m", "2026-09")

    _, out = run(db, "report", "-m", "2026-09")
    assert "Продукты" in out and "Отложено от дохода" in out and "!!" in out

    _, out = run(db, "list", "-m", "2026-09")
    assert "магазин" in out and out.count("\n") == 2

    _, out = run(db, "account", "list")
    assert "5 749,50" in out


def test_errors_are_reported_not_raised(tmp_path, capsys):
    code, _ = run(tmp_path / "f.db", "expense", "10", "Продукты", "-a", "Нет такого")
    assert code == 1
    assert "нет счёта" in capsys.readouterr().err


def test_recurring_and_export(tmp_path):
    db = tmp_path / "f.db"
    run(db, "account", "add", "Карта")
    run(db, "recurring", "add", "Интернет", "600", "Связь и интернет", "-a", "Карта", "--day", "1")
    _, out = run(db, "recurring", "apply")
    assert "Интернет" in out
    _, out = run(db, "recurring", "apply")
    assert "Нечего" in out
    code, out = run(db, "export", str(tmp_path / "x.csv"))
    assert code == 0 and "1" in out

def test_root_redirects_to_mapping(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/mapping"


def test_unauthenticated_redirects_to_login(client):
    r = client.get("/mapping", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "Войти" in r.text


def test_login_wrong_password(client, web_db):
    from app.models import User
    from app.security import hash_password

    web_db.add(User(username="admin", password_hash=hash_password("secret123")))
    web_db.commit()

    r = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
    assert "Неверный логин" in r.text


def test_login_shows_remaining_attempts(client, web_db):
    from app.models import User
    from app.security import hash_password

    web_db.add(User(username="admin", password_hash=hash_password("secret123")))
    web_db.commit()

    r = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert "Осталось попыток: 9" in r.text

    r2 = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert "Осталось попыток: 8" in r2.text


def test_login_locks_after_ten_failed_attempts(client, web_db):
    from app.models import User
    from app.security import hash_password

    web_db.add(User(username="admin", password_hash=hash_password("secret123")))
    web_db.commit()

    for _ in range(9):
        r = client.post("/login", data={"username": "admin", "password": "wrong"})
        assert r.status_code == 401

    r10 = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert r10.status_code == 401
    assert "заблокирован на 30 минут" in r10.text

    # 11-я попытка — уже с ПРАВИЛЬНЫМ паролем, но аккаунт всё равно заблокирован
    r11 = client.post("/login", data={"username": "admin", "password": "secret123"})
    assert r11.status_code == 429
    assert "Попробуйте снова через" in r11.text


def test_login_lockout_does_not_advance_by_trying_during_lockout(client, web_db):
    """Попытки входа ВО ВРЕМЯ блокировки не должны продлевать её или менять
    счётчик — иначе злоумышленник мог бы держать аккаунт заблокированным вечно
    бесконечными попытками, а не 30 фиксированных минут."""
    from app.models import User
    from app.security import hash_password

    web_db.add(User(username="admin", password_hash=hash_password("secret123")))
    web_db.commit()

    for _ in range(10):
        client.post("/login", data={"username": "admin", "password": "wrong"})

    user = web_db.query(User).filter(User.username == "admin").first()
    locked_until_first = user.locked_until

    client.post("/login", data={"username": "admin", "password": "wrong"})
    web_db.refresh(user)
    assert user.locked_until == locked_until_first  # не продлилось
    assert user.failed_login_attempts == 10  # не выросло дальше


def test_successful_login_resets_lockout_state(client, web_db):
    from app.models import User
    from app.security import hash_password

    web_db.add(User(username="admin", password_hash=hash_password("secret123")))
    web_db.commit()

    for _ in range(5):
        client.post("/login", data={"username": "admin", "password": "wrong"})

    user = web_db.query(User).filter(User.username == "admin").first()
    assert user.failed_login_attempts == 5

    r = client.post("/login", data={"username": "admin", "password": "secret123"}, follow_redirects=False)
    assert r.status_code == 303

    web_db.refresh(user)
    assert user.failed_login_attempts == 0
    assert user.locked_until is None


def test_unknown_username_does_not_crash_or_reveal_existence(client, web_db):
    r = client.post("/login", data={"username": "no-such-user", "password": "whatever"})
    assert r.status_code == 401
    assert "Неверный логин или пароль" in r.text
    assert "Осталось попыток" not in r.text  # для несуществующего пользователя счётчика нет


def test_login_correct_then_access_protected_page(logged_in_client):
    r = logged_in_client.get("/mapping")
    assert r.status_code == 200


def test_logout_clears_session(logged_in_client):
    r = logged_in_client.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"

    r2 = logged_in_client.get("/mapping", follow_redirects=False)
    assert r2.status_code == 303
    assert r2.headers["location"] == "/login"

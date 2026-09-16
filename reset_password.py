"""
Сброс пароля существующего пользователя (или создание нового, если такого
логина ещё нет) — на случай утери пароля, сгенерированного install.sh.
Заодно снимает блокировку по неудачным попыткам входа, если она была.

Запуск: python reset_password.py <логин> <новый_пароль>
"""
import sys

from app.database import SessionLocal, Base, engine
from app.models import User
from app.security import hash_password
from app.audit import log_action


def main():
    if len(sys.argv) != 3:
        print("Использование: python reset_password.py <логин> <новый_пароль>")
        sys.exit(1)

    username, new_password = sys.argv[1], sys.argv[2]

    if len(new_password) < 8:
        print("Пароль слишком короткий — задайте хотя бы 8 символов.")
        sys.exit(1)

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()

        if user is None:
            user = User(username=username, password_hash=hash_password(new_password))
            db.add(user)
            log_action(db, "cli", "user_created_via_reset_script", username)
            db.commit()
            print(f"Пользователь {username} не существовал — создан с новым паролем.")
            return

        user.password_hash = hash_password(new_password)
        user.is_active = True
        was_locked = user.locked_until is not None
        user.failed_login_attempts = 0
        user.locked_until = None
        log_action(db, "cli", "password_reset_via_script", f"{username} (была заблокирована: {was_locked})")
        db.commit()

        print(f"Пароль пользователя {username} обновлён.")
        if was_locked:
            print("Блокировка по неудачным попыткам входа также снята.")
    finally:
        db.close()


if __name__ == "__main__":
    main()

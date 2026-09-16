"""
Разовый скрипт для создания первого пользователя админки.
Запуск: python create_admin_user.py <логин> <пароль>
"""
import sys

from app.database import SessionLocal, Base, engine
from app.models import User
from app.security import hash_password


def main():
    if len(sys.argv) != 3:
        print("Использование: python create_admin_user.py <логин> <пароль>")
        sys.exit(1)

    username, password = sys.argv[1], sys.argv[2]

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            print(f"Пользователь {username} уже существует.")
            return

        user = User(username=username, password_hash=hash_password(password))
        db.add(user)
        db.commit()
        print(f"Пользователь {username} создан.")
    finally:
        db.close()


if __name__ == "__main__":
    main()

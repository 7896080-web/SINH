"""Заведение пользователя админки.

    python create_admin_user.py <логин> <пароль>
    python create_admin_user.py <логин> <пароль> --role warehouse

Роль по умолчанию — `admin`, то есть всё как было. `warehouse` видит ТОЛЬКО
раздел возвратов: остальные страницы двигают боевые остатки по всему каталогу
(массовая правка на «Товарах», «Переотправить остаток», токены площадок), и
кладовщику там делать нечего. Список разрешённого — в `app/access.py`.

Существующему пользователю скрипт роль НЕ меняет и пароль НЕ сбрасывает: это
скрипт заведения, а не правки. Молча переписать доступ живого человека — худшее,
что может сделать разовая команда, запущенная не в том окне.
"""
import sys

from app.database import SessionLocal, Base, engine
from app.models import User, UserRole
from app.security import hash_password

ROLES = {r.value for r in UserRole}


def main():
    args = [a for a in sys.argv[1:]]
    role = UserRole.admin
    if "--role" in args:
        i = args.index("--role")
        if i + 1 >= len(args) or args[i + 1] not in ROLES:
            print(f"Роль указывается так: --role <{'|'.join(sorted(ROLES))}>")
            sys.exit(1)
        role = UserRole(args[i + 1])
        del args[i:i + 2]

    if len(args) != 2:
        print("Использование: python create_admin_user.py <логин> <пароль> "
              f"[--role <{'|'.join(sorted(ROLES))}>]")
        sys.exit(1)

    username, password = args

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            # Роль не трогаем намеренно — см. docstring.
            print(f"Пользователь {username} уже существует (роль {existing.role.value}).")
            return

        user = User(username=username, password_hash=hash_password(password), role=role)
        db.add(user)
        db.commit()
        print(f"Пользователь {username} создан, роль {role.value}.")
        if role is UserRole.warehouse:
            print("Ему доступен только раздел «Возвраты».")
    finally:
        db.close()


if __name__ == "__main__":
    main()

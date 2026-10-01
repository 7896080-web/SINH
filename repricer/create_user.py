"""Завести пользователя или сменить ему пароль.

    python create_user.py <логин> <пароль>
"""
import sys

from priceapp.database import Base, SessionLocal, engine
from priceapp.models import User
from priceapp.security import hash_password
from priceapp.settings import ensure_defaults


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    username, password = argv[1].strip(), argv[2]
    if len(password) < 8:
        print("Пароль короче 8 символов.")
        return 2
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        ensure_defaults(db)
        user = db.query(User).filter(User.username == username).first()
        if user is None:
            db.add(User(username=username, password_hash=hash_password(password)))
            print(f"Пользователь {username} создан.")
        else:
            user.password_hash = hash_password(password)
            user.is_active = True
            print(f"Пароль пользователя {username} обновлён.")
        db.commit()
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

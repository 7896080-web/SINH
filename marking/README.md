# Маркировка и поставки

Программа для поставок на Lamoda: входящий файл → проверка остатка и
перемещение в 1С → УПД и стикеры коробов. Дальше по ТЗ: коды маркировки,
Честный знак, этикетки.

- Задание: `../ТЗ_МАРКИРОВКА.md`
- Сценарий оператора: `../СЦЕНАРИЙ_МАРКИРОВКА.md`
- Правила разработки: `CLAUDE.md`
- Установка и обновление: `deploy/README.md`
- Обновление обработки 1С: `1c/ОБНОВЛЕНИЕ_ОБРАБОТКИ.md`

Локально:
```bash
cd marking
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=sqlite:///./marking.db MARKING_SESSION_SECRET=dev-secret \
  MARKING_SECRETS_KEY=$(python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
alembic upgrade head && python create_user.py admin devpassword
uvicorn markapp.main:app --reload --port 8001
python -m markapp.workers.scheduler   # во втором окне
```

"""Схема: организации (ИП — их несколько, у каждого свой вход в ЧЗ и свой
справочник карточек Нацкаталога), партии кодов, коды, документы ввода.
Таблицы создаются при старте (create_all)."""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from kizapp.db import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Org(Base):
    __tablename__ = "org"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, default="")
    inn = Column(String(12), nullable=False, unique=True)
    token_enc = Column(Text, nullable=True)
    token_until = Column(DateTime, nullable=True)


class Batch(Base):
    __tablename__ = "batches"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("org.id"), nullable=False, index=True)
    title = Column(String(300), nullable=False, default="")
    expected = Column(Integer, nullable=True)
    production_date = Column(String(10), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=now_utc)


class Code(Base):
    __tablename__ = "codes"
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False, index=True)
    cis = Column(String(40), nullable=False, unique=True)
    full_enc = Column(Text, nullable=False)
    gtin = Column(String(14), nullable=False, index=True)
    status = Column(String(30), nullable=False, default="", index=True)
    status_at = Column(DateTime, nullable=True)
    doc_id = Column(Integer, ForeignKey("docs.id"), nullable=True)


class Doc(Base):
    """Документ «Ввод в оборот». new → sending → sent / unknown → CHECKED_OK /
    CHECKED_NOT_OK; error — не отправлен. unknown: ответа ЧЗ нет — документ МОГ
    уйти, коды заняты до решения человека."""
    __tablename__ = "docs"
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False, index=True)
    document = Column(Text, nullable=False)
    codes_count = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="new")
    chz_doc_id = Column(String(64), nullable=False, default="")
    error = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=now_utc)
    sent_at = Column(DateTime, nullable=True)


class Card(Base):
    """Карточка Нацкаталога в справочнике ИП: ТН ВЭД и разрешительный документ
    для документа ввода. Свой справочник у каждого ИП — карточки запрошены его
    токеном (05.10.2026: ИП разные)."""
    __tablename__ = "cards"
    __table_args__ = (UniqueConstraint("org_id", "gtin", name="uq_card_org_gtin"),)
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("org.id"), nullable=False, index=True)
    gtin = Column(String(14), nullable=False)
    name = Column(String(500), nullable=False, default="")
    tnved = Column(String(20), nullable=False, default="")
    permit_type = Column(String(30), nullable=False, default="")
    permit_number = Column(String(100), nullable=False, default="")
    permit_date = Column(String(10), nullable=False, default="")
    error = Column(Text, nullable=False, default="")
    fetched_at = Column(DateTime, nullable=True)


class Journal(Base):
    """Что сделано с внешним эффектом (вход, загрузка, документ, выдача txt)."""
    __tablename__ = "journal"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, nullable=False, default=now_utc, index=True)
    action = Column(String(64), nullable=False)
    details = Column(Text, nullable=False, default="")


class Setting(Base):
    """Настройки «ключ — значение»: шаблон этикетки, размер модуля."""
    __tablename__ = "settings"
    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False, default="")

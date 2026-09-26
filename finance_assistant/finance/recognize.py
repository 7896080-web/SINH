"""Распознавание скриншотов платежей и выписок через Claude API.

Модель возвращает строго JSON по схеме (structured outputs), поэтому ответ
не нужно выковыривать из текста. Суммы модель отдаёт строкой ("1234.50"),
в копейки их переводит наш код — чтобы не зависеть от float.
"""

import base64
import csv
import io
import json
import logging

import anthropic

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
# Если классификатор безопасности отклонит запрос, API сам повторит его на
# рекомендованной резервной модели.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class RecognitionError(RuntimeError):
    """Не удалось получить ответ модели — показываем пользователю как есть."""


def _payment_schema(categories: list[str]) -> dict:
    s = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "is_payment": {"type": "boolean"},
            "direction": {"type": "string", "enum": ["out", "in"]},
            "amount": s,
            "currency": s,
            "date": s,
            "card_last4": s,
            "bank": s,
            "merchant": s,
            "description": s,
            "category": {"type": "string", "enum": categories + [""]},
            "category_confident": {"type": "boolean"},
            "looks_personal": {"type": "boolean"},
        },
        "required": ["is_payment", "direction", "amount", "currency", "date", "card_last4",
                     "bank", "merchant", "description", "category", "category_confident",
                     "looks_personal"],
        "additionalProperties": False,
    }


STATEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_statement": {"type": "boolean"},
        "card_last4": {"type": "string"},
        "total_in": {"type": "string"},
        "total_out": {"type": "string"},
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "amount": {"type": "string"},
                    "direction": {"type": "string", "enum": ["out", "in"]},
                    "description": {"type": "string"},
                    "own_transfer": {"type": "boolean"},
                },
                "required": ["date", "amount", "direction", "description", "own_transfer"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["is_statement", "card_last4", "total_in", "total_out", "operations"],
    "additionalProperties": False,
}

PAYMENT_PROMPT = """\
Это скриншот (или текстовое описание) операции по личной банковской карте \
предпринимателя. Обычно это расход на бизнес, оплаченный с личной карты.
Сегодня {today}. Карты владельца: {cards}.
{hint}
Извлеки данные операции:
- is_payment: false, если это вообще не банковская операция/чек.
- direction: "out" — списание/оплата/перевод с карты, "in" — поступление на карту.
- amount: сумма в валюте операции, число с точкой без пробелов ("1234.50"); "" если не видно.
- currency: код валюты (RUB, USD…), по умолчанию RUB.
- date: дата операции ГГГГ-ММ-ДД; если год не указан — ближайшая прошедшая дата; \
"сегодня"/"вчера" переводи в дату; "" если даты нет.
- card_last4: последние 4 цифры карты списания, если видны; иначе "".
- bank: банк карты списания, если понятно (Сбер, Т-Банк, Альфа…); иначе "".
- merchant: получатель/магазин. description: что оплачено, коротко, по-русски.
- category: статья бизнес-расходов строго из списка; "" если ни одна не подходит.
- category_confident: true, только если статья очевидна.
- looks_personal: true, если это явно личная покупка (продукты домой, одежда и т.п.).
Ничего не выдумывай: если поля не видно — пустая строка."""

STATEMENT_PROMPT = """\
Это выписка (или её часть/скриншот) по личной банковской карте за {month}. \
Карты владельца: {cards}.
Извлеки ВСЕ операции, ни одной не пропуская:
- date ГГГГ-ММ-ДД, amount — положительное число с точкой ("1234.50"),
- direction: "out" — списание, "in" — зачисление,
- description — как в выписке, коротко,
- own_transfer: true, если это перевод между картами владельца \
(в описании видны последние цифры другой его карты, или "перевод между своими счетами").
Отложенные/заблокированные суммы (холды), если они помечены отдельно, не включай.
total_in / total_out — итоговые "поступления"/"расходы" за период, если они \
прямо напечатаны в документе; иначе "".
Если прислан просто текст вида "пришло 100000, ушло 80000" — заполни только \
total_in / total_out, operations пустой.
card_last4 — последние 4 цифры карты из документа, если есть.
is_statement: false, если это не выписка и не итоги по карте."""


def file_blocks(files: list[tuple[bytes, str]]) -> list[dict]:
    """(содержимое, mime) -> content-блоки для Claude. CSV/XLSX идут текстом."""
    blocks = []
    for data, mime in files:
        if mime in IMAGE_TYPES:
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": mime,
                "data": base64.standard_b64encode(data).decode()}})
        elif mime == "application/pdf":
            blocks.append({"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf",
                "data": base64.standard_b64encode(data).decode()}})
        elif "spreadsheetml" in mime:
            blocks.append({"type": "text", "text": xlsx_to_csv_text(data)})
        else:  # csv / txt — пробуем как текст
            blocks.append({"type": "text", "text": decode_text(data)})
    return blocks


def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def xlsx_to_csv_text(data: bytes) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            if any(v is not None for v in row):
                writer.writerow(["" if v is None else v for v in row])
    return out.getvalue()


def _cards_text(cards) -> str:
    if not cards:
        return "не заданы"
    return "; ".join(
        f"{c.name}" + (f" (…{c.last4})" if c.last4 else "") + (f", {c.bank}" if c.bank else "")
        for c in cards
    )


class ClaudeRecognizer:
    def __init__(self, client: anthropic.Anthropic | None = None, model: str = DEFAULT_MODEL):
        self.client = client or anthropic.Anthropic()
        self.model = model

    def recognize_payment(self, files, text, *, today, cards, categories) -> dict:
        hint = f'Комментарий владельца к операции: "{text}"' if text else ""
        prompt = PAYMENT_PROMPT.format(today=today, cards=_cards_text(cards), hint=hint)
        return self._ask(file_blocks(files), prompt, _payment_schema(categories),
                         effort="medium", max_tokens=16000)

    def parse_statement(self, files, text, *, month, cards) -> dict:
        prompt = STATEMENT_PROMPT.format(month=month, cards=_cards_text(cards))
        if text:
            prompt += f'\n\nТекст от владельца: "{text}"'
        return self._ask(file_blocks(files), prompt, STATEMENT_SCHEMA,
                         effort="high", max_tokens=64000)

    def _ask(self, blocks, prompt, schema, *, effort, max_tokens) -> dict:
        content = blocks + [{"type": "text", "text": prompt}]
        try:
            # Потоковый режим: длинная выписка может отвечаться долго, а
            # обычный запрос с большим max_tokens упрётся в HTTP-таймаут.
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                output_config={"effort": effort,
                               "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": content}],
            ) as stream:
                message = stream.get_final_message()
        except anthropic.RateLimitError:
            raise RecognitionError("сервис распознавания перегружен, пришлите ещё раз через минуту") from None
        except anthropic.APIStatusError as exc:
            log.exception("Claude API error")
            raise RecognitionError(f"ошибка сервиса распознавания ({exc.status_code})") from None
        except anthropic.APIConnectionError:
            raise RecognitionError("нет связи с сервисом распознавания") from None

        if message.stop_reason == "refusal":
            raise RecognitionError("модель отказалась обрабатывать это изображение")
        if message.stop_reason == "max_tokens":
            raise RecognitionError("документ слишком большой — пришлите его частями")
        # После блока fallback (если резервная модель подхватила ответ) идёт
        # итоговый текст; берём текстовые блоки после последнего такого блока.
        texts: list[str] = []
        for block in message.content:
            if block.type == "fallback":
                texts = []
            elif block.type == "text":
                texts.append(block.text)
        try:
            return json.loads("".join(texts))
        except json.JSONDecodeError:
            log.error("Не JSON от модели: %r", texts)
            raise RecognitionError("не удалось разобрать ответ модели") from None

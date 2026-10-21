"""Плоское экспоненциальное сглаживание (SES) — прогноз Фазы 1 (без ML, раздел 12 ТЗ).

Прогноз на любой горизонт — последний сглаженный уровень (плоская линия). σ для
страхового запаса — стандартное отклонение ОШИБКИ прогноза на шаг вперёд (факт −
прогноз), а не разброс сырых продаж: так требует раздел 5.5 (иначе сезонность и тренд,
уже учтённые прогнозом, задвоились бы в буфере).

Выбросы (разовая оптовая продажа, тендер — раздел 5.5) срезаются ДО сглаживания: день,
где продано больше порога, считается продажей ровно на порог. Иначе такой день поднял
бы уровень на недели вперёд, а ошибки следующих дней раздули бы σ. Порог считается по
ненулевым продажам (сколько берут, когда берут) — так редкие продажи крайних размеров
(1 шт. раз в 10 дней) выбросами не становятся, — и с нижней границей разброса √среднего,
как у счётных данных: при «всегда по одной» случайная пара штук — не опт.

В Фазе 2 этот модуль станет одним из кандидатов бэктест-турнира (раздел 5.2) рядом с
TSB/CrostonSBA из statsforecast.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

ALPHA_GRID = (0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5)
WARMUP_MAX = 7
MIN_NONZERO_FOR_OUTLIERS = 5
MAX_OUTLIER_PASSES = 5


@dataclass
class Fit:
    level: float              # прогноз спроса в день (≥ 0)
    sigma: float              # σ ошибки прогноза на шаг вперёд, шт/день
    n: int                    # сколько дней пошло в расчёт
    method: str               # smoothing / mean / none
    alpha: float | None = None
    outliers: int = 0         # сколько дней срезано как выброс
    errors: list[float] = field(default_factory=list, repr=False)


def _mean(values) -> float:
    return sum(values) / len(values) if values else 0.0


def _pstdev(values) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


def outlier_limit(values: list[float], threshold: float) -> float | None:
    """Порог выброса по ненулевым продажам: среднее + threshold·max(σ, √среднего),
    пересчитанный без уже найденных выбросов (один большой опт раздувает σ так, что
    после одного прохода «нормальным» выглядит всё). None — судить не по чему."""
    kept = [v for v in values if v > 0]
    if threshold <= 0 or len(kept) < MIN_NONZERO_FOR_OUTLIERS:
        return None
    limit = None
    for _ in range(MAX_OUTLIER_PASSES):
        m = _mean(kept)
        limit = m + threshold * max(_pstdev(kept), math.sqrt(m))
        next_kept = [v for v in kept if v <= limit]
        if len(next_kept) == len(kept) or len(next_kept) < MIN_NONZERO_FOR_OUTLIERS:
            break
        kept = next_kept
    return limit


def cap_outliers(values: list[float], threshold: float) -> tuple[list[float], int]:
    limit = outlier_limit(values, threshold)
    if limit is None:
        return list(values), 0
    capped = sum(1 for v in values if v > limit)
    return [min(v, limit) for v in values], capped


def ses_errors(values: list[float], alpha: float, warmup: int) -> tuple[float, list[float]]:
    """Уровень после всего ряда и ошибки на шаг вперёд. Первые `warmup` дней — только
    начальный уровень (их среднее), ошибок по ним нет."""
    level = _mean(values[:warmup])
    errors = []
    for y in values[warmup:]:
        errors.append(y - level)
        level += alpha * (y - level)
    return level, errors


def fit(values: list[float], *, alpha: float | None = None, min_history: int = 14,
        outlier_sigma: float = 3.0) -> Fit:
    values = [float(v) for v in values]
    n = len(values)
    if n == 0:
        return Fit(level=0.0, sigma=0.0, n=0, method="none")
    values, capped = cap_outliers(values, outlier_sigma)
    if n < min_history:
        # Мало истории: среднее и разброс; нижняя граница σ — пуассоновская √среднего,
        # иначе по трём одинаковым дням σ вышла бы нулевой и буфера не было бы вовсе.
        level = max(_mean(values), 0.0)
        sigma = max(_pstdev(values), math.sqrt(level))
        return Fit(level=level, sigma=sigma, n=n, method="mean", outliers=capped)
    warmup = min(WARMUP_MAX, max(1, n // 3))
    best = None
    for a in ((alpha,) if alpha is not None else ALPHA_GRID):
        level, errors = ses_errors(values, a, warmup)
        sse = sum(e * e for e in errors)
        if best is None or sse < best[0] - 1e-12:
            best = (sse, a, level, errors)
    _, best_alpha, level, errors = best
    return Fit(level=max(level, 0.0), sigma=_pstdev(errors), n=n, method="smoothing",
               alpha=best_alpha, outliers=capped, errors=errors)

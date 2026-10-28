#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verdict_1511.py — ВЕРДИКТ ТУРНИРА СИТ 15.11.2026 (блоки C1/C2).

Скрипт исполняет БУКВАЛЬНО замороженные правила `screener_tournament_rules_v1.md`
(заморожены 30.08.2026, правка до 15.11 запрещена). Ни одного решения о способе
счёта здесь не принимается заново: всё, что ниже, — исполнение предрегистрации.

ЧТО СЧИТАЕТСЯ (§ — пункты замороженных правил):
  §1  сравниваются дорожки A1..A8, контроль — A7 и только A7.
      A0 (основной бумажный портфель) в статистический тест НЕ входит; она
      участвует ОТДЕЛЬНО, вторым порогом протокола денег (§6).
  §2  единица измерения — ТОЛЬКО закрытые сделки. open_pnl не идёт в вердикт
      ни в каком виде: ни метрикой, ни «предварительным срезом», ни картинкой.
  §3.1 блок = токен; полный блок = токен, закрытый У ВСЕХ участвующих сит.
      Дорожка с нулём закрытых исключается ПОИМЁННО с причиной.
      Если полных блоков N < 10 — переключение на ЗАПАСНОЙ дизайн «блок = прогон»
      (дата входа), отклик = средний net закрытых позиций дорожки из этого прогона.
      Переключение зависит ТОЛЬКО от N и печатается вместе с обоими N.
  §3.2 ранги внутри блока, ранг 1 = лучший, связки — средний ранг.
  §3.3 Фридман — КОНТРОЛЬНЫЙ, печатается всегда, вердикт по нему не выносится.
  §3.4 Иман–Дэвенпорт — ГЛАВНЫЙ.
  §3.5 alpha = 0.05.
  §4  пост-хок Холма, k-1 пар, каждое сито ПРОТИВ A7. Пары сит между собой
      не считаются и в отчёт не идут.
  §5  вердикт РОВНО одной из трёх строк (см. VERDICT_* ниже).
  §6  второй порог протокола денег считается ОТДЕЛЬНО и печатается рядом.

ПОПРАВКА №1 (tournament_rules_addendum_1.md, принята 02.09.2026 — ДО первых
закрытий 14.09/21.09, при нуле закрытых сделок): дорожка A6 — НАБЛЮДАТЕЛЬНАЯ,
вне подтверждающего семейства (Иман–Дэвенпорт/Фридман/Холм против A7). Она
считается и печатается ОПИСАТЕЛЬНО (n, среднее, медиана, IQR, доля прибыльных)
с пометкой «вне теста»; в k, N и ранги не входит. Флаг читается ИЗ ФАЙЛА
поправки (rules_addendum.py), а не из констант здесь: нет файла — семейство
прежнее (все непустые дорожки), и это печатается. Правила v1 не тронуты,
их sha256 закреплён внутри поправки и сверяется при каждом прогоне.

ЧЕСТНЫЕ ОГОВОРКИ, которые скрипт печатает сам и не прячет:
  * net-доходность опирается на ДОПУЩЕНИЕ о слиппедже (стакана у проекта нет),
    поэтому вердикт печатается при КАЖДОМ множителе из costs.yaml
    sensitivity_multipliers. Если вердикт меняется от множителя — это отдельная
    строка отчёта заглавными буквами: значит решают издержки, а не сито.
  * пост-хок p-value ДВУСТОРОННЕЕ (2*normal_sf(|z|)) — так определён z в
    stats_core. Победителем объявляется только сито, которое и прошло Холма,
    И имеет средний ранг ЛУЧШЕ контроля (иначе «значимо хуже случайного»
    попало бы в победители).
  * мощность (C2) считается ЗАРАНЕЕ и от результатов не зависит: она функция
    только k и N. Порог мощности объявлен явно — POWER_THRESHOLD = 0.5.
  * ключ --sims меняет ТОЛЬКО подробную таблицу мощности в отчёте. На вердикт
    он не влияет: решение принимается по отдельному фиксированному прогону
    (POWER_DECISION_SIMS, POWER_SEED), иначе вердикт зависел бы от ключа CLI.

CLI: python verdict_1511.py [--as-of YYYY-MM-DD] [--sims N]
Артефакты: artifacts/verdict_latest.json и artifacts/YYYY-MM/verdict_YYYYMMDD.json
"""

import argparse
import datetime as _dt
import json
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import costs                      # noqa: E402  модель издержек (A1)
import provenance                 # noqa: E402  штамп происхождения (E2)
import rules_hash                 # noqa: E402  sha256 замороженных правил (C3)
import rules_addendum             # noqa: E402  поправки к правилам (№1: A6 наблюдательная)
from stats_core import (          # noqa: E402  чистая статистика без numpy/scipy
    friedman,
    holm,
    iman_davenport,
    normal_sf,
    z_vs_control,
)

TOOL_VERSION = "1.0.0"

# --- КОНСТАНТЫ ПРОТОКОЛА (все из замороженных правил, менять нельзя) -------

ALPHA = 0.05                      # §3.5
CONTROL_TRACK = "A7"              # §1 — манекен, случайный отбор
PORTFOLIO_TRACK = "A0"            # §1 — в тест НЕ входит, только §6
TOURNAMENT_TRACKS = ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8")
MIN_BLOCKS_TOKEN = 10             # §3.1 — порог переключения на запасной дизайн

# --- ДИЗАЙН БЛОКА ЗАФИКСИРОВАН ФЛАГОМ (С-01, решение навигатора 03.09.2026) ---
# §3.1 замороженных правил переключает дизайн ТОЛЬКО по числу полных блоков
# «блок = токен»: N < 10 -> запасной дизайн «блок = прогон». Замер 01.09.2026
# (STATE.md §8) дал N = 0 по токену, и это структурно: каждое сито берёт 5
# токенов из общего пруда, пересечение шести сит по одному токену — редкость.
# Дизайн «блок = прогон» ПОДТВЕРЖДЁН основным навигатором 03.09.2026 — ДО
# первых закрытий 14.09 (A0) / 21.09 (турнир), при нуле закрытых сделок.
# Флаг ниже ФИКСИРУЕТ дизайн: после первых закрытий он не пересчитывается по
# фактическому N, чтобы выбор дизайна нельзя было сделать по исходу. Оба N
# по-прежнему считаются и печатаются (§3.1 этого требует). Если бы N по токену
# оказался >= 10 (по замеру невозможно), расхождение печатается заглавными
# буквами и ложится в артефакт, но дизайн остаётся зафиксированным — решение
# о расхождении принимает пилот, а не код в день вердикта.
# Предрегистрацию флага проверяет машина (build_verdict, как у поправки №1):
# дата подтверждения сверяется с первым выходом закрытой сделки; флаг с датой
# не раньше первого выхода НЕ применяется — дизайн считается по §3.1, и это
# печатается заглавными буквами. Снять флаг = DESIGN_LOCK = None: это правка
# протокола, и делается она отдельным решением, а не попутно.
DESIGN_LOCK = {
    "design": "run",
    "confirmed_on": "2026-09-03",
    "confirmed_by": "навигатор (пакет СКРИНЕР-ДОВОДКА-03.09, задача С-01)",
    "before_first_closures": ["2026-09-14", "2026-09-21"],
    "basis": ("§3.1: полных блоков «блок = токен» к 15.11 — ноль по замеру "
              "01.09.2026 (STATE.md §8); N < 10 включает запасной дизайн"),
}

# Сентинел «взять флаг модуля»: analyze/projected_n читают DESIGN_LOCK в момент
# вызова, а не в момент определения функции — тест может подменить модульный
# флаг, и это будет видно.
_LOCK_DEFAULT = object()


def _lock_or_none(design_lock):
    """design_lock из аргумента: сентинел -> модульный DESIGN_LOCK; None/пусто
    -> None; словарь без ключа design или с неизвестным дизайном -> None."""
    lock = DESIGN_LOCK if design_lock is _LOCK_DEFAULT else design_lock
    if not lock or not isinstance(lock, dict):
        return None
    if lock.get("design") not in ("token", "run"):
        return None
    return lock


def design_lock_status(lock, first_exit=None):
    """Статус флага дизайна для артефакта: есть ли, предрегистрирован ли,
    применяется ли. Чистая функция, никогда не бросает.

    Предрегистрация: дата подтверждения СТРОГО раньше первого выхода закрытой
    сделки (любой дорожки, включая A0 — строже, чем нужно, намеренно). Нет
    закрытых — «до результатов». Без даты или с датой не раньше первого
    выхода флаг НЕ применяется, дизайн считается по §3.1.
    """
    out = {
        "present": bool(lock),
        "design": None,
        "confirmed_on": None,
        "confirmed_by": None,
        "before_first_closures": [],
        "basis": None,
        "first_exit_date": first_exit.isoformat() if first_exit else None,
        "preregistered": None,
        "applied": False,
        "note": "флага дизайна нет — дизайн по §3.1 (по числу полных блоков-токенов)",
    }
    if not lock:
        return out
    out["design"] = lock.get("design")
    out["confirmed_on"] = lock.get("confirmed_on")
    out["confirmed_by"] = lock.get("confirmed_by")
    out["before_first_closures"] = list(lock.get("before_first_closures") or [])
    out["basis"] = lock.get("basis")
    confirmed = _as_date(lock.get("confirmed_on"))
    if lock.get("design") not in ("token", "run"):
        out["preregistered"] = False
        out["note"] = "флаг нечитаем: design=%r — НЕ применён, дизайн по §3.1" % (lock.get("design"),)
        return out
    if confirmed is None:
        out["preregistered"] = False
        out["note"] = ("дата подтверждения отсутствует или не разобрана — флаг НЕ "
                       "применён, дизайн по §3.1")
        return out
    if first_exit is not None and confirmed >= first_exit:
        out["preregistered"] = False
        out["note"] = ("подтверждён %s, а первая закрытая сделка вышла %s — флаг после "
                       "результатов не предрегистрация, НЕ применён, дизайн по §3.1"
                       % (confirmed.isoformat(), first_exit.isoformat()))
        return out
    out["preregistered"] = True
    out["applied"] = True
    out["note"] = ("подтверждён %s, первая закрытая сделка %s — до результатов; дизайн "
                   "«блок = %s» зафиксирован и по фактическому N не пересчитывается"
                   % (confirmed.isoformat(),
                      first_exit.isoformat() if first_exit else "ещё не вышла",
                      "прогон" if lock.get("design") == "run" else "токен"))
    return out
# Справочное семейство, когда файла поправки НЕТ: прежняя строка «без A6»
# (вопрос С-02 очереди). С поправкой справочным становится, наоборот,
# семейство С наблюдательными дорожками.
LEGACY_REFERENCE_DROP = "A6"
VERDICT_DATE = _dt.date(2026, 11, 15)
LAST_RUN_DATE = _dt.date(2026, 11, 12)   # последний прогон (четверг) перед вердиктом
RUN_WEEKDAYS = (0, 3)             # прогоны пн/чт
HOLD_DAYS_RULE = 60               # закрытие на первом прогоне с возрастом >= 60 дней

# §6 — второй порог протокола денег
P6_EDGE_PP = 5.0                  # >= 5 п.п. среднего net PnL против A7 И A0
P6_MIN_CLOSED = 30                # >= 30 закрытых позиций

# C2 — мощность
POWER_THRESHOLD = 0.5             # объявлен явно, как требует задание
POWER_REFERENCE_EFFECT = 1.0      # эффект, по которому принимается «данных мало»
POWER_EFFECT_SIZES = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0)
POWER_SEED = 20261115
POWER_SIMS_DEFAULT = 2000         # для таблицы в отчёте (ключ --sims)
POWER_DECISION_SIMS = 800         # для решения «данных мало» — ФИКСИРОВАНО

# Три и только три допустимые строки вердикта (§5 + контракт пакета).
VERDICT_WINNER = "победитель %s (p=%s)"
VERDICT_NO_DIFF = "различий с случайным нет"
VERDICT_INSUFFICIENT = "данных мало: N=%d, мощность ниже порога"

ARTIFACT_LATEST = os.path.join("artifacts", "verdict_latest.json")


# =========================================================================
#  МЕЛКИЕ УТИЛИТЫ
# =========================================================================

def _setup_stdout():
    """Кириллица в консоли Windows — норма проекта, а не сюрприз."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _as_date(value):
    """date | 'YYYY-MM-DD' | None -> date | None. Мусор -> None, не исключение."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return _dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _mean(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / float(len(vals))


# Округление отклика ПЕРЕД ранжированием. Закрывает КРИТИЧЕСКИЙ дефект,
# воспроизведённый прогоном 01.09.2026 — не косметика и не страховка.
#
# ЧТО ЛОМАЛОСЬ. §3.2 замороженных правил требует: «Связки получают средний
# ранг». Связки ищутся строгим равенством (stats_core.rank_within_block), а
# отклик блока — среднее нескольких net. Дорожки закрывают в одном прогоне
# РАЗНОЕ ЧИСЛО позиций, и среднее одинаковых значений по разному числу
# слагаемых даёт разный float:
#
#     net = 7.36                     (одна сделка +10 % брутто, класс unknown)
#     sum([7.36] * 1) / 1  ->  7.36
#     sum([7.36] * 5) / 5  ->  7.360000000000001    <- на единицу в последнем бите
#
# ВОСПРОИЗВЕДЁННЫЙ ОТКАЗ. Семь дорожек, двенадцать прогонов, КАЖДАЯ закрытая
# позиция даёт ровно +10 % брутто, то есть все сита отработали ТОЖДЕСТВЕННО.
# Одна дорожка закрывает по 5 позиций в прогоне, остальные по одной. Ранги
# внутри блока становятся [4.5, 4.5, 4.5, 4.5, 4.5, 1.0, 4.5] вместо честных
# [4, 4, 4, 4, 4, 4, 4] — и так в КАЖДОМ блоке, потому что ошибка не
# случайная, а систематическая: её знак задан числом слагаемых.
# Итог: chi2_F = 27.0, F_ID = 6.600 (df 6 и 66), p = 1.7e-05, H0 отвергнута,
# Холм против A7 пройден, печатается «победитель <дорожка> (p<0.0001)».
# Ложная находка ровно того сорта, ради предотвращения которого и написан
# замороженный файл правил.
#
# ЭТО НЕ УМОЗРИТЕЛЬНО. В боевом tournament.json на 01.09.2026 распределение
# «сколько позиций дорожка закрывает в одном прогоне» = {1: 3, 2: 6, 3: 2,
# 4: 4, 5: 69} — разные длины сумм внутри одного блока это норма, а не
# исключение. И дизайн «блок = прогон», где это живёт, — именно тот, который
# применится 15.11 (полных блоков по токену ноль).
#
# ЧЕСТНАЯ ПОМЕТКА О ХОДЕ РАБОТЫ: сначала я проверил это неверно — перебором
# ПЕРЕСТАНОВОК одного и того же набора (одинаковая длина суммы) — и получил
# ноль расхождений на 500 000 проб, после чего ошибочно назвал дефект
# невоспроизводимым. Ошибка была в тесте: расхождение рождается не порядком
# слагаемых, а ИХ ЧИСЛОМ. Оставляю это здесь, чтобы следующий читатель не
# повторил ту же проверку и не сделал тот же вывод.
#
# ПОЧЕМУ 9 ЗНАКОВ НЕ МОГУТ СЛИТЬ РАЗНЫЕ РЕЗУЛЬТАТЫ. Исходный ret_pct скринер
# округляет до ОДНОГО знака (screener.py: round(..., 1)). Наименьшая
# осмысленная разница откликов — 0.1, делённая на размер блока; при потолке
# 60 позиций на дорожку это 0.0017, то есть в миллион раз больше шага
# округления. Девятый знак гасит мусор двоичного сложения и только его.
RESPONSE_ROUND_DIGITS = 9


def _median(values):
    """Медиана. При чётном числе наблюдений — среднее двух центральных."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _round_response(value):
    if value is None:
        return None
    return round(float(value), RESPONSE_ROUND_DIGITS)


def _fmt_p(p):
    """p-value для строки вердикта: без ложной точности и без нулей-обманок."""
    if p is None:
        return "—"
    if p < 1e-4:
        return "<0.0001"
    return "%.4f" % p


def _fmt_num(x, fmt="%.4f"):
    if x is None:
        return "—"
    if isinstance(x, float) and not math.isfinite(x):
        return "inf" if x > 0 else "-inf"
    return fmt % x


def _json_safe(obj):
    """inf/nan в JSON недопустимы по стандарту — заменяем на null.

    Флаг вырожденности при этом никуда не девается: iman_davenport кладёт
    отдельное поле 'degenerate', по нему читатель артефакта всё поймёт.
    """
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, _dt.date):
        return obj.isoformat()
    return obj


# =========================================================================
#  ЗАГРУЗКА СДЕЛОК
# =========================================================================

def load_trades(repo_dir=None, trades_override=None):
    """Единый список сделок через trades.load_all_trades (контракт пакета).

    Импорт ОТЛОЖЕННЫЙ: trades.py пишется параллельным исполнителем, и вердикт
    обязан запускаться и печатать честное «данных мало» даже до его появления.
    Молчать об этом нельзя — отсутствие модуля попадает и в печать, и в артефакт.

    -> (list[dict], info: dict)
    """
    if trades_override is not None:
        return list(trades_override), {
            "source": "override",
            "trades_module_available": True,
            "warning": None,
        }

    try:
        import trades as trades_mod       # noqa: WPS433  отложенный импорт по контракту
    except ModuleNotFoundError as exc:
        if getattr(exc, "name", "") != "trades":
            raise                          # чужой сломанный импорт прятать нельзя
        return [], {
            "source": "missing",
            "trades_module_available": False,
            "warning": (
                "модуль trades.py НЕ НАЙДЕН — вердикт посчитан на ПУСТОМ списке сделок. "
                "Это не «нет закрытых сделок», это «загрузчик ещё не поставлен»: "
                "сквозной прогон обязан быть перепроверен интегратором после появления trades.py"
            ),
        }

    data = trades_mod.load_all_trades(repo_dir if repo_dir is not None else HERE)
    return list(data or []), {
        "source": "trades.load_all_trades",
        "trades_module_available": True,
        "warning": None,
    }


def closed_only(all_trades):
    """§2: в вердикт идут ТОЛЬКО закрытые сделки. Открытые отбрасываются здесь
    и больше нигде не появляются — ни в одном числе отчёта."""
    out = []
    for t in all_trades:
        if str(t.get("status", "")).lower() != "closed":
            continue
        out.append(t)
    return out


# =========================================================================
#  ПОПРАВКА №1: НАБЛЮДАТЕЛЬНЫЕ ДОРОЖКИ И ОПИСАТЕЛЬНАЯ СВОДКА
# =========================================================================

def _observational_list(observational):
    """Нормализовать список наблюдательных дорожек из поправки.

    -> (список дорожек, пометки). Контроль A7 наблюдательным быть не может —
    иначе поправка могла бы убрать планку, против которой считается Холм;
    портфель A0 и мусор отбрасываются с пометкой, дубли схлопываются.
    """
    notes = []
    out = []
    for item in (observational or []):
        tr = str(item).strip().upper()
        if tr == CONTROL_TRACK:
            notes.append("поправка: контроль %s наблюдательным быть не может — "
                         "запись проигнорирована" % tr)
            continue
        if tr not in TOURNAMENT_TRACKS:
            notes.append("поправка: %r — не дорожка турнира, запись проигнорирована"
                         % (item,))
            continue
        if tr not in out:
            out.append(tr)
    return out, notes


def _quantile(sorted_vals, q):
    """Квантиль с линейной интерполяцией между порядковыми статистиками
    (тип 7 по Хайндману–Фану — то, что numpy считает по умолчанию)."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    pos = (n - 1) * float(q)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_vals[lo]) + (float(sorted_vals[hi]) - float(sorted_vals[lo])) * frac


def describe(values):
    """Описательная сводка ряда net-доходностей: n, среднее, медиана, Q1/Q3/IQR,
    доля прибыльных, сумма. Для наблюдательных дорожек (поправка №1) — и
    только для них: ни рангов, ни p-value, ни слова «победитель»."""
    vals = sorted(float(v) for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return {"n_closed": 0, "mean_net": None, "median_net": None,
                "q1_net": None, "q3_net": None, "iqr_net": None,
                "win_rate": None, "sum_net": None}
    q1 = _quantile(vals, 0.25)
    q3 = _quantile(vals, 0.75)
    return {
        "n_closed": n,
        "mean_net": sum(vals) / float(n),
        "median_net": _median(vals),
        "q1_net": q1,
        "q3_net": q3,
        "iqr_net": q3 - q1,
        "win_rate": sum(1 for v in vals if v > 0) / float(n),
        "sum_net": sum(vals),
    }


# =========================================================================
#  ОТКЛИК И БЛОКИ
# =========================================================================

def trade_net(trade, cfg, mult=1.0):
    """Отклик одной сделки: net ret_pct при заданном множителе издержек.

    gross берётся ИЗ ret_gross_pct (то есть из ret_pct закрытой позиции) и
    НЕ пересчитывается из цен — так велит контракт trades.py.
    """
    gross = trade.get("ret_gross_pct")
    if gross is None:
        return None
    return costs.net_ret_pct(
        gross,
        trade.get("liq_class"),
        trade.get("hold_days"),
        cfg,
        mult=mult,
    )


def _token_key(trade):
    key = trade.get("cg_id") or trade.get("sym")
    return str(key) if key is not None else None


def _run_key(trade):
    d = _as_date(trade.get("entry_date"))
    return d.isoformat() if d is not None else None


def build_blocks(closed, tracks, cfg, mult, key_func):
    """Собрать матрицу полных блоков.

    -> (block_keys: list[str], matrix: list[list[float]], partial_n: int)
    matrix[i][j] — отклик дорожки tracks[j] в блоке block_keys[i]
    (среднее net, если дорожка закрыла в этом блоке больше одной позиции).
    partial_n — сколько блоков отброшено как НЕПОЛНЫЕ (§3.1 требует полноты;
    это число обязано попасть в отчёт отдельной строкой).
    """
    bucket = {}
    for t in closed:
        tr = t.get("track")
        if tr not in tracks:
            continue
        key = key_func(t)
        if key is None:
            continue
        net = trade_net(t, cfg, mult=mult)
        if net is None:
            continue
        bucket.setdefault(key, {}).setdefault(tr, []).append(net)

    complete_keys = []
    partial_n = 0
    for key in sorted(bucket):
        if all(tr in bucket[key] for tr in tracks):
            complete_keys.append(key)
        else:
            partial_n += 1

    matrix = []
    for key in complete_keys:
        matrix.append([_round_response(_mean(bucket[key][tr])) for tr in tracks])
    return complete_keys, matrix, partial_n


# =========================================================================
#  C2 — МОЩНОСТЬ (считается заранее, от результатов не зависит)
# =========================================================================

_POWER_CACHE = {}


def power_estimate(k, N, effect_sizes, n_sims=2000, seed=20261115):
    """Грубая оценка мощности симуляцией на стдлибном random.

    Генерим N блоков по k откликов из N(0,1); дорожке с индексом 0 добавляем
    сдвиг effect (в единицах стандартного отклонения откликов); контроль —
    последняя дорожка (индекс k-1). Считаем долю прогонов, где Иман–Дэвенпорт
    отвергает H0 И дорожка 0 проходит Холма против контроля В НУЖНУЮ СТОРОНУ.

    -> {'by_effect': {effect: power}, 'n_sims':..., 'seed':..., 'k','N',
        'alpha','threshold','computable','note'}

    Оценка ГРУБАЯ и это сказано прямо: нормальные отклики — допущение,
    доходности закрытых сделок распределены не нормально (хвосты толще).
    Ранговый тест к форме распределения устойчив, но мощность к ней —
    чувствительна, поэтому число ниже читать как порядок величины.
    """
    k = int(k)
    N = int(N)
    n_sims = int(n_sims)
    effects = [float(e) for e in effect_sizes]

    out = {
        "by_effect": {},
        "n_sims": n_sims,
        "seed": int(seed),
        "k": k,
        "N": N,
        "alpha": ALPHA,
        "threshold": POWER_THRESHOLD,
        "control_index": (k - 1) if k >= 2 else None,
        "computable": True,
        "note": (
            "симуляция на нормальных откликах — грубая оценка порядка величины, "
            "не замер; порог мощности объявлен явно: %.2f" % POWER_THRESHOLD
        ),
    }

    if k < 2 or N < 2 or n_sims < 1:
        out["computable"] = False
        out["note"] = "мощность не определена: нужно k >= 2, N >= 2 и n_sims >= 1"
        for e in effects:
            out["by_effect"][e] = 0.0
        return out

    control = k - 1
    for eff in effects:
        cache_key = (k, N, eff, n_sims, int(seed))
        if cache_key in _POWER_CACHE:
            out["by_effect"][eff] = _POWER_CACHE[cache_key]
            continue

        rng = random.Random("%d|%.10g|%d|%d" % (int(seed), eff, k, N))
        hits = 0
        for _ in range(n_sims):
            blocks = []
            for _b in range(N):
                row = [rng.gauss(0.0, 1.0) for _ in range(k)]
                row[0] += eff
                blocks.append(row)
            fr = friedman(blocks)
            idv = iman_davenport(fr["chi2"], k, N)
            if idv["p"] >= ALPHA:
                continue
            mean_ranks = fr["mean_ranks"]
            pairs = []
            zmap = {}
            for j in range(k):
                if j == control:
                    continue
                z = z_vs_control(mean_ranks[j], mean_ranks[control], k, N)
                zmap[j] = z
                pairs.append((j, 2.0 * normal_sf(abs(z))))
            decided = holm(pairs, ALPHA, m=k - 1)
            for item in decided:
                if item["name"] == 0 and item["passed"] and zmap[0] < 0.0:
                    hits += 1
                    break
        power = hits / float(n_sims)
        _POWER_CACHE[cache_key] = power
        out["by_effect"][eff] = power

    return out


def _power_for_decision(k, N):
    """Мощность, по которой принимается «данных мало».

    Отдельный ФИКСИРОВАННЫЙ прогон (POWER_DECISION_SIMS/POWER_SEED), чтобы
    ключ --sims не мог сдвинуть вердикт. Кэш делает повторные вызовы даровыми.
    """
    if k < 2 or N < 2:
        return 0.0
    res = power_estimate(
        k, N, [POWER_REFERENCE_EFFECT],
        n_sims=POWER_DECISION_SIMS, seed=POWER_SEED,
    )
    return res["by_effect"].get(POWER_REFERENCE_EFFECT, 0.0)


# =========================================================================
#  C2 — ПРОЕКЦИЯ ЧИСЛА БЛОКОВ К 15.11
# =========================================================================

def run_dates(start, end):
    """Даты прогонов пн/чт в отрезке [start, end] включительно."""
    out = []
    d = start
    while d <= end:
        if d.weekday() in RUN_WEEKDAYS:
            out.append(d)
        d += _dt.timedelta(days=1)
    return out


def projected_n(as_of=None, repo_dir=None, observational=None, addendum_present=None,
                design_lock=None):
    """Сколько блоков будет к 15.11 (as_of=None -> сегодня).

    observational — наблюдательные дорожки поправки №1: из семейства (и из
    счёта полных блоков) исключаются, печатаются отдельной строкой; семейство
    С ними идёт справочной строкой `reference`. Без поправки справочной
    остаётся прежняя строка «без A6» (LEGACY_REFERENCE_DROP); поправка есть,
    но дорожек не названо — справочного семейства нет (reference=None), и
    подпись не выдаёт это за отсутствие файла (addendum_present).

    design_lock — флаг зафиксированного дизайна (С-01): если передан,
    `design_that_applies` берётся из флага, арифметика §3.1 остаётся в
    `design_by_rule`, расхождение — в `design_conflict`. По умолчанию None:
    проекция сама по себе — арифметика, флаг ей передаёт build_verdict.

    as_of — не только метка: позиции с входом ПОЗЖЕ as_of не учитываются
    (ревью 02.09: раньше дата была подписью, и тест на живом tournament.json
    зависел бы от того, что скринер откроет завтра).

    Считает по фактическим entry_date ОТКРЫТЫХ позиций tournament.json,
    прогонам пн/чт и правилу «закрытие на первом прогоне с возрастом >= 60
    дней»; последний прогон перед вердиктом — 12.11.2026.

    -> {'per_track_closed': {...}, 'blocks_token': N1, 'blocks_run': N2,
        'design_that_applies': 'token'|'run', ...}

    ЧЕСТНЫЕ ГРАНИЦЫ ПРОЕКЦИИ (печатаются вместе с числом):
      * закрытие по `miss >= 2` (две подряд пропущенные цены) НЕ моделируется —
        цен будущего у нас нет. Оно может только ДОБАВИТЬ закрытий и раньше,
        поэтому проекция — нижняя оценка по числу закрытых позиций;
      * позиции, которые дорожки НАБЕРУТ после as_of, не учитываются: дорожки
        A1-A5,A7 упёрлись в предохранитель 60 открытых и до первых закрытий
        (18.09) новых мест не имеют, а вход позже 12.09 не успевает дожить до
        60 дней к 12.11.
    """
    as_of = _as_date(as_of) or _dt.date.today()
    base = repo_dir if repo_dir is not None else HERE
    path = os.path.join(base, "tournament.json")

    info = {
        "as_of": as_of.isoformat(),
        "verdict_date": VERDICT_DATE.isoformat(),
        "last_run_date": LAST_RUN_DATE.isoformat(),
        "per_track_closed": {},
        "already_closed": {},
        "blocks_token": 0,
        "blocks_run": 0,
        "design_that_applies": "run",
        "design_by_rule": "run",
        "design_locked": False,
        "design_conflict": False,
        "participating": [],
        "excluded_empty": [],
        "observational": [],
        "reference": None,
        "source": path,
        "available": False,
        "assumptions": [
            "закрытие по miss>=2 не моделируется (цен будущего нет) — оценка нижняя",
            "новые позиции после as_of не учитываются (предохранитель 60 открытых)",
        ],
    }

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        info["error"] = "tournament.json не прочитан: %s" % exc
        return info

    tracks = data.get("tracks") or {}
    info["available"] = True

    # все прогоны от самого раннего входа до последнего прогона перед вердиктом
    entry_dates = []
    for tr in TOURNAMENT_TRACKS:
        node = tracks.get(tr) or {}
        for pos in list(node.get("open") or []) + list(node.get("closed") or []):
            d = _as_date(pos.get("entry_date"))
            if d is not None:
                entry_dates.append(d)
    if not entry_dates:
        return info
    all_runs = run_dates(min(entry_dates), LAST_RUN_DATE)

    def first_close_run(entry):
        for d in all_runs:
            if d >= entry and (d - entry).days >= HOLD_DAYS_RULE:
                return d
        return None

    token_sets = {}
    run_sets = {}
    for tr in TOURNAMENT_TRACKS:
        node = tracks.get(tr) or {}
        n_closed = 0

        for pos in list(node.get("closed") or []):
            entry = _as_date(pos.get("entry_date"))
            if entry is not None and entry > as_of:
                continue                      # вход позже as_of — не учитываем
            n_closed += 1
            key = pos.get("cg_id") or pos.get("sym")
            if key:
                token_sets.setdefault(str(key), set()).add(tr)
            rk = _as_date(pos.get("entry_date"))
            if rk:
                run_sets.setdefault(rk.isoformat(), set()).add(tr)
        info["already_closed"][tr] = len(list(node.get("closed") or []))

        for pos in list(node.get("open") or []):
            entry = _as_date(pos.get("entry_date"))
            if entry is None or entry > as_of:
                continue
            if first_close_run(entry) is None:
                continue                      # не доживёт до 60 дней к 12.11
            n_closed += 1
            key = pos.get("cg_id") or pos.get("sym")
            if key:
                token_sets.setdefault(str(key), set()).add(tr)
            run_sets.setdefault(entry.isoformat(), set()).add(tr)

        info["per_track_closed"][tr] = n_closed

    obs, _obs_notes = _observational_list(observational)
    nonempty = [tr for tr in TOURNAMENT_TRACKS if info["per_track_closed"].get(tr, 0) > 0]
    participating = [tr for tr in nonempty if tr not in obs]
    excluded = [tr for tr in TOURNAMENT_TRACKS
                if info["per_track_closed"].get(tr, 0) == 0 and tr not in obs]
    info["participating"] = participating
    info["excluded_empty"] = excluded
    info["observational"] = list(obs)

    def count_full(sets_map, need):
        need = set(need)
        if not need:
            return 0
        return sum(1 for v in sets_map.values() if need <= v)

    info["blocks_token"] = count_full(token_sets, participating)
    info["blocks_run"] = count_full(run_sets, participating)
    info["design_by_rule"] = "token" if info["blocks_token"] >= MIN_BLOCKS_TOKEN else "run"
    info["design_that_applies"] = info["design_by_rule"]
    lock = _lock_or_none(design_lock) if design_lock is not None else None
    if lock:
        info["design_that_applies"] = lock["design"]
        info["design_locked"] = True
        info["design_conflict"] = (lock["design"] != info["design_by_rule"])

    # Справочное семейство печатается рядом с головным всегда — разницу в
    # числе блоков честнее показать, чем спрятать. С поправкой справочным
    # становится семейство С наблюдательными (прежний счёт «семёркой»);
    # без поправки — прежняя строка «без A6» (у неё 22 открытых против 60
    # у прочих, она режет число полных блоков сильнее всех; вопрос С-02).
    if obs:
        ref_tracks = list(nonempty)
        ref_label = ("семейство с наблюдательными дорожками %s (прежний счёт, до поправки)"
                     % ", ".join(obs))
    elif addendum_present:
        info["reference"] = None              # поправка есть, дорожек не названо
        info["reference_note"] = ("поправка есть, наблюдательных дорожек не названо — "
                                  "справочного семейства нет")
        return info
    else:
        ref_tracks = [tr for tr in participating if tr != LEGACY_REFERENCE_DROP]
        ref_label = "семейство без %s (файла поправки нет, вопрос С-02)" % LEGACY_REFERENCE_DROP
    info["reference"] = {
        "label": ref_label,
        "tracks": ref_tracks,
        "blocks_token": count_full(token_sets, ref_tracks),
        "blocks_run": count_full(run_sets, ref_tracks),
    }
    return info


# =========================================================================
#  ГЛАВНЫЙ ТЕСТ + ВЕРДИКТ
# =========================================================================

def analyze(closed, cfg, mult=1.0, alpha=ALPHA, use_power_gate=True,
            observational=None, design_lock=_LOCK_DEFAULT):
    """Полный расчёт вердикта при ОДНОМ множителе издержек.

    Возвращает словарь со всеми числами; печатью не занимается.

    observational — дорожки, выведенные из подтверждающего семейства поправкой
    к правилам (поправка №1: A6). Они считаются ОПИСАТЕЛЬНО (describe) и в
    k/N/ранги не входят; None или пусто — поправки нет, семейство = все
    непустые дорожки, как до 02.09.2026. Список читает build_verdict из файла
    поправки; сюда он приходит уже как данные.

    design_lock — флаг зафиксированного дизайна (С-01, 03.09.2026): по
    умолчанию модульный DESIGN_LOCK; None — дизайн строго по арифметике §3.1
    (так проверяется сам порог). build_verdict передаёт флаг только если его
    предрегистрация подтверждена машиной.
    """
    res = {
        "mult": float(mult),
        "alpha": float(alpha),
        "control_track": CONTROL_TRACK,
        "notes": [],
    }

    # --- §1: A0 в тест не входит -----------------------------------------
    tourney = [t for t in closed if t.get("track") in TOURNAMENT_TRACKS]
    a0_n = sum(1 for t in closed if t.get("track") == PORTFOLIO_TRACK)
    res["a0_closed_n"] = a0_n
    res["notes"].append(
        "§1: A0 исключена из статистического теста по устройству протокола "
        "(другая дата входа), закрытых у A0: %d — она идёт только во второй порог §6" % a0_n
    )

    per_track = {}
    for tr in TOURNAMENT_TRACKS:
        per_track[tr] = sum(1 for t in tourney if t.get("track") == tr)
    res["per_track_closed"] = per_track

    # --- поправка №1: наблюдательные дорожки — вне семейства, но не молча ---
    obs, obs_notes = _observational_list(observational)
    res["observational_tracks"] = obs
    res["notes"].extend(obs_notes)
    if obs:
        res["notes"].append(
            "поправка №1: %s — наблюдательные дорожки, ВНЕ подтверждающего "
            "семейства; считаются описательно, в k/N/ранги не входят" % ", ".join(obs))

    # --- §3.1: исключение пустых дорожек ПОИМЁННО -------------------------
    excluded = []
    participating = []
    for tr in TOURNAMENT_TRACKS:
        if tr in obs:
            continue                      # описательный блок ниже, не исключение §3.1
        if per_track[tr] == 0:
            excluded.append({
                "track": tr,
                "reason": "нет ни одной закрытой позиции к дате вердикта (§3.1) — "
                          "исключение по факту пустоты, а не по результату",
            })
        else:
            participating.append(tr)
    res["excluded_tracks"] = excluded
    res["participating"] = participating
    res["k"] = len(participating)

    # Описательная сводка наблюдательных дорожек — всегда, когда дорожка
    # названа поправкой, даже при нуле закрытых (тогда n=0 и прочерки).
    res["observational"] = {}
    for tr in obs:
        vals = [v for v in (trade_net(t, cfg, mult=mult)
                            for t in tourney if t.get("track") == tr) if v is not None]
        row = describe(vals)
        row["note"] = ("вне теста (поправка №1): описательно, в k/N/ранги не входит; "
                       "ни p-value, ни победителя")
        res["observational"][tr] = row

    # доля сделок, у которых класс ликвидности неизвестен (считается по худшему)
    res["liq_class_fallback_n"] = sum(
        1 for t in tourney if costs.is_fallback_class(t.get("liq_class"))
    )

    # --- §3.1: оба N считаются ВСЕГДА и печатаются оба ---------------------
    keys_token, matrix_token, partial_token = build_blocks(
        tourney, participating, cfg, mult, _token_key)
    keys_run, matrix_run, partial_run = build_blocks(
        tourney, participating, cfg, mult, _run_key)
    res["n_blocks_token"] = len(keys_token)
    res["n_blocks_run"] = len(keys_run)
    res["partial_blocks_token"] = partial_token
    res["partial_blocks_run"] = partial_run

    # §3.1 — арифметика: дизайн по числу полных блоков-токенов.
    if len(keys_token) >= MIN_BLOCKS_TOKEN:
        design_by_rule = "token"
        rule_reason = (
            "полных блоков-токенов N=%d >= %d — работает ГЛАВНЫЙ дизайн §3.1"
            % (len(keys_token), MIN_BLOCKS_TOKEN)
        )
    else:
        design_by_rule = "run"
        rule_reason = (
            "полных блоков-токенов N=%d < %d — ПЕРЕКЛЮЧЕНИЕ на ЗАПАСНОЙ дизайн "
            "«блок = прогон» (§3.1, зафиксирован заранее; переключение зависит "
            "ТОЛЬКО от N, ранги на этот момент не считались)"
            % (len(keys_token), MIN_BLOCKS_TOKEN)
        )
    res["design_by_rule"] = design_by_rule
    res["design_conflict_line"] = None
    # С-01 (03.09.2026): флаг фиксирует дизайн и после первых закрытий по
    # фактическому N не пересчитывается. Оба N уже посчитаны выше и печатаются.
    lock = _lock_or_none(design_lock)
    if lock:
        design = lock["design"]
        res["design_locked"] = True
        res["design_lock"] = dict(lock)
        res["design_conflict"] = (design != design_by_rule)
        res["design_reason"] = (
            "дизайн ЗАФИКСИРОВАН флагом %s (%s): «блок = %s», после первых закрытий "
            "по фактическому N не пересчитывается; по §3.1 сейчас: %s"
            % (lock.get("confirmed_on"), lock.get("confirmed_by"),
               "прогон" if design == "run" else "токен", rule_reason)
        )
        if res["design_conflict"]:
            res["design_conflict_line"] = (
                "ВНИМАНИЕ: ДИЗАЙН ПО §3.1 («блок = %s») РАСХОДИТСЯ С ЗАФИКСИРОВАННЫМ "
                "ФЛАГОМ («блок = %s») — ДЕЙСТВУЕТ ФЛАГ, РАСХОЖДЕНИЕ ТРЕБУЕТ РЕШЕНИЯ ПИЛОТА"
                % ("прогон" if design_by_rule == "run" else "токен",
                   "прогон" if design == "run" else "токен")
            )
    else:
        design = design_by_rule
        res["design_locked"] = False
        res["design_lock"] = None
        res["design_conflict"] = False
        res["design_reason"] = rule_reason
    if design == "token":
        keys, matrix = keys_token, matrix_token
    else:
        keys, matrix = keys_run, matrix_run
    res["design_switched"] = (design == "run")
    res["design"] = design
    res["N"] = len(keys)
    res["block_keys"] = list(keys)

    # Средний И МЕДИАННЫЙ net по дорожке (справочно, в тест идут ранги).
    # Медиана обязательна рядом со средним: пакет A2 прямо запрещает
    # публиковать среднее в одиночку. Причина не стилистическая —
    # 60-дневные доходности микрокапов скошены, и одна ракета делает
    # среднее непохожим на типичный исход дорожки.
    mean_net, median_net, n_closed_by_track = {}, {}, {}
    for tr in participating:
        vals = [v for v in (trade_net(t, cfg, mult=mult)
                            for t in tourney if t.get("track") == tr) if v is not None]
        mean_net[tr] = _mean(vals)
        median_net[tr] = _median(vals)
        n_closed_by_track[tr] = len(vals)
    res["mean_net_by_track"] = mean_net
    res["median_net_by_track"] = median_net
    res["n_closed_by_track"] = n_closed_by_track

    k = res["k"]
    N = res["N"]

    # --- предварительные условия применимости -----------------------------
    blockers = []
    if k < 2:
        blockers.append("участвующих сит меньше двух (k=%d)" % k)
    if CONTROL_TRACK not in participating:
        blockers.append(
            "контроль %s без закрытых позиций — сравнивать не с чем "
            "(§1: контрольный метод A7 и только A7)" % CONTROL_TRACK)
    if N < 2:
        blockers.append("полных блоков меньше двух (N=%d) — статистика не определена" % N)

    res["power_reference_effect"] = POWER_REFERENCE_EFFECT
    res["power_threshold"] = POWER_THRESHOLD

    if blockers:
        res["blockers"] = blockers
        res["friedman"] = None
        res["iman_davenport"] = None
        res["mean_ranks"] = {}
        res["posthoc"] = None
        res["power_reference"] = 0.0
        res["verdict_kind"] = "insufficient"
        res["winner"] = None
        res["winner_p"] = None
        res["verdict_line"] = VERDICT_INSUFFICIENT % N
        res["verdict_reason"] = "; ".join(blockers)
        return res

    res["blockers"] = []

    # --- §3.3 / §3.4 -------------------------------------------------------
    fr = friedman(matrix)
    idv = iman_davenport(fr["chi2"], k, N)
    res["friedman"] = {
        "chi2": fr["chi2"], "df": fr["df"], "p": fr["p"],
        "role": "КОНТРОЛЬНЫЙ (§3.3) — вердикт по нему не выносится",
    }
    res["iman_davenport"] = {
        "F": idv["F"], "df1": idv["df1"], "df2": idv["df2"], "p": idv["p"],
        "degenerate": idv["degenerate"],
        "role": "ГЛАВНЫЙ (§3.4) — вердикт выносится по нему",
    }
    res["mean_ranks"] = {tr: fr["mean_ranks"][j] for j, tr in enumerate(participating)}

    rejected_main = idv["p"] < alpha
    rejected_ctrl = fr["p"] < alpha
    res["h0_rejected"] = rejected_main
    res["h0_rejected_friedman"] = rejected_ctrl
    res["tests_agree"] = (rejected_main == rejected_ctrl)
    if not res["tests_agree"]:
        res["disagreement_line"] = (
            "РАСХОЖДЕНИЕ ТЕСТОВ (§3.4): Иман–Дэвенпорт p=%s -> H0 %s, "
            "Фридман p=%s -> H0 %s. Вердикт выносится по Иману–Дэвенпорту; "
            "само расхождение является результатом и печатается отдельной строкой."
            % (_fmt_p(idv["p"]), "отвергнута" if rejected_main else "не отвергнута",
               _fmt_p(fr["p"]), "отвергнута" if rejected_ctrl else "не отвергнута")
        )
    else:
        res["disagreement_line"] = None

    # --- §4: пост-хок Холма ТОЛЬКО против A7 -------------------------------
    posthoc = None
    winner = None
    winner_p = None
    if rejected_main:
        ci = participating.index(CONTROL_TRACK)
        rc = fr["mean_ranks"][ci]
        pairs = []
        detail = {}
        for j, tr in enumerate(participating):
            if tr == CONTROL_TRACK:
                continue
            z = z_vs_control(fr["mean_ranks"][j], rc, k, N)
            p_two = 2.0 * normal_sf(abs(z))
            pairs.append((tr, p_two))
            detail[tr] = {"z": z, "p": p_two, "mean_rank": fr["mean_ranks"][j]}
        decided = holm(pairs, alpha, m=k - 1)
        posthoc = []
        for item in decided:
            tr = item["name"]
            d = detail[tr]
            better = d["z"] < 0.0        # ранг 1 = лучший -> у лучшего z < 0
            posthoc.append({
                "track": tr,
                "mean_rank": d["mean_rank"],
                "control_mean_rank": rc,
                "z": d["z"],
                "p_two_sided": d["p"],
                "alpha_i": item["alpha_i"],
                "passed_holm": item["passed"],
                "better_than_control": better,
                "is_winner": bool(item["passed"] and better),
            })
        wins = [row for row in posthoc if row["is_winner"]]
        if wins:
            wins.sort(key=lambda r: (r["p_two_sided"], r["track"]))
            winner = wins[0]["track"]
            winner_p = wins[0]["p_two_sided"]
    res["posthoc"] = posthoc
    res["posthoc_note"] = (
        "§4: k-1 пар, каждое сито ПРОТИВ %s; пары сит между собой НЕ считаются. "
        "p двустороннее (2*normal_sf(|z|)); победителем объявляется только сито, "
        "которое и прошло Холма, И имеет средний ранг ЛУЧШЕ контроля."
        % CONTROL_TRACK
    )

    # --- C2: мощность (функция только k и N, от результатов не зависит) ----
    power_ref = _power_for_decision(k, N) if use_power_gate else None
    res["power_reference"] = power_ref

    # --- §5: вердикт РОВНО одной из трёх строк -----------------------------
    if winner is not None:
        res["verdict_kind"] = "winner"
        res["winner"] = winner
        res["winner_p"] = winner_p
        res["verdict_line"] = VERDICT_WINNER % (winner, _fmt_p(winner_p))
        res["verdict_reason"] = (
            "§5 ветка 2: H0 отвергнута главным тестом (p=%s) и %s прошла Холма "
            "против %s в нужную сторону" % (_fmt_p(idv["p"]), winner, CONTROL_TRACK)
        )
    else:
        res["verdict_kind"] = "no_diff"
        res["winner"] = None
        res["winner_p"] = None
        res["verdict_line"] = VERDICT_NO_DIFF
        if rejected_main:
            res["verdict_reason"] = (
                "§5 ветка 2 без победителя: общее различие есть (p=%s), но Холма "
                "против %s не прошло ни одно сито в нужную сторону — преимущества "
                "над случайным нет" % (_fmt_p(idv["p"]), CONTROL_TRACK)
            )
        else:
            res["verdict_reason"] = (
                "§5 ветка 1: H0 не отвергнута (p=%s). Дорожка с лучшим средним "
                "рангом победителем НЕ объявляется — это шум."
                % _fmt_p(idv["p"])
            )

    # --- ОГОВОРКА О МОЩНОСТИ (пункт C2 пакета) -----------------------------
    # ПОЧЕМУ ЭТО ОГОВОРКА, А НЕ ТРЕТЬЯ ВЕТКА ВЕРДИКТА.
    # Пакет просит печатать одну из ТРЁХ строк, среди них «данных мало».
    # Замороженные правила §5 знают только ДВЕ ветки и при p >= 0.05
    # предписывают ровно «ни одно сито не лучше случайного отбора». §8 тех же
    # правил запрещает править их на месте: любое изменение — новый файл v2,
    # с датой и подтверждением, что оно сделано ДО просмотра результатов.
    # Поэтому третья строка НЕ ЗАМЕЩАЕТ вердикт (это была бы тихая правка
    # замороженного протокола), а печатается ОБЯЗАТЕЛЬНОЙ строкой рядом.
    # Так выполнено и требование пакета (строка есть), и §5 (вердикт его).
    # Условие оговорки — только k, N и факт неотвержения: результатами она
    # не управляется и победителя не производит.
    res["insufficient_data"] = bool(
        (not rejected_main) and use_power_gate and power_ref is not None
        and power_ref < POWER_THRESHOLD
    )
    res["insufficient_data_line"] = (
        (VERDICT_INSUFFICIENT % N) if res["insufficient_data"] else None)
    res["insufficient_data_reason"] = (
        ("мощность при k=%d и N=%d против эффекта %.2f сигмы = %.2f < порога "
         "%.2f: на таком N «эффекта нет» и «данных не хватило» неразличимы"
         % (k, N, POWER_REFERENCE_EFFECT, power_ref, POWER_THRESHOLD))
        if res["insufficient_data"] else None
    )

    # §5 ветка 1: лучший средний ранг называется, но победителем не считается
    best_track = min(res["mean_ranks"], key=lambda tr: res["mean_ranks"][tr])
    res["best_mean_rank_track"] = best_track
    res["best_mean_rank_note"] = (
        "лучший средний ранг при неотвергнутой H0, то есть шум"
        if not rejected_main else "лучший средний ранг"
    )
    return res


# =========================================================================
#  §7 — ТЕНЕВЫЕ СТОПЫ (счёт ОТДЕЛЬНЫЙ, на правила сит не влияет)
# =========================================================================

def _sign_test_two_sided(n_better, n_worse):
    """Точный двусторонний знаковый тест на равных шансах (p = 0.5).

    Ничьи выброшены ДО вызова — так и определён знаковый тест. Считается точно
    через math.comb: нормальное приближение на выборке в десятки наблюдений
    врёт именно там, где нам важно.
    Возвращает (n_effective, p) либо (0, None), если сравнивать нечего.
    """
    n = int(n_better) + int(n_worse)
    if n <= 0:
        return 0, None
    k = min(int(n_better), int(n_worse))
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / float(2 ** n)
    return n, min(1.0, 2.0 * tail)


def paragraph7(closed, cfg, mult=1.0, repo_dir=None):
    """§7 замороженных правил: даёт ли выход при −20 % от пика результат лучше
    пассивного удержания 60 дней.

    ЧТО ЭТО И ЧЕГО ЭТО НЕ ЕСТЬ. §7 сформулирован в правилах прямо: «Считается
    на тех же закрытых позициях, ОТДЕЛЬНЫМ счётом, на правила турнира сит не
    влияет». Поэтому здесь нет ни рангов, ни Фридмана, ни Холма: это парное
    сравнение двух правил выхода на ОДНИХ И ТЕХ ЖЕ сделках, а не соревнование
    сит.

    ГРАНИЦА ДАННЫХ, которую надо называть каждый раз. Теневые стопы пишет
    только основной портфель A0 (screener.py, _shadow_check): у дорожек
    турнира полей shadow нет вовсе. Значит §7 считается на A0 и только на A0 —
    это не выбор, а наличие данных. Читается напрямую из positions.json,
    потому что trades.py по контракту теневые поля не переносит.

    ВТОРАЯ ГРАНИЦА, названная ещё в самом screener.py: срабатывание стопа
    проверяется с частотой ПРОГОНОВ (пн/чт), а не ежедневно. Это грубее
    дневной симуляции — зато ровно так пилот и мог бы исполнять правило
    руками. Результат §7 читать как «правило, исполняемое дважды в неделю».

    ИЗДЕРЖКИ: к обеим веткам применяется ОДНА И ТА ЖЕ модель круга. Стоп —
    такой же вход и выход, он не бесплатен; сравнивать его брутто с пассивом
    нетто было бы подтасовкой.
    """
    out = {
        "mult": float(mult),
        "source": "positions.json, дорожка A0 (единственная с теневыми стопами)",
        "check_frequency_note": ("срабатывание стопа проверяется с частотой "
                                 "прогонов пн/чт, а не ежедневно — правило "
                                 "грубее дневной симуляции"),
        "available": False,
        "n": 0,
        "rules": {},
        "paired_vs_passive": {},
        "verdict_line": None,
        "note": None,
    }
    try:
        with open(os.path.join(repo_dir or HERE, "positions.json"), encoding="utf-8") as f:
            led = json.load(f)
    except Exception as exc:
        out["note"] = "positions.json недоступен (%s) — §7 не считается" % type(exc).__name__
        return out

    # Класс ликвидности берём из уже загруженных сделок (trades.py считает
    # его по объёму из candidates_log). Джойн по (тикер, дата входа):
    # в positions.json поля liq_class нет, а считать его тут заново значило
    # бы завести второй источник истины для одной и той же величины.
    # Не нашлось — None, и costs трактует это как самый дорогой класс.
    liq_by_key = {}
    for t in (closed or []):
        if t.get("track") == "A0":
            liq_by_key[(t.get("sym"), str(t.get("entry_date"))[:10])] = t.get("liq_class")

    rows = []
    for pos in (led.get("closed") or []):
        passive = pos.get("ret_pct")
        t20 = pos.get("shadow_t20_ret")
        t30 = pos.get("shadow_t30_ret")
        if passive is None or t20 is None:
            continue
        liq = liq_by_key.get((pos.get("sym"), str(pos.get("entry_date"))[:10]))
        hold = pos.get("age_days") or 60
        rows.append({
            "sym": pos.get("sym"),
            "passive": costs.net_ret_pct(passive, liq, hold, cfg, mult=mult),
            "stop20": costs.net_ret_pct(t20, liq, hold, cfg, mult=mult),
            "stop30": (costs.net_ret_pct(t30, liq, hold, cfg, mult=mult)
                       if t30 is not None else None),
        })

    if not rows:
        out["note"] = ("у A0 нет закрытых позиций с теневыми полями — §7 не "
                       "считается. Первые закрытия A0 ожидаются 14.09.2026.")
        return out

    out["available"] = True
    out["n"] = len(rows)
    for name in ("passive", "stop20", "stop30"):
        vals = [r[name] for r in rows if r.get(name) is not None]
        out["rules"][name] = {
            "n": len(vals),
            "mean_net_pct": _mean(vals),
            "median_net_pct": _median(vals),
        }

    for name in ("stop20", "stop30"):
        diffs = [(r[name] - r["passive"]) for r in rows if r.get(name) is not None]
        better = sum(1 for d in diffs if d > 0)
        worse = sum(1 for d in diffs if d < 0)
        ties = sum(1 for d in diffs if d == 0)
        n_eff, pval = _sign_test_two_sided(better, worse)
        out["paired_vs_passive"][name] = {
            "n_pairs": len(diffs),
            "mean_diff_pp": _mean(diffs),
            "median_diff_pp": _median(diffs),
            "n_better": better, "n_worse": worse, "n_equal": ties,
            "sign_test_n": n_eff, "sign_test_p": pval,
            "significant_at_005": (pval is not None and pval < ALPHA),
        }

    d20 = out["paired_vs_passive"].get("stop20") or {}
    med = d20.get("median_diff_pp") or 0.0
    if d20.get("sign_test_p") is None:
        out["verdict_line"] = ("стоп -20%% и пассив не различаются ни в одной "
                               "сделке (n=%d) — сравнивать нечего" % out["n"])
    elif d20.get("significant_at_005") and med > 0:
        out["verdict_line"] = ("стоп -20%% лучше пассивного удержания (медиана "
                               "разницы %+.2f п.п., знаковый тест p=%s, n=%d)"
                               % (med, _fmt_p(d20["sign_test_p"]), d20["sign_test_n"]))
    elif d20.get("significant_at_005"):
        out["verdict_line"] = ("стоп -20%% ХУЖЕ пассивного удержания (медиана "
                               "разницы %+.2f п.п., знаковый тест p=%s, n=%d)"
                               % (med, _fmt_p(d20["sign_test_p"]), d20["sign_test_n"]))
    else:
        out["verdict_line"] = ("различие стопа -20%% и пассива статистически не "
                               "показано (знаковый тест p=%s, n=%d)"
                               % (_fmt_p(d20["sign_test_p"]), d20["sign_test_n"]))
    return out


# =========================================================================
#  §6 — ВТОРОЙ ПОРОГ ПРОТОКОЛА ДЕНЕГ (считается ОТДЕЛЬНО)
# =========================================================================

def paragraph6(closed, cfg, mult=1.0, as_of=None, observational=None):
    """§6: побить И A7, И A0 на >= 5 п.п. среднего net PnL, при >= 30 закрытых,
    лидируя в ОБЕИХ половинах срока.

    Считается ОТДЕЛЬНО от §3-§5 и печатается рядом: деньги разрешены только
    при выполнении И §5-ветка-2 с победителем, И §6.

    ИНТЕРПРЕТАЦИЯ, названная прямо (в правилах §6 сформулирован словами):
      * «половины срока» режутся по ДАТЕ ВЫХОДА (exit_date) закрытых сделок
        ДОРОЖЕК ТУРНИРА, по середине фактического диапазона их выходов;
        A0 в определении границы не участвует (он входил с другой даты), но
        измеряется в тех же половинах. Альтернатива — резать по всему сроку
        турнира с 20.07 — даёт заведомо пустую первую половину (первые
        закрытия только 21.09.2026) и превращает условие в невыполнимое при
        любом качестве сита. Выбранная база записана полем halves_basis.
      * «лидируя» = средний net дорожки в этой половине строго выше и A7, и A0
        в той же половине.
    """
    out = {
        "mult": float(mult),
        "edge_pp": P6_EDGE_PP,
        "min_closed": P6_MIN_CLOSED,
        # Значение по умолчанию, а не None: на пустом входе функция выходит
        # раньше расчёта границы, и печать "база: None" выглядела бы сбоем,
        # хотя это просто отсутствие данных.
        "halves_basis": "не определена: закрытых сделок нет",
        "control_track": CONTROL_TRACK,
        "portfolio_track": PORTFOLIO_TRACK,
        "computable": False,
        "per_track": {},
        "passed_tracks": [],
        # Поправка №1: наблюдательная дорожка §6 не проходит никогда — она
        # вне теста, а §6 — второй порог ДЛЯ ПОБЕДИТЕЛЯ теста. Строка её
        # считается и печатается (описательно), флаг passed = False.
        "observational_tracks": _observational_list(observational)[0],
        "note": None,
    }
    obs_set = set(out["observational_tracks"])

    nets = {}
    exits = {}
    for t in closed:
        tr = t.get("track")
        if tr is None:
            continue
        net = trade_net(t, cfg, mult=mult)
        if net is None:
            continue
        nets.setdefault(tr, []).append(net)
        exits.setdefault(tr, []).append((_as_date(t.get("exit_date")), net))

    if not nets:
        out["note"] = "закрытых сделок нет — §6 не считается"
        return out

    # Половины режутся по выходам ДОРОЖЕК ТУРНИРА, а A0 из определения границы
    # исключён намеренно (находка ревизии 01.09.2026).
    # Причина фактическая: A0 входила с 14.07, дорожки — с 20.07, и первые
    # закрытия у них разные (A0 — 14.09.2026, турнир — 21.09.2026). Если брать
    # минимум по ВСЕМ выходам, включая A0, то одна ранняя закрытая позиция
    # портфеля сдвигает середину назад, и часть дорожек оказывается в первой
    # половине без единого выхода — «лидерство в обеих половинах» становится
    # невыполнимым по календарю, а не по качеству сита.
    # A0 при этом ОСТАЁТСЯ порогом: его средние считаются в тех же половинах,
    # просто границу задаёт не он.
    tournament_exit_dates = [d for tr, rows in exits.items() if tr != PORTFOLIO_TRACK
                             for (d, _n) in rows if d is not None]
    all_exit_dates = tournament_exit_dates or [
        d for rows in exits.values() for (d, _n) in rows if d is not None]
    out["halves_basis"] = (
        "exit_date дорожек турнира (A0 в определении границы НЕ участвует, "
        "но измеряется в тех же половинах), середина фактического диапазона выходов"
        if tournament_exit_dates else
        "exit_date всех закрытых (дорожек турнира с выходами нет — граница взята по A0)"
    )
    if all_exit_dates:
        d0, d1 = min(all_exit_dates), max(all_exit_dates)
        mid = d0 + _dt.timedelta(days=(d1 - d0).days // 2)
        out["half1"] = [d0.isoformat(), mid.isoformat()]
        out["half2"] = [(mid + _dt.timedelta(days=1)).isoformat(), d1.isoformat()]
    else:
        mid = None
        out["half1"] = None
        out["half2"] = None
        out["note"] = "у закрытых сделок нет дат выхода — половины срока не режутся"

    def half_mean(track, which):
        rows = exits.get(track) or []
        if mid is None:
            return None
        if which == 1:
            vals = [n for (d, n) in rows if d is not None and d <= mid]
        else:
            vals = [n for (d, n) in rows if d is not None and d > mid]
        return _mean(vals)

    ctrl_mean = _mean(nets.get(CONTROL_TRACK) or [])
    port_mean = _mean(nets.get(PORTFOLIO_TRACK) or [])
    out["control_mean_net"] = ctrl_mean
    out["portfolio_mean_net"] = port_mean
    out["computable"] = ctrl_mean is not None and port_mean is not None
    if not out["computable"]:
        missing = []
        if ctrl_mean is None:
            missing.append(CONTROL_TRACK)
        if port_mean is None:
            missing.append(PORTFOLIO_TRACK)
        out["note"] = ("нет закрытых сделок у %s — второй порог не с чем сравнивать"
                       % ", ".join(missing))

    ctrl_h = {1: half_mean(CONTROL_TRACK, 1), 2: half_mean(CONTROL_TRACK, 2)}
    port_h = {1: half_mean(PORTFOLIO_TRACK, 1), 2: half_mean(PORTFOLIO_TRACK, 2)}

    for tr in TOURNAMENT_TRACKS:
        vals = nets.get(tr) or []
        n_closed = len(vals)
        m = _mean(vals)
        row = {
            "n_closed": n_closed,
            "mean_net": m,
            "delta_vs_control_pp": (m - ctrl_mean) if (m is not None and ctrl_mean is not None) else None,
            "delta_vs_portfolio_pp": (m - port_mean) if (m is not None and port_mean is not None) else None,
            "half1_mean": half_mean(tr, 1),
            "half2_mean": half_mean(tr, 2),
        }
        checks = {}
        checks["n_closed_ge_30"] = n_closed >= P6_MIN_CLOSED
        checks["beats_control_by_5pp"] = (
            row["delta_vs_control_pp"] is not None and row["delta_vs_control_pp"] >= P6_EDGE_PP)
        checks["beats_portfolio_by_5pp"] = (
            row["delta_vs_portfolio_pp"] is not None and row["delta_vs_portfolio_pp"] >= P6_EDGE_PP)
        lead = []
        for hi, own in ((1, row["half1_mean"]), (2, row["half2_mean"])):
            if own is None or ctrl_h[hi] is None or port_h[hi] is None:
                lead.append(False)
            else:
                lead.append(own > ctrl_h[hi] and own > port_h[hi])
        checks["leads_half1"] = lead[0]
        checks["leads_half2"] = lead[1]
        row["checks"] = checks
        row["observational"] = tr in obs_set
        row["passed"] = bool(tr != CONTROL_TRACK and tr != PORTFOLIO_TRACK
                             and tr not in obs_set and all(checks.values()))
        out["per_track"][tr] = row
        if row["passed"]:
            out["passed_tracks"].append(tr)

    out["control_halves"] = {"half1": ctrl_h[1], "half2": ctrl_h[2]}
    out["portfolio_halves"] = {"half1": port_h[1], "half2": port_h[2]}
    return out


# =========================================================================
#  СБОРКА ВЕРДИКТА ЦЕЛИКОМ
# =========================================================================

def build_verdict(repo_dir=None, as_of=None, n_sims=POWER_SIMS_DEFAULT,
                  trades_override=None, cfg=None):
    """Полный расчёт: главный множитель 1.0 + таблица чувствительности + §6 +
    мощность + проекция. Возвращает словарь-артефакт."""
    as_of_date = _as_date(as_of) or _dt.date.today()
    cfg = cfg if cfg is not None else costs.load_costs()

    all_trades, load_info = load_trades(repo_dir=repo_dir, trades_override=trades_override)
    closed = closed_only(all_trades)

    # Поправка №1 читается ИЗ ФАЙЛА репозитория, не из констант: нет файла —
    # семейство прежнее, и это печатается вместе с причиной.
    addendum = rules_addendum.load_addendum(
        repo_dir=repo_dir if repo_dir is not None else HERE)
    # ПРЕДРЕГИСТРАЦИЯ ПРОВЕРЯЕТСЯ МАШИНОЙ (ревью 02.09): поправка применяется
    # только если принята РАНЬШЕ первой закрытой сделки (любой дорожки, включая
    # A0 — строже, чем нужно, и это намеренно). Поправка без даты или с датой
    # не раньше первого выхода — не предрегистрация: семейство остаётся
    # полным, а расхождение печатается заглавными буквами и ложится в артефакт.
    first_exit = min([d for d in (_as_date(t.get("exit_date")) for t in closed)
                      if d is not None] or [None]) if closed else None
    declared = (list(addendum.get("observational_tracks") or [])
                if addendum.get("available") else [])
    adopted_date = _as_date(addendum.get("adopted_date")) if addendum.get("available") else None
    preregistered = None
    prereg_note = None
    if addendum.get("available"):
        if adopted_date is None:
            preregistered = False
            prereg_note = "дата принятия отсутствует или не разобрана"
        elif first_exit is not None and adopted_date >= first_exit:
            preregistered = False
            prereg_note = ("принята %s, а первая закрытая сделка вышла %s — поправка "
                           "после результатов не предрегистрация"
                           % (adopted_date.isoformat(), first_exit.isoformat()))
        else:
            preregistered = True
            prereg_note = ("принята %s, первая закрытая сделка %s — до результатов"
                           % (adopted_date.isoformat(),
                              first_exit.isoformat() if first_exit else "ещё не вышла"))
    observational = list(declared) if preregistered else []

    # С-01 (03.09.2026): флаг дизайна проверяется той же машиной, что и
    # поправка — дата подтверждения против первого выхода закрытой сделки.
    lock_status = design_lock_status(DESIGN_LOCK, first_exit)
    lock_for_analysis = DESIGN_LOCK if lock_status["applied"] else None

    mults = [float(m) for m in cfg["sensitivity_multipliers"]]
    head_mult = 1.0 if 1.0 in mults else min(mults, key=lambda m: abs(m - 1.0))

    head = analyze(closed, cfg, mult=head_mult, observational=observational,
                   design_lock=lock_for_analysis)
    head_p6 = paragraph6(closed, cfg, mult=head_mult, as_of=as_of_date,
                         observational=observational)
    head_p7 = paragraph7(closed, cfg, mult=head_mult, repo_dir=repo_dir)

    sensitivity = []
    for m in mults:
        a = head if m == head_mult else analyze(closed, cfg, mult=m,
                                                observational=observational,
                                                design_lock=lock_for_analysis)
        p6 = head_p6 if m == head_mult else paragraph6(closed, cfg, mult=m, as_of=as_of_date,
                                                       observational=observational)
        sensitivity.append({
            "mult": m,
            "verdict_line": a["verdict_line"],
            "verdict_kind": a["verdict_kind"],
            "insufficient_data": a.get("insufficient_data"),
            "winner": a["winner"],
            "p_main": (a["iman_davenport"] or {}).get("p"),
            "N": a["N"],
            "design": a["design"],
            "p6_passed_tracks": list(p6["passed_tracks"]),
            "mean_net_by_track": a["mean_net_by_track"],
            "median_net_by_track": a.get("median_net_by_track"),
        })

    signature = set((row["verdict_kind"], row["winner"], row.get("insufficient_data"))
                    for row in sensitivity)
    verdict_stable = len(signature) <= 1
    sensitivity_alarm = None
    if not verdict_stable:
        sensitivity_alarm = (
            "ВНИМАНИЕ: ВЕРДИКТ МЕНЯЕТСЯ ОТ МНОЖИТЕЛЯ ИЗДЕРЖЕК — "
            "РЕШАЮТ ИЗДЕРЖКИ, А НЕ СИТО. ЧИТАТЬ РЕЗУЛЬТАТ КАК НЕУСТОЙЧИВЫЙ."
        )

    power = power_estimate(head["k"], head["N"], list(POWER_EFFECT_SIZES),
                           n_sims=int(n_sims), seed=POWER_SEED)
    proj = projected_n(as_of=as_of_date, repo_dir=repo_dir, observational=observational,
                       addendum_present=bool(addendum.get("available")),
                       design_lock=lock_for_analysis)

    # --- C2: мощность на ПРОГНОЗНОМ дизайне ------------------------------
    # Пункт C2 пакета требует ответа на вопрос «при N=... шанс увидеть реальное
    # преимущество ~...%» ЗАРАНЕЕ. Сегодняшний N бесполезен для этого ответа:
    # закрытых сделок ноль, и мощность по нему не определена вообще. Считать
    # надо по тому N, который БУДЕТ к 15.11 — он известен уже сейчас, потому
    # что состав закрытых к вердикту сделок зафиксирован (дорожки упёрлись в
    # предохранитель 60, а позиция, открытая после 13.09, до 60 дней не доживёт).
    # Это оценка вперёд, от результатов не зависящая: она посчитана до того,
    # как существует хоть один отклик.
    power_projected = None
    if proj.get("available"):
        k_pr = len(proj.get("participating") or [])
        N_pr = (proj["blocks_token"] if proj.get("design_that_applies") == "token"
                else proj["blocks_run"])
        power_projected = power_estimate(k_pr, N_pr, list(POWER_EFFECT_SIZES),
                                         n_sims=int(n_sims), seed=POWER_SEED)
        power_projected["design"] = proj.get("design_that_applies")
        power_projected["participating"] = list(proj.get("participating") or [])
        power_projected["observational"] = list(proj.get("observational") or [])
        # Справочное семейство (с поправкой — семёрка с A6, без неё — «без A6»):
        # разницу в мощности честнее показать, чем спрятать.
        ref = proj.get("reference") or {}
        if ref:
            k_alt = len(ref.get("tracks") or [])
            N_alt = (ref["blocks_token"] if proj.get("design_that_applies") == "token"
                     else ref["blocks_run"])
            alt = power_estimate(k_alt, N_alt, list(POWER_EFFECT_SIZES),
                                 n_sims=int(n_sims), seed=POWER_SEED)
            alt["participating"] = list(ref.get("tracks") or [])
            alt["label"] = ref.get("label")
            power_projected["reference"] = alt

    money_allowed = bool(head["verdict_kind"] == "winner"
                         and head["winner"] is not None
                         and head["winner"] in head_p6["passed_tracks"])

    artifact = {
        "kind": "verdict_1511",
        "as_of": as_of_date.isoformat(),
        "verdict_date": VERDICT_DATE.isoformat(),
        "provenance": provenance.stamp(TOOL_VERSION),
        "rules_sha256": rules_hash.rules_sha256_or_none(),
        "rules_addendum": {
            "file": addendum.get("file"),
            "available": bool(addendum.get("available")),
            "sha256": addendum.get("sha256"),
            "sha256_lf": addendum.get("sha256_lf"),
            "number": addendum.get("number"),
            "adopted": addendum.get("adopted"),
            "applies_before_first_closures": list(addendum.get("applies_before") or []),
            "declared_tracks": list(declared),
            "observational_tracks": list(observational),
            "applied": bool(observational),
            "preregistered": preregistered,
            "preregistration_note": prereg_note,
            "first_exit_date": first_exit.isoformat() if first_exit else None,
            "rules_v1_sha256_pinned": addendum.get("rules_v1_sha256_pinned"),
            "pin_matches": addendum.get("pin_matches"),
            "warnings": list(addendum.get("warnings") or []),
            "error": addendum.get("error"),
        },
        "rules_addendum_sha256": addendum.get("sha256_lf"),
        "design_lock": lock_status,
        "costs_config_used": {
            "path": os.path.basename(cfg.get("_source_path", "costs.yaml")),
            "version": cfg.get("version"),
            "instrument": cfg.get("instrument"),
            "taker_fee_bps": cfg["taker_fee_bps"]["value"],
            "slippage_bps_by_class": {
                c: cfg["slippage_bps_by_class"][c]["value"] for c in ("deep", "medium", "thin")
            },
            "sensitivity_multipliers": mults,
            "sensitivity_applies_to": cfg.get("sensitivity_applies_to", "all"),
            "headline_mult": head_mult,
            "slippage_is_assumption": True,
        },
        "trades_source": load_info,
        "n_trades_total": len(all_trades),
        "n_closed_total": len(closed),
        "bucket_mismatch_n": sum(1 for t in all_trades if t.get("bucket_mismatch")),
        "alpha": ALPHA,
        "result": head,
        "paragraph6": head_p6,
        "paragraph7": head_p7,
        "sensitivity": sensitivity,
        "verdict_stable_across_costs": verdict_stable,
        "sensitivity_alarm": sensitivity_alarm,
        "power": power,
        "power_projected": power_projected,
        "projection": proj,
        "money_allowed": money_allowed,
        "money_rule": (
            "деньги разрешены ТОЛЬКО при одновременном выполнении §5 ветка 2 "
            "с победителем И §6 (второй порог протокола)"
        ),
        "verdict_line": head["verdict_line"],
        "insufficient_data": head.get("insufficient_data"),
        "insufficient_data_line": head.get("insufficient_data_line"),
        "note": (
            "все числа посчитаны кодом по замороженным правилам "
            "screener_tournament_rules_v1.md; net опирается на ДОПУЩЕНИЕ о "
            "слиппедже, поэтому вердикт напечатан при каждом множителе "
            "чувствительности"
        ),
    }
    return artifact


# =========================================================================
#  ПЕЧАТЬ
# =========================================================================

def _line(char="-", n=78):
    return char * n


def print_report(art):
    res = art["result"]
    p6 = art["paragraph6"]

    print(_line("="))
    print("ВЕРДИКТ ТУРНИРА СИТ — исполнение замороженных правил v1")
    print("дата вердикта по протоколу: %s   расчёт на: %s"
          % (art["verdict_date"], art["as_of"]))
    print("правила sha256: %s" % (art["rules_sha256"] or "НЕ НАЙДЕНЫ"))
    add = art.get("rules_addendum") or {}
    if add.get("available"):
        print("поправка №%s sha256 (LF): %s" % (add.get("number"), add.get("sha256_lf")))
        print("  объявлены наблюдательными: %s; принята %s; первые закрытия по тексту "
              "поправки: %s"
              % (", ".join(add.get("declared_tracks") or []) or "никто",
                 add.get("adopted") or "?",
                 ", ".join(add.get("applies_before_first_closures") or []) or "?"))
        if add.get("preregistered"):
            print("  предрегистрация ПОДТВЕРЖДЕНА машиной: %s" % add.get("preregistration_note"))
            print("  применено — ВНЕ ТЕСТА: %s"
                  % (", ".join(add.get("observational_tracks") or []) or "нет дорожек"))
        else:
            print("  ВНИМАНИЕ: ПОПРАВКА НЕ ПРЕДРЕГИСТРИРОВАНА (%s) — НЕ ПРИМЕНЕНА, "
                  "СЕМЕЙСТВО = ВСЕ НЕПУСТЫЕ ДОРОЖКИ" % add.get("preregistration_note"))
        if add.get("pin_matches"):
            print("  отпечаток правил v1 в поправке СОВПАДАЕТ с файлом правил")
        else:
            print("  ВНИМАНИЕ: SHA256 ПРАВИЛ V1 В ПОПРАВКЕ НЕ СОВПАДАЕТ С ФАЙЛОМ ПРАВИЛ "
                  "— ПОПРАВКА ССЫЛАЕТСЯ НЕ НА ТЕ ПРАВИЛА")
        for w in add.get("warnings") or []:
            print("  поправка: %s" % w)
    else:
        print("поправка №1: файла нет%s — семейство = все непустые дорожки"
              % ((" (%s)" % add["error"]) if add.get("error") else ""))
    lk = art.get("design_lock") or {}
    if lk.get("present"):
        print("дизайн блока: флаг «блок = %s», подтверждён %s (%s)"
              % ("прогон" if lk.get("design") == "run" else "токен",
                 lk.get("confirmed_on") or "?", lk.get("confirmed_by") or "?"))
        if lk.get("applied"):
            print("  предрегистрация флага ПОДТВЕРЖДЕНА машиной: %s" % lk.get("note"))
        else:
            print("  ВНИМАНИЕ: ФЛАГ ДИЗАЙНА НЕ ПРИМЕНЁН (%s)" % lk.get("note"))
    else:
        print("дизайн блока: флага нет — по §3.1 (число полных блоков-токенов)")
    st = art["provenance"]
    print("git_sha: %s   сгенерировано: %s   python %s"
          % (st["git_sha"], st["generated_utc"], st["python"]))
    print(_line("="))

    # --- данные ----------------------------------------------------------
    src = art["trades_source"]
    if src.get("warning"):
        print("!! %s" % src["warning"])
    print("сделок загружено: %d, из них ЗАКРЫТЫХ: %d "
          "(§2: открытые в вердикт не идут ни в каком виде)"
          % (art["n_trades_total"], art["n_closed_total"]))
    print("закрытых по дорожкам: %s"
          % ", ".join("%s=%d" % (tr, res["per_track_closed"][tr]) for tr in TOURNAMENT_TRACKS))
    print("A0 (портфель) закрытых: %d — в тест НЕ входит (§1), только во второй порог §6"
          % res["a0_closed_n"])
    if res["liq_class_fallback_n"]:
        print("класс ликвидности неизвестен у %d сделок — считаются по САМОМУ ДОРОГОМУ (thin)"
              % res["liq_class_fallback_n"])

    # --- §3.1 исключения --------------------------------------------------
    print(_line())
    if res["excluded_tracks"]:
        print("ИСКЛЮЧЕНЫ ПОИМЁННО (§3.1):")
        for row in res["excluded_tracks"]:
            print("  %s — %s" % (row["track"], row["reason"]))
    else:
        print("исключённых дорожек нет: у всех есть закрытые позиции")
    print("участвуют (k=%d): %s"
          % (res["k"], ", ".join(res["participating"]) if res["participating"] else "никто"))
    obs = res.get("observational") or {}
    if obs:
        print("НАБЛЮДАТЕЛЬНЫЕ ДОРОЖКИ (поправка №1, ВНЕ ТЕСТА — описательно, "
              "в k/N/ранги не входят):")
        print("  %-6s %6s %11s %11s %21s %8s"
              % ("сито", "закр.", "ср. net,%", "мед. net,%", "IQR [Q1; Q3]", "доля>0"))
        for tr in sorted(obs):
            row = obs[tr]
            iqr = ("[%s; %s]" % (_fmt_num(row["q1_net"], "%.2f"),
                                 _fmt_num(row["q3_net"], "%.2f"))
                   if row["n_closed"] else "—")
            print("  %-6s %6d %11s %11s %21s %8s"
                  % (tr, row["n_closed"], _fmt_num(row["mean_net"], "%.2f"),
                     _fmt_num(row["median_net"], "%.2f"), iqr,
                     _fmt_num(row["win_rate"], "%.2f")))

    # --- §3.1 дизайн ------------------------------------------------------
    print(_line())
    print("ДИЗАЙН БЛОКА (§3.1) — печатаются ОБА N, как требуют правила:")
    print("  блок = токен : N = %d полных блоков (неполных отброшено: %d)"
          % (res["n_blocks_token"], res["partial_blocks_token"]))
    print("  блок = прогон: N = %d полных блоков (неполных отброшено: %d)"
          % (res["n_blocks_run"], res["partial_blocks_run"]))
    print("  выбран дизайн: %s" % ("блок = токен" if res["design"] == "token" else "блок = ПРОГОН"))
    print("  %s" % res["design_reason"])
    if res.get("design_locked"):
        lk = res.get("design_lock") or {}
        print("  ДИЗАЙН ЗАФИКСИРОВАН ФЛАГОМ (С-01): подтверждён %s; основание: %s"
              % (lk.get("confirmed_on") or "?", lk.get("basis") or "?"))
    if res.get("design_conflict_line"):
        print("  %s" % res["design_conflict_line"])
    if res["design_switched"]:
        print("  ПЕРЕКЛЮЧЕНИЕ НА ЗАПАСНОЙ ДИЗАЙН ОБЪЯВЛЕНО (§3.1, предрегистрация).")

    # --- тесты ------------------------------------------------------------
    print(_line())
    if res["blockers"]:
        print("ГЛАВНЫЙ ТЕСТ НЕ ЗАПУСКАЛСЯ:")
        for b in res["blockers"]:
            print("  * %s" % b)
    else:
        print("СРЕДНИЕ РАНГИ (ранг 1 = лучший) и net, %% (множитель издержек %.2f):"
              % res["mult"])
        print("  (медиана печатается рядом со средним всегда — среднее в одиночку")
        print("   на скошенных 60-дневных доходностях вводит в заблуждение)")
        print("  %-6s %13s %13s %13s %8s"
              % ("сито", "ср. ранг", "ср. net, %", "мед. net, %", "закр."))
        for tr in res["participating"]:
            mark = "  <- контроль" if tr == CONTROL_TRACK else ""
            print("  %-6s %13s %13s %13s %8s%s"
                  % (tr, _fmt_num(res["mean_ranks"][tr], "%.3f"),
                     _fmt_num(res["mean_net_by_track"][tr], "%.2f"),
                     _fmt_num((res.get("median_net_by_track") or {}).get(tr), "%.2f"),
                     (res.get("n_closed_by_track") or {}).get(tr, "—"), mark))
        fr, idv = res["friedman"], res["iman_davenport"]
        print("  Фридман (КОНТРОЛЬНЫЙ, §3.3): chi2 = %s, df = %d, p = %s"
              % (_fmt_num(fr["chi2"], "%.4f"), fr["df"], _fmt_p(fr["p"])))
        print("  Иман-Дэвенпорт (ГЛАВНЫЙ, §3.4): F = %s, df1 = %d, df2 = %d, p = %s%s"
              % (_fmt_num(idv["F"], "%.4f"), idv["df1"], idv["df2"], _fmt_p(idv["p"]),
                 "  [ВЫРОЖДЕННЫЙ СЛУЧАЙ]" if idv["degenerate"] else ""))
        print("  alpha = %.2f; H0 (все сита эквивалентны) %s"
              % (res["alpha"], "ОТВЕРГНУТА" if res["h0_rejected"] else "не отвергнута"))
        if res["disagreement_line"]:
            print("  %s" % res["disagreement_line"])
        print("  лучший средний ранг: %s — %s"
              % (res["best_mean_rank_track"], res["best_mean_rank_note"]))

    # --- пост-хок ----------------------------------------------------------
    print(_line())
    if res["posthoc"] is None:
        print("ПОСТ-ХОК НЕ ЗАПУСКАЛСЯ (§4 запускается исключительно при отвергнутой H0).")
    else:
        print("ПОСТ-ХОК ХОЛМА против %s (§4, k-1=%d пар; пары сит между собой НЕ считаются):"
              % (CONTROL_TRACK, res["k"] - 1))
        print("  %-6s %10s %12s %12s %10s %s"
              % ("сито", "ранг", "z", "p (2-стор)", "alpha_i", "итог"))
        for row in res["posthoc"]:
            if row["is_winner"]:
                verdict = "ПРОШЛА, лучше контроля"
            elif row["passed_holm"]:
                verdict = "прошла, но ХУЖЕ контроля"
            else:
                verdict = "не прошла"
            print("  %-6s %10s %12s %12s %10s %s"
                  % (row["track"], _fmt_num(row["mean_rank"], "%.3f"),
                     _fmt_num(row["z"], "%+.3f"), _fmt_p(row["p_two_sided"]),
                     _fmt_num(row["alpha_i"], "%.5f"), verdict))
        print("  %s" % res["posthoc_note"])

    # --- ВЕРДИКТ -----------------------------------------------------------
    print(_line("="))
    mism = art.get("bucket_mismatch_n") or 0
    if mism:
        print("ВНИМАНИЕ: позиций с расхождением ведро/поля: %d. Это порча данных "
              "(запись лежит в одном списке, а помечена как из другого); "
              "закрытость взята ПО ВЕДРУ, но файл надо проверить." % mism)
    print("ВЕРДИКТ: %s" % res["verdict_line"])
    if res.get("insufficient_data"):
        print("ОГОВОРКА (обязательная, C2): %s" % res["insufficient_data_line"])
        print("  %s" % res["insufficient_data_reason"])
        print("  Вердикт выше вынесен по §5 замороженных правил и остаётся")
        print("  вердиктом; оговорка его НЕ отменяет, а говорит, чего он НЕ")
        print("  доказывает: «не увидели» и «нет» на таком N — не одно и то же.")
    print("основание: %s" % res["verdict_reason"])
    print(_line("="))

    # --- §6 ----------------------------------------------------------------
    print("ВТОРОЙ ПОРОГ ПРОТОКОЛА ДЕНЕГ (§6) — считается ОТДЕЛЬНО:")
    print("  условие: >= %.0f п.п. среднего net против И %s, И %s; >= %d закрытых; "
          "лидерство в ОБЕИХ половинах срока"
          % (P6_EDGE_PP, CONTROL_TRACK, PORTFOLIO_TRACK, P6_MIN_CLOSED))
    print("  база деления на половины: %s" % p6["halves_basis"])
    if not p6["computable"]:
        print("  §6 НЕ СЧИТАЕТСЯ: %s" % (p6.get("note") or "нет данных"))
    else:
        print("  половина 1: %s .. %s   половина 2: %s .. %s"
              % (p6["half1"][0], p6["half1"][1], p6["half2"][0], p6["half2"][1]))
        print("  %-6s %8s %12s %12s %12s %s"
              % ("сито", "закрыто", "средний net", "дельта A7", "дельта A0", "§6"))
        for tr in TOURNAMENT_TRACKS:
            row = p6["per_track"].get(tr) or {}
            if not row.get("n_closed"):
                continue
            mark = ("  (наблюдательная, вне теста)"
                    if tr in (res.get("observational_tracks") or []) else "")
            print("  %-6s %8d %12s %12s %12s %s%s"
                  % (tr, row["n_closed"], _fmt_num(row["mean_net"], "%.2f"),
                     _fmt_num(row["delta_vs_control_pp"], "%+.2f"),
                     _fmt_num(row["delta_vs_portfolio_pp"], "%+.2f"),
                     "ПРОЙДЕН" if row["passed"] else "нет", mark))
        print("  §6 прошли: %s"
              % (", ".join(p6["passed_tracks"]) if p6["passed_tracks"] else "никто"))
        if p6.get("observational_tracks"):
            print("  наблюдательные (вне теста, §6 к ним не применяется): %s"
                  % ", ".join(p6["observational_tracks"]))
    print("  ДЕНЬГИ РАЗРЕШЕНЫ: %s (%s)"
          % ("ДА" if art["money_allowed"] else "НЕТ", art["money_rule"]))

    # --- §7 теневые стопы ----------------------------------------------------
    print(_line())
    p7 = art.get("paragraph7") or {}
    print("ТЕНЕВЫЕ СТОПЫ (§7) — счёт ОТДЕЛЬНЫЙ, на правила сит не влияет:")
    print("  источник: %s" % p7.get("source"))
    print("  оговорка: %s" % p7.get("check_frequency_note"))
    if not p7.get("available"):
        print("  §7 НЕ СЧИТАЕТСЯ: %s" % p7.get("note"))
    else:
        print("  %-12s %6s %14s %14s" % ("правило", "n", "ср. net, %", "мед. net, %"))
        for _nm, _lbl in (("passive", "пассив 60д"), ("stop20", "стоп -20%"),
                          ("stop30", "стоп -30%")):
            _r = (p7.get("rules") or {}).get(_nm) or {}
            print("  %-12s %6s %14s %14s"
                  % (_lbl, _r.get("n", "—"),
                     _fmt_num(_r.get("mean_net_pct"), "%.2f"),
                     _fmt_num(_r.get("median_net_pct"), "%.2f")))
        for _nm, _lbl in (("stop20", "стоп -20%"), ("stop30", "стоп -30%")):
            _d = (p7.get("paired_vs_passive") or {}).get(_nm) or {}
            print("  %s против пассива: медиана разницы %s п.п., лучше в %s из %s, "
                  "знаковый тест p=%s"
                  % (_lbl, _fmt_num(_d.get("median_diff_pp"), "%+.2f"),
                     _d.get("n_better"), _d.get("n_pairs"),
                     _fmt_p(_d.get("sign_test_p"))))
        print("  ВЫВОД §7: %s" % p7.get("verdict_line"))

    # --- чувствительность ---------------------------------------------------
    print(_line())
    print("ТАБЛИЦА ЧУВСТВИТЕЛЬНОСТИ К ИЗДЕРЖКАМ (слиппедж — ДОПУЩЕНИЕ, не замер):")
    print("  %8s %6s %10s %-44s" % ("множ.", "N", "p главн.", "вердикт"))
    for row in art["sensitivity"]:
        print("  %8.2f %6d %10s %-44s"
              % (row["mult"], row["N"], _fmt_p(row["p_main"]), row["verdict_line"]))
    if art["sensitivity_alarm"]:
        print("  %s" % art["sensitivity_alarm"])
    else:
        print("  вердикт УСТОЙЧИВ ко всем множителям из costs.yaml — издержки его не решают")

    # --- мощность -----------------------------------------------------------
    print(_line())
    pw = art["power"]
    print("МОЩНОСТЬ (C2, считается ЗАРАНЕЕ и от результатов не зависит):")
    print("  порог мощности объявлен явно: %.2f; alpha = %.2f; симуляций %d, сид %d"
          % (POWER_THRESHOLD, ALPHA, pw["n_sims"], pw["seed"]))
    if pw["computable"]:
        for eff in sorted(pw["by_effect"]):
            val = pw["by_effect"][eff]
            print("  при N=%d и k=%d шанс увидеть преимущество размера %.2f сигмы ~ %.0f%%%s"
                  % (pw["N"], pw["k"], eff, 100.0 * val,
                     "" if val >= POWER_THRESHOLD else "   (ниже порога)"))
    print("  %s" % pw["note"])

    # --- проекция ------------------------------------------------------------
    print(_line())
    pr = art["projection"]
    print("ПРОЕКЦИЯ К 15.11 (по фактическим entry_date открытых позиций):")
    if not pr.get("available"):
        print("  %s" % pr.get("error", "tournament.json недоступен"))
    else:
        print("  закрытых к %s будет: %s"
              % (pr["last_run_date"],
                 ", ".join("%s=%d" % (tr, pr["per_track_closed"].get(tr, 0))
                           for tr in TOURNAMENT_TRACKS)))
        print("  участвуют: %s; пусты и будут исключены поимённо: %s"
              % (", ".join(pr["participating"]) or "никто",
                 ", ".join(pr["excluded_empty"]) or "нет"))
        print("  блоков «блок = токен» : %d" % pr["blocks_token"])
        print("  блоков «блок = прогон»: %d" % pr["blocks_run"])
        if pr.get("observational"):
            print("  наблюдательные (поправка №1, ВНЕ ТЕСТА): %s"
                  % ", ".join(pr["observational"]))
        ref = pr.get("reference") or {}
        if ref:
            print("  справочно, %s: токен %d, прогон %d"
                  % (ref["label"], ref["blocks_token"], ref["blocks_run"]))
        elif pr.get("reference_note"):
            print("  %s" % pr["reference_note"])
        print("  дизайн, который применится: %s"
              % ("блок = токен" if pr["design_that_applies"] == "token" else "блок = ПРОГОН"))
        for a in pr["assumptions"]:
            print("  оговорка: %s" % a)

        # C2 по существу: шанс увидеть преимущество при том N, который БУДЕТ.
        pwp = art.get("power_projected")
        if pwp:
            print("  --")
            print("  МОЩНОСТЬ НА ЭТОМ ПРОГНОЗНОМ ДИЗАЙНЕ (это и есть ответ C2):")
            if pwp.get("computable"):
                for eff in sorted(pwp["by_effect"]):
                    val = pwp["by_effect"][eff]
                    print("    при N=%d и k=%d шанс увидеть реальное преимущество "
                          "размера %.2f сигмы ~ %.0f%%%s"
                          % (pwp["N"], pwp["k"], eff, 100.0 * val,
                             "" if val >= POWER_THRESHOLD else "   (НИЖЕ ПОРОГА %.2f)"
                             % POWER_THRESHOLD))
                alt = pwp.get("reference") or {}
                if alt.get("computable"):
                    print("    справочно, %s (N=%d, k=%d): %s"
                          % (alt.get("label") or "альтернативное семейство",
                             alt["N"], alt["k"],
                             ", ".join("%.2f сигмы -> %.0f%%" % (e, 100.0 * alt["by_effect"][e])
                                       for e in sorted(alt["by_effect"]))))
            else:
                print("    %s" % pwp.get("note", "не определена"))
            print("    ЧИТАТЬ ТАК: турнир этого размера способен увидеть только")
            print("    КРУПНОЕ преимущество. Исход «различий нет» при малой мощности")
            print("    означает «не увидели», а не «преимущества нет» — и это")
            print("    законный результат, а не сбой (см. §5 замороженных правил).")

    print(_line("="))


# =========================================================================
#  АРТЕФАКТЫ
# =========================================================================

def write_artifacts(art, repo_dir=None):
    """artifacts/verdict_latest.json + artifacts/YYYY-MM/verdict_YYYYMMDD.json"""
    base = repo_dir if repo_dir is not None else HERE
    as_of = _as_date(art["as_of"]) or _dt.date.today()
    payload = json.dumps(_json_safe(art), ensure_ascii=False, indent=2, sort_keys=False)

    latest = os.path.join(base, "artifacts", "verdict_latest.json")
    dated_dir = os.path.join(base, "artifacts", as_of.strftime("%Y-%m"))
    dated = os.path.join(dated_dir, "verdict_%s.json" % as_of.strftime("%Y%m%d"))

    os.makedirs(os.path.dirname(latest), exist_ok=True)
    os.makedirs(dated_dir, exist_ok=True)
    for path in (latest, dated):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.write("\n")
    return [latest, dated]


# =========================================================================
#  CLI
# =========================================================================

def main(argv=None):
    _setup_stdout()
    parser = argparse.ArgumentParser(
        description="Вердикт турнира сит 15.11 по замороженным правилам v1")
    parser.add_argument("--as-of", dest="as_of", default=None,
                        help="дата расчёта YYYY-MM-DD (по умолчанию сегодня)")
    parser.add_argument("--sims", dest="sims", type=int, default=POWER_SIMS_DEFAULT,
                        help="число симуляций для ТАБЛИЦЫ мощности "
                             "(на вердикт не влияет, по умолчанию %d)" % POWER_SIMS_DEFAULT)
    parser.add_argument("--repo-dir", dest="repo_dir", default=None,
                        help="корень репозитория (по умолчанию каталог скрипта)")
    parser.add_argument("--no-artifacts", dest="no_artifacts", action="store_true",
                        help="только печать, ничего не писать на диск")
    args = parser.parse_args(argv)

    if args.as_of is not None and _as_date(args.as_of) is None:
        print("--as-of: ожидалась дата YYYY-MM-DD, получено %r" % args.as_of)
        return 2
    if args.sims < 1:
        print("--sims: нужно положительное число симуляций, получено %d" % args.sims)
        return 2

    art = build_verdict(repo_dir=args.repo_dir, as_of=args.as_of, n_sims=args.sims)
    print_report(art)

    if not args.no_artifacts:
        written = write_artifacts(art, repo_dir=args.repo_dir)
        print("артефакты записаны:")
        for p in written:
            print("  %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())

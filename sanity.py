#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sanity.py — светофор конвейера скринера (блок E1 пакета СКРИНЕР-ПОЛНЫЙ).

ЗАЧЕМ ЭТО НУЖНО ДЛЯ ДЕНЕГ
Турнир копится вслепую до 15.11.2026. Молчаливая поломка конвейера (прогон
перестал запускаться, журнал ссохся, закрытие позиций не сработало, правила
подменены) выяснится в момент вердикта — когда данные уже потеряны и
пересобрать их неоткуда. Светофор превращает «вроде всё работает» в число:
каждый заход начинается и заканчивается его запуском, RED = не считать и не
публиковать, пока причина не устранена.

ЧТО ПРОВЕРЯЕТСЯ (раздел E1 контракта)
  П1 возраст и размер входных файлов: tournament.json, positions.json,
     candidates_log.jsonl, config.yaml
  П2 число строк последнего прогона журнала против медианы последних 10
  П3 доля отказов (reject_reason) — схлопывание воронки до одной причины
  П4 артефакты: metrics_latest.json свежее порога, verdict_latest.json читается
  П5 турнир: нет ли открытой позиции с возрастом больше порога закрытия
  П6 хеш правил совпадает с записанным в artifacts/metrics_latest.json

Итог: GREEN / YELLOW / RED плюс причины, у КАЖДОЙ причины — числовое
подтверждение (без числа причина не публикуется: «что-то не так» нельзя ни
проверить, ни опровергнуть). Пишет artifacts/sanity_latest.json и дописывает
artifacts/sanity_history.csv. Код возврата: 0 при GREEN/YELLOW, 1 при RED.

ТОЛЬКО ЧТЕНИЕ. Скрипт пишет РОВНО два своих файла (artifacts/sanity_latest.json
и artifacts/sanity_history.csv) и не трогает ни tournament.json, ни
positions.json, ни candidates_log.jsonl, ни config.yaml, ни артефакты чужих
блоков.

ЧЕСТНАЯ ГРАНИЦА, которую надо называть вслух: светофор ловит ОСТАНОВКУ и
ССЫХАНИЕ конвейера, а не подмену данных на лету и не ошибку в самой логике сит.
Хеш правил (П6) ловит подмену файла правил между снимками, но не мешает автору
переписать разом и правила, и все артефакты — против такого работает только
внешняя git-история на GitHub. Первый слой не притворяется вторым.

ПРО ВРЕМЯ ФАЙЛОВ: mtime как признак свежести почти бесполезен — на GitHub
Actions `actions/checkout` выставляет всем файлам время выкладки, и любой файл
там «свежий» всегда. Поэтому возраст считается по СОДЕРЖИМОМУ (последний
`_run.timestamp_utc` журнала, последние entry_date/close_date в реестрах,
`provenance.generated_utc` артефактов), а mtime печатается только справочно и
сам по себе никогда не даёт FAIL.

Зависимости: stdlib + PyYAML (только для разбора config.yaml; отсутствие
PyYAML не роняет прогон, а отмечается отдельной строкой).
"""

import argparse
import csv
import hashlib
import json
import os
import statistics
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone

# --- необязательные соседи по репозиторию -----------------------------------
# Импорты в try: светофор обязан работать и в полуразобранном репозитории —
# именно там он и нужен. Отсутствие модуля отмечается числом/строкой в отчёте,
# а не исключением.
try:
    import provenance as _provenance
except Exception:                                    # pragma: no cover
    _provenance = None

try:
    import rules_hash as _rules_hash
except Exception:                                    # pragma: no cover
    _rules_hash = None

try:
    import yaml as _yaml
except Exception:                                    # pragma: no cover
    _yaml = None

TOOL_VERSION = "1.0.0"

# =============================================================================
# ПОРОГИ. Все берутся из РАСПИСАНИЯ ПРОГОНОВ, а не с потолка.
# Расписание: .github/workflows/screener.yml, cron "17 6 * * 1,4" —
# понедельник и четверг 06:17 UTC. Значит максимальный ЗАКОННЫЙ разрыв между
# прогонами = 4 суток (чт -> пн), минимальный = 3 суток (пн -> чт).
# =============================================================================

# Максимальный законный разрыв прогонов, из cron. Отсюда считается всё остальное.
RUN_GAP_MAX_DAYS = 4

# Журнал кандидатов. WARN = 4 (разрыв) + 1 сутки на задержку прогона и коммита:
# здоровая система в это окно укладывается всегда. FAIL = 4 + 4 + 1 = 9:
# столько набегает, только если пропущено ДВА прогона подряд. Один пропуск —
# жёлтый, повод посмотреть Actions; два — красный, конвейер стоит.
LOG_AGE_WARN_DAYS = RUN_GAP_MAX_DAYS + 1        # 5
LOG_AGE_FAIL_DAYS = 2 * RUN_GAP_MAX_DAYS + 1    # 9

# Реестры (tournament.json, positions.json). Порог мягче журнала осознанно:
# дата последней записи в реестре может ЗАКОННО застыть — дорожки A1..A5,A7
# упёрлись в предохранитель T_MAX_OPEN = 60, новых входов может не быть, а
# закрытий до 18.09.2026 нет вовсе. Поэтому реестр — не сторож свежести
# прогонов (им служит журнал), а сторож того, что файл вообще жив.
# WARN = два пропущенных прогона (9), FAIL = около трёх недель тишины (22).
LEDGER_AGE_WARN_DAYS = 2 * RUN_GAP_MAX_DAYS + 1   # 9
LEDGER_AGE_FAIL_DAYS = 22

# П2: последний прогон против медианы последних 10 (контракт E1).
SHRINK_FAIL = 0.60
SHRINK_WARN = 0.80
MEDIAN_WINDOW = 10          # окно медианы
MEDIAN_MIN_RUNS = 3         # меньше трёх прошлых прогонов — медиана не смысл

# П3: воронка. Одна причина на весь прогон = FAIL (контракт E1).
# Плюс жёлтая зона: одна причина съела почти всё. База на 01.09.2026 —
# no_perp ~69% строк, так что 95% срабатывает на настоящем перекосе, а не на норме.
FUNNEL_MIN_REASONS = 2
FUNNEL_CONCENTRATION_WARN = 0.95

# П4: артефакты. metrics_latest.json пересобирается вместе с недельным аудитом,
# отсюда WARN на неделе с запасом. FAIL — месяц без пересчёта: до вердикта
# 15.11 такое молчание уже съедает срок.
METRICS_AGE_WARN_DAYS = 9
METRICS_AGE_FAIL_DAYS = 31

# П5: закрытие позиции — на первом прогоне с возрастом >= 60 дней. С учётом
# разрыва прогонов законный максимум возраста открытой позиции = 60 + 4 = 64.
# 65 (WARN) = закрытие опоздало; 75 (FAIL, из контракта E1) = пропущено
# несколько прогонов закрытия подряд, механизм не работает.
POSITION_CLOSE_AGE_DAYS = 60
POSITION_STALE_WARN_DAYS = POSITION_CLOSE_AGE_DAYS + RUN_GAP_MAX_DAYS + 1   # 65
POSITION_STALE_FAIL_DAYS = 75
# Позиция закрывается также после двух прогонов подряд без цены (miss >= 2),
# поэтому miss >= 3 у ОТКРЫТОЙ позиции — тот же дефект «закрытие не сработало».
POSITION_MISS_CLOSE = 2
POSITION_MISS_WARN = POSITION_MISS_CLOSE + 1                                # 3

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
GREEN, YELLOW, RED = "GREEN", "YELLOW", "RED"

INPUT_FILES = ("tournament.json", "positions.json",
               "candidates_log.jsonl", "config.yaml")

ARTIFACTS_DIR = "artifacts"
SANITY_LATEST = "sanity_latest.json"
SANITY_HISTORY = "sanity_history.csv"
METRICS_LATEST = "metrics_latest.json"
VERDICT_LATEST = "verdict_latest.json"
RULES_FILENAME = "screener_tournament_rules_v1.md"

HISTORY_HEADER = [
    "ts_utc", "verdict", "n_fail", "n_warn",
    "p1", "p2", "p3", "p4", "p5", "p6",
    "log_age_days", "last_run_rows", "rows_vs_median",
    "funnel_reasons_n", "max_open_age_days", "git_sha", "reasons",
]


# =============================================================================
# Мелкие утилиты времени и файлов
# =============================================================================

def utc_now():
    """Текущий момент в UTC (aware). Отдельной функцией — чтобы тесты
    подменяли время параметром, а не переводили часы машины."""
    return datetime.now(timezone.utc)


def parse_ts(value):
    """ISO-время ('2026-08-31T14:05:12Z' и родня) -> aware datetime или None.

    Мусор не роняет светофор: неразобранное время = «времени нет», и это
    честнее выдуманной даты."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_day(value):
    """'YYYY-MM-DD' -> date или None (у дат входа/закрытия времени нет)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def age_days_ts(ts, now):
    """Возраст момента в сутках с двумя знаками (может быть отрицательным,
    если чьи-то часы ушли вперёд — это тоже видно и не прячется)."""
    if ts is None:
        return None
    return round((now - ts).total_seconds() / 86400.0, 2)


def age_days_day(day, now):
    """Возраст даты в целых сутках."""
    if day is None:
        return None
    return (now.date() - day).days


def file_facts(path, now):
    """Факты о файле без разбора содержимого: есть ли, размер, возраст mtime."""
    out = {"path": path, "exists": False, "size_bytes": 0, "mtime_age_days": None}
    try:
        st = os.stat(path)
    except OSError:
        return out
    out["exists"] = True
    out["size_bytes"] = int(st.st_size)
    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    out["mtime_age_days"] = age_days_ts(mtime, now)
    return out


def read_json(path):
    """-> (объект|None, ошибка|None). Читаем осторожно: битый артефакт должен
    стать причиной с текстом, а не трассировкой стека."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except FileNotFoundError:
        return None, "файла нет"
    except json.JSONDecodeError as exc:
        return None, "битый JSON: %s" % exc
    except OSError as exc:
        return None, "ошибка чтения: %s" % exc


# =============================================================================
# Разбор источников
# =============================================================================

def scan_log(path):
    """Потоковый разбор candidates_log.jsonl.

    Строк в журнале десятки тысяч, а нужны только счётчики, поэтому сами строки
    монет НЕ храним — только по прогонам: сколько строк, какие причины отказа,
    какое время. Память не растёт вместе с журналом.

    -> {'exists','error','runs':[{run_id,ts,ts_dt,rows_n,reasons{}}],
        'total_lines','bad_lines'}
    """
    out = {"exists": False, "error": None, "runs": [],
           "total_lines": 0, "bad_lines": 0}
    order, by_run = [], {}
    try:
        f = open(path, encoding="utf-8")
    except FileNotFoundError:
        out["error"] = "файла нет"
        return out
    except OSError as exc:
        out["error"] = "ошибка чтения: %s" % exc
        return out

    def slot(rid):
        if rid not in by_run:
            by_run[rid] = {"run_id": rid, "ts": None, "rows_n": 0,
                           "reasons": Counter()}
            order.append(rid)
        return by_run[rid]

    with f:
        out["exists"] = True
        for line in f:
            line = line.strip()
            if not line:
                continue
            out["total_lines"] += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                out["bad_lines"] += 1
                continue
            if not isinstance(obj, dict):
                out["bad_lines"] += 1
                continue
            if "_meta" in obj:
                continue
            if "_run" in obj:
                info = obj.get("_run") or {}
                rec = slot(info.get("run_id"))
                rec["ts"] = rec["ts"] or info.get("timestamp_utc")
                continue
            rec = slot(obj.get("run_id"))
            rec["rows_n"] += 1
            rec["reasons"][obj.get("reject_reason") or "enter"] += 1
            if not rec["ts"]:
                rec["ts"] = obj.get("timestamp_utc")

    for rid in order:
        rec = by_run[rid]
        out["runs"].append({
            "run_id": rid,
            "ts": rec["ts"],
            "ts_dt": parse_ts(rec["ts"]),
            "rows_n": rec["rows_n"],
            "reasons": dict(rec["reasons"]),
        })
    return out


def _positions_of_tournament(obj):
    """Список (дорожка, позиция, статус) из tournament.json."""
    out = []
    tracks = (obj or {}).get("tracks") or {}
    if not isinstance(tracks, dict):
        return out
    for track, body in tracks.items():
        if not isinstance(body, dict):
            continue
        for status in ("open", "closed"):
            for pos in (body.get(status) or []):
                if isinstance(pos, dict):
                    out.append((track, pos, status))
    return out


def _positions_of_portfolio(obj):
    """То же для positions.json — основной бумажный портфель, дорожка A0."""
    out = []
    if not isinstance(obj, dict):
        return out
    for status in ("open", "closed"):
        for pos in (obj.get(status) or []):
            if isinstance(pos, dict):
                out.append(("A0", pos, status))
    return out


def all_positions(tournament_obj, positions_obj):
    """Все позиции обоих реестров одним списком: (track, pos, status)."""
    return _positions_of_tournament(tournament_obj) + \
        _positions_of_portfolio(positions_obj)


def last_ledger_day(positions):
    """Самая поздняя дата, записанная в реестр (вход или закрытие).

    Это и есть «возраст содержимого» реестра: mtime после git checkout врёт,
    а дата входа/закрытия — факт, переживающий любую выкладку."""
    days = []
    for _track, pos, _status in positions:
        for key in ("entry_date", "close_date"):
            day = parse_day(pos.get(key))
            if day is not None:
                days.append(day)
    return max(days) if days else None


# =============================================================================
# Проверки П1..П6
# =============================================================================

def _check(cid, title, status, reasons, details):
    return {"id": cid, "title": title, "status": status,
            "reasons": list(reasons), "details": details}


def check_inputs(ctx, now):
    """П1: входные файлы — есть, не пустые, разбираются, не протухли."""
    reasons, files = [], {}
    status = PASS

    def worse(cur, new):
        order = {PASS: 0, WARN: 1, FAIL: 2}
        return new if order[new] > order[cur] else cur

    for name in INPUT_FILES:
        facts = file_facts(os.path.join(ctx["repo_dir"], name), now)
        info = dict(facts)
        info["parse_error"] = None
        info["content_age_days"] = None
        info["age_source"] = None
        info["judged_age_days"] = None
        info["status"] = PASS

        if not facts["exists"]:
            info["status"] = FAIL
            reasons.append("П1 %s: файла нет (размер 0 байт)" % name)
            files[name] = info
            status = worse(status, FAIL)
            continue
        if facts["size_bytes"] == 0:
            info["status"] = FAIL
            reasons.append("П1 %s: нулевой размер (0 байт)" % name)
            files[name] = info
            status = worse(status, FAIL)
            continue

        # разбор + возраст по содержимому
        if name == "candidates_log.jsonl":
            log = ctx["log"]
            if log["error"]:
                info["parse_error"] = log["error"]
            runs = log["runs"]
            last = runs[-1] if runs else None
            if last and last["ts_dt"]:
                info["content_age_days"] = age_days_ts(last["ts_dt"], now)
                info["age_source"] = "последний _run.timestamp_utc"
            info["runs_n"] = len(runs)
            warn_days, fail_days = LOG_AGE_WARN_DAYS, LOG_AGE_FAIL_DAYS
            if log["bad_lines"]:
                info["status"] = worse(info["status"], WARN)
                reasons.append("П1 %s: %d нечитаемых строк из %d"
                               % (name, log["bad_lines"], log["total_lines"]))
            if not runs:
                info["status"] = FAIL
                reasons.append("П1 %s: в журнале 0 прогонов при размере %d байт"
                               % (name, facts["size_bytes"]))
        elif name in ("tournament.json", "positions.json"):
            obj = ctx["tournament"] if name == "tournament.json" else ctx["positions"]
            err = ctx["tournament_err"] if name == "tournament.json" else ctx["positions_err"]
            if err:
                info["parse_error"] = err
                info["status"] = FAIL
                reasons.append("П1 %s: не разбирается (%s), размер %d байт"
                               % (name, err, facts["size_bytes"]))
            positions = (_positions_of_tournament(obj) if name == "tournament.json"
                         else _positions_of_portfolio(obj))
            info["positions_n"] = len(positions)
            day = last_ledger_day(positions)
            if day is not None:
                info["content_age_days"] = age_days_day(day, now)
                info["age_source"] = "последняя entry_date/close_date (%s)" % day.isoformat()
            elif info["status"] != FAIL:
                info["status"] = worse(info["status"], WARN)
                reasons.append("П1 %s: 0 позиций с датами — возраст содержимого "
                               "определить не по чему" % name)
            warn_days, fail_days = LEDGER_AGE_WARN_DAYS, LEDGER_AGE_FAIL_DAYS
        else:  # config.yaml
            # ВОЗРАСТ КОНФИГА НЕ ПРОВЕРЯЕТСЯ СОЗНАТЕЛЬНО: параметры заморожены
            # до 15.11.2026 (screener_tournament_rules_v1.md), свежий config.yaml
            # означал бы нарушение заморозки, а не здоровье. Проверяем только
            # что файл на месте, не пуст и разбирается.
            warn_days = fail_days = None
            if _yaml is None:
                info["parse_error"] = "PyYAML недоступен, содержимое не проверено"
                info["status"] = worse(info["status"], WARN)
                reasons.append("П1 %s: PyYAML недоступен, разбор пропущен "
                               "(размер %d байт)" % (name, facts["size_bytes"]))
            else:
                try:
                    with open(os.path.join(ctx["repo_dir"], name), encoding="utf-8") as fh:
                        cfg = _yaml.safe_load(fh)
                    if not isinstance(cfg, dict) or not cfg:
                        info["status"] = FAIL
                        reasons.append("П1 %s: разобран, но верхний уровень не "
                                       "словарь параметров (ключей: %d)"
                                       % (name, len(cfg) if hasattr(cfg, "__len__") else 0))
                    else:
                        info["keys_n"] = len(cfg)
                except Exception as exc:
                    info["parse_error"] = str(exc)
                    info["status"] = FAIL
                    reasons.append("П1 %s: YAML не разбирается (%s), размер %d байт"
                                   % (name, str(exc)[:120], facts["size_bytes"]))

        # судим возраст: по содержимому, если оно его даёт; иначе mtime, но
        # mtime сам по себе НИКОГДА не даёт FAIL (см. шапку про checkout).
        if warn_days is not None:
            if info["content_age_days"] is not None:
                age, by_content = info["content_age_days"], True
            else:
                age, by_content = info["mtime_age_days"], False
                info["age_source"] = info["age_source"] or "mtime (содержимое без дат)"
            info["judged_age_days"] = age
            if age is not None:
                if by_content and age > fail_days:
                    info["status"] = worse(info["status"], FAIL)
                    reasons.append("П1 %s: последняя запись %.2f дн назад "
                                   "(порог FAIL > %d дн, прогоны пн/чт)"
                                   % (name, age, fail_days))
                elif age > warn_days:
                    info["status"] = worse(info["status"], WARN)
                    reasons.append("П1 %s: последняя запись %.2f дн назад "
                                   "(порог WARN > %d дн, прогоны пн/чт)"
                                   % (name, age, warn_days))

        files[name] = info
        status = worse(status, info["status"])

    details = {"files": files,
               "thresholds": {"log_warn_days": LOG_AGE_WARN_DAYS,
                              "log_fail_days": LOG_AGE_FAIL_DAYS,
                              "ledger_warn_days": LEDGER_AGE_WARN_DAYS,
                              "ledger_fail_days": LEDGER_AGE_FAIL_DAYS,
                              "config_age_checked": False}}
    return _check("П1", "входные файлы: наличие, размер, возраст", status, reasons, details)


def check_run_size(ctx, now):
    """П2: размер последнего прогона против медианы последних 10."""
    runs = ctx["log"]["runs"]
    details = {"last_rows": None, "median_rows": None, "ratio": None,
               "history_n": 0, "window": MEDIAN_WINDOW,
               "warn_below": SHRINK_WARN, "fail_below": SHRINK_FAIL}
    if not runs:
        return _check("П2", "размер последнего прогона журнала", FAIL,
                      ["П2 прогонов в журнале 0 — сверять нечего"], details)

    last = runs[-1]
    details["last_rows"] = last["rows_n"]
    details["last_run_ts"] = last["ts"]
    if last["rows_n"] == 0:
        return _check("П2", "размер последнего прогона журнала", FAIL,
                      ["П2 последний прогон пуст: 0 строк"], details)

    history = [r["rows_n"] for r in runs[:-1]][-MEDIAN_WINDOW:]
    details["history_n"] = len(history)
    if len(history) < MEDIAN_MIN_RUNS:
        return _check("П2", "размер последнего прогона журнала", PASS,
                      [], details)

    med = statistics.median(history)
    details["median_rows"] = med
    ratio = last["rows_n"] / med if med else None
    details["ratio"] = round(ratio, 4) if ratio is not None else None
    if not med:
        return _check("П2", "размер последнего прогона журнала", WARN,
                      ["П2 медиана %d прошлых прогонов = 0 строк" % len(history)],
                      details)
    if ratio < SHRINK_FAIL:
        return _check("П2", "размер последнего прогона журнала", FAIL,
                      ["П2 прогон ссохся: %d строк против медианы %.0f "
                       "по %d прогонам = %.0f%% (порог FAIL < %.0f%%)"
                       % (last["rows_n"], med, len(history), ratio * 100,
                          SHRINK_FAIL * 100)], details)
    if ratio < SHRINK_WARN:
        return _check("П2", "размер последнего прогона журнала", WARN,
                      ["П2 прогон меньше нормы: %d строк против медианы %.0f "
                       "по %d прогонам = %.0f%% (порог WARN < %.0f%%)"
                       % (last["rows_n"], med, len(history), ratio * 100,
                          SHRINK_WARN * 100)], details)
    return _check("П2", "размер последнего прогона журнала", PASS, [], details)


def check_funnel(ctx, now):
    """П3: воронка отказов последнего прогона.

    Схлопывание до одной причины = FAIL: значит сито не разбирает вселенную, а
    отбрасывает её целиком по одному признаку (отвалился источник перпов,
    порог съел всё). Такой прогон в турнир попадать не должен."""
    runs = ctx["log"]["runs"]
    details = {"reasons": {}, "reasons_n": 0, "rows_n": 0,
               "top_reason": None, "top_share": None,
               "min_reasons": FUNNEL_MIN_REASONS,
               "concentration_warn": FUNNEL_CONCENTRATION_WARN}
    if not runs:
        return _check("П3", "воронка отказов последнего прогона", FAIL,
                      ["П3 прогонов в журнале 0 — воронку считать не из чего"],
                      details)
    last = runs[-1]
    reasons_map = dict(last["reasons"])
    rows_n = last["rows_n"]
    details["reasons"] = reasons_map
    details["reasons_n"] = len(reasons_map)
    details["rows_n"] = rows_n
    details["last_run_ts"] = last["ts"]
    if rows_n == 0:
        return _check("П3", "воронка отказов последнего прогона", FAIL,
                      ["П3 в последнем прогоне 0 строк — воронки нет"], details)

    top_name, top_n = max(reasons_map.items(), key=lambda kv: kv[1])
    share = top_n / rows_n
    details["top_reason"] = top_name
    details["top_share"] = round(share, 4)
    details["shares"] = {k: round(v / rows_n, 4) for k, v in reasons_map.items()}

    if len(reasons_map) < FUNNEL_MIN_REASONS:
        return _check("П3", "воронка отказов последнего прогона", FAIL,
                      ["П3 воронка схлопнулась: 1 причина на весь прогон "
                       "(%s=%d из %d строк, 100%%)" % (top_name, top_n, rows_n)],
                      details)
    if share >= FUNNEL_CONCENTRATION_WARN:
        return _check("П3", "воронка отказов последнего прогона", WARN,
                      ["П3 воронка перекошена: %s=%d из %d строк (%.1f%%), "
                       "порог WARN >= %.0f%%; всего причин %d"
                       % (top_name, top_n, rows_n, share * 100,
                          FUNNEL_CONCENTRATION_WARN * 100, len(reasons_map))],
                      details)
    return _check("П3", "воронка отказов последнего прогона", PASS, [], details)


def check_artifacts(ctx, now):
    """П4: metrics_latest.json свежий, verdict_latest.json читается.

    Отсутствие артефакта — ЖЁЛТЫЙ, а не красный: блоки метрик и вердикта
    считаются отдельно и до первого прогона их файлов просто нет. Красный тут
    означал бы «стой, конвейер сломан» на ровном месте. А вот БИТЫЙ артефакт —
    красный: молча увезти в отчёт число из полуразобранного файла хуже, чем
    остановиться."""
    details = {"metrics": {}, "verdict": {},
               "thresholds": {"metrics_warn_days": METRICS_AGE_WARN_DAYS,
                              "metrics_fail_days": METRICS_AGE_FAIL_DAYS}}
    reasons, status = [], PASS

    def worse(cur, new):
        order = {PASS: 0, WARN: 1, FAIL: 2}
        return new if order[new] > order[cur] else cur

    # --- metrics_latest.json
    mpath = os.path.join(ctx["repo_dir"], ARTIFACTS_DIR, METRICS_LATEST)
    mfacts = file_facts(mpath, now)
    m = {"exists": mfacts["exists"], "size_bytes": mfacts["size_bytes"],
         "age_days": None, "age_source": None, "error": None}
    if not mfacts["exists"]:
        m["error"] = "файла нет"
        status = worse(status, WARN)
        reasons.append("П4 artifacts/%s не создан (0 файлов метрик) — "
                       "блок метрик ещё не прогонялся" % METRICS_LATEST)
    elif ctx["metrics_err"]:
        m["error"] = ctx["metrics_err"]
        status = worse(status, FAIL)
        reasons.append("П4 artifacts/%s не читается: %s (размер %d байт)"
                       % (METRICS_LATEST, ctx["metrics_err"], mfacts["size_bytes"]))
    else:
        obj = ctx["metrics"] or {}
        gen = None
        if isinstance(obj.get("provenance"), dict):
            gen = parse_ts(obj["provenance"].get("generated_utc"))
        if gen is None:
            gen = parse_ts(obj.get("generated_utc"))
        if gen is not None:
            m["age_days"] = age_days_ts(gen, now)
            m["age_source"] = "provenance.generated_utc"
        else:
            m["age_days"] = mfacts["mtime_age_days"]
            m["age_source"] = "mtime (в артефакте нет generated_utc)"
            status = worse(status, WARN)
            reasons.append("П4 artifacts/%s без штампа generated_utc "
                           "(размер %d байт) — возраст считаем по mtime"
                           % (METRICS_LATEST, mfacts["size_bytes"]))
        age = m["age_days"]
        if age is not None:
            if m["age_source"] == "provenance.generated_utc" and age > METRICS_AGE_FAIL_DAYS:
                status = worse(status, FAIL)
                reasons.append("П4 artifacts/%s протух: %.2f дн (порог FAIL > %d дн)"
                               % (METRICS_LATEST, age, METRICS_AGE_FAIL_DAYS))
            elif age > METRICS_AGE_WARN_DAYS:
                status = worse(status, WARN)
                reasons.append("П4 artifacts/%s несвежий: %.2f дн (порог WARN > %d дн)"
                               % (METRICS_LATEST, age, METRICS_AGE_WARN_DAYS))
    details["metrics"] = m

    # --- verdict_latest.json (возраст не судим: вердикт считается по требованию,
    #     первый настоящий — 15.11.2026; важно только, что файл читается)
    vpath = os.path.join(ctx["repo_dir"], ARTIFACTS_DIR, VERDICT_LATEST)
    vfacts = file_facts(vpath, now)
    v = {"exists": vfacts["exists"], "size_bytes": vfacts["size_bytes"],
         "readable": False, "error": None}
    if not vfacts["exists"]:
        v["error"] = "файла нет"
        status = worse(status, WARN)
        reasons.append("П4 artifacts/%s не создан (0 файлов вердикта) — "
                       "вердикт ещё не считался" % VERDICT_LATEST)
    elif ctx["verdict_err"]:
        v["error"] = ctx["verdict_err"]
        status = worse(status, FAIL)
        reasons.append("П4 artifacts/%s не читается: %s (размер %d байт)"
                       % (VERDICT_LATEST, ctx["verdict_err"], vfacts["size_bytes"]))
    else:
        v["readable"] = True
        v["keys_n"] = len(ctx["verdict"]) if isinstance(ctx["verdict"], dict) else 0
    details["verdict"] = v

    return _check("П4", "артефакты метрик и вердикта", status, reasons, details)


def check_open_ages(ctx, now):
    """П5: открытые позиции старше порога закрытия.

    Ловит ровно одно: закрытие не сработало. Пока такая позиция висит открытой,
    её результат не попадает ни в метрики, ни в вердикт — то есть турнир тихо
    теряет наблюдения там, где их и так мало."""
    positions = all_positions(ctx["tournament"], ctx["positions"])
    open_pos = [(t, p) for t, p, s in positions if s == "open"]
    details = {"open_n": len(open_pos), "max_age_days": None,
               "over_warn_n": 0, "over_fail_n": 0, "worst": None,
               "miss_over_n": 0,
               "thresholds": {"close_age_days": POSITION_CLOSE_AGE_DAYS,
                              "warn_days": POSITION_STALE_WARN_DAYS,
                              "fail_days": POSITION_STALE_FAIL_DAYS,
                              "miss_warn": POSITION_MISS_WARN}}
    reasons, status = [], PASS
    if not open_pos:
        return _check("П5", "возраст открытых позиций", PASS,
                      [], details)

    aged = []
    for track, pos in open_pos:
        day = parse_day(pos.get("entry_date"))
        if day is None:
            continue
        aged.append((age_days_day(day, now), track, pos.get("sym"), pos.get("entry_date")))
    if not aged:
        details["max_age_days"] = None
        return _check("П5", "возраст открытых позиций", WARN,
                      ["П5 у всех %d открытых позиций нет разбираемой entry_date"
                       % len(open_pos)], details)

    aged.sort(reverse=True)
    details["max_age_days"] = aged[0][0]
    details["worst"] = {"track": aged[0][1], "sym": aged[0][2],
                        "entry_date": aged[0][3], "age_days": aged[0][0]}
    over_fail = [a for a in aged if a[0] > POSITION_STALE_FAIL_DAYS]
    over_warn = [a for a in aged if a[0] > POSITION_STALE_WARN_DAYS]
    details["over_fail_n"] = len(over_fail)
    details["over_warn_n"] = len(over_warn)

    if over_fail:
        status = FAIL
        reasons.append("П5 закрытие не сработало: %d открытых позиций старше "
                       "%d дн, худшая %s/%s = %d дн (вход %s, правило закрытия "
                       ">= %d дн)"
                       % (len(over_fail), POSITION_STALE_FAIL_DAYS,
                          over_fail[0][1], over_fail[0][2], over_fail[0][0],
                          over_fail[0][3], POSITION_CLOSE_AGE_DAYS))
    elif over_warn:
        status = WARN
        reasons.append("П5 закрытие опаздывает: %d открытых позиций старше "
                       "%d дн, худшая %s/%s = %d дн (порог FAIL > %d дн)"
                       % (len(over_warn), POSITION_STALE_WARN_DAYS,
                          over_warn[0][1], over_warn[0][2], over_warn[0][0],
                          POSITION_STALE_FAIL_DAYS))

    # miss >= 3 у открытой позиции — тот же дефект другим путём
    miss_over = [(t, p) for t, p in open_pos
                 if isinstance(p.get("miss"), int) and p["miss"] >= POSITION_MISS_WARN]
    details["miss_over_n"] = len(miss_over)
    if miss_over:
        worst = max(miss_over, key=lambda tp: tp[1]["miss"])
        if status == PASS:
            status = WARN
        reasons.append("П5 %d открытых позиций с miss >= %d (закрытие по "
                       "правилу miss >= %d не сработало), худшая %s/%s: miss=%d"
                       % (len(miss_over), POSITION_MISS_WARN, POSITION_MISS_CLOSE,
                          worst[0], worst[1].get("sym"), worst[1]["miss"]))

    return _check("П5", "возраст открытых позиций", status, reasons, details)


def check_rules_hash(ctx, now):
    """П6: хеш правил против записанного в metrics_latest.json.

    Правила турнира — предрегистрация: способ счёта зафиксирован ДО того, как
    стал известен исход. Расхождение хешей означает, что артефакт посчитан по
    ДРУГОЙ редакции правил, чем лежит сейчас в репозитории; публиковать такое
    число нельзя."""
    rules_path = os.path.join(ctx["repo_dir"], RULES_FILENAME)
    details = {"rules_file": RULES_FILENAME, "rules_exists": os.path.isfile(rules_path),
               "sha256_now": None, "sha256_in_metrics": None, "crlf": None}
    reasons, status = [], PASS

    if details["rules_exists"]:
        details["sha256_now"] = _sha256_file(rules_path)
        try:
            with open(rules_path, "rb") as fh:
                details["crlf"] = b"\r\n" in fh.read()
        except OSError:
            details["crlf"] = None

    metrics = ctx["metrics"] if isinstance(ctx["metrics"], dict) else None
    recorded = metrics.get("rules_sha256") if metrics else None
    if isinstance(recorded, str) and recorded.strip():
        details["sha256_in_metrics"] = recorded.strip().lower()

    now_sha, rec_sha = details["sha256_now"], details["sha256_in_metrics"]

    if not details["rules_exists"]:
        if rec_sha:
            # Файл, на который сослался артефакт, исчез — это уже не «ещё не
            # сделано», а пропажа заякоренного документа.
            return _check("П6", "хеш замороженных правил", FAIL,
                          ["П6 файла правил %s нет, а в metrics_latest.json "
                           "записан хеш %s… (16 симв) — заякоренный документ пропал"
                           % (RULES_FILENAME, rec_sha[:16])], details)
        return _check("П6", "хеш замороженных правил", WARN,
                      ["П6 файла правил %s нет (0 байт), сверять нечего"
                       % RULES_FILENAME], details)

    if now_sha is None:
        return _check("П6", "хеш замороженных правил", WARN,
                      ["П6 хеш правил не посчитан: файл %s не читается"
                       % RULES_FILENAME], details)

    if rec_sha is None:
        where = ("в artifacts/%s нет поля rules_sha256" % METRICS_LATEST
                 if metrics is not None
                 else "artifacts/%s ещё не создан" % METRICS_LATEST)
        return _check("П6", "хеш замороженных правил", WARN,
                      ["П6 %s — сверять не с чем; хеш файла правил сейчас "
                       "%s… (16 симв из 64)" % (where, now_sha[:16])], details)

    if rec_sha != now_sha:
        line = ("П6 РАСХОЖДЕНИЕ хеша правил: файл %s…, артефакт %s… "
                "(16 симв, различаются)" % (now_sha[:16], rec_sha[:16]))
        if details["crlf"]:
            line += ("; в файле есть CRLF — на ubuntu-раннере тот же текст "
                     "с LF даёт другой хеш, проверить .gitattributes")
        return _check("П6", "хеш замороженных правил", FAIL, [line], details)

    return _check("П6", "хеш замороженных правил", PASS, [], details)


def _sha256_file(path):
    """sha256 файла. Считаем через rules_hash (единственный источник правды по
    C3), а при его отсутствии — тем же алгоритмом из stdlib."""
    if _rules_hash is not None:
        try:
            value = _rules_hash.rules_sha256_or_none(path)
            if value:
                return value
        except Exception:
            pass
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# =============================================================================
# Сборка светофора
# =============================================================================

def _stamp():
    """Штамп происхождения. Без provenance.py — минимальный честный аналог."""
    if _provenance is not None:
        try:
            return _provenance.stamp(TOOL_VERSION)
        except Exception:
            pass
    v = sys.version_info
    return {"git_sha": os.getenv("GITHUB_SHA", "unknown")[:12] or "unknown",
            "generated_utc": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool_version": TOOL_VERSION,
            "python": "%d.%d.%d" % (v.major, v.minor, v.micro)}


def gather(repo_dir, now):
    """Один проход по диску: читаем всё, что нужно проверкам, ровно по разу."""
    repo_dir = os.path.abspath(repo_dir)
    tournament, t_err = read_json(os.path.join(repo_dir, "tournament.json"))
    positions, p_err = read_json(os.path.join(repo_dir, "positions.json"))
    metrics, m_err = read_json(os.path.join(repo_dir, ARTIFACTS_DIR, METRICS_LATEST))
    verdict, v_err = read_json(os.path.join(repo_dir, ARTIFACTS_DIR, VERDICT_LATEST))
    return {
        "repo_dir": repo_dir,
        "log": scan_log(os.path.join(repo_dir, "candidates_log.jsonl")),
        "tournament": tournament, "tournament_err": t_err,
        "positions": positions, "positions_err": p_err,
        "metrics": metrics, "metrics_err": m_err,
        "verdict": verdict, "verdict_err": v_err,
    }


def run_sanity(repo_dir=".", now=None):
    """Полный светофор. -> словарь результата (он же артефакт).

    now — момент, относительно которого считается возраст (для тестов);
    по умолчанию текущий UTC.
    """
    now = now or utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ctx = gather(repo_dir, now)

    checks = [
        check_inputs(ctx, now),
        check_run_size(ctx, now),
        check_funnel(ctx, now),
        check_artifacts(ctx, now),
        check_open_ages(ctx, now),
        check_rules_hash(ctx, now),
    ]

    n_fail = sum(1 for c in checks if c["status"] == FAIL)
    n_warn = sum(1 for c in checks if c["status"] == WARN)
    verdict = RED if n_fail else (YELLOW if n_warn else GREEN)
    reasons = [r for c in checks for r in c["reasons"]]

    return {
        "provenance": _stamp(),
        "verdict": verdict,
        "counts": {"pass": len(checks) - n_fail - n_warn,
                   "warn": n_warn, "fail": n_fail},
        "reasons": reasons,
        "checks": checks,
        "repo_dir": ctx["repo_dir"],
        "as_of_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": ("Светофор конвейера скринера (E1). GREEN/YELLOW -> код 0, "
                 "RED -> код 1. Возраст считается по содержимому файлов, "
                 "mtime сам по себе FAIL не даёт."),
    }


# =============================================================================
# Артефакты
# =============================================================================

def _clean_cell(text):
    """Одна ячейка csv: без переводов строк и точек с запятой в качестве
    разделителя истории — иначе строка истории разъедется."""
    return " ".join(str(text).replace(";", ",").split())


def write_artifacts(result, repo_dir="."):
    """Пишет artifacts/sanity_latest.json (перезапись) и ДОПИСЫВАЕТ
    artifacts/sanity_history.csv. Это единственные два файла, которые светофор
    имеет право создавать."""
    art_dir = os.path.join(os.path.abspath(repo_dir), ARTIFACTS_DIR)
    os.makedirs(art_dir, exist_ok=True)
    latest = os.path.join(art_dir, SANITY_LATEST)
    with open(latest, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")

    history = os.path.join(art_dir, SANITY_HISTORY)
    need_header = (not os.path.exists(history)) or os.path.getsize(history) == 0
    by_id = {c["id"]: c["status"] for c in result["checks"]}
    p1 = result["checks"][0]["details"]["files"].get("candidates_log.jsonl", {})
    p2 = result["checks"][1]["details"]
    p3 = result["checks"][2]["details"]
    p5 = result["checks"][4]["details"]
    row = [
        result["provenance"]["generated_utc"],
        result["verdict"],
        result["counts"]["fail"],
        result["counts"]["warn"],
        by_id.get("П1", ""), by_id.get("П2", ""), by_id.get("П3", ""),
        by_id.get("П4", ""), by_id.get("П5", ""), by_id.get("П6", ""),
        "" if p1.get("judged_age_days") is None else p1["judged_age_days"],
        "" if p2.get("last_rows") is None else p2["last_rows"],
        "" if p2.get("ratio") is None else p2["ratio"],
        p3.get("reasons_n", ""),
        "" if p5.get("max_age_days") is None else p5["max_age_days"],
        result["provenance"]["git_sha"],
        _clean_cell("; ".join(result["reasons"])) if result["reasons"] else "",
    ]
    with open(history, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        if need_header:
            writer.writerow(HISTORY_HEADER)
        writer.writerow(row)
    return {"latest": latest, "history": history}


# =============================================================================
# CLI
# =============================================================================

def format_report(result):
    """Человекочитаемая сводка — то, что читает пилот в логе Actions."""
    lines = []
    lines.append("СВЕТОФОР КОНВЕЙЕРА СКРИНЕРА (E1)")
    lines.append("=" * 60)
    lines.append("момент проверки: %s | git_sha: %s | python %s"
                 % (result["as_of_utc"], result["provenance"]["git_sha"],
                    result["provenance"]["python"]))
    lines.append("каталог: %s" % result["repo_dir"])
    lines.append("-" * 60)
    for c in result["checks"]:
        mark = {PASS: "OK  ", WARN: "WARN", FAIL: "FAIL"}[c["status"]]
        lines.append("[%s] %s %s" % (mark, c["id"], c["title"]))
        for r in c["reasons"]:
            lines.append("       !! %s" % r)
    lines.append("-" * 60)
    lines.append("ИТОГ: %s (FAIL=%d, WARN=%d, PASS=%d)"
                 % (result["verdict"], result["counts"]["fail"],
                    result["counts"]["warn"], result["counts"]["pass"]))
    if result["verdict"] == RED:
        lines.append("RED: работы по пакету не начинать, пока причина не устранена.")
    elif result["verdict"] == YELLOW:
        lines.append("YELLOW: работать можно, причины выше держать в виду.")
    else:
        lines.append("GREEN: конвейер здоров по всем шести проверкам.")
    return "\n".join(lines)


def summary_line(result):
    """Одна строка итога — её же вставляет в свою сводку check_log.py."""
    head = "САНИТИ %s (FAIL=%d, WARN=%d) на %s" % (
        result["verdict"], result["counts"]["fail"],
        result["counts"]["warn"], result["as_of_utc"])
    if result["reasons"]:
        head += ": " + "; ".join(result["reasons"][:3])
        if len(result["reasons"]) > 3:
            head += " (ещё %d)" % (len(result["reasons"]) - 3)
    return head


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Светофор конвейера скринера (E1): П1..П6, GREEN/YELLOW/RED.")
    parser.add_argument("--repo", default=os.path.dirname(os.path.abspath(__file__)),
                        help="каталог репозитория (по умолчанию — каталог скрипта)")
    parser.add_argument("--as-of", default=None, metavar="YYYY-MM-DD",
                        help="считать возраст на указанную дату (для разбора истории)")
    parser.add_argument("--no-write", action="store_true",
                        help="не писать artifacts/sanity_latest.json и историю")
    args = parser.parse_args(argv)

    now = None
    if args.as_of:
        day = parse_day(args.as_of)
        if day is None:
            print("ОШИБКА: --as-of не разбирается как YYYY-MM-DD: %s" % args.as_of)
            return 1
        now = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) \
            + timedelta(hours=12)

    result = run_sanity(args.repo, now=now)
    print(format_report(result))
    if not args.no_write:
        paths = write_artifacts(result, args.repo)
        print("артефакты: %s | %s" % (paths["latest"], paths["history"]))
    else:
        print("артефакты не писались (--no-write)")
    return 1 if result["verdict"] == RED else 0


if __name__ == "__main__":
    raise SystemExit(main())

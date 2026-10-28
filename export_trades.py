#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""export_trades.py — блок D3 пакета СКРИНЕР-ПОЛНЫЙ: единая схема экспорта.

ЗАЧЕМ ЭТО НУЖНО ДЛЯ ДЕНЕГ
Соседний проект (stage0, DEX ML-система) считает свои цифры по СВОЕЙ схеме
сделок. Пока скринер отдаёт `tournament.json` и `positions.json` в своих
внутренних форматах, каждая сверка двух проектов — это ручной разбор двух
структур, то есть время пилота и место для ошибки. Единая csv-схема убирает
разбор: обе стороны читают один и тот же заголовок.

ЧТО ПИШЕТСЯ
1) Сделки (турнир A1..A8 + бумажный портфель A0):
       artifacts/YYYY-MM/trades_YYYYMMDD.csv
       artifacts/trades_latest.csv
   Схема по контракту: token, ts, signal, side, entry, exit, pnl_gross,
   pnl_net, status — плюс колонка cg_id ПОСЛЕДНЕЙ (тикеры не уникальны, это
   канон обоих проектов; cg_id дописан в хвост, чтобы девять контрактных
   колонок стояли в контрактном порядке и читались позиционно).
2) Дубль недельного экспорта потоков:
       export/flows_latest.json  ->  export/flows_latest.csv
   Схема: ticker, cg_id, run_timestamp, score, mcap_usd, price_usd,
   entered_paper, chain, contract. ОДНА строка на пару сеть-контракт;
   монета без контрактов даёт одну строку с пустыми chain/contract.

ЧЕСТНЫЕ ОГОВОРКИ (читать перед тем, как считать по этим csv)
* На 01.09.2026 закрытых сделок НОЛЬ. Значит pnl_gross/pnl_net пусты у всех
  строк, и это не дефект экспорта, а факт состояния турнира. Пустой вход даёт
  валидный csv из одного заголовка, а не падение.
* Для ОТКРЫТЫХ позиций pnl не пишется НИКОГДА (ни gross, ни net) — §2
  замороженных правил: незакрытая прибыль в отчётность не идёт ни в каком
  виде. Колонка exit у открытых тоже пуста.
* `side` всегда 'long': бумажные сделки турнира — спотовая покупка, шорта в
  дизайне нет. Колонка есть только ради совместимости со схемой потребителя.
* Дата выхода (`exit_date`) в девятиколоночную схему НЕ ВХОДИТ — её там просто
  нет. Кому нужен срок удержания, берёт его из artifacts/metrics_latest.json.
* Провенанс (git_sha, generated_utc) в csv не втиснуть, не сломав заголовок,
  поэтому он лежит РЯДОМ, в файле-спутнике `<имя>.meta.json`. Csv без своего
  meta-спутника — файл неизвестного происхождения, доверять ему нельзя.

БЕЗОПАСНОСТЬ: CSV-ИНЪЕКЦИЯ
token, cg_id, ticker, chain, contract приходят из внешних данных (CoinGecko),
а мы их пишем в файл, который пилот открывает в Excel. Ячейка, начинающаяся с
'=', '+', '-' или '@', в Excel/LibreOffice исполняется как ФОРМУЛА — то есть
чужое имя монеты становится кодом на машине пилота. Поэтому такие ведущие
символы гасим апострофом (`_defuse_formula`), как это давно делает соседний
unlocks_logger.py (там `csv_clean`).
ГРАНИЦА, которую держим отдельно: ЧИСЛА НЕ ТРОГАЕМ. Отрицательный pnl
'-12.3' тоже начинается с '-', и апостроф превратил бы его в текст — колонка
pnl_net перестала бы складываться, а это порча измерения ради безопасности.
Гасим только то, что числом не является (см. `_is_number_text`).

ЗАВИСИМОСТИ: только stdlib + provenance.py (+ trades.py, импорт отложенный).
Запуск: python export_trades.py  [--repo .]  [--as-of YYYY-MM-DD]
"""

import argparse
import csv
import hashlib
import json
import os
import re
from datetime import date, datetime, timezone

import provenance

HERE = os.path.dirname(os.path.abspath(__file__))

# --- контрактные схемы: менять НЕЛЬЗЯ без согласования с обоими проектами ---

# Девять колонок контракта + cg_id в хвосте (см. шапку).
TRADES_HEADER = [
    "token", "ts", "signal", "side", "entry", "exit",
    "pnl_gross", "pnl_net", "status", "cg_id",
]

FLOWS_HEADER = [
    "ticker", "cg_id", "run_timestamp", "score", "mcap_usd", "price_usd",
    "entered_paper", "chain", "contract",
]

# --- контракт flows_latest.json для stage0 (С-26, 03.09.2026) ---------------
# Версия схемы САМОГО json: ключа schema_version в файле нет (его пишет
# export_flows.py из замороженного конвейера), поэтому версия объявляется
# ЗДЕСЬ — по результату проверки формы — и уходит в flows_latest.meta.json
# вместе с sha256 файла. Новая форма (новый/пропавший ключ, другой тип) =
# новая версия и правка validate_flows; до тех пор source_valid=false
# честно говорит потребителю «форма не та, не забирай». Полный контракт —
# STATE.md §11.
FLOWS_SCHEMA_VERSION = 1
FLOWS_TOP_REQUIRED = ("generated_utc", "top_contracts", "blacklist")
FLOWS_TOP_OPTIONAL = ("source", "week_runs", "note")
FLOWS_COIN_KEYS = ("ticker", "cg_id", "run_timestamp", "score", "mcap_usd",
                   "price_usd", "entered_paper", "contracts")
FLOWS_BLACKLIST_KEYS = ("reason", "event_date")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Спотовая покупка — единственная сторона сделки в дизайне турнира.
SIDE_SPOT_LONG = "long"

# Допустимые статусы. Всё остальное — повод не угадывать, а пометить строку.
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"

# Символы, с которых Excel/LibreOffice начинают читать ячейку как ФОРМУЛУ.
FORMULA_LEAD_CHARS = ("=", "+", "-", "@")

# Символы, из которых состоит запись числа. Всё остальное числом не является,
# даже если float() это проглотит (см. _is_number_text).
_NUMBER_CHARS = "0123456789+-.eE"


# --- мелкие помощники форматирования ------------------------------------

def _coerce_date(value):
    """date | 'YYYY-MM-DD' | None -> date | None. Мусор -> ValueError."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()


def _today(as_of=None):
    """Дата, которой датируется артефакт. По умолчанию — сегодня по UTC."""
    d = _coerce_date(as_of)
    return d if d is not None else datetime.now(timezone.utc).date()


def _is_number_text(text):
    """Строка целиком читается как число? '-12.3' -> True, '=1+1' -> False.

    Своя проверка, а не голый float(): float() принимает 'nan', 'inf' и даже
    '1_0', а нам нужно ровно одно — «эту ячейку Excel покажет числом, гасить
    её нельзя». Поэтому сначала отбрасываем всё, где есть посторонний символ.
    """
    s = str(text).strip()
    if not s or any(ch not in _NUMBER_CHARS for ch in s):
        return False
    try:
        float(s)
    except (TypeError, ValueError):
        return False
    return True


def _defuse_formula(text):
    """Погасить ведущий '=', '+', '-', '@' апострофом. -> безопасная строка.

    Защита от CSV-инъекции: имя монеты вида '=cmd|...' Excel исполняет как
    формулу, а имена приходят из CoinGecko, то есть снаружи.
    ЧИСЛА НЕ ТРОГАЕМ: '-12.3' — это pnl, а не формула; апостроф сделал бы из
    него текст и сломал бы арифметику в колонке (см. шапку модуля).
    """
    if text[:1] not in FORMULA_LEAD_CHARS:
        return text
    if _is_number_text(text):
        return text
    return "'" + text


def _num(value):
    """Число в csv. None/NaN/inf -> пустая ячейка (пусто честнее нуля)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        # bool раньше int в проверках: True не должно превратиться в '1'.
        return "true" if value else "false"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        return repr(value)          # repr даёт кратчайшую запись без потери
    if isinstance(value, int):
        return str(value)
    # Сюда попадает НЕ число, пришедшее в числовую колонку (строка из чужого
    # json). Схлопываем пробелы, как в _txt, и гасим формулу: числом это уже
    # не является, а значит и портить нечего.
    return _defuse_formula(" ".join(str(value).split()))


def _txt(value):
    """Текст в csv: без переводов строк, чтобы строка csv осталась строкой.

    Плюс гашение ведущего '=', '+', '-', '@' — сюда идут token, cg_id,
    ticker, chain, contract, то есть чужой текст (см. шапку модуля).
    """
    if value is None:
        return ""
    return _defuse_formula(" ".join(str(value).split()))


def _ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def _write_csv(path, header, rows):
    """Запись csv с фиксированным переводом строки '\\n'.

    lineterminator задан явно: иначе csv на Windows пишет CRLF, файл лежит в
    git, и каждая выгрузка выглядит как полная переделка файла в diff.
    """
    _ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
    return path


def _write_meta(csv_path, extra=None):
    """Файл-спутник с провенансом рядом с csv.

    Имя: <csv без расширения>.meta.json. Провенанс обязан быть у каждого
    артефакта (правило E2), а в заголовок csv его не вписать, не сломав схему.
    """
    stem = os.path.splitext(os.path.abspath(csv_path))[0]
    meta_path = stem + ".meta.json"
    payload = {
        "provenance": provenance.stamp(),
        "csv_file": os.path.basename(csv_path),
        "note": ("Провенанс лежит отдельным файлом, потому что схема csv "
                 "зафиксирована контрактом и лишних колонок не допускает."),
        "note_csv_injection": (
            "Нечисловые поля, начинавшиеся с '=', '+', '-' или '@', записаны "
            "с ведущим апострофом: иначе Excel исполнил бы чужое имя монеты "
            "как формулу. Числа (в т.ч. отрицательный pnl) не тронуты."),
    }
    if extra:
        payload.update(extra)
    _ensure_dir(os.path.dirname(meta_path))
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    return meta_path


# --- сделки --------------------------------------------------------------

def _status_of(trade):
    """Статус сделки из записи trades.py, с защитой от чужого значения."""
    raw = (trade.get("status") or "").strip().lower()
    if raw in (STATUS_OPEN, STATUS_CLOSED):
        return raw
    # Статуса нет или он незнакомый — выводим его из наличия даты выхода,
    # но НИКОГДА не выдумываем 'closed' там, где выхода не видно.
    return STATUS_CLOSED if trade.get("exit_date") else STATUS_OPEN


def trade_row(trade):
    """Одна запись trades.load_all_trades -> строка контрактной схемы.

    Все обращения через .get(): экспорт не имеет права падать из-за
    отсутствующего поля в чужой записи — он для того и отделён от загрузчика.
    """
    status = _status_of(trade)
    closed = status == STATUS_CLOSED
    return [
        _txt(trade.get("sym")),                       # token
        _txt(trade.get("entry_date")),                # ts (дата входа, ISO)
        _txt(trade.get("track")),                     # signal (A0..A8)
        SIDE_SPOT_LONG,                               # side
        _num(trade.get("entry_price")),               # entry
        _num(trade.get("exit_price")) if closed else "",
        _num(trade.get("ret_gross_pct")) if closed else "",
        _num(trade.get("ret_net_pct")) if closed else "",
        status,
        _txt(trade.get("cg_id")),
    ]


def rows_from_trades(trades):
    """Список записей -> список строк csv, порядок детерминированный.

    Сортировка: дата входа, затем дорожка, затем токен. Детерминизм важен
    практически: файл лежит в git, и случайный порядок строк каждую неделю
    превращал бы diff в мусор, в котором не видно настоящих изменений.
    """
    rows = [trade_row(t) for t in (trades or [])]
    rows.sort(key=lambda r: (r[1], r[2], r[0], r[9]))
    return rows


def load_trades(repo_dir="."):
    """Сделки через общий загрузчик trades.py (импорт ОТЛОЖЕННЫЙ).

    trades.py пишет параллельный исполнитель. Импорт внутри функции, а не в
    шапке модуля, чтобы отсутствие соседнего файла не ломало ни импорт этого
    модуля, ни дубль flows в csv, ни недельный экспорт.

    -> (список сделок, строка статуса). Если модуля нет — ([], причина).
    """
    try:
        import trades as trades_mod           # noqa: PLC0415 — намеренно отложенный
    except Exception as exc:                  # ImportError и любые ошибки импорта
        return [], "trades.py недоступен (%s: %s)" % (type(exc).__name__, exc)
    try:
        data = trades_mod.load_all_trades(repo_dir)
    except TypeError:
        # Терпим сигнатуру без аргумента: контракт важнее буквы вызова.
        data = trades_mod.load_all_trades()
    except Exception as exc:
        return [], "trades.load_all_trades упал (%s: %s)" % (type(exc).__name__, exc)
    if not isinstance(data, list):
        return [], "trades.load_all_trades вернул %s вместо списка" % type(data).__name__
    return data, "ok"


def write_trades_csv(trades, repo_dir=".", as_of=None):
    """Записать сделки в artifacts/YYYY-MM/trades_YYYYMMDD.csv и trades_latest.csv.

    -> {'dated': путь, 'latest': путь, 'n_rows': int, 'n_closed': int,
        'n_open': int, 'header': [...]}
    Пустой список сделок — валидный csv из одного заголовка, не падение.
    """
    day = _today(as_of)
    art_dir = os.path.join(os.path.abspath(repo_dir), "artifacts")
    month_dir = os.path.join(art_dir, day.strftime("%Y-%m"))
    rows = rows_from_trades(trades)

    dated = os.path.join(month_dir, "trades_%s.csv" % day.strftime("%Y%m%d"))
    latest = os.path.join(art_dir, "trades_latest.csv")
    for path in (dated, latest):
        _write_csv(path, TRADES_HEADER, rows)

    n_closed = sum(1 for r in rows if r[8] == STATUS_CLOSED)
    info = {
        "dated": dated,
        "latest": latest,
        "n_rows": len(rows),
        "n_closed": n_closed,
        "n_open": len(rows) - n_closed,
        "header": list(TRADES_HEADER),
    }
    meta_extra = {
        "schema": list(TRADES_HEADER),
        "n_rows": info["n_rows"],
        "n_closed": info["n_closed"],
        "n_open": info["n_open"],
        "note_pnl": ("pnl_gross/pnl_net пусты у открытых позиций намеренно: "
                     "незакрытая прибыль в отчётность не идёт (§2 правил)."),
    }
    for path in (dated, latest):
        _write_meta(path, meta_extra)
    return info


# --- дубль недельного экспорта потоков -----------------------------------

def _is_num_or_none(value):
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool))


def validate_flows(data):
    """Проверка формы flows_latest.json по контракту версии FLOWS_SCHEMA_VERSION.

    -> {'schema_version': int|None, 'valid': bool, 'problems': [str, ...]}
    Ничего не бросает и ничего не чинит: потребитель (stage0) читает
    source_valid из meta и решает сам. Неизвестный ключ верхнего уровня —
    тоже проблема: он означает, что форма поменялась без новой версии.
    """
    problems = []
    if not isinstance(data, dict):
        return {"schema_version": None, "valid": False,
                "problems": ["корень не объект: %s" % type(data).__name__]}
    for key in FLOWS_TOP_REQUIRED:
        if key not in data:
            problems.append("нет обязательного ключа %r" % key)
    known = set(FLOWS_TOP_REQUIRED) | set(FLOWS_TOP_OPTIONAL)
    for key in sorted(k for k in data if k not in known):
        problems.append("неизвестный ключ верхнего уровня %r — форма изменилась без версии" % key)
    gen = data.get("generated_utc")
    if "generated_utc" in data and not (isinstance(gen, str) and _TS_RE.match(gen)):
        problems.append("generated_utc не вида YYYY-MM-DDTHH:MM:SSZ: %r" % (gen,))
    runs = data.get("week_runs")
    if runs is not None and not (isinstance(runs, list) and all(isinstance(r, str) for r in runs)):
        problems.append("week_runs не список строк")
    tops = data.get("top_contracts")
    if "top_contracts" in data:
        if not isinstance(tops, list):
            problems.append("top_contracts не список")
        else:
            seen = set()
            for i, coin in enumerate(tops):
                if not isinstance(coin, dict):
                    problems.append("top_contracts[%d] не объект" % i)
                    continue
                for key in FLOWS_COIN_KEYS:
                    if key not in coin:
                        problems.append("top_contracts[%d]: нет ключа %r" % (i, key))
                cg = coin.get("cg_id")
                if not isinstance(cg, str) or not cg:
                    problems.append("top_contracts[%d]: cg_id пуст или не строка" % i)
                elif cg in seen:
                    problems.append("top_contracts[%d]: дубль cg_id %r (дедуп по cg_id обязателен)" % (i, cg))
                else:
                    seen.add(cg)
                if not isinstance(coin.get("ticker"), str):
                    problems.append("top_contracts[%d]: ticker не строка" % i)
                rt = coin.get("run_timestamp")
                if not (isinstance(rt, str) and _TS_RE.match(rt)):
                    problems.append("top_contracts[%d]: run_timestamp не вида YYYY-MM-DDTHH:MM:SSZ" % i)
                for key in ("score", "mcap_usd", "price_usd"):
                    if key in coin and not _is_num_or_none(coin.get(key)):
                        problems.append("top_contracts[%d]: %s не число и не null" % (i, key))
                if "entered_paper" in coin and not isinstance(coin.get("entered_paper"), bool):
                    problems.append("top_contracts[%d]: entered_paper не булево" % i)
                contracts = coin.get("contracts")
                if "contracts" in coin and not (
                        isinstance(contracts, dict)
                        and all(isinstance(k, str) and isinstance(v, str)
                                for k, v in contracts.items())):
                    problems.append("top_contracts[%d]: contracts не объект {сеть: адрес}" % i)
    bl = data.get("blacklist")
    if "blacklist" in data:
        if not isinstance(bl, dict):
            problems.append("blacklist не объект")
        else:
            for sym, rec in bl.items():
                if not isinstance(rec, dict):
                    problems.append("blacklist[%r] не объект" % sym)
                    continue
                for key in FLOWS_BLACKLIST_KEYS:
                    if key not in rec:
                        problems.append("blacklist[%r]: нет ключа %r" % (sym, key))
                ev = rec.get("event_date")
                if ev is not None and not (isinstance(ev, str) and _DATE_RE.match(ev)):
                    problems.append("blacklist[%r]: event_date не YYYY-MM-DD и не null" % sym)
    valid = not problems
    return {"schema_version": FLOWS_SCHEMA_VERSION if valid else None,
            "valid": valid, "problems": problems}


def file_sha256_pair(raw):
    """-> (sha256 LF-нормализованных байтов — КАНОН, sha256 сырых байтов).

    Канон — LF: так файл лежит в git и на ubuntu-раннере, и так его
    воспроизведёт кто угодно: `git show <коммит>:export/flows_latest.json |
    sha256sum`. В дереве пилота на Windows (autocrlf) файл может быть CRLF —
    тогда сырой отпечаток другой, и это честно записывается рядом."""
    return (hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(),
            hashlib.sha256(raw).hexdigest())


def flows_rows(flows_obj):
    """flows_latest.json -> строки схемы потоков.

    Одна строка на пару сеть-контракт. Монета без контрактов (нативная или
    контракты не отдались) даёт РОВНО одну строку с пустыми chain/contract —
    иначе она молча исчезла бы из выгрузки, и потребитель считал бы, что её
    в топе не было.
    """
    rows = []
    for coin in ((flows_obj or {}).get("top_contracts") or []):
        if not isinstance(coin, dict):
            continue
        head = [
            _txt(coin.get("ticker")),
            _txt(coin.get("cg_id")),
            _txt(coin.get("run_timestamp")),
            _num(coin.get("score")),
            _num(coin.get("mcap_usd")),
            _num(coin.get("price_usd")),
            _num(bool(coin.get("entered_paper"))),
        ]
        contracts = coin.get("contracts") or {}
        if not isinstance(contracts, dict) or not contracts:
            rows.append(head + ["", ""])
            continue
        for chain in sorted(contracts):
            rows.append(head + [_txt(chain), _txt(contracts[chain])])
    return rows


def write_flows_csv(repo_dir=".", src=None, dst=None):
    """export/flows_latest.json -> export/flows_latest.csv.

    -> {'src':..., 'dst':..., 'n_rows':..., 'n_coins':..., 'status':...}
    Отсутствие исходника — не ошибка, а состояние («ещё не выгружали»):
    возвращается status с причиной, исключения не поднимаются.
    """
    exp_dir = os.path.join(os.path.abspath(repo_dir), "export")
    src = src or os.path.join(exp_dir, "flows_latest.json")
    dst = dst or os.path.join(exp_dir, "flows_latest.csv")
    info = {"src": src, "dst": dst, "n_rows": 0, "n_coins": 0, "status": "ok",
            "source_sha256": None, "source_sha256_raw": None, "source_crlf": None,
            "source_schema_version": None, "source_valid": None, "source_problems": []}
    try:
        with open(src, "rb") as fh:
            raw = fh.read()
        data = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        info["status"] = "flows_latest.json не найден — csv не обновлён"
        return info
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        info["status"] = "flows_latest.json не читается (%s) — csv не обновлён" % exc
        return info

    # С-26: отпечаток и проверка формы исходника — в meta-спутник, чтобы stage0
    # забирал файл без пилота: сверил sha256 (LF — канон), прочёл source_valid.
    info["source_sha256"], info["source_sha256_raw"] = file_sha256_pair(raw)
    info["source_crlf"] = b"\r\n" in raw
    check = validate_flows(data)
    info["source_schema_version"] = check["schema_version"]
    info["source_valid"] = check["valid"]
    info["source_problems"] = list(check["problems"])

    rows = flows_rows(data)
    _write_csv(dst, FLOWS_HEADER, rows)
    info["n_rows"] = len(rows)
    info["n_coins"] = len((data or {}).get("top_contracts") or [])
    _write_meta(dst, {
        "schema": list(FLOWS_HEADER),
        "source_json": os.path.basename(src),
        "source_generated_utc": (data or {}).get("generated_utc"),
        "source_bytes": len(raw),
        "source_sha256": info["source_sha256"],
        "source_sha256_raw": info["source_sha256_raw"],
        "source_crlf": info["source_crlf"],
        "source_schema_version": info["source_schema_version"],
        "source_valid": info["source_valid"],
        "source_problems": info["source_problems"],
        "source_contract": ("STATE.md §11 — контракт flows_latest.json для stage0: "
                            "source_sha256 — от LF-нормализованных байтов (канон, "
                            "= `git show HEAD:export/flows_latest.json | sha256sum`), "
                            "source_sha256_raw — как файл лежал у экспортёра; "
                            "забирать только при source_valid=true"),
        "n_rows": info["n_rows"],
        "n_coins": info["n_coins"],
        "note_rows": "одна строка на пару сеть-контракт; без контрактов — одна пустая пара",
    })
    return info


# --- общая точка входа ---------------------------------------------------

def export_all(repo_dir=".", trades=None, as_of=None, verbose=True):
    """Полный экспорт D3: сделки + дубль потоков.

    trades=None -> берём через trades.py; передан список -> используем его
    (так тесты и вызывающий код не зависят от состояния соседнего модуля).
    Ничего не поднимает наружу по отсутствию данных: возвращает отчёт.
    """
    if trades is None:
        trades, trades_status = load_trades(repo_dir)
    else:
        trades_status = "передан вызывающим (%d записей)" % len(trades)

    tr_info = write_trades_csv(trades, repo_dir=repo_dir, as_of=as_of)
    fl_info = write_flows_csv(repo_dir=repo_dir)
    report = {
        "trades": tr_info,
        "trades_status": trades_status,
        "flows": fl_info,
        "provenance": provenance.stamp(),
        "ok": trades_status == "ok" or not trades_status.startswith("trades.py недоступен"),
    }
    if verbose:
        _print_report(report)
    return report


def _print_report(report):
    tr = report["trades"]
    fl = report["flows"]
    print("СДЕЛКИ -> %s" % tr["dated"])
    print("         %s" % tr["latest"])
    print("  строк %d (закрытых %d, открытых %d); источник: %s"
          % (tr["n_rows"], tr["n_closed"], tr["n_open"], report["trades_status"]))
    if tr["n_closed"] == 0:
        print("  закрытых сделок нет — колонки pnl пусты по факту, "
              "а не из-за сбоя экспорта")
    print("ПОТОКИ -> %s" % fl["dst"])
    print("  строк %d по %d монетам; статус: %s"
          % (fl["n_rows"], fl["n_coins"], fl["status"]))
    print("схема сделок: %s" % ",".join(TRADES_HEADER))
    print("схема потоков: %s" % ",".join(FLOWS_HEADER))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="D3: единая csv-схема экспорта сделок и потоков.")
    parser.add_argument("--repo", default=HERE,
                        help="каталог репозитория (по умолчанию каталог скрипта)")
    parser.add_argument("--as-of", default=None,
                        help="дата артефакта YYYY-MM-DD (по умолчанию сегодня UTC)")
    args = parser.parse_args(argv)

    try:
        report = export_all(repo_dir=args.repo, as_of=args.as_of)
    except Exception as exc:
        print("экспорт не выполнен: %s: %s" % (type(exc).__name__, exc))
        return 1

    status = report["trades_status"]
    if status != "ok" and status.startswith("trades.py"):
        # Загрузчик сделок — обязательная часть D3. Молчать про его отсутствие
        # нельзя: csv в этом случае пуст не потому, что сделок нет.
        print("ВНИМАНИЕ: %s. Csv сделок записан ПУСТЫМ не по факту турнира." % status)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

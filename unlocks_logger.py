#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""unlocks_logger.py — ежедневный календарь разлоков и листингов (блок D1).

ЗАЧЕМ ЭТО НУЖНО ДЛЯ ДЕНЕГ
Две вещи двигают цену мелкой монеты сильнее любого нашего скора:
  * РАЗЛОК — заранее известный выброс предложения (команда/инвесторы/эйрдроп);
  * ЛИСТИНГ/ДЕЛИСТИНГ — скачок доступной ликвидности и внимания.
Обе известны ЗАРАНЕЕ и обе бесплатны. Логгер копит их день за днём, чтобы к
вердикту 15.11.2026 у нас была СВОЯ история фактов, а не пересказ чужих
твитов задним числом. Без ежедневного накопления такой истории не будет:
источники отдают только «сейчас», прошлое из них не достать.

ЧТО ЭТО НЕ ДЕЛАЕТ (честная граница)
Логгер НИЧЕГО не предсказывает, не считает edge и не трогает сита. Он только
записывает факты в csv. Любой вывод «разлок → цена вниз» — отдельная работа
на накопленных данных, и она невозможна, пока данных нет.

ИСТОЧНИКИ (проверены фактом 01.09.2026, менять только с проверкой)
  Разлоки:
    https://defillama-datasets.llama.fi/emissionsProtocolsList  -> 200, 372 слага
    https://defillama-datasets.llama.fi/emissions/<slug>         -> 200, JSON
       поля: gecko_id, name, metadata.events[] (timestamp сек, noOfTokens[],
       category, unlockType, description), supplyMetrics
    ВНИМАНИЕ: https://api.llama.fi/emissions отдаёт HTTP 402 (платный тариф).
    Использовать ТОЛЬКО CDN-датасеты выше. Заголовок Accept-Encoding обязан
    быть "gzip, deflate" БЕЗ br: requests без пакета brotli не разожмёт ответ
    и вернёт бинарный мусор вместо JSON (проверено, ломается молча).
  Листинги:
    https://api.bybit.com/v5/announcements/index?locale=en-US&type=new_crypto
       -> result.list[] (title, description, url, dateTimestamp мс, tags)
    Плюс ДИФФЫ МНОЖЕСТВ символов день ко дню (факт без чужих слов):
      https://fapi.binance.com/fapi/v1/exchangeInfo   symbols[].symbol, status TRADING
      https://api.mexc.com/api/v3/exchangeInfo        symbols[].symbol, isSpotTradingAllowed
      POST https://api.hyperliquid.xyz/info {"type":"metaAndAssetCtxs"}
                                                     universe[].name, кроме isDelisted
    Снимок множеств лежит в data/listings/symbols_latest.json.

ПОЧЕМУ ДИФФ, А НЕ ОБЪЯВЛЕНИЯ
Объявление — это чужой текст: его может не быть, он может быть на другом
языке, он может врать про дату. Появление символа в перечне биржи — факт,
который биржа подтверждает своим API. Объявления мы тоже пишем, но помечаем
их отдельным видом (kind=announcement), чтобы не путать слово с фактом.

ЗАЩИТА ОТ ЛОЖНОЙ ВОЛНЫ ДЕЛИСТИНГОВ
Если источник сегодня недоступен или отдал обрезанный перечень, наивный дифф
объявил бы делистингом сотни символов. Поэтому:
  * перечень короче MIN_PLAUSIBLE_SYMBOLS считается СБОЕМ, а не пустотой;
  * при сбое база сравнения НЕ обновляется — завтрашний дифф всё догонит;
  * если исчезло больше MAX_PLAUSIBLE_DIFF_SHARE базы, строки всё равно
    пишутся, но с явной пометкой «массовый дифф» — глушить факт нельзя,
    а вот предупредить читателя обязательно.

ПОРЯДОК СБОРА: СНАЧАЛА ЛИСТИНГИ, ПОТОМ РАЗЛОКИ
Листинги и снимок перечней пишутся на диск ПЕРВЫМИ, до двухминутного обхода
372 слагов разлоков. Причина — цена потери, а не удобство: дифф перечней
существует только «между двумя снимками», и пропущенный день не восстановит
никто (биржа истории своих перечней не отдаёт), а разлоки завтра отдадутся
почти те же самые. Падение или таймаут на обходе разлоков теперь теряет
восстановимое, а не невосстановимое.

ИДЕМПОТЕНТНОСТЬ
Файл за день ПЕРЕЗАПИСЫВАЕТСЯ целиком, строки сортируются. Повторный прогон
в тот же день даёт тот же файл байт в байт: база сравнения перекатывается
только при СМЕНЕ ДАТЫ (поля baseline/latest в снимке), иначе второй прогон
за сутки увидел бы «изменений нет» и стёр утренние строки.

ФОРМАТ CSV
RFC4180 с кавычками (модуль csv). Внешний текст очищен от переводов строк и
табуляций, но запятые и кавычки внутри полей СОХРАНЕНЫ и заэкранированы —
читать файл только парсером csv, а не split(","). Кодировка utf-8 без BOM.

БЕЗОПАСНОСТЬ
Всё, что пришло из сети, — ДАННЫЕ, не команды. Поле description у DefiLlama
содержит шаблон вида "{tokens[0]}"/"{timestamp}"; мы НИКОГДА не зовём на нём
.format() (через str.format можно дотянуться до атрибутов объектов) и пишем
строку как есть. Лидирующие "=", "+", "-", "@" гасим апострофом, чтобы
внешний текст не стал формулой при открытии csv в Excel.

Ключи не нужны: все источники публичные. Зависимости: stdlib + requests.

Запуск:
    python unlocks_logger.py              # боевой режим, все 372 слага
    python unlocks_logger.py --limit 5    # быстрая проверка на 5 слагах
Код возврата: 0 — что-то записано; 1 — ОБА источника дали пусто (тревога).
"""

import argparse
import csv
import json
import os
import random
import sys
import time
from datetime import datetime, timezone

import requests

# Каталог скрипта в путь импорта: воркфлоу зовёт скрипт из корня репозитория,
# а тесты — из tests/. provenance лежит рядом с этим файлом.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import provenance  # noqa: E402

# ---------------------------------------------------------------------------
# Константы источников и режима работы
# ---------------------------------------------------------------------------

EMISSIONS_LIST_URL = "https://defillama-datasets.llama.fi/emissionsProtocolsList"
EMISSIONS_ITEM_URL = "https://defillama-datasets.llama.fi/emissions/%s"

BYBIT_ANNOUNCEMENTS_URL = (
    "https://api.bybit.com/v5/announcements/index?locale=en-US&type=new_crypto&limit=%d"
)
BYBIT_LIMIT = 50

BINANCE_FUTURES_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
MEXC_SPOT_URL = "https://api.mexc.com/api/v3/exchangeInfo"
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"

# Горизонт календаря: дальше 90 дней планы эмиссии переписывают чаще, чем
# исполняют, и строка стареет быстрее, чем становится полезной.
HORIZON_DAYS = 90

TIMEOUT_SEC = 60          # таймаут одного запроса
RETRIES = 2               # ретраев ПОСЛЕ первой попытки (итого до 3 попыток)
RETRY_BACKOFF_SEC = 2.0   # пауза перед ретраем = RETRY_BACKOFF_SEC * номер ретрая
PAUSE_MIN_SEC = 0.1       # вежливая пауза между запросами
PAUSE_MAX_SEC = 0.2

# Перечень короче этого считаем сбоем источника, а не «биржа опустела».
MIN_PLAUSIBLE_SYMBOLS = 10
# Доля исчезнувших символов, выше которой дифф помечается подозрительным.
MAX_PLAUSIBLE_DIFF_SHARE = 0.25

# Обрезка внешнего текста: длинные заголовки объявлений не несут смысла,
# а csv раздувают.
MAX_TEXT_LEN = 400

# Границы правдоподобия для отметок времени (2000-01-01 .. 2100-01-01).
MIN_PLAUSIBLE_TS = 946684800
MAX_PLAUSIBLE_TS = 4102444800

UNLOCKS_HEADER = [
    "snapshot_date", "slug", "gecko_id", "name", "event_ts_utc", "event_date",
    "category", "unlock_type", "n_tokens", "description", "source", "git_sha",
]

LISTINGS_HEADER = [
    "snapshot_date", "kind", "venue", "symbol", "title", "url",
    "event_ts_utc", "source", "git_sha",
]

# Заголовки запросов. br из Accept-Encoding убран НАМЕРЕННО (см. шапку).
HTTP_HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
    "User-Agent": "crypto-screener-unlocks-logger/1.0",
}

VENUES = ("binance_futures", "mexc_spot", "hyperliquid_perp")


class SourceError(Exception):
    """Источник не отдал пригодных данных. Ловится вызывающим, прогон живёт."""


# ---------------------------------------------------------------------------
# Мелкие чистые помощники (их же проверяет селфтест)
# ---------------------------------------------------------------------------

def csv_clean(text, max_len=MAX_TEXT_LEN):
    """Внешний текст -> безопасное однострочное поле csv.

    Что делаем:
      * None -> "" (пустое поле честнее строки "None");
      * CR/LF/TAB -> пробел, подряд идущие пробелы схлопываются;
      * лидирующие '=', '+', '-', '@' гасим апострофом (иначе Excel считает
        поле формулой — классическая CSV-инъекция через чужой текст);
      * обрезаем до max_len.
    Чего НЕ делаем: не выкидываем запятые и кавычки. Их экранирует модуль csv,
    и данные остаются целыми. Цена решения — файл нельзя читать split(",").
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    out = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    out = " ".join(out.split())          # схлопывание пробелов + strip
    if out[:1] in ("=", "+", "-", "@"):
        out = "'" + out
    if max_len and len(out) > max_len:
        out = out[:max_len]
    return out


def _fmt_number(value):
    """Число -> строка без экспоненты. Не число -> "".

    Экспонента ('1e+21') ломает чтение csv в половине инструментов, поэтому
    печатаем развёрнуто. Целые печатаем целыми, чтобы 100000000 не стало
    '100000000.0'.
    """
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        if abs(value) < 1e15 and float(value).is_integer():
            return str(int(value))
        return ("%.10f" % value).rstrip("0").rstrip(".")
    if isinstance(value, str):
        try:
            return _fmt_number(float(value.strip()))
        except (TypeError, ValueError):
            return ""
    return ""


def tokens_field(no_of_tokens):
    """noOfTokens (список) -> одно поле csv.

    Почему списком, а не одним числом: у cliff-события список из одного
    элемента (сколько токенов вышло), а у linear — из двух (старая и новая
    скорость, токенов за период). Свернуть в одно число нельзя, не соврав,
    поэтому склеиваем через '|' и пишем это правило прямо здесь:
    потребителю, которому нужно одно число, брать ПЕРВЫЙ элемент.
    """
    if no_of_tokens is None:
        return ""
    if not isinstance(no_of_tokens, (list, tuple)):
        no_of_tokens = [no_of_tokens]
    parts = [_fmt_number(x) for x in no_of_tokens]
    return "|".join(p for p in parts if p != "")


def normalize_ts_seconds(value):
    """Отметка времени -> целые секунды UTC, либо None.

    Терпит миллисекунды (Bybit отдаёт мс, DefiLlama — секунды) и строки.
    Всё за пределами 2000..2100 считаем мусором: лучше потерять строку,
    чем занести в календарь событие 1970 года.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts != ts or ts in (float("inf"), float("-inf")):
        return None
    if abs(ts) > 1e12:            # похоже на миллисекунды
        ts = ts / 1000.0
    if not (MIN_PLAUSIBLE_TS <= ts <= MAX_PLAUSIBLE_TS):
        return None
    return int(ts)


def ts_to_iso(ts_seconds):
    """Секунды UTC -> 'YYYY-MM-DDTHH:MM:SSZ'."""
    return datetime.fromtimestamp(int(ts_seconds), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def ts_to_date(ts_seconds):
    """Секунды UTC -> 'YYYY-MM-DD'."""
    return datetime.fromtimestamp(int(ts_seconds), timezone.utc).strftime("%Y-%m-%d")


def _pause():
    """Вежливая пауза между сетевыми запросами (0.1–0.2 с)."""
    time.sleep(random.uniform(PAUSE_MIN_SEC, PAUSE_MAX_SEC))


# ---------------------------------------------------------------------------
# Сетевой слой
# ---------------------------------------------------------------------------

def make_session():
    """requests.Session с нужными заголовками. Отдельной функцией — чтобы
    один и тот же keep-alive обслуживал сотни запросов к DefiLlama."""
    s = requests.Session()
    s.headers.update(HTTP_HEADERS)
    return s


def _request_json(method, url, session=None, payload=None,
                  timeout=TIMEOUT_SEC, retries=RETRIES):
    """Запрос -> разобранный JSON. При неудаче после ретраев — SourceError.

    Ретраим ВСЁ, кроме явного «нам сюда нельзя»: 401/402/403/404 не лечатся
    повтором, а 402 у api.llama.fi ещё и означает платный тариф — ждать
    бессмысленно, надо чинить URL.
    """
    own_session = session is None
    sess = session or make_session()
    last_error = "неизвестно"
    try:
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
            _pause()
            try:
                if method == "POST":
                    resp = sess.post(url, json=payload, timeout=timeout)
                else:
                    resp = sess.get(url, timeout=timeout)
            except Exception as exc:               # сеть, DNS, таймаут
                last_error = "запрос не прошёл: %s" % exc
                continue
            if resp.status_code in (401, 402, 403, 404):
                raise SourceError("HTTP %d (повтор не поможет): %s"
                                  % (resp.status_code, url))
            if resp.status_code != 200:
                last_error = "HTTP %d" % resp.status_code
                continue
            try:
                return resp.json()
            except Exception as exc:
                # Сюда же попадает случай «сервер прислал brotli, а мы его не
                # умеем»: тело не разжато и на JSON не похоже.
                last_error = "ответ не разобран как JSON: %s" % exc
                continue
        raise SourceError("%s: %s" % (url, last_error))
    finally:
        if own_session:
            sess.close()


def http_get_json(url, session=None, timeout=TIMEOUT_SEC, retries=RETRIES):
    """GET -> JSON или SourceError."""
    return _request_json("GET", url, session=session, timeout=timeout, retries=retries)


def http_post_json(url, payload, session=None, timeout=TIMEOUT_SEC, retries=RETRIES):
    """POST с телом JSON -> JSON или SourceError."""
    return _request_json("POST", url, session=session, payload=payload,
                         timeout=timeout, retries=retries)


# ---------------------------------------------------------------------------
# РАЗЛОКИ: загрузка и разбор
# ---------------------------------------------------------------------------

def fetch_unlock_slugs(session=None):
    """Список слагов протоколов с расписанием эмиссии (на 01.09.2026 — 372)."""
    data = http_get_json(EMISSIONS_LIST_URL, session=session)
    if not isinstance(data, list):
        raise SourceError("emissionsProtocolsList отдал не список, а %s" % type(data).__name__)
    slugs = [s for s in data if isinstance(s, str) and s.strip()]
    if not slugs:
        raise SourceError("emissionsProtocolsList пуст")
    return slugs


def fetch_emissions(slug, session=None):
    """Карточка эмиссии одного протокола."""
    data = http_get_json(EMISSIONS_ITEM_URL % slug, session=session)
    if not isinstance(data, dict):
        raise SourceError("emissions/%s отдал не объект" % slug)
    return data


def parse_unlock_events(payload, slug, now_ts, horizon_days=HORIZON_DAYS,
                        snapshot_date="", git_sha_value=""):
    """Карточка эмиссии -> строки csv по БУДУЩИМ событиям в горизонте.

    Берём событие, если now_ts < timestamp <= now_ts + horizon_days.
    Строго больше now_ts: событие «прямо сейчас» уже случилось, торговать
    его поздно, а в календаре будущего оно создаёт иллюзию предупреждения.
    """
    if not isinstance(payload, dict):
        return []
    horizon_ts = now_ts + int(horizon_days) * 86400

    gecko_id = csv_clean(payload.get("gecko_id"))
    name = csv_clean(payload.get("name"))
    # ЧЕРЕЗ ТУ ЖЕ ЧИСТКУ, что и колонка slug ниже: slug — внешняя строка из
    # emissionsProtocolsList, и подстановка её в шаблон URL внешности не
    # отменяет. Без csv_clean один и тот же slug попадал в файл двумя путями —
    # очищенным в колонке slug и сырым внутри source, а поле с переводом
    # строки рвёт csv-строку надвое.
    source = csv_clean(EMISSIONS_ITEM_URL % slug)

    metadata = payload.get("metadata")
    events = metadata.get("events") if isinstance(metadata, dict) else None
    if not isinstance(events, list):
        return []

    rows = []
    for event in events:
        if not isinstance(event, dict):
            continue
        ts = normalize_ts_seconds(event.get("timestamp"))
        if ts is None:
            continue
        if not (now_ts < ts <= horizon_ts):
            continue
        rows.append({
            "snapshot_date": snapshot_date,
            "slug": csv_clean(slug),
            "gecko_id": gecko_id,
            "name": name,
            "event_ts_utc": ts_to_iso(ts),
            "event_date": ts_to_date(ts),
            "category": csv_clean(event.get("category")),
            "unlock_type": csv_clean(event.get("unlockType")),
            "n_tokens": tokens_field(event.get("noOfTokens")),
            # description пишем как есть (после чистки строк). .format() на нём
            # не зовём НИКОГДА — это чужой текст, а не наш шаблон.
            "description": csv_clean(event.get("description")),
            "source": source,
            "git_sha": git_sha_value,
        })
    return rows


def collect_unlocks(now, limit=None, horizon_days=HORIZON_DAYS, session=None,
                    git_sha_value=""):
    """Полный обход слагов -> (строки, отказы, статистика).

    Сбой одного слага не валит обход: считаем отказы и идём дальше. Сбой
    самого списка слагов = пустой результат разлоков, но листинги живут.
    """
    snapshot_date = now.strftime("%Y-%m-%d")
    now_ts = int(now.timestamp())
    failures = []
    stats = {"slugs_total": 0, "slugs_ok": 0, "slugs_failed": 0, "events_raw": 0}

    try:
        slugs = fetch_unlock_slugs(session=session)
    except Exception as exc:
        failures.append("emissionsProtocolsList: %s" % exc)
        print("ОТКАЗ ИСТОЧНИКА РАЗЛОКОВ: %s" % exc)
        return [], failures, stats

    if limit is not None and limit > 0:
        slugs = slugs[:limit]
        print("РЕЖИМ ПРОВЕРКИ: обходим только %d слагов из полного списка." % len(slugs))
    stats["slugs_total"] = len(slugs)

    rows = []
    for index, slug in enumerate(slugs, 1):
        try:
            payload = fetch_emissions(slug, session=session)
        except Exception as exc:
            stats["slugs_failed"] += 1
            if len(failures) < 10:       # не топим отчёт списком из 372 строк
                failures.append("emissions/%s: %s" % (slug, exc))
            continue
        stats["slugs_ok"] += 1
        got = parse_unlock_events(payload, slug, now_ts, horizon_days=horizon_days,
                                  snapshot_date=snapshot_date, git_sha_value=git_sha_value)
        stats["events_raw"] += len(got)
        rows.extend(got)
        if index % 50 == 0:
            print("  разлоки: обработано %d/%d слагов, событий пока %d"
                  % (index, len(slugs), len(rows)))

    if stats["slugs_failed"]:
        print("РАЗЛОКИ: не ответили %d слагов из %d (остальные записаны)."
              % (stats["slugs_failed"], stats["slugs_total"]))
    return dedupe_and_sort(rows, UNLOCKS_HEADER), failures, stats


# ---------------------------------------------------------------------------
# ЛИСТИНГИ: объявления Bybit
# ---------------------------------------------------------------------------

def fetch_bybit_announcements(limit=BYBIT_LIMIT, session=None):
    """Свежие объявления Bybit типа new_crypto -> список сырых элементов."""
    data = http_get_json(BYBIT_ANNOUNCEMENTS_URL % int(limit), session=session)
    if not isinstance(data, dict):
        raise SourceError("bybit: ответ не объект")
    ret_code = data.get("retCode")
    if ret_code not in (0, None):
        raise SourceError("bybit retCode=%s retMsg=%s" % (ret_code, data.get("retMsg")))
    result = data.get("result")
    items = result.get("list") if isinstance(result, dict) else None
    if not isinstance(items, list):
        raise SourceError("bybit: нет result.list")
    return items


def parse_bybit_announcements(items, snapshot_date="", git_sha_value=""):
    """Объявления -> строки csv вида kind=announcement.

    Символ НЕ вытаскиваем из заголовка: в новостях Bybit заголовок бывает
    рекламный ('USDT Token Splash...'), и выдернутый оттуда «тикер» был бы
    выдумкой. Факт листинга даёт дифф перечней, а тут — только слово биржи.
    """
    rows = []
    if not isinstance(items, list):
        return rows
    for item in items:
        if not isinstance(item, dict):
            continue
        ts = normalize_ts_seconds(item.get("dateTimestamp"))
        if ts is None:
            ts = normalize_ts_seconds(item.get("publishTime"))
        rows.append({
            "snapshot_date": snapshot_date,
            "kind": "announcement",
            "venue": "bybit",
            "symbol": "",
            "title": csv_clean(item.get("title")),
            "url": csv_clean(item.get("url")),
            "event_ts_utc": ts_to_iso(ts) if ts is not None else "",
            "source": BYBIT_ANNOUNCEMENTS_URL % BYBIT_LIMIT,
            "git_sha": git_sha_value,
        })
    return rows


# ---------------------------------------------------------------------------
# ЛИСТИНГИ: перечни символов площадок
# ---------------------------------------------------------------------------

def fetch_binance_futures_symbols(session=None):
    """Символы бессрочных фьючерсов Binance USDT-M в статусе TRADING."""
    data = http_get_json(BINANCE_FUTURES_URL, session=session)
    symbols = data.get("symbols") if isinstance(data, dict) else None
    if not isinstance(symbols, list):
        raise SourceError("binance: нет symbols[]")
    out = set()
    for item in symbols:
        if not isinstance(item, dict):
            continue
        # PENDING_TRADING и SETTLING — не торгуемые прямо сейчас состояния,
        # и их появление/исчезновение не является листингом.
        if item.get("status") != "TRADING":
            continue
        sym = item.get("symbol")
        if isinstance(sym, str) and sym.strip():
            out.add(sym.strip())
    return out


def fetch_mexc_spot_symbols(session=None):
    """Символы спота MEXC, доступные к торговле.

    Поле status у MEXC — '1'/'2' (не 'ENABLED', как в старой документации),
    поэтому основным признаком берём isSpotTradingAllowed, а на status
    опираемся только если поля нет.
    """
    data = http_get_json(MEXC_SPOT_URL, session=session)
    symbols = data.get("symbols") if isinstance(data, dict) else None
    if not isinstance(symbols, list):
        raise SourceError("mexc: нет symbols[]")
    out = set()
    for item in symbols:
        if not isinstance(item, dict):
            continue
        allowed = item.get("isSpotTradingAllowed")
        if allowed is None:
            allowed = str(item.get("status")) in ("1", "ENABLED", "TRADING")
        if not allowed:
            continue
        sym = item.get("symbol")
        if isinstance(sym, str) and sym.strip():
            out.add(sym.strip())
    return out


def fetch_hyperliquid_symbols(session=None):
    """Имена живых перпов Hyperliquid.

    В universe навсегда остаются делистнутые монеты с флагом isDelisted
    (на 01.09.2026 — 56 из 233): они держат индекс в параллельном массиве
    контекстов. Без фильтра по флагу мы бы считали живыми давно мёртвые.
    """
    data = http_post_json(HYPERLIQUID_INFO_URL, {"type": "metaAndAssetCtxs"},
                          session=session)
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise SourceError("hyperliquid: ответ не [meta, ctxs]")
    universe = data[0].get("universe")
    if not isinstance(universe, list):
        raise SourceError("hyperliquid: нет universe[]")
    out = set()
    for item in universe:
        if not isinstance(item, dict) or item.get("isDelisted"):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            out.add(name.strip())
    return out


def diff_symbol_sets(prev, cur):
    """Дифф множеств символов -> {'appeared': [...], 'disappeared': [...]}.

    Чистая функция: никакой сети и никаких файлов. Списки отсортированы,
    чтобы csv был стабилен от прогона к прогону.
    """
    prev_set = set(prev or ())
    cur_set = set(cur or ())
    return {
        "appeared": sorted(cur_set - prev_set),
        "disappeared": sorted(prev_set - cur_set),
    }


# ---------------------------------------------------------------------------
# Снимок множеств символов (база сравнения)
# ---------------------------------------------------------------------------

def symbols_snapshot_path(repo_dir):
    return os.path.join(repo_dir, "data", "listings", "symbols_latest.json")


def load_symbols_snapshot(path):
    """Снимок с диска. Нет файла или битый json -> пустая заготовка.

    Битый файл не должен валить прогон: хуже пустого диффа только
    остановившийся логгер.
    """
    empty = {"baseline_date": None, "baseline": {},
             "latest_date": None, "latest": {}}
    if not os.path.exists(path):
        return empty
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        print("СНИМОК СИМВОЛОВ НЕ ПРОЧИТАН (%s) — считаю первым прогоном." % exc)
        return empty
    if not isinstance(data, dict):
        return empty
    for key in ("baseline", "latest"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    for key in ("baseline_date", "latest_date"):
        if not isinstance(data.get(key), str):
            data[key] = None
    return data


def save_symbols_snapshot(path, snapshot):
    """Снимок на диск (utf-8, отсортированные ключи, перевод строки \\n)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")


def collect_listings(now, repo_dir, session=None, git_sha_value=""):
    """Объявления + диффы перечней -> (строки, отказы, снимок для записи).

    Снимок ВОЗВРАЩАЕТСЯ, а не пишется здесь: запись — дело run(), чтобы эту
    функцию можно было гонять в тестах без побочных эффектов.
    """
    snapshot_date = now.strftime("%Y-%m-%d")
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    failures = []
    rows = []

    # --- 1. Объявления Bybit (слово биржи) --------------------------------
    try:
        items = fetch_bybit_announcements(session=session)
        rows.extend(parse_bybit_announcements(items, snapshot_date=snapshot_date,
                                              git_sha_value=git_sha_value))
    except Exception as exc:
        failures.append("bybit announcements: %s" % exc)
        print("ОТКАЗ ИСТОЧНИКА ОБЪЯВЛЕНИЙ: %s" % exc)

    # --- 2. Перекат базы сравнения ----------------------------------------
    snapshot = load_symbols_snapshot(symbols_snapshot_path(repo_dir))
    if snapshot.get("latest_date") and snapshot.get("latest_date") != snapshot_date:
        # Наступил новый день: вчерашний перечень становится базой.
        snapshot["baseline"] = dict(snapshot.get("latest") or {})
        snapshot["baseline_date"] = snapshot.get("latest_date")
    # Если latest_date == сегодня, это ПОВТОРНЫЙ прогон за сутки: базу не
    # трогаем, иначе второй прогон дал бы «изменений нет» и стёр строки.

    # --- 3. Перечни площадок ----------------------------------------------
    fetchers = {
        "binance_futures": (fetch_binance_futures_symbols, BINANCE_FUTURES_URL),
        "mexc_spot": (fetch_mexc_spot_symbols, MEXC_SPOT_URL),
        "hyperliquid_perp": (fetch_hyperliquid_symbols, HYPERLIQUID_INFO_URL),
    }
    fetched = {}
    for venue in VENUES:
        fetch_fn, url = fetchers[venue]
        try:
            symbols = set(fetch_fn(session=session))
        except Exception as exc:
            failures.append("%s: %s" % (venue, exc))
            print("ОТКАЗ ПЕРЕЧНЯ %s: %s (база сравнения сохранена)" % (venue, exc))
            continue
        if len(symbols) < MIN_PLAUSIBLE_SYMBOLS:
            failures.append("%s: перечень из %d символов — считаю сбоем"
                            % (venue, len(symbols)))
            print("ОТКАЗ ПЕРЕЧНЯ %s: пришло %d символов, это не похоже на правду."
                  % (venue, len(symbols)))
            continue
        fetched[venue] = symbols

    # --- 4. Диффы ----------------------------------------------------------
    for venue in VENUES:
        if venue not in fetched:
            continue
        base = snapshot.get("baseline", {}).get(venue)
        url = fetchers[venue][1]
        if not base:
            print("%s: базы сравнения нет (первый снимок) — дифф не считаю, "
                  "перечень записан на завтра." % venue)
            continue
        diff = diff_symbol_sets(base, fetched[venue])
        base_date = snapshot.get("baseline_date") or "неизвестно"
        share_gone = len(diff["disappeared"]) / float(max(1, len(set(base))))
        suspicious = share_gone > MAX_PLAUSIBLE_DIFF_SHARE
        if suspicious:
            print("ВНИМАНИЕ %s: исчезло %d из %d символов (%.0f%%). Похоже на смену "
                  "формата API, а не на делистинг — строки помечены."
                  % (venue, len(diff["disappeared"]), len(set(base)), 100 * share_gone))
        mark = "ВНИМАНИЕ массовый дифф: " if suspicious else ""
        for symbol in diff["appeared"]:
            rows.append({
                "snapshot_date": snapshot_date,
                "kind": "listing",
                "venue": venue,
                "symbol": csv_clean(symbol),
                "title": "%sсимвол появился в перечне %s (дифф против снимка %s)"
                         % (mark, venue, base_date),
                "url": "",
                "event_ts_utc": now_iso,
                "source": url,
                "git_sha": git_sha_value,
            })
        for symbol in diff["disappeared"]:
            rows.append({
                "snapshot_date": snapshot_date,
                "kind": "delisting",
                "venue": venue,
                "symbol": csv_clean(symbol),
                "title": "%sсимвол исчез из перечня %s (дифф против снимка %s)"
                         % (mark, venue, base_date),
                "url": "",
                "event_ts_utc": now_iso,
                "source": url,
                "git_sha": git_sha_value,
            })

    # --- 5. Обновление снимка ---------------------------------------------
    if fetched:
        latest = dict(snapshot.get("latest") or {})
        for venue, symbols in fetched.items():
            latest[venue] = sorted(symbols)
        snapshot["latest"] = latest
        snapshot["latest_date"] = snapshot_date
    snapshot["updated_utc"] = now_iso
    snapshot["git_sha"] = git_sha_value
    snapshot["venues_ok"] = sorted(fetched.keys())

    return dedupe_and_sort(rows, LISTINGS_HEADER), failures, snapshot


# ---------------------------------------------------------------------------
# Запись файлов
# ---------------------------------------------------------------------------

def dedupe_and_sort(rows, header):
    """Убрать полные дубли и отсортировать по всем полям.

    Зачем: одно и то же событие встречается в карточке дважды, а порядок
    словарей и множеств не гарантирован. Без сортировки повторный прогон дал
    бы тот же смысл, но другие байты — и git-диф каждый день был бы шумом.
    """
    seen = set()
    unique = []
    for row in rows:
        key = tuple(str(row.get(col, "")) for col in header)
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    unique.sort(key=lambda r: tuple(str(r.get(col, "")) for col in header))
    return unique


def daily_csv_path(repo_dir, kind, day):
    """Путь файла за день: data/<kind>/YYYY-MM/<kind>_YYYYMMDD.csv."""
    return os.path.join(repo_dir, "data", kind, day.strftime("%Y-%m"),
                        "%s_%s.csv" % (kind, day.strftime("%Y%m%d")))


def write_csv(path, header, rows):
    """Перезапись файла целиком (идемпотентность за день). -> число строк."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow([row.get(col, "") for col in header])
    return len(rows)


# ---------------------------------------------------------------------------
# Прогон целиком
# ---------------------------------------------------------------------------

def run(repo_dir=None, limit=None, horizon_days=HORIZON_DAYS, now=None,
        session=None, git_sha_value=None):
    """Один суточный прогон. -> отчёт словарём, включая exit_code.

    Сбой одного источника не мешает другому. Код 1 отдаём ТОЛЬКО когда пусты
    ОБА: это единственная ситуация, где день прожит зря и Actions обязан
    поднять тревогу.
    """
    if repo_dir is None:
        repo_dir = _HERE
    if now is None:
        now = datetime.now(timezone.utc)
    if git_sha_value is None:
        git_sha_value = provenance.git_sha()

    print("=" * 70)
    print("КАЛЕНДАРЬ РАЗЛОКОВ И ЛИСТИНГОВ — прогон за %s (UTC)"
          % now.strftime("%Y-%m-%d %H:%M:%S"))
    print("git_sha: %s | горизонт разлоков: %d дней" % (git_sha_value, horizon_days))
    print("=" * 70)

    # ПОРЯДОК ЗДЕСЬ НЕ КОСМЕТИКА, А ЗАЩИТА НЕВОССТАНОВИМЫХ ДАННЫХ.
    # Листинги идут ПЕРВЫМИ и ложатся на диск СРАЗУ, до обхода разлоков:
    #   * дифф перечней символов — факт «между двумя снимками». Биржа не
    #     отдаёт историю своих перечней: пропущенный день не догоняется ничем
    #     и никогда. Снимок symbols_latest.json нужен ещё и как база для
    #     ЗАВТРАШНЕГО диффа — не записав его, мы теряем и завтрашний день;
    #   * разлоки, наоборот, восстановимы: календарь на 90 дней вперёд завтра
    #     отдастся почти тот же самый, и пропуск одного дня ничего не стирает.
    # Обход 372 слагов занимает около двух минут и держит всю сеть на себе —
    # именно он падает по таймауту воркфлоу (30 мин) или по сбою источника.
    # Раньше он шёл первым, и на диск не ложилось НИ ОДНОЙ строки, пока он не
    # закончится: падение на нём уносило ровно то, что не догнать завтра.
    listing_rows, listing_failures, snapshot = collect_listings(
        now, repo_dir, session=session, git_sha_value=git_sha_value)
    listings_path = daily_csv_path(repo_dir, "listings", now)
    write_csv(listings_path, LISTINGS_HEADER, listing_rows)
    save_symbols_snapshot(symbols_snapshot_path(repo_dir), snapshot)

    unlock_rows, unlock_failures, unlock_stats = collect_unlocks(
        now, limit=limit, horizon_days=horizon_days, session=session,
        git_sha_value=git_sha_value)
    unlocks_path = daily_csv_path(repo_dir, "unlocks", now)
    write_csv(unlocks_path, UNLOCKS_HEADER, unlock_rows)

    failures = list(unlock_failures) + list(listing_failures)
    exit_code = 1 if (not unlock_rows and not listing_rows) else 0

    kinds = {}
    for row in listing_rows:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1

    print("-" * 70)
    print("РАЗЛОКИ:  строк %d (слагов ок %d, отказали %d) -> %s"
          % (len(unlock_rows), unlock_stats["slugs_ok"],
             unlock_stats["slugs_failed"], unlocks_path))
    print("ЛИСТИНГИ: строк %d (%s) -> %s"
          % (len(listing_rows),
             ", ".join("%s=%d" % kv for kv in sorted(kinds.items())) or "пусто",
             listings_path))
    print("ОТКАЗОВ ИСТОЧНИКОВ: %d" % len(failures))
    for line in failures[:10]:
        print("   * %s" % line)
    if exit_code:
        print("ПУСТО В ОБОИХ ИСТОЧНИКАХ — возвращаю 1, это тревога, а не норма.")
    print("=" * 70)

    return {
        "snapshot_date": now.strftime("%Y-%m-%d"),
        "unlocks_rows": len(unlock_rows),
        "listings_rows": len(listing_rows),
        "listings_by_kind": kinds,
        "unlocks_path": unlocks_path,
        "listings_path": listings_path,
        "failures": failures,
        "unlock_stats": unlock_stats,
        "exit_code": exit_code,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Ежедневный логгер разлоков (DefiLlama) и листингов "
                    "(Bybit + диффы перечней Binance/MEXC/Hyperliquid).")
    parser.add_argument("--repo-dir", default=_HERE,
                        help="корень репозитория (по умолчанию каталог скрипта)")
    parser.add_argument("--limit", type=int, default=None,
                        help="обойти только N первых слагов разлоков "
                             "(для проверки; в бою лимита нет)")
    parser.add_argument("--horizon-days", type=int, default=HORIZON_DAYS,
                        help="горизонт календаря разлоков в днях (по умолчанию %d)"
                             % HORIZON_DAYS)
    args = parser.parse_args(argv)

    session = make_session()
    try:
        report = run(repo_dir=args.repo_dir, limit=args.limit,
                     horizon_days=args.horizon_days, session=session)
    finally:
        session.close()
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())

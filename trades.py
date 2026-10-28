#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trades.py — ЕДИНЫЙ загрузчик бумажных сделок скринера (блок A2 пакета
СКРИНЕР-ПОЛНЫЙ). Им пользуются metrics.py, verdict_1511.py и export_trades.py.

ЗАЧЕМ ЭТО НУЖНО ДЛЯ ДЕНЕГ
Вердикт 15.11.2026 решает, выписывать ли реальный чек. Он обязан считаться на
ОДНОМ списке сделок, а не на трёх слегка разных выборках, собранных в трёх
скриптах. Любое расхождение между «сделками вердикта», «сделками метрик» и
«сделками экспорта» превращает спор о цифре в спор о памяти. Поэтому список
собирается здесь один раз, и все читают его.

ЧТО ЗДЕСЬ ФАКТ, А ЧТО ВЫЧИСЛЕНО
  * ret_gross_pct БЕРЁТСЯ ИЗ поля ret_pct закрытой позиции и НЕ пересчитывается.
    Это данные турнира, записанные скринером в момент закрытия по правилам
    протокола; пересчёт по ценам дал бы «почти то же самое» число, и разница
    между ним и протокольным осталась бы незамеченной. Один источник истины.
  * ret_net_pct считается costs.net_ret_pct (мультипликативно, с оборота).
    Слиппедж внутри — ДОПУЩЕНИЕ, не замер (см. шапку costs.py): любое net-число
    публикуется только с таблицей чувствительности.
  * vol_usd_24h подтягивается из candidates_log.jsonl по cg_id: последняя строка
    с этим cg_id НЕ ПОЗЖЕ даты входа, поле features.vol. Именно «не позже» —
    объём, известный после входа, был бы заглядыванием в будущее.
  * liq_class — класс ликвидности по этому объёму; нет объёма -> 'unknown',
    а unknown считается по самому дорогому классу thin (costs.py).

ЧЕСТНАЯ ГРАНИЦА ДАННЫХ (проверено фактом 01.09.2026)
Журнал кандидатов начинается прогоном 03.08.2026, а первые позиции турнира и
портфеля A0 открыты 14–30.07.2026. У этих сделок суточного объёма на дату входа
в журнале ФИЗИЧЕСКИ НЕТ, и vol_usd_24h у них будет None -> класс unknown ->
издержки по худшему классу. Это не ошибка загрузчика и не повод подставить
объём «примерно из соседнего прогона»: число таких сделок обязано быть видно
отдельно (metrics.py, поле liq_class_fallback_n).

ОТКРЫТЫЕ ПОЗИЦИИ: ret_gross_pct и ret_net_pct = None ВСЕГДА.
§2 замороженных правил: «open_pnl в вердикт не идёт ни в каком виде» — ни как
основная метрика, ни как предварительный срез, ни как иллюстрация. Поэтому
открытая позиция физически не может принести сюда число: его тут нет.

Зависимости: stdlib + PyYAML (через costs.py). Сети и ключей не требует.
CLI: python trades.py — сводка по источникам и дорожкам, ничего не пишет.
"""

import json
import os
import sys
from datetime import date, datetime, timezone

import costs

__all__ = [
    "load_all_trades",
    "costs_cfg_for",
    "build_vol_index",
    "lookup_vol",
    "sources_report",
    "TRADE_FIELDS",
    "TOURNAMENT_JSON",
    "POSITIONS_JSON",
    "CANDIDATES_LOG",
]

TOURNAMENT_JSON = "tournament.json"
POSITIONS_JSON = "positions.json"
CANDIDATES_LOG = "candidates_log.jsonl"
COSTS_YAML = "costs.yaml"

# Код дорожки основного бумажного портфеля. В статистический тест §1 правил он
# НЕ входит (другая дата старта), но в метриках и экспорте нужен как второй
# порог §6 — поэтому грузится тем же загрузчиком и помечается отдельно.
A0_TRACK = "A0"

# Полная схема записи. Держим списком, чтобы потребители (export_trades.py)
# могли сверить набор полей, а не угадывать его.
TRADE_FIELDS = (
    "track",
    # bucket / bucket_mismatch добавлены 01.09.2026 после находки ревизии:
    # закрытость сделки определяет ВЕДРО (open/closed), а не наличие полей,
    # иначе нереализованный PnL открытой позиции попадает в вердикт вопреки
    # §2 замороженных правил. Расхождение видно потребителю, а не проглочено.
    "bucket",
    "bucket_mismatch",
    "sym",
    "cg_id",
    "entry_date",
    "exit_date",
    "entry_price",
    "exit_price",
    "last_price",
    "peak_price",
    "status",
    "close_reason",
    "hold_days",
    "ret_gross_pct",
    "vol_usd_24h",
    "liq_class",
    "ret_net_pct",
)

# Кэш индекса объёмов: журнал 8.6 МБ, а за один прогон его просят и метрики,
# и вердикт, и экспорт. Ключ включает размер и mtime — подменённый файл
# перечитывается, а не отдаётся из памяти.
_VOL_INDEX_CACHE = {}


# =========================================================================
#  СЛУЖЕБНОЕ
# =========================================================================

def _to_float(value):
    """Число или None. bool числом не считается (True не цена и не объём)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        val = float(value)
    elif isinstance(value, str):
        try:
            val = float(value.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    if val != val or val in (float("inf"), float("-inf")):  # nan / inf
        return None
    return val


def _day(value):
    """Первые 10 символов ISO-даты ('2026-07-20') либо None.

    Годится и для '2026-07-20', и для '2026-08-03T10:12:58Z': протокол
    сортировки и сравнения дат тут строковый, и это законно ровно потому,
    что формат ISO-8601 лексикографически упорядочен.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < 10:
        return None
    head = text[:10]
    if head[4] != "-" or head[7] != "-":
        return None
    if not (head[:4].isdigit() and head[5:7].isdigit() and head[8:10].isdigit()):
        return None
    return head


def _parse_date(value):
    """date из ISO-строки либо None. Не бросает."""
    head = _day(value)
    if head is None:
        return None
    try:
        return datetime.strptime(head, "%Y-%m-%d").date()
    except ValueError:
        return None


def _days_between(start, end):
    """Целое число суток между двумя ISO-датами; None, если дат нет."""
    d0 = _parse_date(start)
    d1 = _parse_date(end)
    if d0 is None or d1 is None:
        return None
    return (d1 - d0).days


def _read_json(path):
    """JSON-файл или None, если файла нет.

    Битый JSON НЕ проглатываем: молча вернуть пустой список сделок значит
    напечатать «закрытых сделок нет» вместо «файл сломан» — а это ровно тот
    случай, когда ошибка выглядит как результат.
    """
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def costs_cfg_for(repo_dir, costs_cfg=None):
    """Конфиг издержек: сначала costs.yaml рядом с данными, потом штатный.

    Публичная: metrics.py и verdict_1511.py обязаны считать net ПО ТОМУ ЖЕ
    конфигу, по которому его посчитал загрузчик, иначе в одном артефакте
    окажутся два разных net под одним именем.
    """
    if costs_cfg is not None:
        return costs_cfg
    local = os.path.join(repo_dir, COSTS_YAML)
    if os.path.isfile(local):
        return costs.load_costs(local)
    return costs.load_costs()


# Прежнее приватное имя оставлено рабочим: на него мог сослаться соседний блок.
_costs_cfg_for = costs_cfg_for


def _today_iso(as_of=None):
    """Опорная дата для возраста ОТКРЫТЫХ позиций (UTC).

    as_of задаётся в тестах и в вердикте с --as-of: возраст открытой позиции
    иначе зависел бы от того, в какую минуту запущен скрипт, и два артефакта
    одного дня расходились бы без причины.
    """
    if as_of is None:
        return datetime.now(timezone.utc).date().isoformat()
    if isinstance(as_of, datetime):
        return as_of.date().isoformat()
    if isinstance(as_of, date):
        return as_of.isoformat()
    day = _day(as_of)
    if day is None:
        raise ValueError("as_of: ожидалась дата ISO 'YYYY-MM-DD', получено %r" % (as_of,))
    return day


# =========================================================================
#  ИНДЕКС СУТОЧНЫХ ОБЪЁМОВ ИЗ ЖУРНАЛА КАНДИДАТОВ
# =========================================================================

def build_vol_index(log_path):
    """Индекс {cg_id: [(day, vol_usd_24h), ...]} ОДНИМ проходом по журналу.

    Журнал 8.6 МБ и растёт: читаем построчно и НИКОГДА не грузим целиком.
    Внутри списка порядок — как в файле (хронологический), поэтому «последняя
    строка не позже даты входа» ищется простым проходом с конца.

    Строки `_meta` и `_run` пропускаются, битые строки — тоже (журнал пишется
    в конце прогона и может быть оборван на полуслове; ронять из-за этого
    расчёт всех метрик нельзя). Счёт пропущенного возвращается вторым
    значением, чтобы порча журнала была ВИДНА числом, а не догадкой.

    -> (index, stats), stats = {'lines','coin_lines','bad_lines','runs','ids'}
    """
    index = {}
    stats = {"lines": 0, "coin_lines": 0, "bad_lines": 0, "runs": 0, "ids": 0}
    if not os.path.isfile(log_path):
        return index, stats

    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            stats["lines"] += 1
            try:
                obj = json.loads(raw)
            except ValueError:
                stats["bad_lines"] += 1
                continue
            if not isinstance(obj, dict):
                stats["bad_lines"] += 1
                continue
            if "_meta" in obj:
                continue
            if "_run" in obj:
                stats["runs"] += 1
                continue
            cg_id = obj.get("cg_id")
            if not cg_id or not isinstance(cg_id, str):
                continue
            stats["coin_lines"] += 1
            day = (_day(obj.get("timestamp_utc"))
                   or _day(obj.get("fetched_at"))
                   or _day(obj.get("run_id")))
            if day is None:
                continue
            features = obj.get("features")
            vol = _to_float(features.get("vol")) if isinstance(features, dict) else None
            index.setdefault(cg_id, []).append((day, vol))

    stats["ids"] = len(index)
    return index, stats


def _vol_index_cached(log_path):
    """Тот же индекс, но с кэшем по (путь, размер, mtime)."""
    full = os.path.abspath(log_path)
    try:
        st = os.stat(full)
        key = (full, st.st_size, st.st_mtime_ns)
    except OSError:
        key = (full, -1, -1)
    hit = _VOL_INDEX_CACHE.get(key)
    if hit is not None:
        return hit
    result = build_vol_index(full)
    _VOL_INDEX_CACHE.clear()      # держим только текущий журнал, память не копим
    _VOL_INDEX_CACHE[key] = result
    return result


def lookup_vol(index, cg_id, entry_date):
    """Суточный объём монеты на дату входа: последняя запись НЕ ПОЗЖЕ entry_date.

    Возвращает число или None. None означает ровно «в журнале на эту дату
    объёма нет» — и обязано превратиться в класс unknown, а не в ноль:
    нулевой оборот и отсутствие данных об обороте это разные факты.

    Строгое «не позже» защищает от заглядывания в будущее: объём, замеренный
    ПОСЛЕ входа, знать в момент входа было нельзя.
    """
    if not cg_id:
        return None
    rows = index.get(cg_id)
    if not rows:
        return None
    day = _day(entry_date)
    if day is None:
        return None
    for row_day, vol in reversed(rows):
        if row_day <= day:
            return vol
    return None


# =========================================================================
#  СБОРКА СДЕЛОК
# =========================================================================

def _make_trade(track, pos, index, cfg, today_iso, bucket="open"):
    """Одна запись сделки из позиции турнира или портфеля A0.

    `bucket` — из какого списка взята позиция: "open" или "closed".
    Он и есть источник истины о закрытости, см. ниже.
    """
    entry_date = _day(pos.get("entry_date"))
    entry_price = _to_float(pos.get("entry_price"))
    last_price = _to_float(pos.get("last_price"))
    peak_price = _to_float(pos.get("peak_price"))

    close_date = _day(pos.get("close_date"))
    has_ret = pos.get("ret_pct") is not None
    looks_closed = bool(close_date) or has_ret or bool(pos.get("close_reason"))

    # ЗАКРЫТОСТЬ ОПРЕДЕЛЯЕТ ВЕДРО, А НЕ НАЛИЧИЕ ПОЛЕЙ.
    #
    # Раньше здесь стояло `is_closed = looks_closed`, и это было дырой в §2
    # замороженных правил («open_pnl в вердикт не идёт ни в каком виде»).
    # Воспроизведено прогоном 01.09.2026: позиция, ОСТАВШАЯСЯ в
    # tracks.A1.open, но несущая ret_pct = 50.0, приезжала в вердикт как
    # закрытая — нереализованный PnL входил и в ранги, и в §6.
    # Такая позиция появляется не в теории: screener.py сначала делает
    # p.update({...ret_pct...}) и только потом переносит запись в closed
    # (screener.py, paper_update_positions и t_update). Обрыв прогона между
    # этими действиями, ручная правка файла или любое будущее добавление
    # «текущего результата» к открытой позиции дают ровно этот вход.
    #
    # Решение: ведро главнее. Позиция из "closed" — закрыта, из "open" —
    # открыта, ЧТО БЫ в ней ни лежало. Расхождение не проглатывается молча,
    # а помечается полем bucket_mismatch, чтобы его было видно в отчёте.
    in_closed_bucket = (bucket == "closed")
    is_closed = in_closed_bucket
    bucket_mismatch = (looks_closed != in_closed_bucket)

    if is_closed:
        status = "closed"
        exit_date = close_date
        # Цена выхода: A0 пишет close_price, турнир — нет (там выход по
        # last_price, см. screener.py). Порядок ровно такой, как в скринере.
        exit_price = _to_float(pos.get("close_price"))
        if exit_price is None:
            exit_price = last_price if last_price is not None else entry_price
        close_reason = pos.get("close_reason")
        # ФАКТ ТУРНИРА, НЕ ПЕРЕСЧЁТ: отклик берётся из ret_pct как есть.
        ret_gross_pct = _to_float(pos.get("ret_pct"))
        hold_days = _days_between(entry_date, exit_date)
    else:
        status = "open"
        exit_date = None
        exit_price = None
        close_reason = None
        ret_gross_pct = None          # §2 правил: open_pnl не считается никогда
        hold_days = _days_between(entry_date, today_iso)

    if hold_days is not None and hold_days < 0:
        hold_days = 0

    cg_id = pos.get("cg_id")
    vol = lookup_vol(index, cg_id, entry_date)
    liq_class = costs.classify_liquidity(vol, cfg)

    if status == "closed" and ret_gross_pct is not None:
        ret_net_pct = costs.net_ret_pct(ret_gross_pct, liq_class, hold_days, cfg)
    else:
        ret_net_pct = None

    return {
        "track": track,
        "sym": pos.get("sym"),
        "cg_id": cg_id,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "last_price": last_price,
        "peak_price": peak_price,
        "status": status,
        "bucket": bucket,
        "bucket_mismatch": bucket_mismatch,
        "close_reason": close_reason,
        "hold_days": hold_days,
        "ret_gross_pct": ret_gross_pct,
        "vol_usd_24h": vol,
        "liq_class": liq_class,
        "ret_net_pct": ret_net_pct,
    }


def load_all_trades(repo_dir=".", as_of=None, costs_cfg=None):
    """Единый список сделок турнира (A1..A8) и портфеля A0.

    repo_dir — корень репозитория скринера (там лежат tournament.json,
    positions.json, candidates_log.jsonl, costs.yaml).
    as_of — опорная дата для возраста ОТКРЫТЫХ позиций (по умолчанию сегодня
    UTC); на закрытые сделки не влияет вообще.
    costs_cfg — готовый конфиг издержек (иначе берётся costs.yaml из repo_dir,
    а если его там нет — штатный рядом с costs.py).

    Одна запись — см. TRADE_FIELDS и шапку модуля.

    Порядок: сначала A0, потом дорожки турнира по алфавиту; внутри дорожки —
    закрытые, затем открытые, как они лежат в файлах. Порядок детерминирован,
    чтобы датированные артефакты разных дней сравнивались построчно.

    Отсутствие файла источника — это ПУСТО, а не ошибка (на чистой машине
    turnir может ещё не существовать); битый JSON — ошибка и исключение.
    """
    repo_dir = os.path.abspath(repo_dir or ".")
    cfg = _costs_cfg_for(repo_dir, costs_cfg)
    today_iso = _today_iso(as_of)
    index, _stats = _vol_index_cached(os.path.join(repo_dir, CANDIDATES_LOG))

    trades = []

    # --- A0: основной бумажный портфель ---------------------------------
    ledger = _read_json(os.path.join(repo_dir, POSITIONS_JSON))
    if isinstance(ledger, dict):
        for bucket in ("closed", "open"):
            for pos in ledger.get(bucket) or []:
                if isinstance(pos, dict):
                    trades.append(_make_trade(A0_TRACK, pos, index, cfg,
                                              today_iso, bucket=bucket))

    # --- A1..A8: дорожки турнира ----------------------------------------
    tournament = _read_json(os.path.join(repo_dir, TOURNAMENT_JSON))
    if isinstance(tournament, dict):
        tracks = tournament.get("tracks")
        if isinstance(tracks, dict):
            for track in sorted(tracks.keys()):
                node = tracks.get(track)
                if not isinstance(node, dict):
                    continue
                for bucket in ("closed", "open"):
                    for pos in node.get(bucket) or []:
                        if isinstance(pos, dict):
                            trades.append(_make_trade(track, pos, index, cfg,
                                                      today_iso, bucket=bucket))

    return trades


def sources_report(repo_dir="."):
    """Паспорт входных файлов: есть ли, какого размера, когда изменены.

    Нужен артефакту метрик и светофору sanity.py: «закрытых сделок ноль» и
    «файла турнира нет» — разные новости, и по одному нулю их не различить.
    """
    repo_dir = os.path.abspath(repo_dir or ".")
    out = {}
    for name in (TOURNAMENT_JSON, POSITIONS_JSON, CANDIDATES_LOG, COSTS_YAML):
        path = os.path.join(repo_dir, name)
        item = {"exists": os.path.isfile(path), "size_bytes": None, "mtime_utc": None}
        if item["exists"]:
            try:
                st = os.stat(path)
                item["size_bytes"] = st.st_size
                item["mtime_utc"] = datetime.fromtimestamp(
                    st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except OSError:
                pass
        out[name] = item
    return out


# =========================================================================
#  CLI — только чтение и печать, ничего не пишет
# =========================================================================

def _main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    argv = list(sys.argv[1:] if argv is None else argv)
    repo_dir = argv[0] if argv else "."

    print("trades.py — единый загрузчик сделок (блок A2)")
    print("репозиторий: %s" % os.path.abspath(repo_dir))
    for name, info in sources_report(repo_dir).items():
        if info["exists"]:
            print("  %-22s есть, %d байт, изменён %s"
                  % (name, info["size_bytes"], info["mtime_utc"]))
        else:
            print("  %-22s НЕТ" % name)

    index, stats = _vol_index_cached(os.path.join(os.path.abspath(repo_dir), CANDIDATES_LOG))
    print("журнал кандидатов: %d строк, %d прогонов, %d монет в индексе, битых строк %d"
          % (stats["lines"], stats["runs"], stats["ids"], stats["bad_lines"]))

    trades = load_all_trades(repo_dir)
    if not trades:
        print("сделок нет вообще — проверь пути к tournament.json/positions.json")
        return 0

    tracks = {}
    for t in trades:
        node = tracks.setdefault(t["track"], {"open": 0, "closed": 0, "unknown_vol": 0})
        node[t["status"]] += 1
        if t["liq_class"] == "unknown":
            node["unknown_vol"] += 1

    print("")
    print("  %-6s %8s %8s %s" % ("дор.", "открыт", "закрыт", "без объёма на входе"))
    total_open = total_closed = total_unknown = 0
    for track in sorted(tracks):
        node = tracks[track]
        total_open += node["open"]
        total_closed += node["closed"]
        total_unknown += node["unknown_vol"]
        print("  %-6s %8d %8d %d" % (track, node["open"], node["closed"], node["unknown_vol"]))
    print("  %-6s %8d %8d %d" % ("ВСЕГО", total_open, total_closed, total_unknown))

    if total_closed == 0:
        print("")
        print("ЗАКРЫТЫХ СДЕЛОК НОЛЬ. Это не сбой: по правилам скринера позиция")
        print("закрывается на первом прогоне с возрастом >= 60 дней, первые")
        print("закрытия ожидаются 12-22.09.2026. Считать по открытым запрещает")
        print("§2 замороженных правил.")
    if total_unknown:
        print("")
        print("У %d сделок нет суточного объёма на дату входа -> класс unknown ->"
              % total_unknown)
        print("издержки по худшему классу thin. Журнал кандидатов начинается")
        print("03.08.2026, а часть позиций открыта раньше — данных за те дни нет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

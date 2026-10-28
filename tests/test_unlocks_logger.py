#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Селфтест календаря разлоков и листингов (блок D1): tests/test_unlocks_logger.py

В СЕТЬ НЕ ХОДИМ НИ РАЗУ. Все шесть функций загрузки подменяются заглушками,
поэтому тест одинаково работает на машине без интернета и в Actions, и его
результат зависит только от нашего кода, а не от настроения бирж.

Что проверяем (каждый пункт — про конкретную ошибку, которая уже стоила бы
дня данных):
  * разбор событий разлока: берутся ТОЛЬКО будущие и ТОЛЬКО в пределах
    90 дней, границы горизонта проверены с обеих сторон;
  * экранирование description: перевод строки не рвёт файл, запятая и
    кавычка возвращаются из csv тем же самым текстом;
  * колонка source проходит ТУ ЖЕ чистку, что и колонка slug: один и тот же
    внешний slug не должен попадать в файл двумя путями — очищенным и сырым;
  * порядок сбора: невосстановимые листинги и снимок перечней ложатся на
    диск ДО двухминутного обхода разлоков, поэтому падение на разлоках их
    больше не уносит;
  * дифф множеств символов: появилось/исчезло;
  * первый прогон НЕ объявляет листингом весь перечень биржи;
  * сбой перечня не превращается в волну ложных делистингов;
  * повторный прогон в тот же день даёт файл байт в байт (иначе второй
    прогон стёр бы утренние строки — база сравнения не должна перекатываться
    внутри суток);
  * сбой одного источника не валит второй, а пустота ОБОИХ даёт код 1.

Запуск:
    python tests/test_unlocks_logger.py
    python -m unittest discover -s tests -v
"""

import contextlib
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unlocks_logger as ul  # noqa: E402


UTC = timezone.utc
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
NOW_TS = int(NOW.timestamp())
YESTERDAY = NOW - timedelta(days=1)
SHA = "deadbeef1234"


# =========================================================================
# Заглушки источников
# =========================================================================

def emissions_payload(events, gecko_id="test-coin", name="Test Coin"):
    """Карточка эмиссии той же формы, что отдаёт CDN DefiLlama."""
    return {
        "gecko_id": gecko_id,
        "name": name,
        "metadata": {"events": events, "token": "TEST"},
        "supplyMetrics": {},
    }


def cliff(ts, tokens=1000, category="insiders", description="A cliff unlock"):
    return {"timestamp": ts, "noOfTokens": [tokens], "category": category,
            "unlockType": "cliff", "description": description}


@contextlib.contextmanager
def patched(**stubs):
    """Подменить перечисленные функции модуля на время блока.

    Имена — ровно те, что зовёт рабочий код: подмена по имени в модуле
    ловит и случай, когда кто-то поменяет реализацию загрузки.
    """
    with contextlib.ExitStack() as stack:
        for name, stub in stubs.items():
            assert hasattr(ul, name), "в модуле нет функции %s" % name
            stack.enter_context(mock.patch.object(ul, name, stub))
        yield


def slugs_stub(slugs):
    def _fn(session=None):
        return list(slugs)
    return _fn


def emissions_stub(mapping):
    def _fn(slug, session=None):
        if slug not in mapping:
            raise ul.SourceError("нет такого слага: %s" % slug)
        return mapping[slug]
    return _fn


def raising_stub(message="источник лежит"):
    def _fn(*args, **kwargs):
        raise ul.SourceError(message)
    return _fn


def symbols_stub(symbols):
    def _fn(session=None):
        return set(symbols)
    return _fn


def bybit_stub(items):
    def _fn(limit=ul.BYBIT_LIMIT, session=None):
        return list(items)
    return _fn


ALL_LISTING_SOURCES_DOWN = {
    "fetch_bybit_announcements": raising_stub("bybit лежит"),
    "fetch_binance_futures_symbols": raising_stub("binance лежит"),
    "fetch_mexc_spot_symbols": raising_stub("mexc лежит"),
    "fetch_hyperliquid_symbols": raising_stub("hyperliquid лежит"),
}


def many(prefix, count):
    """Правдоподобный по размеру перечень символов."""
    return {"%s%03d" % (prefix, i) for i in range(count)}


def read_csv(path):
    """Файл -> (заголовок, список строк-словарей). Читаем ИМЕННО парсером csv."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    header, body = rows[0], rows[1:]
    return header, [dict(zip(header, r)) for r in body]


# =========================================================================
# Чистка текста и чисел
# =========================================================================

class TestTextAndNumbers(unittest.TestCase):

    def test_переводы_строк_и_табы_схлопываются(self):
        got = ul.csv_clean("первая\nвторая\r\nтретья\tчетвёртая")
        self.assertEqual(got, "первая вторая третья четвёртая")
        self.assertNotIn("\n", got)
        self.assertNotIn("\r", got)
        self.assertNotIn("\t", got)

    def test_пустое_и_не_строка(self):
        self.assertEqual(ul.csv_clean(None), "")
        self.assertEqual(ul.csv_clean("   "), "")
        self.assertEqual(ul.csv_clean(123), "123")

    def test_формула_гасится_апострофом(self):
        # Классическая CSV-инъекция: чужой текст, начинающийся со знака
        # равенства, Excel исполняет как формулу.
        for evil in ("=1+1", "+cmd", "-2+3", "@SUM(A1)"):
            self.assertTrue(ul.csv_clean(evil).startswith("'"), evil)
        # А нормальный текст не трогаем.
        self.assertFalse(ul.csv_clean("Cliff unlock").startswith("'"))

    def test_обрезка_длинного_текста(self):
        self.assertEqual(len(ul.csv_clean("я" * 1000)), ul.MAX_TEXT_LEN)

    def test_запятые_и_кавычки_переживают_круг_через_csv(self):
        # Их НЕ вырезаем: экранирует модуль csv, данные остаются целыми.
        text = 'Unlock of 1,000 tokens from "Team, LLC" round'
        cleaned = ul.csv_clean(text)
        self.assertEqual(cleaned, text)
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerow(["a", cleaned, "b"])
        parsed = list(csv.reader(io.StringIO(buf.getvalue())))[0]
        self.assertEqual(parsed[1], text)
        self.assertEqual(len(parsed), 3)      # запятая не породила лишнее поле

    def test_числа_без_экспоненты(self):
        self.assertEqual(ul._fmt_number(100000000), "100000000")
        self.assertEqual(ul._fmt_number(1e21), "1000000000000000000000")
        self.assertEqual(ul._fmt_number(805600.0), "805600")
        self.assertEqual(ul._fmt_number(0.5), "0.5")
        self.assertEqual(ul._fmt_number("1380000"), "1380000")
        self.assertEqual(ul._fmt_number("не число"), "")
        self.assertEqual(ul._fmt_number(None), "")
        self.assertEqual(ul._fmt_number(True), "")

    def test_список_токенов_склеивается_вертикальной_чертой(self):
        # linear-событие несёт СТАРУЮ и НОВУЮ скорость; свернуть в одно число
        # нельзя, не соврав.
        self.assertEqual(ul.tokens_field([805600, 1380000]), "805600|1380000")
        self.assertEqual(ul.tokens_field([100000000]), "100000000")
        self.assertEqual(ul.tokens_field(None), "")
        self.assertEqual(ul.tokens_field(42), "42")

    def test_отметки_времени(self):
        self.assertEqual(ul.normalize_ts_seconds(1759363200), 1759363200)
        # миллисекунды Bybit
        self.assertEqual(ul.normalize_ts_seconds(1788260045000), 1788260045)
        self.assertEqual(ul.normalize_ts_seconds("1759363200"), 1759363200)
        for junk in (None, "", "abc", 0, -5, 1e18, True, float("nan")):
            self.assertIsNone(ul.normalize_ts_seconds(junk), junk)

    def test_перевод_секунд_в_дату(self):
        self.assertEqual(ul.ts_to_iso(1759363200), "2025-10-02T00:00:00Z")
        self.assertEqual(ul.ts_to_date(1759363200), "2025-10-02")


# =========================================================================
# Разбор событий разлока
# =========================================================================

class TestParseUnlockEvents(unittest.TestCase):

    def test_берутся_только_будущие_и_только_в_горизонте(self):
        horizon = ul.HORIZON_DAYS * 86400
        events = [
            cliff(NOW_TS - 86400, description="вчера — не берём"),
            cliff(NOW_TS, description="ровно сейчас — не берём"),
            cliff(NOW_TS + 1, description="через секунду — берём"),
            cliff(NOW_TS + 30 * 86400, description="через 30 дней — берём"),
            cliff(NOW_TS + horizon, description="ровно граница — берём"),
            cliff(NOW_TS + horizon + 1, description="за границей — не берём"),
        ]
        rows = ul.parse_unlock_events(emissions_payload(events), "test", NOW_TS)
        got = sorted(r["description"] for r in rows)
        self.assertEqual(got, sorted([
            "через секунду — берём",
            "через 30 дней — берём",
            "ровно граница — берём",
        ]))

    def test_горизонт_настраивается(self):
        events = [cliff(NOW_TS + 10 * 86400)]
        self.assertEqual(len(ul.parse_unlock_events(
            emissions_payload(events), "t", NOW_TS, horizon_days=7)), 0)
        self.assertEqual(len(ul.parse_unlock_events(
            emissions_payload(events), "t", NOW_TS, horizon_days=30)), 1)

    def test_поля_строки_заполнены(self):
        events = [{
            "timestamp": NOW_TS + 86400,
            "noOfTokens": [805600, 1380000],
            "category": "ecosystem",
            "unlockType": "linear",
            "description": "Linear unlock was increased\nfrom {tokens[0]} tokens",
        }]
        rows = ul.parse_unlock_events(
            emissions_payload(events), "drift", NOW_TS,
            snapshot_date="2026-09-01", git_sha_value=SHA)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(sorted(row.keys()), sorted(ul.UNLOCKS_HEADER))
        self.assertEqual(row["slug"], "drift")
        self.assertEqual(row["gecko_id"], "test-coin")
        self.assertEqual(row["name"], "Test Coin")
        self.assertEqual(row["event_date"], "2026-09-02")
        self.assertEqual(row["event_ts_utc"], "2026-09-02T12:00:00Z")
        self.assertEqual(row["category"], "ecosystem")
        self.assertEqual(row["unlock_type"], "linear")
        self.assertEqual(row["n_tokens"], "805600|1380000")
        self.assertEqual(row["snapshot_date"], "2026-09-01")
        self.assertEqual(row["git_sha"], SHA)
        self.assertIn("drift", row["source"])
        # Шаблон {tokens[0]} остаётся как есть: это чужой текст, а не наш
        # формат-шаблон, и .format() на нём звать нельзя.
        self.assertIn("{tokens[0]}", row["description"])
        self.assertNotIn("\n", row["description"])

    def test_source_очищается_так_же_как_slug(self):
        """Slug внешний, и подстановка его в шаблон URL внешности не отменяет.

        Раньше колонка source собиралась как EMISSIONS_ITEM_URL % slug БЕЗ
        csv_clean, хотя соседняя колонка slug очищалась: одна и та же чужая
        строка попадала в файл двумя путями, и перевод строки внутри неё рвал
        csv-строку надвое.
        """
        evil = "evil\nslug\tс табом"
        rows = ul.parse_unlock_events(
            emissions_payload([cliff(NOW_TS + 86400)]), evil, NOW_TS,
            snapshot_date="2026-09-01", git_sha_value=SHA)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source"], ul.csv_clean(ul.EMISSIONS_ITEM_URL % evil))
        for control in ("\n", "\r", "\t"):
            self.assertNotIn(control, row["source"])
        # Обе колонки несут ОДИН И ТОТ ЖЕ вид слага, а не два разных.
        self.assertEqual(row["slug"], "evil slug с табом")
        self.assertTrue(row["source"].endswith(row["slug"]), row["source"])

    def test_мусор_не_роняет_разбор(self):
        events = [
            cliff(NOW_TS + 86400, description="нормальное"),
            {"timestamp": "не число"},
            {"noOfTokens": [1]},                       # нет timestamp
            "строка вместо события",
            None,
        ]
        rows = ul.parse_unlock_events(emissions_payload(events), "t", NOW_TS)
        self.assertEqual(len(rows), 1)

    def test_кривая_карточка_даёт_пусто_а_не_исключение(self):
        self.assertEqual(ul.parse_unlock_events(None, "t", NOW_TS), [])
        self.assertEqual(ul.parse_unlock_events("строка", "t", NOW_TS), [])
        self.assertEqual(ul.parse_unlock_events({}, "t", NOW_TS), [])
        self.assertEqual(ul.parse_unlock_events(
            {"metadata": {"events": "не список"}}, "t", NOW_TS), [])

    def test_обход_слагов_и_лимит(self):
        payload = emissions_payload([cliff(NOW_TS + 86400)])
        mapping = {"a": payload, "b": payload, "c": payload}
        with patched(fetch_unlock_slugs=slugs_stub(["a", "b", "c"]),
                     fetch_emissions=emissions_stub(mapping)):
            rows, failures, stats = ul.collect_unlocks(NOW, git_sha_value=SHA)
            self.assertEqual(stats["slugs_ok"], 3)
            self.assertEqual(len(rows), 3)
            self.assertEqual(failures, [])
            rows2, _, stats2 = ul.collect_unlocks(NOW, limit=2, git_sha_value=SHA)
            self.assertEqual(stats2["slugs_total"], 2)
            self.assertEqual(len(rows2), 2)

    def test_один_слаг_отвалился_остальные_записаны(self):
        payload = emissions_payload([cliff(NOW_TS + 86400)])
        mapping = {"a": payload, "c": payload}       # "b" отсутствует -> ошибка
        with patched(fetch_unlock_slugs=slugs_stub(["a", "b", "c"]),
                     fetch_emissions=emissions_stub(mapping)):
            rows, failures, stats = ul.collect_unlocks(NOW, git_sha_value=SHA)
        self.assertEqual(len(rows), 2)
        self.assertEqual(stats["slugs_failed"], 1)
        self.assertEqual(len(failures), 1)

    def test_дубли_убираются_и_порядок_стабилен(self):
        same = cliff(NOW_TS + 86400, description="одно и то же")
        payload = emissions_payload([same, dict(same)])
        with patched(fetch_unlock_slugs=slugs_stub(["z", "a"]),
                     fetch_emissions=emissions_stub({"z": payload, "a": payload})):
            rows, _, _ = ul.collect_unlocks(NOW, git_sha_value=SHA)
        self.assertEqual(len(rows), 2)                       # по одной на слаг
        self.assertEqual([r["slug"] for r in rows], ["a", "z"])   # отсортировано


# =========================================================================
# Дифф множеств символов
# =========================================================================

class TestDiff(unittest.TestCase):

    def test_появилось_и_исчезло(self):
        got = ul.diff_symbol_sets({"AAA", "BBB", "CCC"}, {"BBB", "CCC", "DDD"})
        self.assertEqual(got["appeared"], ["DDD"])
        self.assertEqual(got["disappeared"], ["AAA"])

    def test_без_изменений(self):
        got = ul.diff_symbol_sets(["AAA"], ["AAA"])
        self.assertEqual(got, {"appeared": [], "disappeared": []})

    def test_списки_отсортированы(self):
        got = ul.diff_symbol_sets([], ["ZZZ", "AAA", "MMM"])
        self.assertEqual(got["appeared"], ["AAA", "MMM", "ZZZ"])

    def test_пустые_входы(self):
        self.assertEqual(ul.diff_symbol_sets(None, None),
                         {"appeared": [], "disappeared": []})


# =========================================================================
# Листинги: база сравнения и её ловушки
# =========================================================================

class ListingsCase(unittest.TestCase):

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="unlocks_test_")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def seed_snapshot(self, venues, date_str):
        """Положить снимок, будто он снят в указанный день."""
        ul.save_symbols_snapshot(ul.symbols_snapshot_path(self.repo), {
            "baseline_date": None,
            "baseline": {},
            "latest_date": date_str,
            "latest": {k: sorted(v) for k, v in venues.items()},
        })

    def snapshot(self):
        return ul.load_symbols_snapshot(ul.symbols_snapshot_path(self.repo))


class TestCollectListings(ListingsCase):

    def test_первый_прогон_не_объявляет_листингом_всю_биржу(self):
        stubs = {
            "fetch_bybit_announcements": bybit_stub([]),
            "fetch_binance_futures_symbols": symbols_stub(many("BIN", 50)),
            "fetch_mexc_spot_symbols": symbols_stub(many("MEX", 60)),
            "fetch_hyperliquid_symbols": symbols_stub(many("HL", 40)),
        }
        with patched(**stubs):
            rows, failures, snap = ul.collect_listings(NOW, self.repo, git_sha_value=SHA)
        self.assertEqual(rows, [])
        self.assertEqual(failures, [])
        self.assertEqual(snap["latest_date"], "2026-09-01")
        self.assertEqual(len(snap["latest"]["binance_futures"]), 50)

    def test_второй_день_даёт_листинг_и_делистинг(self):
        base = many("BIN", 50)
        self.seed_snapshot({"binance_futures": base}, YESTERDAY.strftime("%Y-%m-%d"))
        today = (base - {"BIN000"}) | {"NEWUSDT"}
        stubs = dict(ALL_LISTING_SOURCES_DOWN)
        stubs["fetch_bybit_announcements"] = bybit_stub([])
        stubs["fetch_binance_futures_symbols"] = symbols_stub(today)
        with patched(**stubs):
            rows, failures, snap = ul.collect_listings(NOW, self.repo, git_sha_value=SHA)
        by_kind = {(r["kind"], r["symbol"]) for r in rows}
        self.assertIn(("listing", "NEWUSDT"), by_kind)
        self.assertIn(("delisting", "BIN000"), by_kind)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(sorted(row.keys()), sorted(ul.LISTINGS_HEADER))
            self.assertEqual(row["venue"], "binance_futures")
            self.assertEqual(row["git_sha"], SHA)
            self.assertEqual(row["event_ts_utc"], "2026-09-01T12:00:00Z")
        # Площадки, которые лежат, дали отказы — но не строки.
        self.assertEqual(len(failures), 2)

    def test_сбой_перечня_не_рождает_волну_делистингов(self):
        base = many("BIN", 50)
        self.seed_snapshot({"binance_futures": base}, YESTERDAY.strftime("%Y-%m-%d"))
        stubs = dict(ALL_LISTING_SOURCES_DOWN)
        stubs["fetch_bybit_announcements"] = bybit_stub([])
        with patched(**stubs):
            rows, failures, snap = ul.collect_listings(NOW, self.repo, git_sha_value=SHA)
        self.assertEqual(rows, [])                       # ни одного делистинга
        self.assertEqual(len(failures), 3)
        # База сравнения цела: завтрашний дифф всё догонит.
        self.assertEqual(sorted(snap["latest"]["binance_futures"]), sorted(base))

    def test_обрезанный_перечень_считается_сбоем(self):
        base = many("BIN", 50)
        self.seed_snapshot({"binance_futures": base}, YESTERDAY.strftime("%Y-%m-%d"))
        stubs = dict(ALL_LISTING_SOURCES_DOWN)
        stubs["fetch_bybit_announcements"] = bybit_stub([])
        stubs["fetch_binance_futures_symbols"] = symbols_stub({"BIN000", "BIN001"})
        with patched(**stubs):
            rows, failures, snap = ul.collect_listings(NOW, self.repo, git_sha_value=SHA)
        self.assertEqual(rows, [])
        self.assertTrue(any("считаю сбоем" in f for f in failures))
        self.assertEqual(len(snap["latest"]["binance_futures"]), 50)

    def test_массовый_дифф_помечается_но_не_глушится(self):
        base = many("BIN", 100)
        self.seed_snapshot({"binance_futures": base}, YESTERDAY.strftime("%Y-%m-%d"))
        today = set(sorted(base)[:50])                  # исчезла половина
        stubs = dict(ALL_LISTING_SOURCES_DOWN)
        stubs["fetch_bybit_announcements"] = bybit_stub([])
        stubs["fetch_binance_futures_symbols"] = symbols_stub(today)
        with patched(**stubs):
            rows, _, _ = ul.collect_listings(NOW, self.repo, git_sha_value=SHA)
        self.assertEqual(len(rows), 50)                 # строки на месте
        self.assertTrue(all("массовый дифф" in r["title"] for r in rows))

    def test_объявления_bybit_разбираются(self):
        items = [
            {"title": "New listing: MARSCOINUSDT Perpetual Contract",
             "description": "", "url": "https://example.invalid/a",
             "dateTimestamp": 1788260045000},
            {"title": "Плохая\nстрока, с запятой", "url": "", "dateTimestamp": "мусор"},
            "не объект",
        ]
        rows = ul.parse_bybit_announcements(items, snapshot_date="2026-09-01",
                                            git_sha_value=SHA)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["kind"] == "announcement" for r in rows))
        self.assertTrue(all(r["venue"] == "bybit" for r in rows))
        self.assertTrue(all(r["symbol"] == "" for r in rows))   # тикер не выдумываем
        # 1788260045000 мс = 1788260045 с = 01.09.2026 10:54:05 UTC
        self.assertEqual(rows[0]["event_ts_utc"], "2026-09-01T10:54:05Z")
        self.assertEqual(rows[1]["event_ts_utc"], "")           # мусорная дата -> пусто
        self.assertNotIn("\n", rows[1]["title"])

    def test_битый_снимок_не_роняет_прогон(self):
        path = ul.symbols_snapshot_path(self.repo)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{это не json")
        stubs = dict(ALL_LISTING_SOURCES_DOWN)
        stubs["fetch_bybit_announcements"] = bybit_stub([])
        stubs["fetch_binance_futures_symbols"] = symbols_stub(many("BIN", 50))
        with patched(**stubs):
            rows, _, snap = ul.collect_listings(NOW, self.repo, git_sha_value=SHA)
        self.assertEqual(rows, [])                      # считаем первым прогоном
        self.assertEqual(len(snap["latest"]["binance_futures"]), 50)


# =========================================================================
# Прогон целиком: файлы, идемпотентность, коды возврата
# =========================================================================

class TestRun(ListingsCase):

    def full_stubs(self, unlock_events=None, bybit_items=None,
                   binance=None, mexc=None, hl=None):
        events = unlock_events if unlock_events is not None else [cliff(NOW_TS + 86400)]
        payload = emissions_payload(events)
        return {
            "fetch_unlock_slugs": slugs_stub(["alpha", "beta"]),
            "fetch_emissions": emissions_stub({"alpha": payload, "beta": payload}),
            "fetch_bybit_announcements": bybit_stub(
                bybit_items if bybit_items is not None else []),
            "fetch_binance_futures_symbols": symbols_stub(
                binance if binance is not None else many("BIN", 50)),
            "fetch_mexc_spot_symbols": symbols_stub(
                mexc if mexc is not None else many("MEX", 60)),
            "fetch_hyperliquid_symbols": symbols_stub(
                hl if hl is not None else many("HL", 40)),
        }

    def test_файлы_ложатся_по_помесячным_папкам(self):
        with patched(**self.full_stubs()):
            report = ul.run(repo_dir=self.repo, now=NOW, git_sha_value=SHA)
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["unlocks_path"].endswith(
            os.path.join("2026-09", "unlocks_20260901.csv")))
        self.assertTrue(report["listings_path"].endswith(
            os.path.join("2026-09", "listings_20260901.csv")))
        header, rows = read_csv(report["unlocks_path"])
        self.assertEqual(header, ul.UNLOCKS_HEADER)
        self.assertEqual(len(rows), 2)
        header, _ = read_csv(report["listings_path"])
        self.assertEqual(header, ul.LISTINGS_HEADER)

    def test_к_началу_обхода_разлоков_листинги_уже_на_диске(self):
        """Порядок записи, проверенный изнутри прогона, а не по коду глазами."""
        seen = {}

        def slugs_fn(session=None):
            seen["listings_csv"] = os.path.exists(
                ul.daily_csv_path(self.repo, "listings", NOW))
            seen["snapshot"] = os.path.exists(ul.symbols_snapshot_path(self.repo))
            return ["alpha"]

        stubs = self.full_stubs()
        stubs["fetch_unlock_slugs"] = slugs_fn
        with patched(**stubs):
            report = ul.run(repo_dir=self.repo, now=NOW, git_sha_value=SHA)
        self.assertTrue(seen.get("listings_csv"),
                        "csv листингов обязан лечь на диск ДО обхода разлоков")
        self.assertTrue(seen.get("snapshot"),
                        "снимок перечней обязан лечь на диск ДО обхода разлоков")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["unlocks_rows"], 1)

    def test_смерть_на_обходе_разлоков_не_уносит_листинги(self):
        """Главная защита порядка: прогон убит таймаутом на разлоках.

        Разлоки завтра отдадутся те же самые, а дифф перечней за пропущенный
        день не восстановит никто — биржа истории своих перечней не отдаёт.
        Поэтому листинги и снимок обязаны быть уже на диске к моменту смерти.
        KeyboardInterrupt взят намеренно: это BaseException, и внутренние
        `except Exception` его не перехватывают — точная модель убитого
        по таймауту прогона.
        """
        base = many("BIN", 50)
        self.seed_snapshot({"binance_futures": base}, YESTERDAY.strftime("%Y-%m-%d"))
        today = (base - {"BIN000"}) | {"NEWUSDT"}

        def killed(slug, session=None):
            raise KeyboardInterrupt("прогон убит на обходе 372 слагов")

        stubs = self.full_stubs(binance=today)
        stubs["fetch_emissions"] = killed
        with patched(**stubs):
            with self.assertRaises(KeyboardInterrupt):
                ul.run(repo_dir=self.repo, now=NOW, git_sha_value=SHA)

        listings_path = ul.daily_csv_path(self.repo, "listings", NOW)
        self.assertTrue(os.path.exists(listings_path),
                        "csv листингов потерян вместе с упавшими разлоками")
        header, rows = read_csv(listings_path)
        self.assertEqual(header, ul.LISTINGS_HEADER)
        self.assertEqual({(r["kind"], r["symbol"]) for r in rows},
                         {("listing", "NEWUSDT"), ("delisting", "BIN000")})
        # Снимок — база ЗАВТРАШНЕГО диффа — записан тоже: без него потерян и
        # завтрашний день, а не только сегодняшний.
        snap = self.snapshot()
        self.assertEqual(snap["latest_date"], "2026-09-01")
        self.assertEqual(sorted(snap["latest"]["binance_futures"]), sorted(today))
        # Разлоков за день нет — и это ровно та потеря, которую догонит завтра.
        self.assertFalse(os.path.exists(ul.daily_csv_path(self.repo, "unlocks", NOW)))

    def test_кривой_slug_не_рвёт_строку_файла(self):
        """Сырой slug в колонке source рвал csv-строку надвое — больше нет."""
        evil = "ev\nil"
        payload = emissions_payload([cliff(NOW_TS + 86400)])
        stubs = self.full_stubs()
        stubs["fetch_unlock_slugs"] = slugs_stub([evil])
        stubs["fetch_emissions"] = emissions_stub({evil: payload})
        with patched(**stubs):
            report = ul.run(repo_dir=self.repo, now=NOW, git_sha_value=SHA)
        header, rows = read_csv(report["unlocks_path"])
        self.assertEqual(header, ul.UNLOCKS_HEADER)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), len(ul.UNLOCKS_HEADER))
        self.assertEqual(rows[0]["slug"], "ev il")
        self.assertTrue(rows[0]["source"].endswith("ev il"), rows[0]["source"])
        # Файл не разъехался: шапка + одна строка, ровно две физические строки.
        with open(report["unlocks_path"], "r", encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 2)

    def test_повторный_прогон_за_тот_же_день_даёт_тот_же_файл(self):
        # Ключевая ловушка: после первого прогона перечень уже записан. Если
        # база сравнения перекатится внутри суток, второй прогон увидит
        # «изменений нет» и СОТРЁТ утренние строки листингов.
        base = many("BIN", 50)
        self.seed_snapshot({"binance_futures": base}, YESTERDAY.strftime("%Y-%m-%d"))
        today = (base - {"BIN000"}) | {"NEWUSDT"}
        stubs = self.full_stubs(binance=today)
        with patched(**stubs):
            first = ul.run(repo_dir=self.repo, now=NOW, git_sha_value=SHA)
            with open(first["listings_path"], "rb") as fh:
                bytes_1 = fh.read()
            with open(first["unlocks_path"], "rb") as fh:
                unlock_bytes_1 = fh.read()
            second = ul.run(repo_dir=self.repo, now=NOW, git_sha_value=SHA)
            with open(second["listings_path"], "rb") as fh:
                bytes_2 = fh.read()
            with open(second["unlocks_path"], "rb") as fh:
                unlock_bytes_2 = fh.read()
        self.assertEqual(first["listings_path"], second["listings_path"])
        self.assertEqual(bytes_1, bytes_2)
        self.assertEqual(unlock_bytes_1, unlock_bytes_2)
        self.assertEqual(first["listings_rows"], 2)
        self.assertEqual(second["listings_rows"], 2)

    def test_следующий_день_считает_дифф_от_сегодняшнего(self):
        stubs = self.full_stubs(binance=many("BIN", 50))
        with patched(**stubs):
            ul.run(repo_dir=self.repo, now=NOW, git_sha_value=SHA)
        tomorrow = NOW + timedelta(days=1)
        stubs2 = self.full_stubs(binance=many("BIN", 50) | {"FRESHUSDT"})
        with patched(**stubs2):
            report = ul.run(repo_dir=self.repo, now=tomorrow, git_sha_value=SHA)
        _, rows = read_csv(report["listings_path"])
        self.assertEqual([(r["kind"], r["symbol"]) for r in rows],
                         [("listing", "FRESHUSDT")])

    def test_описание_с_запятой_и_кавычкой_возвращается_целым(self):
        evil = 'A cliff of 1,000 tokens from "Team, LLC"\nsecond line'
        expected = 'A cliff of 1,000 tokens from "Team, LLC" second line'
        stubs = self.full_stubs(
            unlock_events=[cliff(NOW_TS + 86400, description=evil)])
        with patched(**stubs):
            report = ul.run(repo_dir=self.repo, now=NOW, git_sha_value=SHA)
        header, rows = read_csv(report["unlocks_path"])
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["description"], expected)
            self.assertEqual(len(row), len(ul.UNLOCKS_HEADER))
        # И построчно файл не разъехался: строк ровно 3 (шапка + 2).
        with open(report["unlocks_path"], "r", encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 3)

    def test_сбой_разлоков_не_валит_листинги(self):
        stubs = self.full_stubs(bybit_items=[
            {"title": "New listing: FOOUSDT", "url": "https://example.invalid/x",
             "dateTimestamp": 1788260045000}])
        stubs["fetch_unlock_slugs"] = raising_stub("defillama лежит")
        with patched(**stubs):
            report = ul.run(repo_dir=self.repo, now=NOW, git_sha_value=SHA)
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["unlocks_rows"], 0)
        self.assertEqual(report["listings_rows"], 1)
        # Пустой файл разлоков всё равно написан — с шапкой, без строк.
        header, rows = read_csv(report["unlocks_path"])
        self.assertEqual(header, ul.UNLOCKS_HEADER)
        self.assertEqual(rows, [])
        self.assertTrue(any("defillama лежит" in f for f in report["failures"]))

    def test_сбой_листингов_не_валит_разлоки(self):
        stubs = self.full_stubs()
        stubs.update(ALL_LISTING_SOURCES_DOWN)
        with patched(**stubs):
            report = ul.run(repo_dir=self.repo, now=NOW, git_sha_value=SHA)
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["unlocks_rows"], 2)
        self.assertEqual(report["listings_rows"], 0)
        self.assertEqual(len(report["failures"]), 4)

    def test_пустота_обоих_источников_даёт_код_1(self):
        stubs = dict(ALL_LISTING_SOURCES_DOWN)
        stubs["fetch_unlock_slugs"] = raising_stub("defillama лежит")
        stubs["fetch_emissions"] = raising_stub("defillama лежит")
        with patched(**stubs):
            report = ul.run(repo_dir=self.repo, now=NOW, git_sha_value=SHA)
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["unlocks_rows"], 0)
        self.assertEqual(report["listings_rows"], 0)

    def test_живой_источник_без_событий_это_ноль_строк_и_тоже_код_1(self):
        # Честность: «источники ответили, но событий нет» — тоже повод
        # разбудить пилота, потому что 372 протокола без единого разлока
        # на 90 дней это скорее сломанный разбор, чем спокойный рынок.
        stubs = self.full_stubs(unlock_events=[cliff(NOW_TS - 86400)])
        stubs.update(ALL_LISTING_SOURCES_DOWN)
        with patched(**stubs):
            report = ul.run(repo_dir=self.repo, now=NOW, git_sha_value=SHA)
        self.assertEqual(report["exit_code"], 1)

    def test_снимок_символов_лежит_по_канону(self):
        with patched(**self.full_stubs()):
            ul.run(repo_dir=self.repo, now=NOW, git_sha_value=SHA)
        path = ul.symbols_snapshot_path(self.repo)
        self.assertTrue(path.endswith(os.path.join("data", "listings",
                                                   "symbols_latest.json")))
        with open(path, "r", encoding="utf-8") as fh:
            snap = json.load(fh)
        self.assertEqual(sorted(snap["venues_ok"]), sorted(ul.VENUES))
        self.assertEqual(snap["git_sha"], SHA)
        self.assertEqual(snap["latest_date"], "2026-09-01")


if __name__ == "__main__":
    unittest.main(verbosity=2)

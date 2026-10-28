#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Селфтест почасового логгера фандинга (блок D2): tests/test_funding_logger.py

СЕТИ НЕ КАСАЕТСЯ ВООБЩЕ. Весь путь «ответ биржи -> строки csv -> файл»
проверяется на заглушке: тест обязан ловить нашу ошибку разбора, а не
доступность Hyperliquid. Тест, ходящий в сеть, краснеет от чужих проблем и
через месяц его перестают читать.

Проверяем ровно то, на чём стоит ценность архива:
  * выравнивание universe и контекстов ПО ИНДЕКСУ (общего ключа нет —
    перепутанная пара означает фандинг одной монеты, записанный под именем
    другой; такую порчу потом не обнаружить);
  * рассогласование длин двух списков: обрезка по минимуму + сообщение,
    БЕЗ падения (иначе одна кривая отдача биржи стоит нам целого часа);
  * идемпотентность по ключу (ПЛАНОВЫЙ СЛОТ cron, монета): повторный прогон
    в том же слоте не удваивает строки, а ЗАДЕРЖАННЫЙ прогон соседнего слота
    свои данные записывает (находка ревизии 01.09.2026: ключ брался из часа
    фактического старта, и задержка Actions на десятки минут заставляла
    следующий слот молча считать себя дублем);
  * пропуск часов виден: run_once называет дырку; у main РОВНО ТРИ исхода
    (С-22, 02.09.2026): healed -> 0 + ИНФО, partial -> 2, error -> 1 — каждый
    проверен сквозным прогоном main() на заглушках;
  * С-24 (03.09.2026): пропуск считается ПО ПАРАМ (слот, монета) для монет
    текущей вселенной; оборванный добор продолжается с недобранных монет
    (старейший долг первым); пара «без истории у биржи» — не дефект и не
    метка в архиве; архив, молчавший дольше окна, краснеет; ИНФО — только на
    пропуск от INFO_GAP_HOURS часов, рутинный healed оставляет ::notice::;
  * делистнутые инструменты (нет midPx и premium) -> ПУСТЫЕ ячейки, не нули;
  * числа кладутся строками ровно как отдал сервер (без float-округления);
  * заголовок пишется один раз, при создании файла.

Запуск: python -m unittest discover -s tests -v
"""

import contextlib
import csv
import io
import itertools
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import funding_logger as fl  # noqa: E402

from datetime import datetime, timezone  # noqa: E402


TS = "2026-09-01T21:07:03Z"
SHA = "0123456789ab"


def uni(name, lev=10, delisted=False):
    """Запись universe в форме, которую реально отдаёт биржа (факт 01.09.2026)."""
    rec = {"szDecimals": 5, "name": name, "maxLeverage": lev, "marginTableId": 56}
    if delisted:
        rec["isDelisted"] = True
    return rec


def ctx(funding="0.0000125", oi="100.5", mark="77350.5", oracle="77369.0",
        mid="77350.5", premium="-0.0002326513", ntl="2891981894.68",
        base="37067.29242"):
    """Контекст инструмента. Значения — строки, как в живом ответе."""
    rec = {
        "funding": funding,
        "openInterest": oi,
        "prevDayPx": "79084.0",
        "dayNtlVlm": ntl,
        "oraclePx": oracle,
        "markPx": mark,
        "impactPxs": ["77349.0", "77351.0"],
        "dayBaseVlm": base,
    }
    # У делистнутых полей midPx/premium в ответе НЕТ вовсе — не None, а отсутствуют.
    if mid is not None:
        rec["midPx"] = mid
    if premium is not None:
        rec["premium"] = premium
    return rec


def payload(universe, ctxs):
    """Форма ответа metaAndAssetCtxs: [ {universe: [...]}, [ctx, ...] ]."""
    return [{"universe": universe}, ctxs]


def rows_as_dicts(rows):
    """Строки -> словари по CSV_HEADER, чтобы тест читался именами колонок."""
    return [dict(zip(fl.CSV_HEADER, r)) for r in rows]


class TestParsePayload(unittest.TestCase):
    """Разбор ответа-заглушки в строки csv."""

    def test_schema_and_order(self):
        res = fl.parse_payload(payload([uni("BTC", 40)], [ctx()]), TS, sha=SHA)
        self.assertEqual(len(res["rows"]), 1)
        row = res["rows"][0]
        self.assertEqual(len(row), len(fl.CSV_HEADER),
                         "число ячеек обязано совпадать с числом колонок заголовка")
        d = rows_as_dicts(res["rows"])[0]
        self.assertEqual(d["ts_utc"], TS)
        self.assertEqual(d["coin"], "BTC")
        self.assertEqual(d["funding"], "0.0000125")
        self.assertEqual(d["open_interest"], "100.5")
        self.assertEqual(d["mark_px"], "77350.5")
        self.assertEqual(d["oracle_px"], "77369.0")
        self.assertEqual(d["mid_px"], "77350.5")
        self.assertEqual(d["premium"], "-0.0002326513")
        self.assertEqual(d["day_ntl_vlm"], "2891981894.68")
        self.assertEqual(d["day_base_vlm"], "37067.29242")
        self.assertEqual(d["max_leverage"], "40")
        self.assertEqual(d["git_sha"], SHA)

    def test_numbers_are_verbatim_strings(self):
        """Точность источника не теряется: 20 знаков остаются 20 знаками.

        Через float такое число не проходит без потерь, и потеря была бы
        нашей ошибкой в данных, которых больше неоткуда взять.
        """
        long_num = "2891981894.6824841499"
        res = fl.parse_payload(payload([uni("X")], [ctx(ntl=long_num)]), TS, sha=SHA)
        self.assertEqual(rows_as_dicts(res["rows"])[0]["day_ntl_vlm"], long_num)

    def test_alignment_is_by_index(self):
        """Третьей монете обязан достаться третий контекст, а не первый."""
        universe = [uni("BTC"), uni("ETH"), uni("SOL")]
        ctxs = [ctx(funding="0.001"), ctx(funding="0.002"), ctx(funding="0.003")]
        res = fl.parse_payload(payload(universe, ctxs), TS, sha=SHA)
        got = [(d["coin"], d["funding"]) for d in rows_as_dicts(res["rows"])]
        self.assertEqual(got, [("BTC", "0.001"), ("ETH", "0.002"), ("SOL", "0.003")])

    def test_delisted_leaves_empty_cells(self):
        """Нет midPx/premium -> пустая ячейка. Ноль тут был бы враньём:
        нулевая премия — это утверждение, а у нас просто нет данных."""
        universe = [uni("DEAD", delisted=True)]
        ctxs = [ctx(mid=None, premium=None)]
        res = fl.parse_payload(payload(universe, ctxs), TS, sha=SHA)
        d = rows_as_dicts(res["rows"])[0]
        self.assertEqual(d["mid_px"], "")
        self.assertEqual(d["premium"], "")
        self.assertEqual(d["funding"], "0.0000125", "остальные поля обязаны сохраниться")
        self.assertEqual(res["n_delisted"], 1)

    def test_row_without_name_is_skipped_with_warning(self):
        universe = [uni("BTC"), {"szDecimals": 2, "maxLeverage": 5}]
        ctxs = [ctx(), ctx()]
        res = fl.parse_payload(payload(universe, ctxs), TS, sha=SHA)
        self.assertEqual([d["coin"] for d in rows_as_dicts(res["rows"])], ["BTC"])
        self.assertTrue(any("без имени" in w for w in res["warnings"]))

    def test_cleans_separators_in_coin_name(self):
        """Имя приходит извне: перевод строки в нём превратил бы одну строку
        архива в две и сломал бы всех читателей файла."""
        res = fl.parse_payload(payload([uni("A\nB\tC")], [ctx()]), TS, sha=SHA)
        coin = rows_as_dicts(res["rows"])[0]["coin"]
        self.assertNotIn("\n", coin)
        self.assertNotIn("\t", coin)
        self.assertEqual(coin, "A B C")


class TestLengthMismatch(unittest.TestCase):
    """Рассогласование длин двух списков: не падать, обрезать, сообщить."""

    def test_more_universe_than_ctx(self):
        universe = [uni("A"), uni("B"), uni("C")]
        ctxs = [ctx(funding="0.001"), ctx(funding="0.002")]
        res = fl.parse_payload(payload(universe, ctxs), TS, sha=SHA)
        self.assertEqual(len(res["rows"]), 2, "режем по минимуму длин")
        self.assertEqual([d["coin"] for d in rows_as_dicts(res["rows"])], ["A", "B"])
        self.assertEqual(res["n_universe"], 3)
        self.assertEqual(res["n_ctx"], 2)
        self.assertTrue(any("РАССОГЛАСОВАНИЕ" in w for w in res["warnings"]),
                        "молча обрезать нельзя — это должно быть видно в логе")

    def test_more_ctx_than_universe(self):
        universe = [uni("A")]
        ctxs = [ctx(), ctx(), ctx()]
        res = fl.parse_payload(payload(universe, ctxs), TS, sha=SHA)
        self.assertEqual(len(res["rows"]), 1)
        self.assertTrue(any("РАССОГЛАСОВАНИЕ" in w for w in res["warnings"]))

    def test_equal_lengths_give_no_warning(self):
        res = fl.parse_payload(payload([uni("A")], [ctx()]), TS, sha=SHA)
        self.assertEqual(res["warnings"], [])

    def test_both_empty_is_not_a_crash(self):
        res = fl.parse_payload(payload([], []), TS, sha=SHA)
        self.assertEqual(res["rows"], [])
        self.assertTrue(any("пустой ответ" in w for w in res["warnings"]))

    def test_mismatch_survives_full_run(self):
        """Полный прогон (разбор + запись) на кривых длинах обязан дойти до
        файла: потерять хвост списка лучше, чем потерять весь час."""
        universe = [uni("A"), uni("B"), uni("C")]
        ctxs = [ctx()]
        with tempfile.TemporaryDirectory() as tmp:
            res = fl.run_once(payload(universe, ctxs), now=_dt(21), base_dir=tmp, sha=SHA)
            self.assertEqual(res["written"], 1)
            self.assertTrue(os.path.exists(res["path"]))


class TestBrokenPayload(unittest.TestCase):
    """Структурно неверный ответ -> внятный ValueError, а не IndexError."""

    def test_not_a_list(self):
        with self.assertRaises(ValueError):
            fl.parse_payload({"universe": []}, TS, sha=SHA)

    def test_too_short(self):
        with self.assertRaises(ValueError):
            fl.parse_payload([{"universe": []}], TS, sha=SHA)

    def test_no_universe_key(self):
        with self.assertRaises(ValueError):
            fl.parse_payload([{"assets": []}, []], TS, sha=SHA)

    def test_second_element_not_list(self):
        with self.assertRaises(ValueError):
            fl.parse_payload([{"universe": []}, {"a": 1}], TS, sha=SHA)


class TestSlotKeyAndPath(unittest.TestCase):
    """Ключ слота: какой ПЛАНОВЫЙ час обслуживает прогон."""

    def test_slot_key_of_timely_run(self):
        self.assertEqual(fl.slot_key_from_ts("2026-09-01T21:07:03Z"), "2026-09-01T21")
        self.assertEqual(fl.slot_key_from_ts("2026-09-01T21:59:59Z"), "2026-09-01T21")
        self.assertNotEqual(fl.slot_key_from_ts("2026-09-01T21:07:03Z"),
                            fl.slot_key_from_ts("2026-09-01T22:07:03Z"))

    def test_slot_key_of_delayed_run_stays_on_its_slot(self):
        """Суть находки: старт в 22:02 — это ЗАДЕРЖАННЫЙ слот 21:07, а не
        час 22. Иначе он занял бы собой час 22 и настоящий слот 22:07 был бы
        отброшен как дубль."""
        self.assertEqual(fl.slot_key_from_ts("2026-09-01T22:02:00Z"), "2026-09-01T21")
        self.assertEqual(fl.slot_key_from_ts("2026-09-01T22:06:59Z"), "2026-09-01T21")
        self.assertEqual(fl.slot_key_from_ts("2026-09-01T22:07:00Z"), "2026-09-01T22")

    def test_slot_rolls_back_over_midnight(self):
        self.assertEqual(fl.slot_key_from_ts("2026-09-02T00:02:00Z"), "2026-09-01T23")
        self.assertEqual(fl.slot_key(_dt(0, 2, day=2)), "2026-09-01T23")

    def test_slot_minute_is_configurable(self):
        """Расписание живёт в воркфлоу; код обязан уметь принять другое."""
        self.assertEqual(fl.slot_key(_dt(15, 2), slot_minute=0), "2026-09-01T15")
        self.assertEqual(fl.slot_key(_dt(15, 2), slot_minute=7), "2026-09-01T14")

    def test_slot_key_of_garbage_is_empty(self):
        self.assertEqual(fl.slot_key_from_ts(""), "")
        self.assertEqual(fl.slot_key_from_ts("хлам"), "")
        self.assertEqual(fl.slot_key_from_ts(None), "")
        self.assertEqual(fl.slot_key_from_ts("2026-13-45T99:99:99Z"), "")

    def test_path_layout(self):
        p = fl.csv_path_for(now=_dt(21), base_dir="/data/funding")
        self.assertEqual(os.path.basename(p), "funding_20260901.csv")
        self.assertEqual(os.path.basename(os.path.dirname(p)), "2026-09")

    def test_missing_slots_counts_the_hole(self):
        self.assertEqual(fl.missing_slots("2026-09-01T21", "2026-09-01T22"), (0, []))
        n, listed = fl.missing_slots("2026-09-01T21", "2026-09-02T01")
        self.assertEqual(n, 3)
        self.assertEqual(listed, ["2026-09-01T22", "2026-09-01T23", "2026-09-02T00"])

    def test_missing_slots_of_garbage_is_silent(self):
        """Тревога на пустом месте обесценивает тревогу настоящую."""
        self.assertEqual(fl.missing_slots("", "2026-09-01T22"), (0, []))
        self.assertEqual(fl.missing_slots("хлам", "2026-09-01T22"), (0, []))
        self.assertEqual(fl.missing_slots("2026-09-01T23", "2026-09-01T21"), (0, []))

    def test_long_gap_is_truncated_but_counted(self):
        n, listed = fl.missing_slots("2026-09-01T00", "2026-09-02T12")
        self.assertEqual(n, 35)
        self.assertEqual(len(listed), fl.MAX_LISTED_GAPS)


class TestDelayedRun(unittest.TestCase):
    """Задержка Actions не имеет права съедать час (находка 01.09.2026)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.pl = payload([uni("BTC"), uni("ETH")], [ctx(), ctx()])

    def tearDown(self):
        self.tmp.cleanup()

    def test_delayed_slot_does_not_eat_the_next_one(self):
        """Слот 14:07 стартовал в 15:02, слот 15:07 — в 15:10. Оба обязаны
        лечь в архив: до починки второй считался дублем «часа 15» и молча
        пропадал вместе с первым."""
        r1 = fl.run_once(self.pl, now=_dt(15, 2), base_dir=self.dir, sha=SHA)
        self.assertEqual(r1["slot"], "2026-09-01T14")
        self.assertEqual(r1["written"], 2)
        r2 = fl.run_once(self.pl, now=_dt(15, 10), base_dir=self.dir, sha=SHA)
        self.assertEqual(r2["slot"], "2026-09-01T15")
        self.assertEqual(r2["written"], 2, "настоящий слот 15:07 обязан записаться")
        self.assertEqual(r2["skipped"], 0)
        self.assertEqual(r2["n_missed"], 0, "дырки нет: слоты 14 и 15 идут подряд")

    def test_ts_stays_the_real_measurement_time(self):
        """Слот — про расписание, ts — про данные. Подменять ts плановым
        временем нельзя: срез снят в 15:02, а не в 14:07."""
        r = fl.run_once(self.pl, now=_dt(15, 2), base_dir=self.dir, sha=SHA)
        self.assertEqual(r["ts_utc"], "2026-09-01T15:02:03Z")
        self.assertEqual(r["slot_utc"], "2026-09-01T14:07:00Z")
        self.assertEqual(r["delay_sec"], 55 * 60 + 3)
        with open(r["path"], "r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["ts_utc"], "2026-09-01T15:02:03Z")

    def test_repeat_inside_one_delayed_slot_is_idempotent(self):
        """Идемпотентность повторного прогона В ТОТ ЖЕ слот обязана выжить:
        15:02 и 15:05 — оба слот 14:07."""
        fl.run_once(self.pl, now=_dt(15, 2), base_dir=self.dir, sha=SHA)
        r2 = fl.run_once(self.pl, now=_dt(15, 5), base_dir=self.dir, sha=SHA)
        self.assertEqual(r2["slot"], "2026-09-01T14")
        self.assertEqual(r2["written"], 0)
        self.assertEqual(r2["skipped"], 2)

    def test_slot_before_midnight_keeps_its_day_file(self):
        """Слот 23:07, отработавший в 00:02, обязан лечь в файл СВОИХ суток —
        иначе повторный прогон в 00:05 не найдёт своей же записи и удвоит час."""
        r1 = fl.run_once(self.pl, now=_dt(23, 7, day=1), base_dir=self.dir, sha=SHA)
        r2 = fl.run_once(self.pl, now=_dt(0, 2, day=2), base_dir=self.dir, sha=SHA)
        self.assertEqual(r2["slot"], "2026-09-01T23")
        self.assertEqual(r2["path"], r1["path"], "тот же слот — тот же дневной файл")
        self.assertEqual(r2["written"], 0, "слот 23 уже снят — дозаписи быть не должно")
        r3 = fl.run_once(self.pl, now=_dt(0, 7, day=2), base_dir=self.dir, sha=SHA)
        self.assertEqual(r3["slot"], "2026-09-02T00")
        self.assertNotEqual(r3["path"], r1["path"])
        self.assertEqual(r3["written"], 2, "новые сутки, новый слот — пишем")


class TestGapIsNotSilent(unittest.TestCase):
    """Пропущенный час обязан быть НАЗВАН, а не подразумеваться."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.pl = payload([uni("BTC")], [ctx()])

    def tearDown(self):
        self.tmp.cleanup()

    def test_contiguous_slots_raise_nothing(self):
        fl.run_once(self.pl, now=_dt(21, 7), base_dir=self.dir, sha=SHA)
        r = fl.run_once(self.pl, now=_dt(22, 7), base_dir=self.dir, sha=SHA)
        self.assertEqual(r["n_missed"], 0)
        self.assertEqual(r["missed_slots"], [])

    def test_hole_is_named(self):
        fl.run_once(self.pl, now=_dt(21, 7), base_dir=self.dir, sha=SHA)
        r = fl.run_once(self.pl, now=_dt(23, 7), base_dir=self.dir, sha=SHA)
        self.assertEqual(r["last_slot"], "2026-09-01T21")
        self.assertEqual(r["n_missed"], 1)
        self.assertEqual(r["missed_slots"], ["2026-09-01T22"])
        self.assertEqual(r["written"], 1, "дырка не отменяет записи текущего слота")

    def test_first_run_ever_does_not_invent_a_hole(self):
        r = fl.run_once(self.pl, now=_dt(21, 7), base_dir=self.dir, sha=SHA)
        self.assertEqual(r["n_missed"], 0)
        self.assertEqual(r["last_slot"], "")

    def test_hole_over_midnight_is_seen(self):
        """Дырка через полночь — самая обычная: ночью на прогоны не смотрят.
        Пустой файл новых суток не должен выдавать её за чистый старт."""
        fl.run_once(self.pl, now=_dt(22, 7, day=1), base_dir=self.dir, sha=SHA)
        r = fl.run_once(self.pl, now=_dt(1, 7, day=2), base_dir=self.dir, sha=SHA)
        self.assertEqual(r["slot"], "2026-09-02T01")
        self.assertEqual(r["last_slot"], "2026-09-01T22")
        self.assertEqual(r["n_missed"], 2)
        self.assertEqual(r["missed_slots"], ["2026-09-01T23", "2026-09-02T00"])


class TestMainExitCodes(unittest.TestCase):
    """main() без сети: заглушка вместо fetch_payload, фиксированные часы.

    Проверяется главное требование находки — потеря часа не молчит: она
    выходит из процесса ненулевым кодом, а значит красным шагом Actions и
    тревогой в Telegram. Строчка в зелёном логе тревогой не является.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.pl = payload([uni("BTC")], [ctx()])
        self._fetch, self._now, self._sha = fl.fetch_payload, fl.utc_now, fl.git_sha
        self._hist, self._notify = fl.fetch_funding_history, fl.notify_telegram
        fl.fetch_payload = lambda *a, **k: self.pl
        # История биржи в тесте пуста: добрать нечем -> исход partial, код 2
        # (ни один слот не закрыт, OI потерян).
        fl.fetch_funding_history = lambda *a, **k: []
        fl.git_sha = lambda: SHA
        # ИНФО — заглушка: тест не имеет права ходить в Telegram, даже если в
        # окружении разработчика есть токен.
        fl.notify_telegram = lambda text, **k: True

    def tearDown(self):
        fl.fetch_payload, fl.utc_now, fl.git_sha = self._fetch, self._now, self._sha
        fl.fetch_funding_history, fl.notify_telegram = self._hist, self._notify
        self.tmp.cleanup()

    def _main_at(self, moment, extra=()):
        fl.utc_now = lambda: moment
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = fl.main(["--data-dir", self.dir] + list(extra))
        return code, buf.getvalue()

    def test_normal_run_is_green(self):
        code, out = self._main_at(_dt(21, 7))
        self.assertEqual(code, 0)
        self.assertIn("2026-09-01T21", out)

    def test_repeat_in_same_slot_is_green_and_writes_nothing(self):
        self._main_at(_dt(21, 7))
        code, out = self._main_at(_dt(21, 41))
        self.assertEqual(code, 0)
        self.assertIn("уже записан", out)

    def test_gap_returns_nonzero_and_names_the_hours(self):
        self._main_at(_dt(21, 7))
        code, out = self._main_at(_dt(23, 7))
        self.assertEqual(code, 2, "незалеченная потеря часа обязана красить прогон")
        self.assertIn("ПРОПУСК ЧАСОВ", out)
        self.assertIn("2026-09-01T22", out)
        self.assertIn("ИТОГ: исход=partial", out)

    def test_delay_over_an_hour_shows_up_as_a_gap(self):
        """Опоздание больше часа по построению не видно в самом опоздании
        (оно всегда < 60 мин): слот 21:07, стартовавший в 22:40, обслуживает
        уже слот 22 — и час 21 виден именно как дырка."""
        self._main_at(_dt(20, 7))
        code, out = self._main_at(_dt(22, 40))
        self.assertEqual(code, 2)
        self.assertIn("2026-09-01T21", out)

    def test_bad_slot_minute_is_refused(self):
        code, _ = self._main_at(_dt(21, 7), extra=["--slot-minute", "77"])
        self.assertEqual(code, 1)


class TestIdempotence(unittest.TestCase):
    """Дедуп по (час UTC, монета)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.pl = payload([uni("BTC"), uni("ETH"), uni("SOL")], [ctx(), ctx(), ctx()])

    def tearDown(self):
        self.tmp.cleanup()

    def _read(self, path):
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return list(csv.reader(fh))

    def test_second_run_same_hour_writes_nothing(self):
        r1 = fl.run_once(self.pl, now=_dt(21, 7), base_dir=self.dir, sha=SHA)
        self.assertEqual(r1["written"], 3)
        r2 = fl.run_once(self.pl, now=_dt(21, 41), base_dir=self.dir, sha=SHA)
        self.assertEqual(r2["written"], 0, "тот же час — дозаписи быть не должно")
        self.assertEqual(r2["skipped"], 3)
        lines = self._read(r1["path"])
        self.assertEqual(len(lines), 4, "заголовок + 3 строки, и ни одной лишней")

    def test_next_hour_appends(self):
        fl.run_once(self.pl, now=_dt(21, 7), base_dir=self.dir, sha=SHA)
        r2 = fl.run_once(self.pl, now=_dt(22, 7), base_dir=self.dir, sha=SHA)
        self.assertEqual(r2["written"], 3)
        lines = self._read(r2["path"])
        self.assertEqual(len(lines), 7, "заголовок + 3 + 3")
        self.assertEqual(lines[0], fl.CSV_HEADER, "заголовок пишется РОВНО один раз")

    def test_new_coin_in_same_hour_is_added(self):
        """Дедуп по паре (час, монета), а не по часу целиком: если биржа
        добавила инструмент между прогонами, он обязан попасть в архив."""
        fl.run_once(self.pl, now=_dt(21, 7), base_dir=self.dir, sha=SHA)
        pl2 = payload([uni("BTC"), uni("ETH"), uni("SOL"), uni("NEW")],
                      [ctx(), ctx(), ctx(), ctx()])
        r2 = fl.run_once(pl2, now=_dt(21, 41), base_dir=self.dir, sha=SHA)
        self.assertEqual(r2["written"], 1)
        self.assertEqual(r2["skipped"], 3)
        rows = self._read(r2["path"])[1:]
        self.assertEqual(rows[-1][fl.CSV_HEADER.index("coin")], "NEW")

    def test_duplicate_inside_one_batch(self):
        """Дубль внутри одной отдачи биржи тоже не должен попасть в файл дважды."""
        pl = payload([uni("BTC"), uni("BTC")], [ctx(), ctx()])
        res = fl.run_once(pl, now=_dt(21, 7), base_dir=self.dir, sha=SHA)
        self.assertEqual(res["written"], 1)
        self.assertEqual(res["skipped"], 1)

    def test_different_day_different_file(self):
        r1 = fl.run_once(self.pl, now=_dt(21, 7, day=1), base_dir=self.dir, sha=SHA)
        r2 = fl.run_once(self.pl, now=_dt(21, 7, day=2), base_dir=self.dir, sha=SHA)
        self.assertNotEqual(r1["path"], r2["path"])
        self.assertEqual(r2["written"], 3, "новый день — новый файл, дедуп не мешает")

    def test_dry_run_writes_nothing(self):
        res = fl.run_once(self.pl, now=_dt(21, 7), base_dir=self.dir, sha=SHA,
                          dry_run=True)
        self.assertEqual(res["written"], 0)
        self.assertEqual(res["would_write"], 3)
        self.assertFalse(os.path.exists(res["path"]))

    def test_empty_rows_do_not_create_file(self):
        """Пустой файл с одним заголовком потом неотличим от «логгер не бегал»."""
        res = fl.run_once(payload([], []), now=_dt(21, 7), base_dir=self.dir, sha=SHA)
        self.assertEqual(res["written"], 0)
        self.assertFalse(os.path.exists(res["path"]))

    def test_read_existing_keys_on_missing_file(self):
        self.assertEqual(fl.read_existing_keys(os.path.join(self.dir, "нет.csv")), set())

    def test_keys_are_found_by_header_not_position(self):
        """Дедуп ищет колонки по имени: добавленная слева колонка не должна
        заставить его сравнивать не те поля."""
        path = os.path.join(self.dir, "shifted.csv")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(["extra"] + fl.CSV_HEADER)
            w.writerow(["мусор", TS, "BTC"] + [""] * (len(fl.CSV_HEADER) - 2))
        keys = fl.read_existing_keys(path)
        self.assertIn(("2026-09-01T21", "BTC"), keys)


class TestWindowSweep(unittest.TestCase):
    """Окно сверки 48 ч: ожидаемые слоты против записанных (Б3 пакета 02.09)."""

    def test_window_lists_48_slots_before_current(self):
        keys = fl.window_slot_keys(_dt(21, 7).replace(minute=7, second=0,
                                                      microsecond=0))
        self.assertEqual(len(keys), fl.BACKFILL_WINDOW_HOURS)
        self.assertEqual(keys[-1], "2026-09-01T20", "последний слот окна — час назад")
        self.assertNotIn("2026-09-01T21", keys, "текущий слот в окно не входит")

    def test_fresh_archive_invents_no_holes(self):
        """Ждать записей от часов, когда логгера не существовало, нельзя."""
        self.assertEqual(fl.missing_window_slots(_dt(21, 7), set()), [])

    def test_slots_before_first_record_are_not_holes(self):
        known = {"2026-09-01T18", "2026-09-01T21"}
        missing = fl.missing_window_slots(_dt(21, 7), known)
        self.assertEqual(missing, ["2026-09-01T19", "2026-09-01T20"])

    def test_window_sees_hole_covered_by_later_record(self):
        """span-проверка видит только дырку у последней записи; окно обязано
        видеть и дырку МЕЖДУ записями."""
        with tempfile.TemporaryDirectory() as tmp:
            pl = payload([uni("BTC")], [ctx()])
            fl.run_once(pl, now=_dt(17, 7), base_dir=tmp, sha=SHA)
            fl.run_once(pl, now=_dt(20, 7), base_dir=tmp, sha=SHA)
            r = fl.run_once(pl, now=_dt(21, 7), base_dir=tmp, sha=SHA)
            self.assertEqual(r["n_missed"], 0, "span-проверка дырки не видит")
            self.assertEqual(r["missing_window"],
                             ["2026-09-01T18", "2026-09-01T19"],
                             "окно обязано видеть дырку между записями")


class FakeHistory(object):
    """Заглушка fetch_funding_history: отдаёт по монете заготовленные записи."""

    def __init__(self, per_coin):
        self.per_coin = per_coin
        self.calls = []

    def __call__(self, coin, start_ms, end_ms, session=None, **kw):
        self.calls.append((coin, start_ms, end_ms))
        return self.per_coin.get(coin, [])


def hist_entry(hour, minute=0, day=1, rate="0.0007", prem="-0.0001", coin="X"):
    """Запись fundingHistory в форме биржи: time в мс, момент РАСЧЁТА ставки."""
    dt = datetime(2026, 9, day, hour, minute, 0, tzinfo=timezone.utc)
    return {"coin": coin, "fundingRate": rate, "premium": prem,
            "time": int(dt.timestamp() * 1000)}


class TestBackfill(unittest.TestCase):
    """Добор пропущенных часов фандинга из истории биржи (Б3 пакета 02.09).

    Сети не касается: история подменяется заглушкой. Проверяем ровно то, на
    чём стоит починка: дырка закрывается фандингом, OI остаётся честно пустым
    с пометкой missing_oi, повторный добор не дублирует строки, оборванный
    бюджет не теряет уже добранное.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.pl = payload([uni("BTC"), uni("ETH")], [ctx(), ctx()])
        self._hist = fl.fetch_funding_history

    def tearDown(self):
        fl.fetch_funding_history = self._hist
        self.tmp.cleanup()

    def _rows(self, path):
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def _gap_19_20(self):
        """Архив со слотами 18 и 21: дырка ровно 19 и 20."""
        fl.run_once(self.pl, now=_dt(18, 7), base_dir=self.dir, sha=SHA)
        r = fl.run_once(self.pl, now=_dt(21, 7), base_dir=self.dir, sha=SHA)
        self.assertEqual(r["missing_window"],
                         ["2026-09-01T19", "2026-09-01T20"])
        return r

    def _backfill(self, r, **kw):
        """Добор по плану прогона r — как его зовёт _run."""
        args = dict(coins=r["coins"], new_coins=r["new_coins"], raw_names=r["coins_raw"],
                    known_slots=r["known_slots"], base_dir=self.dir, sha=SHA,
                    pause_between=0, slot_dt=r["slot_dt"])
        args.update(kw)
        return fl.backfill_funding(r["gap"], **args)

    def test_synthetic_gap_is_backfilled(self):
        r = self._gap_19_20()
        self.assertEqual(r["gap"], {"BTC": ["2026-09-01T19", "2026-09-01T20"],
                                    "ETH": ["2026-09-01T19", "2026-09-01T20"]})
        self.assertEqual(r["gap_pairs"], 4)
        # Записи расчёта в 20:00 и 21:00 закрывают слоты 19 и 20.
        fake = FakeHistory({
            "BTC": [hist_entry(20, rate="0.001"), hist_entry(21, rate="0.002")],
            "ETH": [hist_entry(20, rate="0.003"), hist_entry(21, rate="0.004")],
        })
        fl.fetch_funding_history = fake
        bf = self._backfill(r)
        self.assertEqual(bf["coins_ok"], 2)
        self.assertEqual(bf["rows_written"], 4, "2 монеты x 2 слота")
        self.assertEqual(bf["pairs_filled"], 4)
        self.assertEqual(bf["pairs_no_history"], 0)
        self.assertEqual(bf["pairs_unresolved"], 0)
        self.assertEqual(bf["slots_filled"], 2)
        self.assertEqual(bf["empty_slots"], [])
        rows = self._rows(r["path"])
        filled = [x for x in rows if x["note"] == fl.BACKFILL_NOTE]
        self.assertEqual(len(filled), 4)
        for x in filled:
            self.assertEqual(x["open_interest"], "", "OI задним числом — пустой, не ноль")
            self.assertEqual(x["mark_px"], "")
            self.assertNotEqual(x["funding"], "", "фандинг обязан быть добран")
        by_key = {(fl.slot_key_from_ts(x["ts_utc"]), x["coin"]): x["funding"]
                  for x in filled}
        self.assertEqual(by_key[("2026-09-01T19", "BTC")], "0.001")
        self.assertEqual(by_key[("2026-09-01T20", "ETH")], "0.004")
        # Дырка закрыта: следующий прогон окна пропусков не видит.
        r3 = fl.run_once(self.pl, now=_dt(22, 7), base_dir=self.dir, sha=SHA)
        self.assertEqual(r3["missing_window"], [])

    def test_backfill_is_idempotent(self):
        r = self._gap_19_20()
        fake = FakeHistory({"BTC": [hist_entry(20)], "ETH": [hist_entry(20)]})
        fl.fetch_funding_history = fake
        bf1 = self._backfill(r)
        bf2 = self._backfill(r)
        self.assertEqual(bf1["rows_written"], 2)
        self.assertEqual(bf2["rows_written"], 0, "повторный добор не дублирует строк")
        n_lines = len(self._rows(r["path"]))
        bf3 = self._backfill(r)
        self.assertEqual(bf3["rows_written"], 0)
        self.assertEqual(len(self._rows(r["path"])), n_lines)
        # А по плану СЛЕДУЮЩЕГО прогона слот 19 уже не в долгу (пара записана),
        # запрашиваются только пары без строки — идемпотентность по парам.
        r2 = fl.run_once(self.pl, now=_dt(21, 41), base_dir=self.dir, sha=SHA)
        self.assertEqual(r2["gap"], {"BTC": ["2026-09-01T20"], "ETH": ["2026-09-01T20"]})

    def test_backfill_row_lands_in_slot_day_file(self):
        """Слот 23:07 с временем расчёта 00:00 следующих суток обязан лечь
        в файл СВОИХ суток — то же правило, что у живого среза."""
        fake = FakeHistory({"BTC": [hist_entry(0, day=2, rate="0.005")]})
        fl.fetch_funding_history = fake
        bf = fl.backfill_funding({"BTC": ["2026-09-01T23"]}, coins=["BTC"],
                                 base_dir=self.dir, sha=SHA, pause_between=0,
                                 slot_dt=_dt(0, 7, day=2))
        self.assertEqual(bf["rows_written"], 1)
        path = fl.csv_path_for(now=_dt(23, 7, day=1), base_dir=self.dir)
        rows = self._rows(path)
        self.assertEqual(rows[0]["ts_utc"], "2026-09-02T00:00:00Z")
        self.assertEqual(rows[0]["note"], fl.BACKFILL_NOTE)

    def test_deadline_stops_but_keeps_what_was_fetched(self):
        r = self._gap_19_20()
        fake = FakeHistory({"BTC": [hist_entry(20, rate="0.001")]})
        fl.fetch_funding_history = fake
        bf = self._backfill(r, deadline_sec=-1)
        self.assertTrue(bf["timed_out"])
        self.assertEqual(len(fake.calls), 0, "бюджет 0 — в сеть не ходим вовсе")
        self.assertEqual(bf["coins_unattempted"], 2)
        self.assertEqual(bf["pairs_unresolved"], 4, "не запрошенные пары — долг, не потеря")
        self.assertEqual(bf["unfilled_slots"], ["2026-09-01T19", "2026-09-01T20"])
        self.assertEqual(bf["oldest_unresolved_slot"], "2026-09-01T19")

    def test_failed_coin_does_not_kill_the_rest(self):
        r = self._gap_19_20()
        def boom(coin, *a, **k):
            if coin == "BTC":
                raise RuntimeError("connection lost")
            return [hist_entry(20, rate="0.009")]
        fl.fetch_funding_history = boom
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bf = self._backfill(r)
        self.assertEqual(bf["coins_failed"], 1)
        self.assertEqual(bf["coins_failed_names"], ["BTC"])
        self.assertEqual(bf["rows_written"], 1, "ETH обязан добраться несмотря на BTC")
        self.assertEqual(bf["pairs_unresolved"], 2, "обе пары BTC остались долгом")
        self.assertEqual(bf["pairs_no_history"], 1, "у ETH нет записи за слот 20")

    def test_main_end_to_end_backfills_and_returns_0_with_info(self):
        """Полный путь: main видит дырку, добирает фандинг ПОЛНОСТЬЮ и возвращает
        0 (С-22). Короткий пропуск (2 ч — обычная задержка расписания GitHub)
        оставляет ::notice::, длинный (6 ч, порог INFO_GAP_HOURS) — ровно одно
        ИНФО; следующий час — зелёный и без сообщения."""
        stubs = (fl.fetch_payload, fl.utc_now, fl.git_sha, fl.fetch_funding_history,
                 fl.notify_telegram)
        sent = []
        fl.fetch_payload = lambda *a, **k: self.pl
        fl.git_sha = lambda: SHA
        fl.notify_telegram = lambda text, **k: sent.append(text) or True
        fl.fetch_funding_history = FakeHistory({
            "BTC": [hist_entry(h) for h in range(13, 22)],
            "ETH": [hist_entry(h) for h in range(13, 22)]})
        try:
            fl.utc_now = lambda: _dt(12, 7)
            with contextlib.redirect_stdout(io.StringIO()):
                fl.main(["--data-dir", self.dir])
            # пропуск 13..17 = 5 ч < INFO_GAP_HOURS — без ИНФО, с ::notice::
            fl.utc_now = lambda: _dt(18, 7)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = fl.main(["--data-dir", self.dir])
            out = buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("ИТОГ: исход=healed", out)
            self.assertIn("фандинг добран", out)
            self.assertIn("::notice", out)
            self.assertEqual(sent, [], "рутинный пропуск ИНФО не шлёт")
            # пропуск 19..00 = 6 ч (слот 18 снят живым срезом) — ИНФО ровно один раз
            fl.fetch_funding_history = FakeHistory({
                "BTC": [hist_entry(h) for h in range(20, 24)]
                       + [hist_entry(0, day=2), hist_entry(1, day=2)],
                "ETH": [hist_entry(h) for h in range(20, 24)]
                       + [hist_entry(0, day=2), hist_entry(1, day=2)]})
            fl.utc_now = lambda: _dt(1, 7, day=2)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code2 = fl.main(["--data-dir", self.dir])
            out2 = buf.getvalue()
            self.assertEqual(code2, 0, "пропуск залечен полностью — прогон зелёный, "
                                       "потеря OI объявлена ИНФО, а не тревогой")
            self.assertIn("ИТОГ: исход=healed", out2)
            self.assertIn(fl.BACKFILL_NOTE, out2)
            self.assertNotIn("безвозвратно", out2)
            self.assertNotIn("не восстановимы", out2)
            self.assertEqual(len(sent), 1, "ИНФО обязано уйти ровно один раз")
            self.assertIn("пропуск 6 ч / 12 пар залечен", sent[0])
            fl.utc_now = lambda: _dt(2, 7, day=2)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code3 = fl.main(["--data-dir", self.dir])
            self.assertEqual(code3, 0, "всё добрано: окно чисто")
            self.assertEqual(len(sent), 1, "на чистом окне ИНФО не повторяется")
            self.assertIn("ИТОГ: исход=ok", buf.getvalue())
        finally:
            (fl.fetch_payload, fl.utc_now, fl.git_sha,
             fl.fetch_funding_history, fl.notify_telegram) = stubs

    def test_no_backfill_flag_skips_network(self):
        stubs = (fl.fetch_payload, fl.utc_now, fl.git_sha, fl.fetch_funding_history,
                 fl.notify_telegram)
        calls = FakeHistory({})
        fl.fetch_payload = lambda *a, **k: self.pl
        fl.git_sha = lambda: SHA
        fl.fetch_funding_history = calls
        fl.notify_telegram = lambda text, **k: True
        try:
            fl.utc_now = lambda: _dt(18, 7)
            with contextlib.redirect_stdout(io.StringIO()):
                fl.main(["--data-dir", self.dir])
            fl.utc_now = lambda: _dt(21, 7)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = fl.main(["--data-dir", self.dir, "--no-backfill"])
            self.assertEqual(code, 2)
            self.assertEqual(calls.calls, [], "с --no-backfill в сеть не ходим")
            self.assertIn("ДОБОР ВЫКЛЮЧЕН", buf.getvalue())
        finally:
            (fl.fetch_payload, fl.utc_now, fl.git_sha,
             fl.fetch_funding_history, fl.notify_telegram) = stubs


class TestLegacySchemaUpgrade(unittest.TestCase):
    """Файлы старой 12-колоночной схемы (до колонки note)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _make_legacy(self):
        path = os.path.join(self.dir, "funding_legacy.csv")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(fl.LEGACY_CSV_HEADER)
            w.writerow([TS, "BTC", "0.0000125", "100.5", "77350.5", "77369.0",
                        "77350.5", "-0.0002326513", "2891981894.68",
                        "37067.29242", "40", SHA])
        return path

    def _live_row(self, ts="2026-09-01T22:07:03Z", coin="ETH"):
        vals = {"ts_utc": ts, "coin": coin, "funding": "0.002", "git_sha": SHA}
        return [vals.get(c, "") for c in fl.CSV_HEADER]

    def _backfill_row(self, ts="2026-09-01T23:00:00Z", coin="BTC"):
        vals = {"ts_utc": ts, "coin": coin, "funding": "0.003", "git_sha": SHA,
                "note": fl.BACKFILL_NOTE}
        return [vals.get(c, "") for c in fl.CSV_HEADER]

    def test_live_append_keeps_legacy_schema(self):
        """Пустую note истории не навязываем: живая строка ложится в 12 колонок."""
        path = self._make_legacy()
        n = fl.append_rows(path, [self._live_row()])
        self.assertEqual(n, 1)
        with open(path, "r", encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], fl.LEGACY_CSV_HEADER)
        self.assertEqual(len(rows[2]), len(fl.LEGACY_CSV_HEADER))

    def test_backfill_append_upgrades_schema_without_losing_values(self):
        path = self._make_legacy()
        fl.append_rows(path, [self._backfill_row()])
        with open(path, "r", encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], fl.CSV_HEADER, "заголовок дополнен колонкой note")
        self.assertEqual(len(rows[1]), len(fl.CSV_HEADER), "старая строка дополнена")
        self.assertEqual(rows[1][0], TS, "значения старой строки не тронуты")
        self.assertEqual(rows[1][1], "BTC")
        self.assertEqual(rows[1][-1], "", "note старой строки пустая")
        self.assertEqual(rows[2][-1], fl.BACKFILL_NOTE)

    def test_dedup_still_works_after_upgrade(self):
        path = self._make_legacy()
        fl.append_rows(path, [self._backfill_row()])
        keys = fl.read_existing_keys(path)
        self.assertIn(("2026-09-01T21", "BTC"), keys)
        self.assertIn(("2026-09-01T22", "BTC"), keys)


class TestRetryPolicy(unittest.TestCase):
    """Сетевые повторы: растущая пауза, redact в печати ошибок (Б1)."""

    class _Resp(object):
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    class _FlakySession(object):
        """Падает n_fail раз, потом отдаёт данные."""

        def __init__(self, n_fail, exc=None, data=None):
            self.n_fail = n_fail
            self.exc = exc or RuntimeError("timeout")
            self.data = data if data is not None else []
            self.calls = 0

        def post(self, *a, **kw):
            self.calls += 1
            if self.calls <= self.n_fail:
                raise self.exc
            return TestRetryPolicy._Resp(self.data)

    def setUp(self):
        self._sleep = fl.time.sleep
        self.pauses = []
        fl.time.sleep = lambda s: self.pauses.append(s)

    def tearDown(self):
        fl.time.sleep = self._sleep

    def test_recovers_after_timeouts_with_growing_pause(self):
        sess = self._FlakySession(n_fail=2, data=[{"universe": []}, []])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got = fl.fetch_payload(session=sess, pause=1.0, retries=3)
        self.assertEqual(got, [{"universe": []}, []])
        self.assertEqual(sess.calls, 3)
        self.assertEqual(self.pauses, [1.0, 2.0],
                         "пауза обязана расти: base*1, base*2")

    def test_exhausted_retries_reraise_last_error(self):
        sess = self._FlakySession(n_fail=99)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                fl.fetch_payload(session=sess, pause=1.0, retries=2)
        self.assertEqual(sess.calls, 3, "1 попытка + 2 повтора")

    def test_error_text_goes_through_redact(self):
        """Текст исключения с токеном не имеет права дойти до лога сырым."""
        from secrets_guard import PLACEHOLDER
        fake_token = "12345678901:AbCdEfGhIjKlMnOpQrStUvWxYz-123456789"
        sess = self._FlakySession(
            n_fail=99, exc=RuntimeError("boom https://api.telegram.org/bot%s/x"
                                        % fake_token))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(RuntimeError):
                fl.fetch_payload(session=sess, pause=0, retries=1)
        out = buf.getvalue()
        self.assertNotIn(fake_token, out)
        self.assertIn(PLACEHOLDER, out)

    def test_history_fetch_uses_same_retry_path(self):
        sess = self._FlakySession(n_fail=1, data=[hist_entry(20)])
        with contextlib.redirect_stdout(io.StringIO()):
            got = fl.fetch_funding_history("BTC", 0, 10 ** 13, session=sess,
                                           pause=1.0, retries=2)
        self.assertEqual(len(got), 1)
        self.assertEqual(sess.calls, 2)


class TestCsvRoundTrip(unittest.TestCase):
    """Файл обязан читаться обратно ровно как записан."""

    def test_written_values_survive_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            pl = payload([uni("A,B")], [ctx(funding="-0.000125")])
            res = fl.run_once(pl, now=_dt(21, 7), base_dir=tmp, sha=SHA)
            with open(res["path"], "r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 1)
            # Запятая в имени экранируется модулем csv и не рвёт строку на две.
            self.assertEqual(rows[0]["coin"], "A,B")
            self.assertEqual(rows[0]["funding"], "-0.000125")
            self.assertEqual(rows[0]["git_sha"], SHA)


class TestExitCodeHonesty(unittest.TestCase):
    """С-22 (02.09.2026): три исхода — три кода, каждый проверен сквозным main().

      healed  -> 0, ИНФО уходит ровно один раз (notify_telegram подменена)
      partial -> 2, ИНФО нет, в outputs число недобранных часов
      error   -> 1, ИНФО нет, outputs outcome=error

    Сети нет: срез, история и Telegram — заглушки. $GITHUB_OUTPUT указывает
    во временный файл, и тест читает его так же, как прочитал бы воркфлоу.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.pl = payload([uni("BTC"), uni("ETH")], [ctx(), ctx()])
        self.stubs = (fl.fetch_payload, fl.utc_now, fl.git_sha,
                      fl.fetch_funding_history, fl.notify_telegram,
                      fl.append_rows, fl.time.monotonic)
        self.sent = []
        fl.fetch_payload = lambda *a, **k: self.pl
        fl.git_sha = lambda: SHA
        fl.notify_telegram = lambda text, **k: self.sent.append(text) or True
        self.out_path = os.path.join(self.dir, "gh_output.txt")
        self._env = mock.patch.dict(os.environ, {"GITHUB_OUTPUT": self.out_path})
        self._env.start()

    def tearDown(self):
        (fl.fetch_payload, fl.utc_now, fl.git_sha, fl.fetch_funding_history,
         fl.notify_telegram, fl.append_rows, fl.time.monotonic) = self.stubs
        self._env.stop()
        self.tmp.cleanup()

    def _main_at(self, moment, extra=()):
        fl.utc_now = lambda: moment
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = fl.main(["--data-dir", self.dir] + list(extra))
        return code, buf.getvalue()

    def _outputs(self):
        """Пары key=value из файла outputs; каждый main дописывает свои, поэтому
        последняя запись ключа побеждает — как и у GitHub."""
        out = {}
        with open(self.out_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k] = v
        return out

    def _full_history(self, hours=(20, 21)):
        return FakeHistory({"BTC": [hist_entry(h) for h in hours],
                            "ETH": [hist_entry(h) for h in hours]})

    def _make_gap(self, hour=18):
        """Слот `hour` записан; следующий прогон в 21:07 видит дырку до 20."""
        code, _ = self._main_at(_dt(hour, 7))
        self.assertEqual(code, 0)
        self.assertEqual(self._outputs()["outcome"], fl.OUTCOME_OK)

    def _make_long_gap(self):
        """Слот 14 записан; прогон в 21:07 видит дырку 15..20 = 6 ч — от порога
        INFO_GAP_HOURS: залеченный такой пропуск шлёт ИНФО."""
        self.assertEqual(fl.INFO_GAP_HOURS, 6)
        self._make_gap(hour=14)
        fl.fetch_funding_history = self._full_history(hours=range(16, 22))

    # --- ветка 1: healed ------------------------------------------------------

    def test_healed_gap_is_green_and_informs_once(self):
        self._make_long_gap()
        code, out = self._main_at(_dt(21, 7))
        self.assertEqual(code, 0, "залеченный пропуск не имеет права красить прогон")
        self.assertIn("ИТОГ: исход=healed", out)
        self.assertNotIn("ТРЕВОГА", out)
        self.assertEqual(len(self.sent), 1, "ИНФО уходит ровно один раз")
        text = self.sent[0]
        self.assertTrue(text.startswith(fl.INFO_PREFIX), text)
        self.assertIn("пропуск 6 ч / 12 пар залечен", text)
        self.assertIn("фандинг добран", text)
        self.assertIn("OI пуст (missing_oi)", text)
        o = self._outputs()
        self.assertEqual(o["outcome"], fl.OUTCOME_HEALED)
        self.assertEqual(o["exit_code"], "0")
        self.assertEqual(o["gap_hours"], "6")
        self.assertEqual(o["unfilled_hours"], "0")
        self.assertEqual(o["pairs_missing"], "12")
        self.assertEqual(o["pairs_filled"], "12")
        self.assertEqual(o["pairs_unresolved"], "0")
        self.assertEqual(o["pairs_no_history"], "0")
        self.assertEqual(o["coins_failed"], "0")
        self.assertEqual(o["timed_out"], "false")
        self.assertEqual(o["info_sent"], "true")
        self.assertIn("залечен", o["summary"])
        self.assertNotIn("::warning", out)

    def test_routine_healed_leaves_notice_and_skips_info(self):
        """С-24 (замер 03.09: cron исполняется раз в 4–5 ч): пропуск короче
        INFO_GAP_HOURS — рутина расписания, ИНФО не шлётся, след — ::notice::
        на странице прогона и info_sent=skipped в outputs."""
        fl.fetch_funding_history = self._full_history()
        self._make_gap()
        code, out = self._main_at(_dt(21, 7))
        self.assertEqual(code, 0)
        self.assertIn("ИТОГ: исход=healed", out)
        self.assertEqual(self.sent, [], "рутинный пропуск ИНФО не шлёт")
        self.assertIn("::notice", out)
        self.assertIn("ИНФО не отправлялось", out)
        self.assertNotIn("::warning", out)
        o = self._outputs()
        self.assertEqual(o["outcome"], fl.OUTCOME_HEALED)
        self.assertEqual(o["gap_hours"], "2")
        self.assertEqual(o["info_sent"], fl.INFO_SKIPPED)

    def test_healed_with_undelivered_info_stays_green_but_leaves_a_trace(self):
        """Решение навигатора 03.09.2026 (С-22): ИНФО не доставлено — код всё
        равно 0 (код — функция исхода), но след обязан остаться: аннотация
        ::warning:: и info_sent=false в outputs. Молчание зелёного прогона —
        тот самый дефект, ради которого переписан логгер."""
        self._make_long_gap()
        fl.notify_telegram = lambda text, **k: False
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "111:fake",
                                          "TELEGRAM_CHAT_ID": "42"}):
            code, out = self._main_at(_dt(21, 7))
        self.assertEqual(code, 0)
        self.assertIn("::warning", out)
        self.assertIn("ИНФО не доставлено", out)
        o = self._outputs()
        self.assertEqual(o["outcome"], fl.OUTCOME_HEALED)
        self.assertEqual(o["info_sent"], "false")

    def test_partial_and_error_leave_info_sent_empty(self):
        fl.fetch_funding_history = FakeHistory({"BTC": [hist_entry(20)],
                                                "ETH": [hist_entry(20)]})
        self._make_gap()
        self._main_at(_dt(21, 7))
        self.assertEqual(self._outputs()["info_sent"], "", "на partial ИНФО не шлётся вовсе")

    def test_dry_run_healed_sends_nothing(self):
        fl.fetch_funding_history = self._full_history()
        self._make_gap()
        code, out = self._main_at(_dt(21, 7), extra=["--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(self.sent, [], "сухой прогон в Telegram не пишет")
        self.assertIn("СУХОЙ ПРОГОН: ИНФО в Telegram не отправляется", out)
        self.assertEqual(self._outputs()["outcome"], fl.OUTCOME_HEALED)

    # --- ветка 2: partial -----------------------------------------------------

    def test_partial_backfill_is_red_and_counts_unfilled_hours(self):
        # История отдаёт только расчёт 20:00 -> закрыт слот 19, слот 20 пуст.
        fl.fetch_funding_history = FakeHistory({"BTC": [hist_entry(20)],
                                                "ETH": [hist_entry(20)]})
        self._make_gap()
        code, out = self._main_at(_dt(21, 7))
        self.assertEqual(code, 2, "час без единой строки обязан красить прогон (С-22)")
        self.assertEqual(self.sent, [], "на partial ИНФО не шлётся — тревогу шлёт воркфлоу")
        self.assertIn("ИТОГ: исход=partial", out)
        self.assertIn("часов без единой строки после добора 1 из 2", out)
        o = self._outputs()
        self.assertEqual(o["outcome"], fl.OUTCOME_PARTIAL)
        self.assertEqual(o["exit_code"], "2")
        self.assertEqual(o["gap_hours"], "2")
        self.assertEqual(o["unfilled_hours"], "1")
        self.assertEqual(o["pairs_no_history"], "2", "у обеих монет нет записи за слот 20")
        self.assertIn("часов без единой строки после добора 1 из 2", o["summary"])

    def test_failed_coin_is_partial_even_when_all_slots_closed(self):
        """Слоты закрыты BTC, но ETH отказала: по монетам это долг (С-24), и
        зелёным такой прогон быть не может; тревога называет пары и монету."""
        def hist(coin, *a, **k):
            if coin == "ETH":
                raise RuntimeError("connection lost")
            return [hist_entry(20), hist_entry(21)]
        fl.fetch_funding_history = hist
        self._make_gap()
        code, out = self._main_at(_dt(21, 7))
        self.assertEqual(code, 2)
        self.assertEqual(self.sent, [])
        o = self._outputs()
        self.assertEqual(o["outcome"], fl.OUTCOME_PARTIAL)
        self.assertEqual(o["unfilled_hours"], "2", "часы с недобранными парами ETH")
        self.assertEqual(o["pairs_unresolved"], "2")
        self.assertEqual(o["pairs_filled"], "2")
        self.assertEqual(o["coins_failed"], "1")
        self.assertEqual(o["coins_failed_names"], "ETH")
        self.assertIn("монет с ошибкой 1 из 2", out)
        self.assertIn("не добрано пар 2 из 4", o["summary"])
        self.assertIn("ETH", o["summary"])

    def test_exhausted_budget_is_partial(self):
        fl.fetch_funding_history = self._full_history()
        # Часы идут по 100 с за вызов: при бюджете 150 с первая монета
        # успевает, на второй бюджет исчерпан — слоты закрыты одной BTC.
        ticks = itertools.count(0, 100)
        fl.time.monotonic = lambda: next(ticks)
        self._make_gap()
        code, out = self._main_at(_dt(21, 7), extra=["--backfill-deadline", "150"])
        self.assertEqual(code, 2)
        self.assertEqual(self.sent, [])
        o = self._outputs()
        self.assertEqual(o["outcome"], fl.OUTCOME_PARTIAL)
        self.assertEqual(o["timed_out"], "true")
        self.assertIn("бюджет добора исчерпан", out)

    def test_no_backfill_flag_is_partial_with_all_hours_unfilled(self):
        fl.fetch_funding_history = self._full_history()
        self._make_gap()
        code, out = self._main_at(_dt(21, 7), extra=["--no-backfill"])
        self.assertEqual(code, 2)
        o = self._outputs()
        self.assertEqual(o["outcome"], fl.OUTCOME_PARTIAL)
        self.assertEqual(o["unfilled_hours"], "2")
        self.assertIn("добор выключен", o["summary"])

    # --- ветка 3: error -------------------------------------------------------

    def test_source_down_is_error_code_1(self):
        def boom(*a, **k):
            raise RuntimeError("timeout")
        fl.fetch_payload = boom
        code, out = self._main_at(_dt(21, 7))
        self.assertEqual(code, 1)
        self.assertEqual(self.sent, [])
        self.assertIn("ИСТОЧНИК НЕДОСТУПЕН", out)
        o = self._outputs()
        self.assertEqual(o["outcome"], fl.OUTCOME_ERROR)
        self.assertEqual(o["exit_code"], "1")
        self.assertIn("источник недоступен", o["summary"])

    def test_unparseable_answer_is_error_code_1(self):
        fl.fetch_payload = lambda *a, **k: {"not": "a list"}
        code, out = self._main_at(_dt(21, 7))
        self.assertEqual(code, 1)
        self.assertIn("НЕ РАЗОБРАН", out)
        self.assertEqual(self._outputs()["outcome"], fl.OUTCOME_ERROR)

    def test_unexpected_exception_is_error_code_1_not_a_traceback_exit(self):
        """Любое исключение по дороге — исход error с строкой ИТОГ и outputs,
        а не голая трассировка без результата для тревоги."""
        def disk_full(*a, **k):
            raise OSError("disk full")
        fl.append_rows = disk_full
        code, out = self._main_at(_dt(21, 7))
        self.assertEqual(code, 1)
        self.assertIn("СБОЙ ПРОГОНА", out)
        self.assertIn("disk full", out)
        o = self._outputs()
        self.assertEqual(o["outcome"], fl.OUTCOME_ERROR)
        self.assertIn("disk full", o["summary"])
        self.assertEqual(self.sent, [])

    def test_exit_table_is_the_single_source(self):
        self.assertEqual(fl.EXIT_BY_OUTCOME,
                         {"ok": 0, "healed": 0, "partial": 2, "error": 1})


GAP2 = {"BTC": ["2026-09-01T19", "2026-09-01T20"],
        "ETH": ["2026-09-01T19", "2026-09-01T20"]}


class TestClassifyOutcome(unittest.TestCase):
    """Чистая функция исхода (С-22 + С-24): дефект — монета с ошибкой, бюджет,
    час без единой строки, молчание дольше окна; пары без истории — нет."""

    def _bf(self, filled=4, unresolved=0, failed=0, total=2, timed_out=False, rows=4,
            would=0, empty=(), unfilled=(), no_history=0, names=(), unattempted=0):
        unfilled_slots = sorted(set(empty) | set(unfilled))
        return {"pairs_filled": filled, "pairs_unresolved": unresolved,
                "pairs_no_history": no_history, "slots_filled": 2 - len(empty),
                "coins_failed": failed, "coins_failed_names": list(names),
                "coins_total": total, "coins_unattempted": unattempted,
                "timed_out": timed_out, "rows_written": rows, "would_write": would,
                "empty_slots": list(empty), "unfilled_slots": unfilled_slots,
                "oldest_unresolved_slot": min(unfilled) if unfilled else "",
                "stopped_at": 2 if timed_out else None}

    def test_no_gap_is_ok(self):
        oc = fl.classify_outcome({})
        self.assertEqual((oc["outcome"], oc["exit_code"]), (fl.OUTCOME_OK, 0))
        self.assertEqual(oc["summary"], "")
        self.assertEqual(oc["pairs_missing"], 0)

    def test_full_backfill_is_healed(self):
        oc = fl.classify_outcome(GAP2, self._bf())
        self.assertEqual((oc["outcome"], oc["exit_code"]), (fl.OUTCOME_HEALED, 0))
        self.assertEqual(oc["unfilled_hours"], 0)
        self.assertEqual(oc["pairs_missing"], 4)
        self.assertIn("пропуск 2 ч / 4 пар залечен", oc["summary"])
        self.assertIn("2026-09-01T19 … 2026-09-01T20", oc["summary"])

    def test_any_defect_is_partial(self):
        for bf, reason in (
                (self._bf(filled=2, empty=["2026-09-01T20"], no_history=2),
                 "часов без единой строки после добора 1 из 2"),
                (self._bf(filled=2, failed=1, unresolved=2, names=["ETH"],
                          unfilled=GAP2["ETH"]), "монет с ошибкой 1 из 2 (ETH)"),
                (self._bf(filled=2, timed_out=True, unresolved=2, unattempted=1,
                          unfilled=GAP2["ETH"]), "бюджет добора исчерпан")):
            oc = fl.classify_outcome(GAP2, bf, slot_key_now="2026-09-01T21")
            self.assertEqual((oc["outcome"], oc["exit_code"]), (fl.OUTCOME_PARTIAL, 2), bf)
            self.assertIn(reason, oc["summary"])
        self.assertIn("не добрано пар 2 из 4", oc["summary"])
        self.assertIn("продолжит следующий прогон", oc["summary"])
        self.assertEqual(oc["oldest_unresolved_age_h"], 2)
        self.assertIn("из окна выйдет через 46 ч", oc["summary"])

    def test_no_history_pairs_are_not_a_defect(self):
        """Пара без истории у биржи при живом соседе в том же слоте: healed, если
        что-то записано; ok «записать нечего», если нет; метки нет — переспрос."""
        oc = fl.classify_outcome(GAP2, self._bf(filled=3, rows=3, no_history=1))
        self.assertEqual((oc["outcome"], oc["exit_code"]), (fl.OUTCOME_HEALED, 0))
        self.assertIn("без истории у биржи 1 пар", oc["summary"])
        oc0 = fl.classify_outcome({"ETH": ["2026-09-01T20"]},
                                  self._bf(filled=0, rows=0, no_history=1, total=1))
        self.assertEqual((oc0["outcome"], oc0["exit_code"]), (fl.OUTCOME_OK, 0))
        self.assertIn("записать нечего", oc0["summary"])
        self.assertEqual(oc0["pairs_no_history"], 1)

    def test_silence_longer_than_window_is_partial(self):
        oc = fl.classify_outcome(GAP2, self._bf(), silent_since="2026-08-30T05")
        self.assertEqual((oc["outcome"], oc["exit_code"]), (fl.OUTCOME_PARTIAL, 2))
        self.assertIn("архив молчал дольше окна", oc["summary"])
        self.assertIn("2026-08-30T05", oc["summary"])

    def test_no_backfill_or_missing_result_is_partial(self):
        gap = {"BTC": ["2026-09-01T19"]}
        self.assertEqual(fl.classify_outcome(gap, None)["unfilled_hours"], 1)
        oc = fl.classify_outcome(gap, self._bf(), no_backfill=True)
        self.assertEqual(oc["outcome"], fl.OUTCOME_PARTIAL)
        self.assertEqual(oc["unfilled_hours"], 1)
        self.assertEqual(oc["pairs_unresolved"], 1)

    def test_dry_run_counts_would_write(self):
        oc = fl.classify_outcome({"BTC": ["2026-09-01T19"]},
                                 self._bf(filled=1, rows=0, would=3, total=1), dry_run=True)
        self.assertEqual(oc["outcome"], fl.OUTCOME_HEALED)
        self.assertEqual(oc["rows"], 3)

    def test_summary_is_bounded(self):
        names = ["C%03d" % i for i in range(200)]
        plan = {n: ["2026-09-01T19"] for n in names}
        bf = self._bf(filled=0, rows=0, failed=200, unresolved=200, total=200,
                      names=names, unfilled=["2026-09-01T19"], empty=["2026-09-01T19"])
        oc = fl.classify_outcome(plan, bf, slot_key_now="2026-09-01T21")
        self.assertLessEqual(len(oc["summary"]), fl.SUMMARY_LIMIT)
        self.assertIn("C000, C001, C002, C003, C004 +195", oc["summary"])

    def test_error_outcome_carries_summary(self):
        oc = fl.error_outcome("источник недоступен: x")
        self.assertEqual((oc["outcome"], oc["exit_code"]), (fl.OUTCOME_ERROR, 1))
        self.assertEqual(oc["summary"], "источник недоступен: x")


class TestGithubOutput(unittest.TestCase):
    """Outputs шага: без переменной — ничего; булевы как true/false; одна строка."""

    def test_without_env_writes_nothing(self):
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": ""}):
            self.assertEqual(fl.github_output([("a", 1)]), 0)

    def test_writes_pairs_one_per_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.txt")
            n = fl.github_output([("outcome", "healed"), ("timed_out", False),
                                  ("summary", "строка\nс переводом"), ("none", None),
                                  ("coins_failed_names", ["BTC", "ETH"])],
                                 path=path)
            self.assertEqual(n, 5)
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            self.assertEqual(lines, ["outcome=healed", "timed_out=false",
                                     "summary=строка с переводом", "none=",
                                     "coins_failed_names=BTC, ETH"])


class _Resp200(object):
    def __init__(self):
        self.status_code = 200
        self.text = "{\"ok\":true,\"result\":{\"secret-looking-body\":1}}"


class _Sess200(object):
    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return _Resp200()


class TestNotifyTelegram(unittest.TestCase):
    """ИНФО из скрипта: без ключей — в лог; с ключами — только HTTP-код в лог."""

    def test_without_env_prints_and_returns_false(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ok = fl.notify_telegram("тестовое ИНФО")
        self.assertFalse(ok)
        self.assertIn("Telegram не настроен", buf.getvalue())
        self.assertIn("тестовое ИНФО", buf.getvalue())

    def test_with_env_posts_and_prints_only_status(self):
        sess = _Sess200()
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "111:fake",
                                          "TELEGRAM_CHAT_ID": "42"}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ok = fl.notify_telegram("ИНФО", session=sess)
        self.assertTrue(ok)
        url, kw = sess.calls[0]
        self.assertTrue(url.endswith("/bot111:fake/sendMessage"))
        self.assertEqual(kw["json"]["chat_id"], "42")
        self.assertEqual(kw["json"]["text"], "ИНФО")
        out = buf.getvalue()
        self.assertIn("Telegram ответил кодом 200", out)
        self.assertNotIn("secret-looking-body", out, "тело ответа не печатается")

    def test_network_error_is_redacted_and_returns_false(self):
        from secrets_guard import PLACEHOLDER
        fake = "12345678901:AbCdEfGhIjKlMnOpQrStUvWxYz-123456789"

        class Boom(object):
            def __init__(self):
                self.calls = 0

            def post(self, url, **kw):
                self.calls += 1
                raise RuntimeError("HTTPSConnectionPool: Max retries exceeded with url: %s" % url)

        boom = Boom()
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": fake,
                                          "TELEGRAM_CHAT_ID": "42"}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ok = fl.notify_telegram("ИНФО", session=boom, pause=0)
        self.assertFalse(ok)
        out = buf.getvalue()
        self.assertNotIn(fake, out)
        self.assertIn(PLACEHOLDER, out)
        self.assertIn("ИНФО не доставлено", out)
        self.assertEqual(boom.calls, fl.TG_RETRIES + 1, "1 попытка + повторы")

    def test_retries_with_growing_pause_then_succeeds(self):
        """429 на первой попытке, 200 на второй: сообщение доставлено, пауза
        между попытками растёт (base*1)."""
        class Flaky(object):
            def __init__(self):
                self.codes = [429, 200]
                self.calls = 0

            def post(self, url, **kw):
                self.calls += 1
                resp = _Resp200()
                resp.status_code = self.codes[self.calls - 1]
                return resp

        pauses = []
        real_sleep = fl.time.sleep
        fl.time.sleep = lambda s: pauses.append(s)
        try:
            with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "111:fake",
                                              "TELEGRAM_CHAT_ID": "42"}):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    ok = fl.notify_telegram("ИНФО", session=Flaky(), pause=1.0, retries=2)
        finally:
            fl.time.sleep = real_sleep
        self.assertTrue(ok)
        self.assertEqual(pauses, [1.0])
        self.assertIn("кодом 429", buf.getvalue())
        self.assertIn("кодом 200", buf.getvalue())

    def test_gives_up_after_retries_on_5xx(self):
        class Down(object):
            def __init__(self):
                self.calls = 0

            def post(self, url, **kw):
                self.calls += 1
                resp = _Resp200()
                resp.status_code = 502
                return resp

        down = Down()
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "111:fake",
                                          "TELEGRAM_CHAT_ID": "42"}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ok = fl.notify_telegram("ИНФО", session=down, pause=0, retries=2)
        self.assertFalse(ok)
        self.assertEqual(down.calls, 3)
        self.assertIn("не доставлено после 3 попыток", buf.getvalue())


class PairGapBase(unittest.TestCase):
    """Песочница С-24: временный архив, заглушки сети, фиксированные часы."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.pl = payload([uni("BTC"), uni("ETH")], [ctx(), ctx()])
        self.stubs = (fl.fetch_payload, fl.utc_now, fl.git_sha,
                      fl.fetch_funding_history, fl.notify_telegram, fl.time.monotonic)
        self.sent = []
        fl.fetch_payload = lambda *a, **k: self.pl
        fl.git_sha = lambda: SHA
        fl.notify_telegram = lambda text, **k: self.sent.append(text) or True
        fl.fetch_funding_history = FakeHistory({})
        self.out_path = os.path.join(self.dir, "gh_output.txt")
        self._env = mock.patch.dict(os.environ, {"GITHUB_OUTPUT": self.out_path,
                                                 "TELEGRAM_BOT_TOKEN": "",
                                                 "TELEGRAM_CHAT_ID": ""})
        self._env.start()

    def tearDown(self):
        (fl.fetch_payload, fl.utc_now, fl.git_sha, fl.fetch_funding_history,
         fl.notify_telegram, fl.time.monotonic) = self.stubs
        self._env.stop()
        self.tmp.cleanup()

    def _main_at(self, moment, extra=()):
        fl.utc_now = lambda: moment
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = fl.main(["--data-dir", self.dir] + list(extra))
        return code, buf.getvalue()

    def _outputs(self):
        out = {}
        with open(self.out_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k] = v
        return out

    def _rows(self, moment):
        path = fl.csv_path_for(now=moment, base_dir=self.dir)
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def _write_slot(self, moment, coins):
        """Живые строки заданных монет за слот moment — без main."""
        pl = payload([uni(c) for c in coins], [ctx() for _ in coins])
        return fl.run_once(pl, now=moment, base_dir=self.dir, sha=SHA)


class TestPairGaps(PairGapBase):
    """С-24: пропуск считается по парам (слот, монета), пол у каждой монеты свой."""

    def test_gap_only_for_coins_missing_in_slot(self):
        self._write_slot(_dt(18, 7), ["BTC", "ETH"])
        self._write_slot(_dt(19, 7), ["BTC"])            # ETH за 19 не снята
        self._write_slot(_dt(20, 7), ["BTC", "ETH"])
        r = fl.run_once(self.pl, now=_dt(21, 7), base_dir=self.dir, sha=SHA)
        self.assertEqual(r["gap"], {"ETH": ["2026-09-01T19"]})
        self.assertEqual(r["missing_window"], ["2026-09-01T19"])
        self.assertEqual(r["gap_pairs"], 1)
        self.assertEqual(r["new_coins"], [])
        self.assertIn("2026-09-01T19", r["known_slots"], "слот не пуст — есть BTC")

    def test_new_coin_mid_window_gets_no_invented_holes_but_one_probe(self):
        """Монета, появившаяся во вселенной посреди окна: дырок ей не выдумываем
        (пол = первая запись), но историю за окно спрашиваем один раз."""
        for h in (18, 19, 20):
            self._write_slot(_dt(h, 7), ["BTC", "ETH"])
        pl3 = payload([uni("BTC"), uni("ETH"), uni("NEW")], [ctx(), ctx(), ctx()])
        r = fl.run_once(pl3, now=_dt(21, 7), base_dir=self.dir, sha=SHA)
        self.assertEqual(r["gap"], {}, "у BTC/ETH дырок нет, у NEW — не выдумываем")
        self.assertEqual(r["new_coins"], ["NEW"])
        fake = FakeHistory({"NEW": [hist_entry(20, coin="NEW"), hist_entry(21, coin="NEW")]})
        fl.fetch_funding_history = fake
        bf = fl.backfill_funding(r["gap"], coins=r["coins"], new_coins=r["new_coins"],
                                 raw_names=r["coins_raw"], known_slots=r["known_slots"],
                                 base_dir=self.dir, sha=SHA, pause_between=0,
                                 slot_dt=r["slot_dt"])
        self.assertEqual([c for c, _s, _e in fake.calls], ["NEW"], "один пробный запрос")
        self.assertEqual(bf["coins_new"], 1)
        self.assertEqual(bf["rows_written"], 2, "слоты 19 и 20 NEW записаны из истории")
        self.assertEqual(bf["pairs_unresolved"], 0)
        # Следующий прогон: пол NEW = 19, дырок нет; повторного пробного запроса нет.
        r2 = fl.run_once(pl3, now=_dt(22, 7), base_dir=self.dir, sha=SHA)
        self.assertEqual(r2["gap"], {})
        self.assertEqual(r2["new_coins"], [])

    def test_fresh_archive_has_no_gap_and_no_probe(self):
        r = fl.run_once(self.pl, now=_dt(21, 7), base_dir=self.dir, sha=SHA)
        self.assertEqual(r["gap"], {})
        self.assertEqual(r["new_coins"], [], "свежий архив: пробовать нечего")
        self.assertEqual(r["silent_since"], "")

    def test_coin_gone_from_universe_is_not_requested(self):
        self._write_slot(_dt(18, 7), ["BTC", "ETH", "OLD"])
        self._write_slot(_dt(20, 7), ["BTC", "ETH"])
        r = fl.run_once(self.pl, now=_dt(21, 7), base_dir=self.dir, sha=SHA)
        self.assertNotIn("OLD", r["gap"], "монеты вне вселенной не добираются")
        self.assertEqual(sorted(r["gap"]), ["BTC", "ETH"])

    def test_backfill_queue_oldest_debt_first(self):
        plan = {"A": ["2026-09-01T20"], "B": ["2026-09-01T18", "2026-09-01T20"],
                "C": ["2026-09-01T19"], "D": ["2026-09-01T18"]}
        q = fl.backfill_queue(plan, ["A", "B", "C", "D"], new_coins=["N", "A"])
        self.assertEqual([c for c, _s, _n in q], ["B", "D", "C", "A", "N"])
        self.assertEqual([n for _c, _s, n in q], [False, False, False, False, True])

    def test_raw_name_is_used_for_history_requests(self):
        """clean_cell гасит ведущий '=' апострофом в архиве, но биржу спрашивают
        сырым именем — иначе история монеты не найдётся никогда."""
        pl = payload([uni("=EVIL"), uni("BTC")], [ctx(), ctx()])
        for h in (18,):
            fl.run_once(pl, now=_dt(h, 7), base_dir=self.dir, sha=SHA)
        r = fl.run_once(pl, now=_dt(21, 7), base_dir=self.dir, sha=SHA)
        self.assertIn("'=EVIL", r["gap"])
        self.assertEqual(r["coins_raw"]["'=EVIL"], "=EVIL")
        fake = FakeHistory({"=EVIL": [hist_entry(20), hist_entry(21)],
                            "BTC": [hist_entry(20), hist_entry(21)]})
        fl.fetch_funding_history = fake
        bf = fl.backfill_funding(r["gap"], coins=r["coins"], raw_names=r["coins_raw"],
                                 known_slots=r["known_slots"], base_dir=self.dir, sha=SHA,
                                 pause_between=0, slot_dt=r["slot_dt"])
        self.assertIn("=EVIL", [c for c, _s, _e in fake.calls])
        self.assertEqual(bf["rows_written"], 4)
        coins = {row["coin"] for row in self._rows(_dt(21, 7)) if row["note"] == fl.BACKFILL_NOTE}
        self.assertEqual(coins, {"'=EVIL", "BTC"}, "в архиве — обезвреженное имя")


class TestContinuation(PairGapBase):
    """С-24: оборванный добор продолжается ровно с недобранных монет."""

    def setUp(self):
        super().setUp()
        self.pl = payload([uni("A"), uni("B"), uni("C")], [ctx(), ctx(), ctx()])
        self.full = FakeHistory({c: [hist_entry(h, coin=c) for h in range(17, 24)]
                                 for c in ("A", "B", "C")})
        fl.fetch_funding_history = self.full

    def _interrupt_after_first_coin(self):
        """Часы идут по 100 с за вызов: при бюджете 150 с первая монета
        успевает, на второй бюджет исчерпан."""
        fl.time.monotonic = lambda: next(itertools.count(0, 100)) if False else next(self._ticks)
        self._ticks = itertools.count(0, 100)

    def test_interrupted_backfill_continues_with_remaining_coins(self):
        self._main_at(_dt(18, 7))
        self._interrupt_after_first_coin()
        code, out = self._main_at(_dt(21, 7), extra=["--backfill-deadline", "150"])
        self.assertEqual(code, 2, "добор оборван бюджетом — честно красный (С-22)")
        self.assertEqual([c for c, _s, _e in self.full.calls], ["A"])
        self.assertIn("продолжит следующий прогон", out)
        o = self._outputs()
        self.assertEqual(o["pairs_unresolved"], "4")
        self.assertEqual(o["pairs_filled"], "2")
        # Повторный прогон в том же слоте: запрашиваются ТОЛЬКО B и C.
        fl.time.monotonic = self.stubs[5]
        self.full.calls[:] = []
        code2, out2 = self._main_at(_dt(21, 41))
        self.assertEqual(code2, 0)
        self.assertEqual([c for c, _s, _e in self.full.calls], ["B", "C"],
                         "продолжение с недобранных монет, A не переспрашивается")
        self.assertIn("ИТОГ: исход=healed", out2)
        self.assertEqual(self._outputs()["pairs_unresolved"], "0")
        rows = [r for r in self._rows(_dt(21, 7)) if r["note"] == fl.BACKFILL_NOTE]
        self.assertEqual(len(rows), 6, "3 монеты × 2 слота, без дублей")

    def test_new_gaps_between_runs_do_not_starve_the_tail(self):
        """Между прогонами появились новые дырки у ВСЕХ монет: очередь начинает
        со старейшего долга (B, C за 19), а не снова с A (ревью 03.09)."""
        self._main_at(_dt(18, 7))
        self._interrupt_after_first_coin()
        self._main_at(_dt(21, 7), extra=["--backfill-deadline", "150"])
        fl.time.monotonic = self.stubs[5]
        self.full.calls[:] = []
        code, _ = self._main_at(_dt(23, 7))
        self.assertEqual(code, 0)
        self.assertEqual([c for c, _s, _e in self.full.calls][:2], ["B", "C"])
        self.assertEqual([c for c, _s, _e in self.full.calls][2], "A")
        rows = [r for r in self._rows(_dt(21, 7)) if r["note"] == fl.BACKFILL_NOTE]
        # Слот 21 у всех снят живым срезом прогона 21:07; добрано: A — 19, 20 (первый
        # прогон) и 22; B и C — 19, 20, 22. Итого 2 + 1 + 3 + 3 = 9, без дублей.
        self.assertEqual(len(rows), 9)
        self.assertEqual(sorted({fl.slot_key_from_ts(r["ts_utc"]) for r in rows}),
                         ["2026-09-01T19", "2026-09-01T20", "2026-09-01T22"])


class TestNoHistoryPairs(PairGapBase):
    """С-24: пара без истории у биржи — не дефект и не метка в архиве."""

    def test_missing_hour_for_one_coin_is_not_a_defect_and_writes_no_marker(self):
        fl.fetch_funding_history = FakeHistory({"BTC": [hist_entry(20), hist_entry(21)],
                                                "ETH": [hist_entry(21)]})
        self._main_at(_dt(18, 7))
        code, out = self._main_at(_dt(21, 7))
        self.assertEqual(code, 0, "слот 19 не пуст (есть BTC), ETH без истории — не дефект")
        o = self._outputs()
        self.assertEqual(o["outcome"], fl.OUTCOME_HEALED)
        self.assertEqual(o["pairs_no_history"], "1")
        self.assertEqual(o["pairs_unresolved"], "0")
        self.assertIn("без истории у биржи", out)
        self.assertIn("метка в архив НЕ пишется", out)
        rows = self._rows(_dt(21, 7))
        self.assertTrue(all(r["funding"] for r in rows), "ни одной строки без фандинга — меток нет")
        # Следующий прогон в том же слоте переспрашивает ТОЛЬКО ETH и молчит зелёным.
        fake = FakeHistory({"BTC": [hist_entry(20), hist_entry(21)], "ETH": [hist_entry(21)]})
        fl.fetch_funding_history = fake
        code2, out2 = self._main_at(_dt(21, 41))
        self.assertEqual(code2, 0)
        self.assertEqual([c for c, _s, _e in fake.calls], ["ETH"])
        o2 = self._outputs()
        self.assertEqual(o2["outcome"], fl.OUTCOME_OK)
        self.assertIn("записать нечего", o2["summary"])
        self.assertEqual(self.sent, [])
        # Биржа отдала запись позже — пара закрывается настоящей строкой, не меткой.
        fl.fetch_funding_history = FakeHistory({"ETH": [hist_entry(20), hist_entry(21)]})
        code3, _ = self._main_at(_dt(21, 50))
        self.assertEqual(code3, 0)
        self.assertEqual(self._outputs()["outcome"], fl.OUTCOME_HEALED)
        eth19 = [r for r in self._rows(_dt(21, 7))
                 if r["coin"] == "ETH" and fl.slot_key_from_ts(r["ts_utc"]) == "2026-09-01T19"]
        self.assertEqual(len(eth19), 1)
        self.assertEqual(eth19[0]["funding"], "0.0007")

    def test_empty_history_list_is_not_a_failed_coin(self):
        """HTTP 200 + [] — штатный ответ биржи «в окне ничего нет»: не отказ
        монеты, пары остаются «без истории»; час без единой строки — partial (С-22)."""
        fl.fetch_funding_history = FakeHistory({})
        self._main_at(_dt(18, 7))
        code, out = self._main_at(_dt(21, 7))
        self.assertEqual(code, 2)
        o = self._outputs()
        self.assertEqual(o["coins_failed"], "0")
        self.assertEqual(o["pairs_no_history"], "4")
        self.assertEqual(o["pairs_unresolved"], "0")
        self.assertEqual(o["unfilled_hours"], "2")
        self.assertIn("часов без единой строки после добора 2 из 2", o["summary"])


class TestSilenceLongerThanWindow(PairGapBase):
    """С-24 (ревью 03.09): архив, молчавший дольше окна, — потеря, не свежий старт."""

    def test_silence_backfills_whole_window_and_goes_red_once(self):
        self._main_at(_dt(18, 7, day=1))
        r = fl.run_once(self.pl, now=_dt(3, 7, day=4), base_dir=self.dir, sha=SHA,
                        dry_run=True)
        self.assertEqual(r["silent_since"], "2026-09-01T18")
        self.assertEqual(len(r["missing_window"]), fl.BACKFILL_WINDOW_HOURS)
        self.assertEqual(r["gap_pairs"], 2 * fl.BACKFILL_WINDOW_HOURS)
        full = FakeHistory({c: [hist_entry(h, day=d, coin=c) for d in (2, 3, 4) for h in range(24)]
                            for c in ("BTC", "ETH")})
        fl.fetch_funding_history = full
        code, out = self._main_at(_dt(3, 7, day=4))
        self.assertEqual(code, 2, "молчание дольше окна красит прогон один раз")
        self.assertIn("АРХИВ МОЛЧАЛ ДОЛЬШЕ ОКНА", out)
        o = self._outputs()
        self.assertEqual(o["outcome"], fl.OUTCOME_PARTIAL)
        self.assertIn("архив молчал дольше окна", o["summary"])
        self.assertEqual(o["pairs_filled"], str(2 * fl.BACKFILL_WINDOW_HOURS))
        code2, _ = self._main_at(_dt(4, 7, day=4))
        self.assertEqual(code2, 0, "окно добрано — следующий прогон зелёный")
        self.assertEqual(self._outputs()["outcome"], fl.OUTCOME_OK)

    def test_fresh_archive_is_not_silence(self):
        code, out = self._main_at(_dt(3, 7, day=4))
        self.assertEqual(code, 0)
        self.assertNotIn("МОЛЧАЛ", out)
        self.assertEqual(self._outputs()["outcome"], fl.OUTCOME_OK)


class TestUndeliveredInfoDecision(PairGapBase):
    """Решение навигатора 03.09.2026 (С-22): «залечено, но ИНФО не доставлено»
    = код 0 + ::warning:: + info_sent=false в артефакте прогона (outputs);
    таблица кодов не меняется, запасного канала нет."""

    def _long_gap(self):
        fl.fetch_funding_history = FakeHistory({"BTC": [hist_entry(h) for h in range(16, 22)],
                                                "ETH": [hist_entry(h) for h in range(16, 22)]})
        self._main_at(_dt(14, 7))

    def test_undelivered_info_is_code_0_with_warning_and_false(self):
        self._long_gap()
        fl.notify_telegram = lambda text, **k: False
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "111:fake",
                                          "TELEGRAM_CHAT_ID": "42"}):
            code, out = self._main_at(_dt(21, 7))
        self.assertEqual(code, 0)
        self.assertEqual(fl.EXIT_BY_OUTCOME, {"ok": 0, "healed": 0, "partial": 2, "error": 1})
        self.assertIn("::warning title=funding-logger — ИНФО не доставлено::", out)
        o = self._outputs()
        self.assertEqual(o["outcome"], fl.OUTCOME_HEALED)
        self.assertEqual(o["exit_code"], "0")
        self.assertEqual(o["info_sent"], "false")

    def test_unconfigured_telegram_is_false_without_warning(self):
        """Локально без ключей ИНФО идёт в лог: info_sent=false, но ::warning::
        не ставится — недоставки не было, доставлять было некуда."""
        self._long_gap()
        fl.notify_telegram = self.stubs[4]        # настоящая функция, без ключей
        code, out = self._main_at(_dt(21, 7))
        self.assertEqual(code, 0)
        self.assertIn("Telegram не настроен", out)
        self.assertNotIn("::warning", out)
        self.assertEqual(self._outputs()["info_sent"], "false")

    def test_outputs_are_written_before_info_is_sent(self):
        """Ревью 03.09: outputs — до ИНФО, чтобы убитый во время отправки job
        оставил шагу тревоги исход; info_sent дописывается после."""
        self._long_gap()
        seen = {}

        def notify(text, **k):
            seen.update(self._outputs())
            return True
        fl.notify_telegram = notify
        code, _ = self._main_at(_dt(21, 7))
        self.assertEqual(code, 0)
        self.assertEqual(seen.get("outcome"), fl.OUTCOME_HEALED, "outcome записан до ИНФО")
        self.assertEqual(seen.get("info_sent"), "", "info_sent до отправки пуст")
        self.assertEqual(self._outputs()["info_sent"], "true")


class TestRunBudget(PairGapBase):
    """Общий бюджет прогона (ревью 03.09): добор не имеет права перелететь
    timeout-minutes job вместе с ИНФО и git-циклом."""

    def test_backfill_deadline_is_trimmed_by_run_budget(self):
        fl.fetch_funding_history = FakeHistory({"BTC": [hist_entry(20), hist_entry(21)],
                                                "ETH": [hist_entry(20), hist_entry(21)]})
        self._main_at(_dt(18, 7))
        ticks = itertools.count(0, 400)          # 400 с между любыми двумя взглядами на часы
        fl.time.monotonic = lambda: next(ticks)
        code, out = self._main_at(_dt(21, 7))
        self.assertEqual(code, 2)
        self.assertIn("бюджет добора урезан до 0 с", out)
        o = self._outputs()
        self.assertEqual(o["timed_out"], "true")
        self.assertEqual(o["pairs_unresolved"], "4")

    def test_history_requests_use_short_retry_profile(self):
        self.assertEqual(fl.HISTORY_TIMEOUT_SEC, 20)
        self.assertEqual(fl.HISTORY_RETRIES, 1)
        self.assertLess(fl.RUN_BUDGET_SEC + 2 * fl.HISTORY_TIMEOUT_SEC + fl.RATE_LIMIT_WAIT_MAX_SEC
                        + (fl.TG_RETRIES + 1) * fl.TG_TIMEOUT_SEC + 60, 600,
                        "худший хвост прогона обязан укладываться в timeout-minutes: 10")


class TestRateLimitWait(unittest.TestCase):
    """25.09.2026: на 429 пауза перед повтором не меньше RATE_LIMIT_WAIT_SEC."""

    class _E(Exception):
        def __init__(self, code, headers=None):
            super().__init__('http %d' % code)
            self.response = type('R', (), {'status_code': code, 'headers': headers or {}})()

    def test_429_waits_at_least_rate_limit(self):
        self.assertEqual(fl._retry_wait(self._E(429), 2.0), fl.RATE_LIMIT_WAIT_SEC)

    def test_429_honours_retry_after_with_cap(self):
        self.assertEqual(fl._retry_wait(self._E(429, {'Retry-After': '17'}), 2.0), 17.0)
        self.assertEqual(fl._retry_wait(self._E(429, {'Retry-After': '999'}), 2.0), fl.RATE_LIMIT_WAIT_MAX_SEC)

    def test_other_errors_keep_growing_pause(self):
        self.assertEqual(fl._retry_wait(self._E(500), 4.0), 4.0)
        self.assertEqual(fl._retry_wait(RuntimeError('timeout'), 2.0), 2.0)


def _dt(hour, minute=0, day=1):
    """Фиксированный момент времени: тест не должен зависеть от часов машины."""
    return datetime(2026, 9, day, hour, minute, 3, tzinfo=timezone.utc)


if __name__ == "__main__":
    unittest.main(verbosity=2)

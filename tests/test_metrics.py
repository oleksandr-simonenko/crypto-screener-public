#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Селфтест метрик и загрузчика сделок (блоки A2/A3/A4): tests/test_metrics.py

ВСЁ НА СИНТЕТИКЕ. Ни один тест не читает боевые tournament.json,
positions.json и candidates_log.jsonl: на 01.09.2026 закрытых сделок там ноль,
и тест, написанный по боевым файлам, зелёный ровно потому, что считать нечего.
Такой тест молчит именно в тот день, когда закрытия появятся.

Что проверяется:
  * медиана, IQR и доля прибыльных — на числах, выписанных руками
    (квантили сверены с numpy.percentile отдельным разовым прогоном:
     метод — линейная интерполяция, тип 7 Hyndman-Fan);
  * max_drawdown на ИЗВЕСТНОЙ кривой, посчитанной в уме;
  * top3_contribution при неположительной сумме = None и отметка причиной;
  * ПУСТОЙ вход даёт валидный артефакт со status «нет закрытых сделок»
    и не падает — это ровно сегодняшнее состояние проекта;
  * воронка A4 считает разрезы no_perp (нет площадок / перп исчез / новая монета);
  * загрузчик trades.py берёт ret_gross_pct ИЗ ret_pct и не пересчитывает его,
    а объём подтягивает строкой НЕ ПОЗЖЕ даты входа (без заглядывания вперёд);
  * diff_metrics видит появление первых закрытых сделок и смену хеша правил.

Запуск: python -m unittest discover -s tests -v
"""

import contextlib
import io
import itertools
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import costs        # noqa: E402
import metrics      # noqa: E402
import trades as trades_mod   # noqa: E402


# =========================================================================
#  ВСПОМОГАТЕЛЬНОЕ: синтетические сделки и синтетический репозиторий
# =========================================================================

def make_trade(track="A1", net=None, gross=None, exit_date="2026-09-20",
               status="closed", liq_class="deep", sym="TST", cg_id="tst",
               hold_days=60):
    """Одна синтетическая сделка в схеме trades.TRADE_FIELDS.

    net и gross задаются НЕЗАВИСИМО: метрики обязаны брать то, что им дали,
    а не пересчитывать одно из другого.
    """
    return {
        "track": track,
        "sym": sym,
        "cg_id": cg_id,
        "entry_date": "2026-07-20",
        "exit_date": exit_date if status == "closed" else None,
        "entry_price": 1.0,
        "exit_price": 1.1 if status == "closed" else None,
        "last_price": 1.1,
        "peak_price": 1.2,
        "status": status,
        "close_reason": "60д" if status == "closed" else None,
        "hold_days": hold_days,
        "ret_gross_pct": gross if status == "closed" else None,
        "vol_usd_24h": 50_000_000.0,
        "liq_class": liq_class,
        "ret_net_pct": net if status == "closed" else None,
    }


def closed_with_nets(nets, track="A1", start_day=1):
    """Список закрытых сделок с заданными net (gross = net для простоты).

    Даты выхода идут по возрастанию, чтобы порядок кривой капитала был
    предсказуем и совпадал с порядком списка.
    """
    out = []
    for i, net in enumerate(nets):
        out.append(make_trade(track=track, net=float(net), gross=float(net),
                              exit_date="2026-09-%02d" % (start_day + i),
                              sym="T%d" % i, cg_id="t%d" % i))
    return out


def write_repo(tmp, tournament=None, positions=None, log_lines=None):
    """Создать синтетический репозиторий скринера во временном каталоге."""
    with open(os.path.join(tmp, "tournament.json"), "w", encoding="utf-8") as fh:
        json.dump(tournament if tournament is not None
                  else {"tracks": {}, "meta": {}}, fh, ensure_ascii=False)
    with open(os.path.join(tmp, "positions.json"), "w", encoding="utf-8") as fh:
        json.dump(positions if positions is not None
                  else {"open": [], "closed": [], "meta": {}}, fh, ensure_ascii=False)
    with open(os.path.join(tmp, "candidates_log.jsonl"), "w", encoding="utf-8") as fh:
        for line in (log_lines or []):
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return tmp


def coin_line(run_id, ts, cg_id, reject_reason, venues_n=0, vol=None, decision="reject"):
    """Строка монеты журнала кандидатов в боевой схеме."""
    return {
        "run_id": run_id,
        "timestamp_utc": ts,
        "fetched_at": ts,
        "ticker": cg_id.upper(),
        "cg_id": cg_id,
        "decision": decision,
        "reject_reason": reject_reason,
        "score": None,
        "features": {"price": 1.0, "mcap": 10_000_000, "vol": vol, "venues_n": venues_n},
        "notes": [],
        "features_tier": "cheap_only",
        "feature_schema_version": 4,
        "screener_version": "4.13",
    }


def run_line(run_id, ts):
    return {"_run": {"run_id": run_id, "timestamp_utc": ts, "screener_version": "4.13"}}


# =========================================================================
#  A2/A3 — АРИФМЕТИКА МЕТРИК ДОРОЖКИ
# =========================================================================

class TestTrackMetricsArithmetic(unittest.TestCase):

    def test_median_iqr_share_profitable(self):
        """Медиана, IQR и доля прибыльных на числах, выписанных руками.

        nets = [-12.5, -7.25, -3.0, 1.5, 4.75, 9.0, 15.25], n = 7.
        Медиана — четвёртый элемент = 1.5.
        Квартили (линейная интерполяция, позиция (n-1)*q):
          q1: позиция 1.5 -> -7.25 + 0.5*(-3.0 + 7.25) = -5.125
          q3: позиция 4.5 ->  4.75 + 0.5*( 9.0 - 4.75) =  6.875
        Прибыльных (net > 0) четыре из семи -> 4/7.
        """
        nets = [-12.5, -7.25, -3.0, 1.5, 4.75, 9.0, 15.25]
        res = metrics.track_metrics(closed_with_nets(nets))

        self.assertEqual(res["n_closed"], 7)
        self.assertEqual(res["status"], metrics.HAS_CLOSED_STATUS)
        self.assertAlmostEqual(res["median_net"], 1.5, places=9)
        self.assertAlmostEqual(res["iqr_net"][0], -5.125, places=9)
        self.assertAlmostEqual(res["iqr_net"][1], 6.875, places=9)
        # артефакт округляет числа до 6 знаков (читаемость json), поэтому
        # допуск сравнения — 1e-6, а не машинная точность
        self.assertAlmostEqual(res["share_profitable_net"], 4.0 / 7.0, delta=1e-6)
        self.assertAlmostEqual(res["mean_net"], sum(nets) / 7.0, delta=1e-6)
        self.assertAlmostEqual(res["worst_trade_net"], -12.5, places=9)
        self.assertAlmostEqual(res["best_trade_net"], 15.25, places=9)

    def test_median_even_count(self):
        """Чётное число сделок: медиана = среднее двух средних."""
        res = metrics.track_metrics(closed_with_nets([1.0, 2.0, 3.0, 4.0]))
        self.assertAlmostEqual(res["median_net"], 2.5, places=9)
        self.assertAlmostEqual(res["iqr_net"][0], 1.75, places=9)
        self.assertAlmostEqual(res["iqr_net"][1], 3.25, places=9)

    def test_gross_and_net_are_independent(self):
        """gross и net берутся ИЗ записи, а не выводятся один из другого."""
        rows = [make_trade(net=-1.0, gross=5.0, exit_date="2026-09-01"),
                make_trade(net=-3.0, gross=3.0, exit_date="2026-09-02")]
        res = metrics.track_metrics(rows)
        self.assertAlmostEqual(res["mean_gross"], 4.0, places=9)
        self.assertAlmostEqual(res["mean_net"], -2.0, places=9)

    def test_share_profitable_strictly_positive(self):
        """Ровный ноль прибыльной сделкой НЕ считается: издержки уже вычтены."""
        res = metrics.track_metrics(closed_with_nets([0.0, 0.0, 5.0, -5.0]))
        self.assertAlmostEqual(res["share_profitable_net"], 0.25, places=9)

    def test_open_positions_never_counted(self):
        """§2 замороженных правил: открытые позиции не идут в метрики никак."""
        rows = closed_with_nets([10.0, -4.0])
        rows.append(make_trade(status="open"))
        rows.append(make_trade(status="open"))
        res = metrics.track_metrics(rows)
        self.assertEqual(res["n_closed"], 2)
        self.assertEqual(res["n_open"], 2)
        self.assertAlmostEqual(res["mean_net"], 3.0, places=9)


class TestMaxDrawdown(unittest.TestCase):

    def test_known_curve(self):
        """Известная кривая: net = [+10, -20, +5].

        E: 1.00 -> 1.10 -> 0.90 -> 0.95; пик 1.10, дно 0.90.
        Просадка = (1.10 - 0.90) / 1.10 * 100 = 18.1818...%
        """
        self.assertAlmostEqual(metrics.max_drawdown_pct([10.0, -20.0, 5.0]),
                               (1.10 - 0.90) / 1.10 * 100.0, places=9)

    def test_monotone_growth_has_no_drawdown(self):
        self.assertAlmostEqual(metrics.max_drawdown_pct([5.0, 5.0, 5.0]), 0.0, places=12)

    def test_first_trade_loss_measures_from_start(self):
        """Убыток первой же сделкой: пик = стартовый капитал."""
        self.assertAlmostEqual(metrics.max_drawdown_pct([-25.0]), 25.0, places=9)

    def test_scale_invariance(self):
        """Модель кривой масштабно-инвариантна: '$100' не подгоночный параметр."""
        nets = [12.0, -30.0, 8.0, -5.0]
        # та же последовательность, посчитанная вручную по определению
        equity, peak, worst = 1.0, 1.0, 0.0
        for net in nets:
            equity += net / 100.0
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak * 100.0)
        self.assertAlmostEqual(metrics.max_drawdown_pct(nets), worst, places=12)

    def test_order_is_by_exit_date_not_input_order(self):
        """Кривая строится по exit_date, а не по порядку в списке."""
        rows = [make_trade(net=5.0, gross=5.0, exit_date="2026-09-30", sym="B", cg_id="b"),
                make_trade(net=-20.0, gross=-20.0, exit_date="2026-09-10", sym="A", cg_id="a")]
        res = metrics.track_metrics(rows)
        # верный порядок: сначала -20 (просадка 20%), потом +5
        self.assertAlmostEqual(res["max_drawdown_pct"], 20.0, places=9)


class TestDrawdownOrderIsDeterministic(unittest.TestCase):
    """Находка ревизии: просадка зависела от порядка строк в json.

    Сортировка ТОЛЬКО по exit_date неполна — в один день закрывается сразу
    несколько позиций, а sorted() устойчива, поэтому внутри дня сохранялся
    порядок входного списка. Тот же набор сделок, разложенный в файле иначе,
    давал другую просадку с шестью знаками после запятой. Здесь это проверяется
    ВСЕМИ перестановками, а не одной.
    """

    def _same_day(self):
        """Три сделки, закрытые В ОДИН ДЕНЬ. Порядок ключа — по cg_id."""
        return [
            make_trade(net=10.0, gross=10.0, exit_date="2026-09-15", sym="AAA", cg_id="aaa"),
            make_trade(net=-20.0, gross=-20.0, exit_date="2026-09-15", sym="BBB", cg_id="bbb"),
            make_trade(net=5.0, gross=5.0, exit_date="2026-09-15", sym="CCC", cg_id="ccc"),
        ]

    def test_all_permutations_give_one_number(self):
        """Шесть перестановок одного набора -> одно и то же число."""
        seen = {metrics.track_metrics(list(p))["max_drawdown_pct"]
                for p in itertools.permutations(self._same_day())}
        self.assertEqual(len(seen), 1,
                         "просадка зависит от порядка строк: получено %s" % sorted(seen))
        # порядок ключа = по cg_id: +10, -20, +5 -> пик 1.10, дно 0.90
        self.assertAlmostEqual(seen.pop(), (1.10 - 0.90) / 1.10 * 100.0, places=6)

    def test_old_exit_date_only_order_would_have_differed(self):
        """Доказательство, что находка реальна, а не гипотетична.

        Считаем ту же тройку старым способом (сортировка только по exit_date,
        то есть фактически по порядку списка) в двух раскладках — числа разные.
        Значит новая полная сортировка чинит настоящую беду.
        """
        rows = self._same_day()
        old_a = metrics.max_drawdown_pct(
            [t["ret_net_pct"] for t in sorted(rows, key=lambda t: t["exit_date"])])
        old_b = metrics.max_drawdown_pct(
            [t["ret_net_pct"] for t in sorted(rows[::-1], key=lambda t: t["exit_date"])])
        self.assertNotAlmostEqual(old_a, old_b, places=6)

    def test_sort_key_is_total_and_survives_holes(self):
        """Ключ полный и не падает на дырах: None в датах и в net."""
        holed = {"track": "A1", "sym": None, "cg_id": None,
                 "exit_date": None, "entry_date": None, "ret_net_pct": None}
        full = make_trade(net=1.0, gross=1.0, exit_date="2026-09-15")
        keys = sorted([metrics.curve_sort_key(holed), metrics.curve_sort_key(full)])
        # сделка без даты выхода уходит В КОНЕЦ, а не в начало
        self.assertEqual(keys[-1][0], "9999-12-31")
        self.assertEqual(len(metrics.curve_sort_key(full)),
                         len(metrics.EQUITY_CURVE_ORDER_FIELDS))

    def test_track_publishes_the_order_it_used(self):
        """Порядок обязан быть НАПИСАН в артефакте, а не подразумеваться."""
        res = metrics.track_metrics(self._same_day())
        self.assertIn("exit_date", res["equity_curve_order"])
        self.assertIn("cg_id", res["equity_curve_order"])
        # и на пустой дорожке тоже: порядок — свойство расчёта, не данных
        self.assertIn("exit_date", metrics.track_metrics([])["equity_curve_order"])


class TestEqualWeightDrawdown(unittest.TestCase):
    """Вторая, честная метрика: просадка равновзвешенного портфеля по датам."""

    def test_known_curve(self):
        """N=3, выходы в разные дни, net = [+10, -20, +5], доля каждой 1/3.

        E: 1 -> 1+0.10/3 = 1.033333 -> -0.20/3 -> 0.966667 -> +0.05/3 -> 0.983333
        Просадка = (1.033333 - 0.966667) / 1.033333 * 100 = 6.451613 %
        """
        rows = closed_with_nets([10.0, -20.0, 5.0])
        got = metrics.max_drawdown_pct_equal_weight(rows)
        peak = 1.0 + 0.10 / 3.0
        low = peak - 0.20 / 3.0
        self.assertAlmostEqual(got, (peak - low) / peak * 100.0, places=9)

    def test_same_day_exits_collapse_into_one_move(self):
        """Все выходы в один день -> одно движение капитала, порядок не важен.

        Сумма net = -5 на трёх сделках -> E = 1 - 0.05/100/3*3 = 0.983333,
        просадка от старта = 1.666667 %.
        """
        rows = [make_trade(net=10.0, gross=10.0, exit_date="2026-09-15", cg_id="a", sym="A"),
                make_trade(net=-20.0, gross=-20.0, exit_date="2026-09-15", cg_id="b", sym="B"),
                make_trade(net=5.0, gross=5.0, exit_date="2026-09-15", cg_id="c", sym="C")]
        equity = 1.0 + (10.0 - 20.0 + 5.0) / 100.0 / 3.0
        want = (1.0 - equity) / 1.0 * 100.0
        got = metrics.max_drawdown_pct_equal_weight(rows)
        self.assertAlmostEqual(got, want, places=9)
        # и от перестановок не меняется вовсе — по устройству модели
        self.assertAlmostEqual(metrics.max_drawdown_pct_equal_weight(rows[::-1]),
                               got, places=12)

    def test_empty_gives_none(self):
        self.assertIsNone(metrics.max_drawdown_pct_equal_weight([]))
        self.assertIsNone(metrics.track_metrics([])["max_drawdown_pct_equal_weight"])

    def test_track_publishes_both_numbers_and_the_caveat(self):
        """Рядом с просадкой обязана лежать оговорка про «одну ставку за раз»."""
        res = metrics.track_metrics(closed_with_nets([10.0, -20.0, 5.0]))
        self.assertIsNotNone(res["max_drawdown_pct"])
        self.assertIsNotNone(res["max_drawdown_pct_equal_weight"])
        note = res["max_drawdown_note"]
        self.assertIn("одна ставка за раз", note)
        self.assertIn("60", note)
        self.assertIn("НЕ просадка портфеля", note)
        # оговорка есть и на пустой дорожке: она про модель, а не про данные
        self.assertIn("одна ставка за раз", metrics.track_metrics([])["max_drawdown_note"])

    def test_equal_weight_is_milder_on_the_same_data(self):
        """Санити: делёж капитала на N уменьшает амплитуду кривой.

        Не закон природы, а проверка, что вторая метрика — другая модель, а не
        переименованная первая.
        """
        rows = closed_with_nets([10.0, -20.0, 5.0])
        seq = metrics.track_metrics(rows)["max_drawdown_pct"]
        eqw = metrics.track_metrics(rows)["max_drawdown_pct_equal_weight"]
        self.assertLess(eqw, seq)


class TestTop3Contribution(unittest.TestCase):

    def test_negative_sum_gives_none_and_note(self):
        """Сумма net <= 0 -> доля не определена: None и объяснение."""
        share, note = metrics.top3_contribution([-10.0, -5.0, 2.0, 1.0])
        self.assertIsNone(share)
        self.assertIn("сумма net", note)

        res = metrics.track_metrics(closed_with_nets([-10.0, -5.0, 2.0, 1.0]))
        self.assertIsNone(res["top3_contribution_share"])
        self.assertTrue(res["top3_contribution_note"])

    def test_zero_sum_also_none(self):
        """Ровный ноль тоже не годится: делить на него нельзя."""
        share, _note = metrics.top3_contribution([5.0, -5.0])
        self.assertIsNone(share)

    def test_positive_sum_is_computed(self):
        """Сумма 100, три лучших дают 90 -> доля 0.9."""
        share, _note = metrics.top3_contribution([50.0, 25.0, 15.0, 5.0, 5.0])
        self.assertAlmostEqual(share, 90.0 / 100.0, places=9)

    def test_fewer_than_three_trades(self):
        """Сделок меньше трёх — берём сколько есть, доля = 1.0."""
        share, _note = metrics.top3_contribution([4.0, 6.0])
        self.assertAlmostEqual(share, 1.0, places=9)


class TestEmptyTrack(unittest.TestCase):

    def test_empty_input_gives_nulls_not_zeros(self):
        """Пусто -> None, а не 0: ноль это результат, пусто — его отсутствие."""
        res = metrics.track_metrics([])
        self.assertEqual(res["n_closed"], 0)
        self.assertEqual(res["status"], metrics.NO_CLOSED_STATUS)
        for field in ("mean_gross", "median_gross", "mean_net", "median_net",
                      "share_profitable_net", "max_drawdown_pct",
                      "top3_contribution_share", "worst_trade_net", "best_trade_net"):
            self.assertIsNone(res[field], "поле %s должно быть None на пустом входе" % field)
        self.assertEqual(res["iqr_net"], [None, None])

    def test_only_open_positions_is_empty_input(self):
        """Дорожка с одними открытыми = пустой вход для метрик."""
        res = metrics.track_metrics([make_trade(status="open") for _ in range(5)])
        self.assertEqual(res["n_closed"], 0)
        self.assertEqual(res["n_open"], 5)
        self.assertEqual(res["status"], metrics.NO_CLOSED_STATUS)


# =========================================================================
#  ПОДСТАНОВКА ХУДШЕГО КЛАССА ИЗДЕРЖЕК — ДВА ЧИСЛА, А НЕ ОДНО
# =========================================================================

class TestLiqClassFallbackCounts(unittest.TestCase):
    """Находка ревизии: поле считалось по ЗАКРЫТЫМ сделкам.

    Смысл поля — «сколько сделок посчитано по самому дорогому классу, потому
    что объём на дату входа неизвестен». Закрытых 01.09.2026 ноль, поле
    показывало 0, а подстановка шла по 151 сделке из 421 — то есть поле молчало
    ровно в тот день, когда его и надо было прочитать.
    """

    def test_open_positions_are_counted_in_all(self):
        """Ноль закрытых, все открытые без класса -> _all видит их, _closed нет."""
        rows = [make_trade(status="open", liq_class="unknown") for _ in range(4)]
        res = metrics.track_metrics(rows)
        self.assertEqual(res["n_closed"], 0)
        self.assertEqual(res["liq_class_fallback_n_all"], 4)
        self.assertEqual(res["liq_class_fallback_n_closed"], 0)
        # головное поле без суффикса = _all: ноль в этом месте и был находкой
        self.assertEqual(res["liq_class_fallback_n"], 4)
        self.assertIn("liq_class_fallback_n_all", res["liq_class_fallback_note"])

    def test_closed_and_open_are_separated(self):
        """Смешанная дорожка: числа расходятся, и оба видны."""
        rows = [make_trade(net=1.0, gross=2.0, exit_date="2026-09-01",
                           liq_class="unknown", cg_id="a", sym="A"),
                make_trade(net=1.0, gross=2.0, exit_date="2026-09-02",
                           liq_class="deep", cg_id="b", sym="B"),
                make_trade(status="open", liq_class="unknown", cg_id="c", sym="C"),
                make_trade(status="open", liq_class="unknown", cg_id="d", sym="D")]
        res = metrics.track_metrics(rows)
        self.assertEqual(res["liq_class_fallback_n_closed"], 1)
        self.assertEqual(res["liq_class_fallback_n_all"], 3)
        self.assertEqual(res["liq_class_counts"], {"deep": 1, "unknown": 1})
        self.assertEqual(res["liq_class_counts_all"], {"deep": 1, "unknown": 3})

    def test_no_fallback_at_all_is_honest_zero(self):
        """Все классы известны -> оба числа ноль. Это ноль-результат, он законен."""
        res = metrics.track_metrics(closed_with_nets([1.0, 2.0]))
        self.assertEqual(res["liq_class_fallback_n_all"], 0)
        self.assertEqual(res["liq_class_fallback_n_closed"], 0)

    def test_artifact_shows_fallback_with_zero_closed(self):
        """Сборка целиком: закрытых ноль, а подстановка видна числом.

        Воспроизводит боевой случай — позиция открыта раньше начала журнала
        кандидатов, объёма на дату входа взять неоткуда, класс unknown.
        """
        log = [run_line("r1", "2026-08-03T10:00:00Z"),
               coin_line("r1", "2026-08-03T10:00:00Z", "tst", "low_score", vol=9_000_000.0)]
        positions = {"open": [{"sym": "TST", "cg_id": "tst", "entry_date": "2026-07-20",
                               "entry_price": 1.0, "last_price": 1.0, "peak_price": 1.0,
                               "miss": 0}],
                     "closed": [], "meta": {}}
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(tmp, positions=positions, log_lines=log)
            payload = metrics.build_metrics(tmp, as_of="2026-09-01")

        self.assertEqual(payload["n_closed_total"], 0)
        self.assertEqual(payload["n_trades_total"], 1)
        self.assertEqual(payload["liq_class_fallback_n_all"], 1)
        self.assertEqual(payload["liq_class_fallback_n_closed"], 0)
        self.assertEqual(payload["liq_class_fallback_n"], 1)
        # и это сказано словами в шапке артефакта, а не только числом
        self.assertIn("liq_class_fallback_n_all", payload["note"])


# =========================================================================
#  ПУТИ В АРТЕФАКТЕ — ТОЛЬКО ОТНОСИТЕЛЬНЫЕ
# =========================================================================

class TestArtifactPathsAreRelative(unittest.TestCase):
    """Находка ревизии: в json уезжал абсолютный путь машины-исполнителя.

    Последствия два: один и тот же расчёт локально и на GitHub Actions даёт
    разные файлы (diff недели показывает изменение там, где менялась машина),
    и в публикуемый артефакт попадает имя пользователя Windows.
    """

    def test_funnel_log_path_is_relative_to_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(tmp, log_lines=[run_line("r1", "2026-08-03T10:00:00Z")])
            res = metrics.funnel_metrics(os.path.join(tmp, "candidates_log.jsonl"),
                                         repo_dir=tmp)
        self.assertEqual(res["log_path"], "candidates_log.jsonl")
        self.assertFalse(os.path.isabs(res["log_path"]))

    def test_no_absolute_path_anywhere_in_payload(self):
        """Весь артефакт целиком: каталога машины в нём быть не должно."""
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(tmp)
            payload = metrics.build_metrics(tmp)
            text = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn(tmp, text)
            self.assertNotIn(tmp.replace("\\", "/"), text)
            self.assertNotIn(os.path.abspath(tmp), text)
        self.assertFalse(os.path.isabs(payload["funnel"]["log_path"]))
        self.assertIn("репозитория", payload["paths_are_relative_to"])

    def test_rel_to_repo_uses_forward_slashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep = os.path.join(tmp, "artifacts", "2026-09", "metrics.json")
            self.assertEqual(metrics.rel_to_repo(deep, tmp),
                             "artifacts/2026-09/metrics.json")

    def test_path_outside_repo_falls_back_to_basename(self):
        """Файл вне репозитория: относительного пути нет, абсолютный печатать нельзя."""
        with tempfile.TemporaryDirectory() as outer:
            repo = os.path.join(outer, "repo")
            os.makedirs(repo)
            outside = os.path.join(outer, "чужой.jsonl")
            got = metrics.rel_to_repo(outside, repo)
        self.assertEqual(got, "чужой.jsonl")
        self.assertFalse(os.path.isabs(got))

    def test_none_path_stays_none(self):
        self.assertIsNone(metrics.rel_to_repo(None, "."))
        self.assertIsNone(metrics.funnel_metrics(None)["log_path"])


# =========================================================================
#  A4 — ВОРОНКА
# =========================================================================

class TestFunnel(unittest.TestCase):

    def _log(self):
        """Два прогона в РАЗНЫХ ISO-неделях с известным составом.

        Прогон 1 (2026-08-03, W32): aaa no_perp/venues=0, bbb enter,
                                    ccc no_perp/venues=2, ddd reject low_score.
        Прогон 2 (2026-08-10, W33): aaa no_perp/venues=0 (как был),
                                    bbb no_perp/venues=0 (ПЕРП ИСЧЕЗ),
                                    eee no_perp/venues=0 (НОВАЯ монета),
                                    ddd reject low_score.
        Значит в прогоне 2: no_perp = 3, нет площадок вовсе = 3,
        vanished_since_prev = 1 (bbb), new_coin = 1 (eee).
        """
        r1, t1 = "run-1", "2026-08-03T10:00:00Z"
        r2, t2 = "run-2", "2026-08-10T10:00:00Z"
        return [
            {"_meta": {"created": t1}},
            run_line(r1, t1),
            coin_line(r1, t1, "aaa", "no_perp", venues_n=0),
            coin_line(r1, t1, "bbb", None, venues_n=3, decision="enter"),
            coin_line(r1, t1, "ccc", "no_perp", venues_n=2),
            coin_line(r1, t1, "ddd", "low_score", venues_n=4),
            run_line(r2, t2),
            coin_line(r2, t2, "aaa", "no_perp", venues_n=0),
            coin_line(r2, t2, "bbb", "no_perp", venues_n=0),
            coin_line(r2, t2, "eee", "no_perp", venues_n=0),
            coin_line(r2, t2, "ddd", "low_score", venues_n=4),
        ]

    def test_no_perp_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(tmp, log_lines=self._log())
            res = metrics.funnel_metrics(os.path.join(tmp, "candidates_log.jsonl"))

        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["n_runs"], 2)
        self.assertEqual(res["bad_lines"], 0)

        run1, run2 = res["runs"]
        self.assertEqual(run1["n_lines"], 4)
        self.assertEqual(run1["no_perp_n"], 2)
        self.assertEqual(run1["enter_n"], 1)
        self.assertEqual(run1["reject_counts"],
                         {"low_score": 1, "no_perp": 2, "none": 1})
        # первый прогон: сравнивать не с чем, и это НЕ ноль, а «неизвестно»
        self.assertIsNone(run1["no_perp_sources"]["vanished_since_prev"])
        self.assertIsNone(run1["no_perp_sources"]["new_coin"])
        self.assertEqual(run1["no_perp_sources"]["no_venue_at_all"], 1)
        self.assertEqual(run1["no_perp_sources"]["venues_but_no_perp"], 1)

        self.assertEqual(run2["no_perp_n"], 3)
        src = run2["no_perp_sources"]
        self.assertEqual(src["no_venue_at_all"], 3)
        self.assertEqual(src["vanished_since_prev"], 1)   # bbb потеряла перп
        self.assertEqual(src["new_coin"], 1)              # eee пришла новой
        self.assertEqual(src["prev_run_id"], "run-1")
        self.assertAlmostEqual(src["share_vanished_since_prev"], 1.0 / 3.0, places=6)

    def test_no_perp_trend_and_weekly_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(tmp, log_lines=self._log())
            res = metrics.funnel_metrics(os.path.join(tmp, "candidates_log.jsonl"))

        self.assertEqual(len(res["no_perp_trend"]), 2)
        self.assertAlmostEqual(res["no_perp_trend"][0][1], 2.0 / 4.0, places=6)
        self.assertAlmostEqual(res["no_perp_trend"][1][1], 3.0 / 4.0, places=6)
        self.assertEqual([w["week"] for w in res["weekly"]], ["2026-W32", "2026-W33"])
        # дельта в ПРОЦЕНТНЫХ ПУНКТАХ: 75% - 50% = +25 п.п.
        self.assertAlmostEqual(res["delta_last_week_pp"], 25.0, places=6)

    def test_missing_log_does_not_crash(self):
        res = metrics.funnel_metrics(os.path.join(tempfile.gettempdir(), "нет-такого-файла.jsonl"))
        self.assertEqual(res["n_runs"], 0)
        self.assertEqual(res["runs"], [])
        self.assertIn("не найден", res["status"])

    def test_broken_lines_are_counted_not_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "candidates_log.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(run_line("run-1", "2026-08-03T10:00:00Z")) + "\n")
                fh.write(json.dumps(coin_line("run-1", "2026-08-03T10:00:00Z", "aaa", "no_perp")) + "\n")
                fh.write("{это не json\n")
            res = metrics.funnel_metrics(path)
        self.assertEqual(res["bad_lines"], 1)
        self.assertEqual(res["n_runs"], 1)


# =========================================================================
#  ЗАГРУЗЧИК СДЕЛОК
# =========================================================================

class TestTradesLoader(unittest.TestCase):

    def test_ret_gross_is_taken_verbatim_not_recomputed(self):
        """ret_gross_pct = ret_pct турнира ДОСЛОВНО.

        Цены в записи намеренно НЕ согласованы с ret_pct (по ценам вышло бы
        +10%, а записано +7.5%). Загрузчик обязан отдать записанное: это
        протокольные данные, а не наша арифметика.
        """
        tournament = {"tracks": {"A1": {"open": [], "closed": [{
            "sym": "TST", "cg_id": "tst", "entry_date": "2026-08-03",
            "entry_price": 1.0, "last_price": 1.10, "peak_price": 1.30,
            "miss": 0, "ret_pct": 7.5, "peak_ret_pct": 30.0,
            "close_date": "2026-10-02", "close_reason": "60д"}]}}}
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(tmp, tournament=tournament, log_lines=[
                run_line("run-1", "2026-08-03T10:00:00Z"),
                coin_line("run-1", "2026-08-03T10:00:00Z", "tst", None,
                          venues_n=3, vol=50_000_000.0, decision="enter"),
            ])
            rows = trades_mod.load_all_trades(tmp)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "closed")
        self.assertAlmostEqual(row["ret_gross_pct"], 7.5, places=9)
        self.assertEqual(row["exit_date"], "2026-10-02")
        self.assertEqual(row["hold_days"], 60)
        self.assertEqual(row["liq_class"], "deep")   # 50M >= порога deep
        cfg = costs.load_costs()
        self.assertAlmostEqual(row["ret_net_pct"],
                               costs.net_ret_pct(7.5, "deep", 60, cfg), places=9)

    def test_open_position_has_no_returns(self):
        """§2 правил: у открытой позиции результата нет ни в каком виде."""
        tournament = {"tracks": {"A1": {"closed": [], "open": [{
            "sym": "OPN", "cg_id": "opn", "entry_date": "2026-08-03",
            "entry_price": 1.0, "last_price": 2.0, "peak_price": 2.5, "miss": 0}]}}}
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(tmp, tournament=tournament)
            rows = trades_mod.load_all_trades(tmp, as_of="2026-09-01")

        self.assertEqual(rows[0]["status"], "open")
        self.assertIsNone(rows[0]["ret_gross_pct"])
        self.assertIsNone(rows[0]["ret_net_pct"])
        self.assertIsNone(rows[0]["exit_date"])
        self.assertEqual(rows[0]["hold_days"], 29)   # 03.08 -> 01.09

    def test_volume_lookup_never_looks_into_future(self):
        """Берётся последняя строка НЕ ПОЗЖЕ даты входа.

        В журнале три строки по монете: до входа (2M -> medium), в день входа
        (500k -> thin) и после входа (50M -> deep). Правильный ответ — 500k:
        объём, замеренный после входа, в момент входа знать было нельзя.
        """
        log = [
            run_line("r1", "2026-08-03T10:00:00Z"),
            coin_line("r1", "2026-08-03T10:00:00Z", "tst", "low_score", vol=2_000_000.0),
            run_line("r2", "2026-08-10T10:00:00Z"),
            coin_line("r2", "2026-08-10T10:00:00Z", "tst", None, vol=500_000.0, decision="enter"),
            run_line("r3", "2026-08-13T10:00:00Z"),
            coin_line("r3", "2026-08-13T10:00:00Z", "tst", "low_score", vol=50_000_000.0),
        ]
        positions = {"open": [{"sym": "TST", "cg_id": "tst", "entry_date": "2026-08-10",
                               "entry_price": 1.0, "last_price": 1.0, "peak_price": 1.0,
                               "miss": 0}],
                     "closed": [], "meta": {}}
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(tmp, positions=positions, log_lines=log)
            rows = trades_mod.load_all_trades(tmp, as_of="2026-09-01")

        self.assertEqual(rows[0]["track"], "A0")
        self.assertAlmostEqual(rows[0]["vol_usd_24h"], 500_000.0, places=6)
        self.assertEqual(rows[0]["liq_class"], "thin")

    def test_no_volume_before_entry_gives_unknown(self):
        """Вход раньше начала журнала -> объёма нет -> unknown -> худший класс.

        Это ровно случай позиций, открытых 14-30.07.2026: журнал кандидатов
        начинается 03.08.2026, и подставить им объём неоткуда.
        """
        log = [run_line("r1", "2026-08-03T10:00:00Z"),
               coin_line("r1", "2026-08-03T10:00:00Z", "tst", "low_score", vol=9_000_000.0)]
        positions = {"open": [{"sym": "TST", "cg_id": "tst", "entry_date": "2026-07-20",
                               "entry_price": 1.0, "last_price": 1.0, "peak_price": 1.0,
                               "miss": 0}],
                     "closed": [], "meta": {}}
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(tmp, positions=positions, log_lines=log)
            rows = trades_mod.load_all_trades(tmp, as_of="2026-09-01")

        self.assertIsNone(rows[0]["vol_usd_24h"])
        self.assertEqual(rows[0]["liq_class"], "unknown")
        self.assertTrue(costs.is_fallback_class(rows[0]["liq_class"]))

    def test_schema_fields_exact(self):
        """Схема записи — контракт для metrics/verdict/export."""
        tournament = {"tracks": {"A7": {"closed": [], "open": [{
            "sym": "RND", "cg_id": "rnd", "entry_date": "2026-08-03",
            "entry_price": 1.0, "last_price": 1.0, "peak_price": 1.0, "miss": 0}]}}}
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(tmp, tournament=tournament)
            rows = trades_mod.load_all_trades(tmp, as_of="2026-09-01")
        self.assertEqual(set(rows[0].keys()), set(trades_mod.TRADE_FIELDS))

    def test_missing_files_give_empty_list(self):
        """Нет файлов — нет сделок, но и не падение."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(trades_mod.load_all_trades(tmp), [])


# =========================================================================
#  СБОРКА АРТЕФАКТА НА ПУСТОМ ВХОДЕ (сегодняшнее состояние проекта)
# =========================================================================

class TestBuildMetricsEmpty(unittest.TestCase):

    def test_empty_repo_gives_valid_artifact(self):
        """Ноль закрытых -> валидный артефакт со status «нет закрытых сделок»."""
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(tmp)
            payload = metrics.build_metrics(tmp)
            latest, dated = metrics.write_artifacts(payload, tmp)

            self.assertTrue(os.path.isfile(latest))
            self.assertTrue(os.path.isfile(dated))
            with open(latest, "r", encoding="utf-8") as fh:
                on_disk = json.load(fh)

        self.assertEqual(payload["status"], metrics.NO_CLOSED_STATUS)
        self.assertEqual(payload["n_closed_total"], 0)
        for key in ("provenance", "costs_config_used", "tracks", "funnel",
                    "rules_sha256", "n_closed_total", "note"):
            self.assertIn(key, payload, "в артефакте обязано быть поле %s" % key)
        self.assertIn("git_sha", payload["provenance"])
        self.assertIn("generated_utc", payload["provenance"])
        self.assertEqual(on_disk["status"], metrics.NO_CLOSED_STATUS)
        # таблица чувствительности существует даже на пустых данных
        self.assertEqual(len(payload["overall"]["sensitivity"]),
                         len(payload["costs_config_used"]["sensitivity_multipliers"]))

    def test_artifact_is_json_serialisable_and_dated_path(self):
        """Датированная копия ложится в artifacts/YYYY-MM/metrics_YYYYMMDD.json."""
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(tmp)
            payload = metrics.build_metrics(tmp)
            _latest, dated = metrics.write_artifacts(payload, tmp, as_of="2026-09-01")
            self.assertTrue(dated.replace("\\", "/").endswith(
                "artifacts/2026-09/metrics_20260901.json"))
            json.dumps(payload, ensure_ascii=False)   # не должно бросать

    def test_tracks_without_closed_are_named(self):
        """§3.1 правил: дорожка без закрытых называется ПОИМЁННО."""
        tournament = {"tracks": {
            "A1": {"closed": [], "open": [{"sym": "A", "cg_id": "a",
                                           "entry_date": "2026-08-03", "entry_price": 1.0,
                                           "last_price": 1.0, "peak_price": 1.0, "miss": 0}]},
            "A7": {"closed": [], "open": []},
        }}
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(tmp, tournament=tournament)
            payload = metrics.build_metrics(tmp)
        self.assertIn("A1", payload["tracks_without_closed"])
        self.assertEqual(payload["n_open_total"], 1)


class TestBuildMetricsWithClosed(unittest.TestCase):

    def test_closed_trades_reach_the_artifact(self):
        """Появление закрытых сделок меняет status и наполняет числа."""
        closed = [{"sym": "S%d" % i, "cg_id": "s%d" % i, "entry_date": "2026-08-03",
                   "entry_price": 1.0, "last_price": 1.0 + i / 10.0, "peak_price": 2.0,
                   "miss": 0, "ret_pct": float(10 * i - 10), "peak_ret_pct": 50.0,
                   "close_date": "2026-10-0%d" % (i + 1), "close_reason": "60д"}
                  for i in range(1, 5)]
        tournament = {"tracks": {"A1": {"closed": closed, "open": []}}}
        log = [run_line("r1", "2026-08-03T10:00:00Z")]
        for i in range(1, 5):
            log.append(coin_line("r1", "2026-08-03T10:00:00Z", "s%d" % i, None,
                                 venues_n=2, vol=50_000_000.0, decision="enter"))
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(tmp, tournament=tournament, log_lines=log)
            payload = metrics.build_metrics(tmp)

        self.assertEqual(payload["status"], metrics.HAS_CLOSED_STATUS)
        self.assertEqual(payload["n_closed_total"], 4)
        node = payload["tracks"]["A1"]
        self.assertEqual(node["n_closed"], 4)
        self.assertIsNotNone(node["median_net"])
        # net строго хуже gross при положительных издержках
        self.assertLess(node["mean_net"], node["mean_gross"])
        # чувствительность: чем больше множитель издержек, тем хуже net
        rows = node["sensitivity"]
        nets = [r["mean_net"] for r in rows]
        self.assertEqual(nets, sorted(nets, reverse=True))


# =========================================================================
#  ДИФФ ДЛЯ АУДИТА
# =========================================================================

class TestDiffMetrics(unittest.TestCase):

    def _payload(self, tracks, n_closed_total, rules_sha="aaa", when="2026-09-01T00:00:00Z"):
        return {
            "provenance": {"git_sha": "deadbeef", "generated_utc": when,
                           "tool_version": "1.0.0", "python": "3.12.0"},
            "rules_sha256": rules_sha,
            "n_closed_total": n_closed_total,
            "n_open_total": 0,
            "n_trades_total": n_closed_total,
            "liq_class_fallback_n": 0,
            "tracks": tracks,
            "funnel": {"n_runs": 1, "runs": [{"run_id": "r1", "no_perp_share": 0.60}],
                       "delta_last_week_pp": None},
        }

    def test_no_previous_artifact(self):
        cur = self._payload({"A1": metrics.track_metrics([])}, 0)
        res = metrics.diff_metrics(None, cur)
        self.assertIn("нет", res["status"])
        self.assertEqual(res["tracks_appeared"], ["A1"])
        self.assertTrue(res["summary"])

    def test_first_closed_trades_show_as_appeared(self):
        prev = self._payload({"A1": metrics.track_metrics([])}, 0)
        cur = self._payload({"A1": metrics.track_metrics(closed_with_nets([3.0, -1.0]))}, 2,
                            when="2026-09-08T00:00:00Z")
        res = metrics.diff_metrics(prev, cur)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["totals"]["n_closed_total"]["delta"], 2)
        node = res["tracks"]["A1"]
        self.assertIn("median_net", node["appeared"])
        self.assertEqual(node["changed_fields"]["n_closed"]["delta"], 2)
        self.assertTrue(any("A1" in line for line in res["summary"]))

    def test_rules_hash_change_is_flagged_loudly(self):
        prev = self._payload({"A1": metrics.track_metrics([])}, 0, rules_sha="aaa")
        cur = self._payload({"A1": metrics.track_metrics([])}, 0, rules_sha="bbb")
        res = metrics.diff_metrics(prev, cur)
        self.assertTrue(res["rules_sha256_changed"])
        self.assertTrue(any("ПРАВИЛ" in line.upper() for line in res["summary"]))

    def test_track_disappeared(self):
        prev = self._payload({"A1": metrics.track_metrics([]),
                              "A9": metrics.track_metrics([])}, 0)
        cur = self._payload({"A1": metrics.track_metrics([])}, 0)
        res = metrics.diff_metrics(prev, cur)
        self.assertEqual(res["tracks_disappeared"], ["A9"])

    def test_survives_foreign_payload(self):
        """Чужой/обрезанный артефакт не должен ронять аудит."""
        res = metrics.diff_metrics({"нечто": 1}, {"иное": 2})
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["summary"])


# =========================================================================
#  CLI ЦЕЛИКОМ
# =========================================================================

class TestCli(unittest.TestCase):

    def test_main_writes_artifacts_and_returns_zero(self):
        """python metrics.py на пустом репозитории: код возврата 0, файлы есть."""
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(tmp)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = metrics.main([tmp])
            out = buf.getvalue()

            self.assertEqual(code, 0)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "artifacts", "metrics_latest.json")))
        self.assertIn(metrics.NO_CLOSED_STATUS, out)

    def test_previous_dated_artifact_is_found_and_diffed(self):
        """Дифф считается против ПРЕДЫДУЩЕГО датированного файла, не против latest."""
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(tmp)
            old_dir = os.path.join(tmp, "artifacts", "2026-08")
            os.makedirs(old_dir)
            old = {
                "provenance": {"git_sha": "old123", "generated_utc": "2026-08-25T00:00:00Z",
                               "tool_version": "1.0.0", "python": "3.12.0"},
                "rules_sha256": "старый-хеш",
                "n_closed_total": 0,
                "tracks": {"A1": metrics.track_metrics([])},
                "funnel": {"n_runs": 0, "runs": [], "delta_last_week_pp": None},
            }
            with open(os.path.join(old_dir, "metrics_20260825.json"), "w", encoding="utf-8") as fh:
                json.dump(old, fh, ensure_ascii=False)

            payload = metrics.build_metrics(tmp)
            _latest, dated = metrics.write_artifacts(payload, tmp)
            prev, prev_path = metrics.load_previous_artifact(tmp, exclude_path=dated)

            self.assertIsNotNone(prev)
            self.assertTrue(prev_path.endswith("metrics_20260825.json"))
            res = metrics.diff_metrics(prev, payload)
            self.assertEqual(res["status"], "ok")
            self.assertTrue(res["rules_sha256_changed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

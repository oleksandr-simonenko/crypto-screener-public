#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Селфтест §7 замороженных правил — теневые стопы (verdict_1511.paragraph7).

Отдельный файл, а не дополнение к test_verdict.py: §7 считается ОТДЕЛЬНЫМ
счётом и на правила турнира сит не влияет — пусть и тест живёт отдельно.

Что проверяем:
  * пустой вход (закрытых нет — сегодняшнее состояние) не падает и честно
    говорит, что §7 не считается;
  * знаковый тест точен — сверен с руками посчитанной биномиальной вероятностью;
  * издержки применяются к ОБЕИМ веткам одинаково (иначе сравнение подтасовано);
  * когда стоп заведомо лучше — это видно; когда заведомо хуже — тоже;
  * ничьи выбрасываются из знакового теста, а не считаются за победу.
"""

import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import costs                       # noqa: E402
import verdict_1511 as V           # noqa: E402


def _write_positions(dirpath, closed):
    path = os.path.join(dirpath, "positions.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"open": [], "closed": closed, "meta": {}}, f, ensure_ascii=False)
    return path


def _pos(sym, passive, t20, t30=None, entry="2026-07-14", age=60):
    p = {"sym": sym, "entry_date": entry, "ret_pct": passive,
         "shadow_t20_ret": t20, "age_days": age}
    if t30 is not None:
        p["shadow_t30_ret"] = t30
    return p


class TestSignTest(unittest.TestCase):
    """Точный двусторонний знаковый тест — сверка с биномиальной формулой."""

    def test_no_pairs(self):
        self.assertEqual(V._sign_test_two_sided(0, 0), (0, None))

    def test_all_better(self):
        n, p = V._sign_test_two_sided(8, 0)
        self.assertEqual(n, 8)
        # 2 * P(X = 0) при n=8 и p=0.5 = 2 / 256
        self.assertAlmostEqual(p, 2.0 / 256.0, places=12)

    def test_symmetric(self):
        # знаковый тест симметричен: 8-0 и 0-8 дают одно p
        self.assertAlmostEqual(V._sign_test_two_sided(8, 0)[1],
                               V._sign_test_two_sided(0, 8)[1], places=15)

    def test_matches_binomial_by_hand(self):
        # n=10, k=2: 2 * sum(C(10,i)) / 2^10 для i = 0..2
        expected = 2.0 * (math.comb(10, 0) + math.comb(10, 1) + math.comb(10, 2)) / 1024.0
        self.assertAlmostEqual(V._sign_test_two_sided(8, 2)[1], expected, places=12)

    def test_p_never_exceeds_one(self):
        # Ровный счёт даёт p, которое формула 2*tail могла бы загнать выше 1.
        # Начинаем с 1: пара (0, 0) — это «сравнивать нечего», у неё p = None,
        # и она проверяется отдельным тестом test_no_pairs.
        for a in range(1, 9):
            n, p = V._sign_test_two_sided(a, a)
            self.assertLessEqual(p, 1.0)
            self.assertGreater(p, 0.0)


class TestParagraph7(unittest.TestCase):

    def setUp(self):
        self.cfg = costs.load_costs()

    def test_empty_is_honest_not_crash(self):
        """Сегодняшнее боевое состояние: закрытых нет. Обязан быть честный отказ."""
        with tempfile.TemporaryDirectory() as d:
            _write_positions(d, [])
            out = V.paragraph7([], self.cfg, mult=1.0, repo_dir=d)
        self.assertFalse(out["available"])
        self.assertEqual(out["n"], 0)
        self.assertIsNotNone(out["note"])
        self.assertIsNone(out["verdict_line"])

    def test_missing_file_is_honest(self):
        with tempfile.TemporaryDirectory() as d:
            out = V.paragraph7([], self.cfg, mult=1.0, repo_dir=d)
        self.assertFalse(out["available"])
        self.assertIn("недоступен", out["note"])

    def test_positions_without_shadow_are_skipped(self):
        """Позиция без теневых полей в §7 не участвует — и это не падение."""
        with tempfile.TemporaryDirectory() as d:
            _write_positions(d, [{"sym": "AAA", "entry_date": "2026-07-14",
                                  "ret_pct": -30.0, "age_days": 60}])
            out = V.paragraph7([], self.cfg, mult=1.0, repo_dir=d)
        self.assertFalse(out["available"])

    def test_stop_clearly_better_is_detected(self):
        """Стоп спас во всех сделках — знаковый тест обязан это увидеть."""
        closed = [_pos("C%d" % i, -50.0, -20.0) for i in range(10)]
        with tempfile.TemporaryDirectory() as d:
            _write_positions(d, closed)
            out = V.paragraph7([], self.cfg, mult=1.0, repo_dir=d)
        self.assertTrue(out["available"])
        self.assertEqual(out["n"], 10)
        d20 = out["paired_vs_passive"]["stop20"]
        self.assertEqual(d20["n_better"], 10)
        self.assertEqual(d20["n_worse"], 0)
        self.assertTrue(d20["significant_at_005"])
        self.assertGreater(d20["median_diff_pp"], 0)
        self.assertIn("лучше", out["verdict_line"])

    def test_stop_clearly_worse_is_detected(self):
        """Стоп срезал прибыль во всех сделках — это тоже обязано быть видно."""
        closed = [_pos("C%d" % i, 120.0, -20.0) for i in range(10)]
        with tempfile.TemporaryDirectory() as d:
            _write_positions(d, closed)
            out = V.paragraph7([], self.cfg, mult=1.0, repo_dir=d)
        d20 = out["paired_vs_passive"]["stop20"]
        self.assertEqual(d20["n_worse"], 10)
        self.assertTrue(d20["significant_at_005"])
        self.assertIn("ХУЖЕ", out["verdict_line"])

    def test_ties_are_excluded_not_counted_as_wins(self):
        """Несработавший стоп несёт результат пассива — это НИЧЬЯ.

        Если бы ничьи считались победами, правило «стоп лучше» подтверждалось бы
        само собой: у большинства позиций стоп просто не срабатывает.
        """
        closed = ([_pos("T%d" % i, 10.0, 10.0) for i in range(20)]     # 20 ничьих
                  + [_pos("W%d" % i, -50.0, -20.0) for i in range(3)])  # 3 победы стопа
        with tempfile.TemporaryDirectory() as d:
            _write_positions(d, closed)
            out = V.paragraph7([], self.cfg, mult=1.0, repo_dir=d)
        d20 = out["paired_vs_passive"]["stop20"]
        self.assertEqual(d20["n_pairs"], 23)
        self.assertEqual(d20["n_equal"], 20)
        self.assertEqual(d20["sign_test_n"], 3, "ничьи обязаны выпадать из знакового теста")

    def test_costs_applied_to_both_branches_equally(self):
        """Одинаковый брутто у пассива и стопа обязан давать одинаковый нетто.

        Это защита от подтасовки: если издержки применить только к одной ветке,
        сравнение правил выхода превратится в сравнение моделей издержек.
        """
        closed = [_pos("EQ", 42.0, 42.0)]
        with tempfile.TemporaryDirectory() as d:
            _write_positions(d, closed)
            out = V.paragraph7([], self.cfg, mult=1.0, repo_dir=d)
        self.assertEqual(out["rules"]["passive"]["mean_net_pct"],
                         out["rules"]["stop20"]["mean_net_pct"])
        self.assertEqual(out["paired_vs_passive"]["stop20"]["mean_diff_pp"], 0.0)

    def test_net_is_worse_than_gross(self):
        """Издержки обязаны ухудшать результат, а не улучшать."""
        closed = [_pos("G", 100.0, 100.0)]
        with tempfile.TemporaryDirectory() as d:
            _write_positions(d, closed)
            out = V.paragraph7([], self.cfg, mult=1.0, repo_dir=d)
        self.assertLess(out["rules"]["passive"]["mean_net_pct"], 100.0)

    def test_liq_class_joins_from_loaded_trades(self):
        """Класс ликвидности подхватывается из загруженных сделок по (тикер, дата).

        Дешёвый класс обязан давать нетто ЛУЧШЕ, чем отсутствие джойна
        (отсутствие = самый дорогой класс по принципу «не знаешь — считай по худшему»).
        """
        closed_pos = [_pos("JOIN", 50.0, 50.0)]
        trades_deep = [{"track": "A0", "sym": "JOIN",
                        "entry_date": "2026-07-14", "liq_class": "deep"}]
        with tempfile.TemporaryDirectory() as d:
            _write_positions(d, closed_pos)
            with_join = V.paragraph7(trades_deep, self.cfg, mult=1.0, repo_dir=d)
            without = V.paragraph7([], self.cfg, mult=1.0, repo_dir=d)
        self.assertGreater(with_join["rules"]["passive"]["mean_net_pct"],
                           without["rules"]["passive"]["mean_net_pct"])

    def test_median_is_published_next_to_mean(self):
        """Пакет A2: среднее не публикуется без медианы рядом."""
        closed = [_pos("A", -50.0, -20.0), _pos("B", 10.0, 10.0), _pos("C", 300.0, 300.0)]
        with tempfile.TemporaryDirectory() as d:
            _write_positions(d, closed)
            out = V.paragraph7([], self.cfg, mult=1.0, repo_dir=d)
        for name in ("passive", "stop20"):
            self.assertIn("mean_net_pct", out["rules"][name])
            self.assertIn("median_net_pct", out["rules"][name])
            self.assertIsNotNone(out["rules"][name]["median_net_pct"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

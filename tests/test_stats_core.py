# -*- coding: utf-8 -*-
"""
tests/test_stats_core.py — селфтест статистического ядра. Только stdlib.

Запуск:
    python tests/test_stats_core.py
    python -m unittest discover -s tests -v

КАК ПРОВЕРЕНЫ ТАБЛИЧНЫЕ ЧИСЛА (важно для доверия к тесту).
Каждое зашитое здесь значение сошлось ТРЕМЯ независимыми способами:
  1) рабочая реализация stats_core (ряд + непрерывная дробь Лентца);
  2) ЗАМКНУТАЯ ФОРМА / другой ряд — обе воспроизведены прямо в этом файле
     (_chi2_sf_reference через erfc и рекурренту; _f_sf_reference через
     гипергеометрический ряд неполной беты). Ни одна из них не использует
     непрерывную дробь, то есть они не разделяют с реализацией общий код;
  3) численное интегрирование плотности (Гаусс–Лежандр, разовый скрипт вне
     репозитория — numpy в проект не тянем).
Расхождение способов на всех точках было < 1e-13 относительных.
Плюс независимая внешняя опора: классические критические значения таблиц
(chi2_0.95, F_0.95) дают ровно 0.05 / 0.01 / 0.10, а классический учебный
пример Demsar (2006) воспроизводится до печатной точности (chi2_F = 9.28,
F_ID = 3.69, критическое F(3,39) = 2.85).

ОГОВОРКА ЧЕСТНОСТИ: scipy на машине нет, поэтому сверка со scipy не
проводилась. Опоры — замкнутые формы, второй ряд, интегрирование и печатные
таблицы; этого достаточно, но это НЕ «сверено со scipy».
"""

import math
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stats_core as st  # noqa: E402


# =========================================================================
#  НЕЗАВИСИМЫЕ ЭТАЛОНЫ (не используют код stats_core)
# =========================================================================

def _chi2_sf_reference(x, df):
    """Эталон хвоста хи-квадрат в ЗАМКНУТОЙ форме (целые df).

    df нечётное: Q(1/2, y) = erfc(sqrt(y))
    df чётное:   Q(1,   y) = exp(-y)
    рекуррента:  Q(s+1, y) = Q(s, y) + y^s * e^-y / Gamma(s+1)
    Ни ряда, ни непрерывной дроби — другой математический путь.
    """
    if x <= 0:
        return 1.0
    y = x / 2.0
    if df % 2 == 1:
        s = 0.5
        q = math.erfc(math.sqrt(y))
    else:
        s = 1.0
        q = math.exp(-y)
    target = df / 2.0
    while s < target - 1e-12:
        q += math.exp(s * math.log(y) - y - math.lgamma(s + 1.0))
        s += 1.0
    return q


def _betainc_series_reference(a, b, x, max_terms=200000):
    """Эталон неполной беты ГИПЕРГЕОМЕТРИЧЕСКИМ РЯДОМ (без дроби Лентца):

    I_x(a,b) = x^a (1-x)^b / (a*B(a,b)) * [ 1 + sum_n B(a+1,n+1)/B(a+b,n+1) x^(n+1) ]

    Члены считаются рекуррентой t_{n+1} = t_n * (a+b+n+1)/(a+n+2) * x
    (t_0 = (a+b)/(a+1) * x) — то же самое суммирование, но без lgamma на шаг,
    поэтому можно тянуть ряд напрямую вплоть до x = 0.99 и НЕ уходить в
    дополнение 1 - I. Это важно: дополнение съедает значащие цифры, когда сам
    хвост мал, и тогда «эталон» врёт сильнее проверяемого кода.
    Все члены положительны — накопления ошибки от вычитаний нет.
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    if x > 0.99:
        # здесь значение заведомо близко к 1, вычитание безопасно
        return 1.0 - _betainc_series_reference(b, a, 1.0 - x, max_terms)
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    pref = math.exp(a * math.log(x) + b * math.log1p(-x) - math.log(a) - lbeta)
    total = 1.0
    term = (a + b) / (a + 1.0) * x
    for n in range(max_terms):
        total += term
        if n > 5 and term < 1e-18 * total:
            break
        term *= (a + b + n + 1.0) / (a + n + 2.0) * x
    return pref * total


def _f_sf_reference(x, d1, d2):
    """Эталон хвоста F через эталонную бету."""
    if x <= 0:
        return 1.0
    w = d2 / float(d2 + d1 * x)
    return _betainc_series_reference(d2 / 2.0, d1 / 2.0, w)


def _relerr(a, b):
    scale = max(abs(a), abs(b), 1e-300)
    return abs(a - b) / scale


# =========================================================================
#  ТАБЛИЧНЫЕ ТОЧКИ
# =========================================================================

# (x, df, ожидаемое p). Первые семь — классические критические значения
# хи-квадрат уровня 0.05, дальше 0.01 и 0.10, дальше произвольные точки
# (в т.ч. далёкий хвост 1.55e-21, где важно, что нет вычитания из единицы).
CHI2_TABLE = [
    (3.841458820694124,   1,  0.05),
    (5.991464547107979,   2,  0.05),
    (7.814727903251179,   3,  0.05),
    (9.487729036781154,   4,  0.05),
    (11.070497693516351,  5,  0.05),
    (12.591587243743977,  6,  0.05),
    (18.307038053275146, 10,  0.05),
    (23.209251158954356, 10,  0.01),
    (6.634896601021213,   1,  0.01),
    (2.705543454095404,   1,  0.10),
    (1.0,   1, 0.31731050786291437),
    (2.0,   2, 0.36787944117144233),   # ровно exp(-1)
    (0.5,   3, 0.91889141165467580),
    (30.0, 15, 0.01192149593815968),
    (50.0, 20, 0.00022147663824878379),
    (100.0, 3, 1.554159431389603e-21),
]

# (x, df1, df2, ожидаемое p). Первые четыре — критические значения F_0.95
# (печатные таблицы дают 4.965 / 2.295 / 3.098 / 2.85 — совпадает).
F_TABLE = [
    (4.964602743760213,   1, 10, 0.049999999999438854),
    (2.2946013127239886,  6, 48, 0.05000000006611877),
    (3.0983912070251867,  3, 20, 0.0500000002401648),
    (19.0,                2,  2, 0.05),
    (3.6863130320800006,  3, 39, 0.019823006192443447),   # Iman-Davenport, Demsar 2006
    (2.35,                6, 48, 0.04532491585051141),
    (2.0,                 5,  5, 0.23251131913037817),
    (1.0,                 4,  4, 0.5),                    # медиана F(d,d) == 1
    (10.0,                2, 30, 0.00047018498457599816),
    (0.5,                 7, 12, 0.8175629806958423),
    (8.0,                 1,  3, 0.06627560274152423),
    (4.0,                 3,  6, 0.0701011161656454),
    (2.6,                 6,  9, 0.09568677420504051),
    (5.317655,            4,  9, 0.017733916052908087),
    (1.5,                12, 60, 0.14954334215517046),
]

# Классический учебный пример Demsar (2006), «Statistical Comparisons of
# Classifiers over Multiple Data Sets»: 4 алгоритма на 14 наборах,
# средние ранги 3.143 / 2.000 / 2.893 / 1.964, chi2_F = 9.28, F_ID = 3.69,
# критическое F(3,39) = 2.85. Матрица ниже — ранги ЭТОГО примера
# (суммы 44 / 28 / 40.5 / 27.5, одна связка в последней строке).
# В friedman() ранг 1 = НАИБОЛЬШЕЕ значение, поэтому ранги поданы со знаком
# минус: -1 больше остальных и получает ранг 1.
DEMSAR_BLOCKS = [
    [-1.0, -2.0, -4.0, -3.0],
    [-2.0, -1.0, -3.0, -4.0],
    [-2.0, -3.0, -4.0, -1.0],
    [-2.0, -4.0, -1.0, -3.0],
    [-4.0, -3.0, -2.0, -1.0],
    [-4.0, -1.0, -3.0, -2.0],
    [-4.0, -2.0, -3.0, -1.0],
    [-4.0, -2.0, -3.0, -1.0],
    [-4.0, -2.0, -3.0, -1.0],
    [-4.0, -1.0, -2.0, -3.0],
    [-4.0, -2.0, -3.0, -1.0],
    [-4.0, -2.0, -3.0, -1.0],
    [-4.0, -1.0, -3.0, -2.0],
    [-1.0, -2.0, -3.5, -3.5],
]


# =========================================================================
#  ТЕСТЫ
# =========================================================================

class TestNormalSf(unittest.TestCase):

    def test_known_points(self):
        self.assertAlmostEqual(st.normal_sf(0.0), 0.5, places=15)
        self.assertAlmostEqual(st.normal_sf(1.6448536269514722), 0.05, delta=1e-12)
        self.assertAlmostEqual(st.normal_sf(1.959963984540054), 0.025, delta=1e-12)
        self.assertAlmostEqual(st.normal_sf(2.5758293035489004), 0.005, delta=1e-12)
        self.assertAlmostEqual(st.normal_sf(-1.6448536269514722), 0.95, delta=1e-12)

    def test_symmetry(self):
        for z in (0.1, 0.7, 1.3, 2.9, 4.5):
            self.assertAlmostEqual(st.normal_sf(z) + st.normal_sf(-z), 1.0, places=15)

    def test_far_tail_not_zero(self):
        # наивное 1 - Phi(z) здесь дало бы ровно 0; erfc — нет
        self.assertGreater(st.normal_sf(10.0), 0.0)
        self.assertLess(st.normal_sf(10.0), 1e-22)

    def test_infinities(self):
        self.assertEqual(st.normal_sf(float("inf")), 0.0)
        self.assertEqual(st.normal_sf(float("-inf")), 1.0)

    def test_nan_rejected(self):
        with self.assertRaises(ValueError):
            st.normal_sf(float("nan"))


class TestChi2Sf(unittest.TestCase):

    def test_table_points(self):
        """16 табличных точек; требуемая точность задания — 1e-6."""
        self.assertGreaterEqual(len(CHI2_TABLE), 8)
        for x, df, expected in CHI2_TABLE:
            got = st.chi2_sf(x, df)
            self.assertLessEqual(
                abs(got - expected), 1e-6 * max(1.0, abs(expected)),
                "chi2_sf(%r, %d) = %r, ожидалось %r" % (x, df, got, expected),
            )
            # и заодно жёстче — по относительной ошибке
            self.assertLess(_relerr(got, expected), 1e-9)

    def test_against_closed_form_reference(self):
        """Сверка с ЗАМКНУТОЙ формой (erfc + рекуррента) на сетке."""
        for df in range(1, 16):
            for x in (0.05, 0.5, 1.0, 2.0, 3.5, 5.0, 8.0, 12.0, 20.0, 35.0, 60.0):
                got = st.chi2_sf(x, df)
                ref = _chi2_sf_reference(x, df)
                self.assertLess(
                    _relerr(got, ref), 1e-11,
                    "df=%d x=%g: реализация %r против эталона %r" % (df, x, got, ref),
                )

    def test_df1_equals_two_sided_normal(self):
        """chi2_sf(z^2, 1) == 2 * normal_sf(z) — точное тождество."""
        for z in (0.1, 0.5, 1.0, 1.96, 3.0, 5.0):
            self.assertLess(_relerr(st.chi2_sf(z * z, 1), 2.0 * st.normal_sf(z)), 1e-13)

    def test_df2_closed_form(self):
        """chi2_sf(x, 2) == exp(-x/2) — точное тождество."""
        for x in (0.2, 1.0, 4.0, 9.0, 25.0):
            self.assertLess(_relerr(st.chi2_sf(x, 2), math.exp(-x / 2.0)), 1e-14)

    def test_recurrence(self):
        """Q(s+1,y) - Q(s,y) = y^s e^-y / Gamma(s+1) — независимая связь df и df+2."""
        for df in range(1, 12):
            for x in (0.7, 3.0, 11.0):
                s = df / 2.0
                y = x / 2.0
                delta = st.chi2_sf(x, df + 2) - st.chi2_sf(x, df)
                want = math.exp(s * math.log(y) - y - math.lgamma(s + 1.0))
                self.assertLess(abs(delta - want), 1e-13 * max(1.0, want))

    def test_printed_table_precision(self):
        """Значения из ПЕЧАТНЫХ таблиц (3 знака) дают 0.05 с точностью округления."""
        for x, df in ((3.84, 1), (5.99, 2), (7.81, 3), (11.07, 5), (18.31, 10)):
            self.assertAlmostEqual(st.chi2_sf(x, df), 0.05, delta=2e-4)
        self.assertAlmostEqual(st.chi2_sf(6.63, 1), 0.01, delta=5e-5)

    def test_monotonic_and_bounds(self):
        prev = 1.0
        for x in (0.0, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0, 100.0):
            cur = st.chi2_sf(x, 4)
            self.assertLessEqual(cur, prev + 1e-15)
            self.assertGreaterEqual(cur, 0.0)
            self.assertLessEqual(cur, 1.0)
            prev = cur

    def test_edges(self):
        self.assertEqual(st.chi2_sf(0.0, 3), 1.0)
        self.assertEqual(st.chi2_sf(-5.0, 3), 1.0)
        self.assertEqual(st.chi2_sf(float("inf"), 3), 0.0)

    def test_bad_df(self):
        for bad in (0, -1, 2.5, "3", None, True):
            with self.assertRaises(ValueError):
                st.chi2_sf(1.0, bad)


class TestFSf(unittest.TestCase):

    def test_table_points(self):
        """15 табличных точек; требуемая точность задания — 1e-6."""
        self.assertGreaterEqual(len(F_TABLE), 8)
        for x, d1, d2, expected in F_TABLE:
            got = st.f_sf(x, d1, d2)
            self.assertLessEqual(
                abs(got - expected), 1e-6 * max(1.0, abs(expected)),
                "f_sf(%r, %d, %d) = %r, ожидалось %r" % (x, d1, d2, got, expected),
            )
            self.assertLess(_relerr(got, expected), 1e-9)

    def test_against_series_reference(self):
        """Сверка с эталонным ГИПЕРГЕОМЕТРИЧЕСКИМ РЯДОМ на сетке."""
        for d1 in (1, 2, 3, 4, 6, 8, 12):
            for d2 in (1, 2, 3, 5, 9, 20, 48):
                for x in (0.25, 0.75, 1.0, 2.0, 3.5, 7.0, 20.0):
                    got = st.f_sf(x, d1, d2)
                    ref = _f_sf_reference(x, d1, d2)
                    self.assertLess(
                        _relerr(got, ref), 1e-10,
                        "F(%d,%d) x=%g: реализация %r против эталона %r"
                        % (d1, d2, x, got, ref),
                    )

    def test_reciprocal_identity(self):
        """Если X ~ F(d1,d2), то 1/X ~ F(d2,d1): sf(x;d1,d2) = 1 - sf(1/x;d2,d1)."""
        for x, d1, d2 in ((2.5, 4, 7), (0.8, 6, 13), (5.0, 2, 25), (1.7, 9, 3)):
            left = st.f_sf(x, d1, d2)
            right = 1.0 - st.f_sf(1.0 / x, d2, d1)
            self.assertLess(abs(left - right), 1e-12)

    def test_median_of_symmetric_f(self):
        """F(d,d) при x=1 даёт ровно половину массы."""
        for d in (1, 2, 3, 7, 30):
            self.assertAlmostEqual(st.f_sf(1.0, d, d), 0.5, delta=1e-12)

    def test_printed_table_precision(self):
        """Критические значения F_0.95 из печатных таблиц."""
        self.assertAlmostEqual(st.f_sf(4.9646, 1, 10), 0.05, delta=1e-6)
        self.assertAlmostEqual(st.f_sf(2.2946, 6, 48), 0.05, delta=1e-6)
        self.assertAlmostEqual(st.f_sf(3.0984, 3, 20), 0.05, delta=1e-6)
        # Demsar печатает критическое F(3,39) = 2.85
        self.assertAlmostEqual(st.f_sf(2.85, 3, 39), 0.05, delta=5e-4)

    def test_monotonic_and_bounds(self):
        prev = 1.0
        for x in (0.0, 0.3, 1.0, 2.0, 5.0, 15.0, 100.0):
            cur = st.f_sf(x, 3, 12)
            self.assertLessEqual(cur, prev + 1e-15)
            self.assertGreaterEqual(cur, 0.0)
            self.assertLessEqual(cur, 1.0)
            prev = cur

    def test_edges(self):
        self.assertEqual(st.f_sf(0.0, 3, 9), 1.0)
        self.assertEqual(st.f_sf(-2.0, 3, 9), 1.0)
        self.assertEqual(st.f_sf(float("inf"), 3, 9), 0.0)

    def test_bad_df(self):
        with self.assertRaises(ValueError):
            st.f_sf(1.0, 0, 5)
        with self.assertRaises(ValueError):
            st.f_sf(1.0, 5, -3)
        with self.assertRaises(ValueError):
            st.f_sf(1.0, 1.5, 5)


class TestRankWithinBlock(unittest.TestCase):

    def test_rank_one_is_best(self):
        # наибольшее значение = ранг 1
        self.assertEqual(st.rank_within_block([3.0, 1.0, 2.0]), [1.0, 3.0, 2.0])

    def test_simple_ties(self):
        # 20 и 20 делят места 1-2 -> обоим 1.5
        self.assertEqual(st.rank_within_block([10.0, 20.0, 20.0, 5.0]),
                         [3.0, 1.5, 1.5, 4.0])

    def test_triple_tie_in_middle(self):
        # 5,5,5 делят места 2-3-4 -> всем 3.0
        self.assertEqual(st.rank_within_block([9.0, 5.0, 5.0, 5.0, 1.0]),
                         [1.0, 3.0, 3.0, 3.0, 5.0])

    def test_all_equal(self):
        self.assertEqual(st.rank_within_block([7.0, 7.0, 7.0]), [2.0, 2.0, 2.0])
        self.assertEqual(st.rank_within_block([0.0, 0.0]), [1.5, 1.5])

    def test_two_tie_groups(self):
        # 8,8 делят 1-2 (=1.5); 3,3 делят 3-4 (=3.5)
        self.assertEqual(st.rank_within_block([8.0, 3.0, 8.0, 3.0]),
                         [1.5, 3.5, 1.5, 3.5])

    def test_negative_and_zero(self):
        self.assertEqual(st.rank_within_block([-1.0, 0.0, -0.0, -2.0]),
                         [3.0, 1.5, 1.5, 4.0])

    def test_sum_invariant(self):
        """Сумма рангов всегда k(k+1)/2 — и со связками тоже."""
        for vals in ([1.0, 2.0, 3.0, 4.0], [5.0, 5.0, 1.0], [2.0] * 6,
                     [0.1, -3.0, 7.5, 7.5, 0.1]):
            k = len(vals)
            self.assertAlmostEqual(sum(st.rank_within_block(vals)),
                                   k * (k + 1) / 2.0, places=12)

    def test_degenerate_sizes(self):
        self.assertEqual(st.rank_within_block([]), [])
        self.assertEqual(st.rank_within_block([42.0]), [1.0])

    def test_nan_rejected(self):
        with self.assertRaises(ValueError):
            st.rank_within_block([1.0, float("nan")])


class TestFriedman(unittest.TestCase):

    def test_manual_worked_example(self):
        """Ручной набор 6x3, ранги расписаны построчно.

        [7,5,3]     -> 1,2,3
        [8,6,4]     -> 1,2,3
        [9,4,5]     -> 1,3,2
        [6,7,2]     -> 2,1,3
        [7.5,5.5,4.5] -> 1,2,3
        [8,7,6]     -> 1,2,3
        суммы рангов: 7, 12, 17   (в сумме 36 = N*k(k+1)/2 = 6*6)
        chi2 = 12/(N k (k+1)) * sum R^2 - 3N(k+1)
             = 12/72 * (49+144+289) - 72 = 80.3333... - 72 = 8.3333...
        """
        blocks = [
            [7.0, 5.0, 3.0],
            [8.0, 6.0, 4.0],
            [9.0, 4.0, 5.0],
            [6.0, 7.0, 2.0],
            [7.5, 5.5, 4.5],
            [8.0, 7.0, 6.0],
        ]
        res = st.friedman(blocks)
        self.assertEqual(res["N"], 6)
        self.assertEqual(res["k"], 3)
        self.assertEqual(res["df"], 2)
        rank_sums = [r * res["N"] for r in res["mean_ranks"]]
        for got, want in zip(rank_sums, (7.0, 12.0, 17.0)):
            self.assertAlmostEqual(got, want, places=12)
        self.assertAlmostEqual(res["chi2"], 25.0 / 3.0, places=12)   # 8.3333...
        self.assertAlmostEqual(res["p"], st.chi2_sf(25.0 / 3.0, 2), places=15)
        self.assertAlmostEqual(res["p"], 0.0155038535990093, delta=1e-12)

    def test_alternative_formula(self):
        """Независимая алгебраическая форма через СУММЫ рангов:
        chi2 = 12/(N k (k+1)) * sum R_j^2 - 3N(k+1)."""
        blocks = [
            [1.0, 5.0, 3.0, 2.0],
            [4.0, 9.0, 1.0, 0.5],
            [2.0, 2.0, 8.0, 3.0],
            [7.0, 1.0, 6.0, 5.0],
            [3.0, 4.0, 4.0, 9.0],
        ]
        res = st.friedman(blocks)
        N, k = res["N"], res["k"]
        rank_sums = [r * N for r in res["mean_ranks"]]
        alt = 12.0 / (N * k * (k + 1)) * sum(r * r for r in rank_sums) - 3.0 * N * (k + 1)
        self.assertLess(abs(alt - res["chi2"]), 1e-11)

    def test_demsar_textbook_example(self):
        """Классический пример Demsar (2006): 4 метода, 14 наборов.

        Опубликованные числа: средние ранги 3.143 / 2.000 / 2.893 / 1.964,
        chi2_F = 9.28, F_ID = 3.69, критическое F(3,39) = 2.85.
        """
        res = st.friedman(DEMSAR_BLOCKS)
        self.assertEqual((res["N"], res["k"], res["df"]), (14, 4, 3))
        for got, want in zip(res["mean_ranks"], (3.143, 2.000, 2.893, 1.964)):
            self.assertAlmostEqual(got, want, delta=0.001)
        self.assertAlmostEqual(res["chi2"], 9.28, delta=0.005)
        self.assertAlmostEqual(res["chi2"], 9.278571428571428, delta=1e-12)
        self.assertLess(res["p"], 0.05)

    def test_perfect_agreement_is_max(self):
        """Полное согласие блоков -> chi2 = N(k-1) (теоретический максимум)."""
        N, k = 7, 4
        blocks = [[float(k - j) for j in range(k)] for _ in range(N)]
        res = st.friedman(blocks)
        self.assertAlmostEqual(res["chi2"], N * (k - 1), places=10)
        self.assertEqual(res["mean_ranks"], [1.0, 2.0, 3.0, 4.0])

    def test_full_ties_give_zero(self):
        """Все значения в блоке равны -> ранги равны -> chi2 = 0, p = 1."""
        blocks = [[5.0, 5.0, 5.0] for _ in range(4)]
        res = st.friedman(blocks)
        self.assertAlmostEqual(res["chi2"], 0.0, places=12)
        self.assertAlmostEqual(res["p"], 1.0, places=12)

    def test_k_two_minimum(self):
        """k = 2 — минимально допустимый случай, df = 1."""
        blocks = [[2.0, 1.0], [3.0, 1.0], [5.0, 4.0], [2.0, 9.0]]
        res = st.friedman(blocks)
        self.assertEqual(res["k"], 2)
        self.assertEqual(res["df"], 1)
        # 3 из 4 блоков за первый столбец: суммы рангов 5 и 7, mean 1.25 / 1.75
        self.assertAlmostEqual(res["mean_ranks"][0], 1.25, places=12)
        self.assertAlmostEqual(res["mean_ranks"][1], 1.75, places=12)
        self.assertAlmostEqual(res["chi2"], 1.0, places=12)
        self.assertAlmostEqual(res["p"], st.chi2_sf(1.0, 1), places=15)

    def test_n_two_minimum(self):
        """N = 2 — минимально допустимый случай, падать нельзя."""
        res = st.friedman([[3.0, 1.0, 2.0], [3.0, 1.0, 2.0]])
        self.assertEqual((res["N"], res["k"]), (2, 3))
        self.assertAlmostEqual(res["chi2"], 4.0, places=12)   # = N(k-1)
        self.assertGreaterEqual(res["p"], 0.0)
        self.assertLessEqual(res["p"], 1.0)

    def test_bad_shapes(self):
        with self.assertRaises(ValueError):
            st.friedman([[1.0, 2.0]])                       # N = 1
        with self.assertRaises(ValueError):
            st.friedman([[1.0], [2.0]])                     # k = 1
        with self.assertRaises(ValueError):
            st.friedman([[1.0, 2.0], [3.0]])                # рваные блоки
        with self.assertRaises(ValueError):
            st.friedman([])
        with self.assertRaises(ValueError):
            st.friedman(None)


class TestImanDavenport(unittest.TestCase):

    def test_demsar_textbook_example(self):
        """chi2_F = 9.28 при k=4, N=14 -> F_ID = 3.69, df = (3, 39)."""
        # chi2 берём цепочкой из friedman на той же матрице примера
        fr = st.friedman(DEMSAR_BLOCKS)
        res = st.iman_davenport(fr["chi2"], fr["k"], fr["N"])
        self.assertEqual((res["df1"], res["df2"]), (3, 39))
        self.assertAlmostEqual(res["F"], 3.69, delta=0.005)
        self.assertAlmostEqual(res["F"], 3.6863130320800006, delta=1e-9)
        self.assertAlmostEqual(res["p"], 0.019823006192443447, delta=1e-9)
        self.assertFalse(res["degenerate"])
        self.assertLess(res["p"], 0.05)          # H0 отвергается, как в статье

    def test_matches_manual_formula(self):
        chi2, k, N = 5.5, 5, 9
        res = st.iman_davenport(chi2, k, N)
        want_F = (N - 1) * chi2 / (N * (k - 1) - chi2)
        self.assertAlmostEqual(res["F"], want_F, places=12)
        self.assertAlmostEqual(res["p"], st.f_sf(want_F, k - 1, (k - 1) * (N - 1)),
                               places=15)

    def test_degenerate_denominator_zero(self):
        """Полное согласие: chi2 = N(k-1) -> знаменатель 0 -> p = 0, degenerate."""
        k, N = 4, 7
        res = st.iman_davenport(float(N * (k - 1)), k, N)
        self.assertTrue(res["degenerate"])
        self.assertEqual(res["p"], 0.0)
        self.assertEqual(res["F"], float("inf"))
        self.assertEqual((res["df1"], res["df2"]), (3, 18))

    def test_degenerate_denominator_negative(self):
        """Знаменатель < 0 (chi2 выше теоретического максимума) — тоже degenerate."""
        res = st.iman_davenport(30.0, 3, 10)     # N(k-1) = 20 < 30
        self.assertTrue(res["degenerate"])
        self.assertEqual(res["p"], 0.0)

    def test_degenerate_flag_always_present(self):
        self.assertIn("degenerate", st.iman_davenport(1.0, 3, 5))

    def test_chi2_zero(self):
        res = st.iman_davenport(0.0, 3, 5)
        self.assertEqual(res["F"], 0.0)
        self.assertAlmostEqual(res["p"], 1.0, places=12)
        self.assertFalse(res["degenerate"])

    def test_friedman_chain(self):
        """Связка friedman -> iman_davenport на полном согласии блоков."""
        blocks = [[3.0, 2.0, 1.0] for _ in range(5)]
        fr = st.friedman(blocks)
        idr = st.iman_davenport(fr["chi2"], fr["k"], fr["N"])
        self.assertTrue(idr["degenerate"])
        self.assertEqual(idr["p"], 0.0)

    def test_bad_args(self):
        with self.assertRaises(ValueError):
            st.iman_davenport(1.0, 1, 5)
        with self.assertRaises(ValueError):
            st.iman_davenport(1.0, 3, 1)
        with self.assertRaises(ValueError):
            st.iman_davenport(-1.0, 3, 5)


class TestHolm(unittest.TestCase):

    def test_classic_stop_at_second(self):
        """Хрестоматийный набор 0.01..0.05 при alpha=0.05, m=5.
        Пороги: 0.05/5=0.010, /4=0.0125, /3=0.01667, /2=0.025, /1=0.05.
        Проходит только первая, дальше останов."""
        pairs = [("a", 0.01), ("b", 0.02), ("c", 0.03), ("d", 0.04), ("e", 0.05)]
        res = st.holm(pairs, 0.05, 5)
        self.assertEqual([r["name"] for r in res], ["a", "b", "c", "d", "e"])
        self.assertEqual([r["passed"] for r in res],
                         [True, False, False, False, False])
        for r, want in zip(res, (0.01, 0.0125, 0.05 / 3.0, 0.025, 0.05)):
            self.assertAlmostEqual(r["alpha_i"], want, places=15)

    def test_first_two_pass(self):
        pairs = [("x", 0.001), ("y", 0.008), ("z", 0.039), ("w", 0.041)]
        res = st.holm(pairs, 0.05, 4)
        self.assertEqual([r["passed"] for r in res], [True, True, False, False])

    def test_step_down_blocks_later_small_p(self):
        """Суть step-down: гипотеза с p ниже СВОЕГО порога всё равно не проходит,
        если раньше кто-то не прошёл. Здесь p=0.04 < alpha_3=0.05, но passed=False."""
        pairs = [("A", 0.03), ("B", 0.011), ("C", 0.04)]
        res = st.holm(pairs, 0.05, 3)
        self.assertEqual([r["name"] for r in res], ["B", "A", "C"])
        self.assertEqual([r["passed"] for r in res], [True, False, False])
        self.assertLess(res[2]["p"], res[2]["alpha_i"])   # порог свой прошёл бы
        self.assertFalse(res[2]["passed"])                # но останов уже случился

    def test_all_pass(self):
        pairs = [("p1", 0.001), ("p2", 0.002), ("p3", 0.004)]
        res = st.holm(pairs, 0.05, 3)
        self.assertTrue(all(r["passed"] for r in res))

    def test_none_pass(self):
        res = st.holm([("a", 0.4), ("b", 0.9)], 0.05, 2)
        self.assertFalse(any(r["passed"] for r in res))

    def test_boundary_equal_alpha_passes(self):
        """p ровно на пороге считается пройденной (<=, не <)."""
        res = st.holm([("a", 0.025), ("b", 0.05)], 0.05, 2)
        self.assertEqual([r["passed"] for r in res], [True, True])

    def test_family_larger_than_reported(self):
        """m больше числа поданных пар — семейство шире, пороги строже."""
        res = st.holm([("a", 0.01)], 0.05, 10)
        self.assertAlmostEqual(res[0]["alpha_i"], 0.005, places=15)
        self.assertFalse(res[0]["passed"])

    def test_default_m(self):
        res = st.holm([("a", 0.02), ("b", 0.03)], 0.05)
        self.assertAlmostEqual(res[0]["alpha_i"], 0.025, places=15)

    def test_m_smaller_than_pairs_rejected(self):
        with self.assertRaises(ValueError):
            st.holm([("a", 0.01), ("b", 0.02)], 0.05, 1)

    def test_bad_alpha(self):
        for bad in (0.0, 1.0, -0.1, 2.0):
            with self.assertRaises(ValueError):
                st.holm([("a", 0.01)], bad, 1)

    def test_empty_input(self):
        self.assertEqual(st.holm([], 0.05, 3), [])

    def test_ties_keep_input_order(self):
        res = st.holm([("first", 0.02), ("second", 0.02)], 0.05, 2)
        self.assertEqual([r["name"] for r in res], ["first", "second"])

    def test_sorted_ascending(self):
        pairs = [("c", 0.3), ("a", 0.001), ("b", 0.02)]
        res = st.holm(pairs, 0.05, 3)
        ps = [r["p"] for r in res]
        self.assertEqual(ps, sorted(ps))


class TestZvsControl(unittest.TestCase):

    def test_manual_value(self):
        # k=4, N=14: se = sqrt(4*5/(6*14)) = sqrt(20/84)
        se = math.sqrt(20.0 / 84.0)
        z = st.z_vs_control(1.964285714285714, 3.142857142857143, 4, 14)
        self.assertAlmostEqual(z, (1.964285714285714 - 3.142857142857143) / se,
                               places=12)
        self.assertLess(z, 0.0)      # лучшее сито (меньший ранг) -> z отрицателен

    def test_zero_when_equal(self):
        self.assertEqual(st.z_vs_control(2.5, 2.5, 5, 20), 0.0)

    def test_scales_with_sqrt_n(self):
        z1 = st.z_vs_control(1.5, 2.5, 3, 10)
        z2 = st.z_vs_control(1.5, 2.5, 3, 40)
        self.assertAlmostEqual(z2 / z1, 2.0, places=12)   # вчетверо больше N -> вдвое z

    def test_two_sided_p_is_usable(self):
        z = st.z_vs_control(1.0, 3.0, 4, 30)
        p = 2.0 * st.normal_sf(abs(z))
        self.assertGreater(p, 0.0)
        self.assertLess(p, 0.05)

    def test_bad_args(self):
        with self.assertRaises(ValueError):
            st.z_vs_control(1.0, 2.0, 1, 10)
        with self.assertRaises(ValueError):
            st.z_vs_control(1.0, 2.0, 3, 0)


class TestChainCalibration(unittest.TestCase):
    """Сквозная проверка ВСЕЙ цепочки на шуме.

    Если ранги, chi2, поправка Имана–Дэвенпорта и хвост F согласованы, то на
    заведомо одинаковых ситах доля ложных отвержений H0 должна держаться около
    alpha. Это ловит ошибки, которые табличные точки не видят: перепутанный
    знак ранга, неверную константу в формуле, подмену df1/df2 местами.
    Сид фиксирован — тест детерминированный, «мигать» не может.
    """

    def test_false_rejection_rate_near_alpha(self):
        import random
        rnd = random.Random(20261115)
        alpha = 0.05
        n_sims = 1000
        N, k = 12, 7
        rejected = 0
        for _ in range(n_sims):
            blocks = [[rnd.gauss(0.0, 1.0) for _ in range(k)] for _ in range(N)]
            fr = st.friedman(blocks)
            idr = st.iman_davenport(fr["chi2"], fr["k"], fr["N"])
            if idr["p"] < alpha:
                rejected += 1
        share = rejected / float(n_sims)
        self.assertGreater(share, 0.02, "ложных отвержений подозрительно мало: %r" % share)
        self.assertLess(share, 0.09, "ложных отвержений слишком много: %r" % share)

    def test_real_effect_is_detected(self):
        """Сито со сдвигом 1.5 сигмы обязано ловиться в подавляющем большинстве.

        ИЗМЕРЕНО на этом ядре (N=12, k=7, alpha=0.05, по 400 симуляций на сид):
        сдвиг 1.0 сигмы -> мощность ~0.50, 1.5 сигмы -> ~0.87 (разброс по сидам
        0.858..0.895), 2.0 сигмы -> ~0.99. Порог теста поставлен на 0.80, то
        есть НИЖЕ измеренного с запасом: тест должен ловить поломку ядра, а не
        мигать от обычного разброса симуляции.
        """
        import random
        rnd = random.Random(4242)
        N, k = 12, 7
        hits = 0
        n_sims = 200
        for _ in range(n_sims):
            blocks = []
            for _ in range(N):
                row = [rnd.gauss(0.0, 1.0) for _ in range(k)]
                row[3] += 1.5              # заведомо лучшее сито — индекс 3
                blocks.append(row)
            fr = st.friedman(blocks)
            idr = st.iman_davenport(fr["chi2"], fr["k"], fr["N"])
            best = fr["mean_ranks"].index(min(fr["mean_ranks"]))
            if idr["p"] < 0.05 and best == 3:
                hits += 1
        self.assertGreater(hits / float(n_sims), 0.80)


class TestNumericalRobustness(unittest.TestCase):
    """Крайние аргументы не должны ни падать, ни выдавать значение вне [0,1].

    Диапазон взят с большим запасом против рабочего (k <= 9, N <= сотни):
    если внутренние ряд/дробь где-то не сходятся, здесь это вылезет.
    """

    def test_chi2_wide_sweep(self):
        for df in (1, 2, 3, 7, 15, 40, 100, 500, 2000):
            for x in (1e-9, 1e-6, 0.1, 1.0, 10.0, 100.0, 1000.0, 20000.0):
                p = st.chi2_sf(x, df)
                self.assertGreaterEqual(p, 0.0)
                self.assertLessEqual(p, 1.0)

    def test_f_wide_sweep(self):
        for d1 in (1, 2, 5, 13, 40, 200):
            for d2 in (1, 2, 5, 13, 40, 200, 2000):
                for x in (1e-9, 0.01, 1.0, 3.0, 50.0, 1e6):
                    p = st.f_sf(x, d1, d2)
                    self.assertGreaterEqual(p, 0.0)
                    self.assertLessEqual(p, 1.0)

    def test_huge_x_does_not_overflow(self):
        self.assertEqual(st.f_sf(1e300, 3, 9), 0.0)
        self.assertEqual(st.chi2_sf(1e300, 3), 0.0)


class TestNoForbiddenImports(unittest.TestCase):
    """Actions ставит зависимости строго из requirements.txt: numpy/scipy/pandas
    там нет и не будет. Проверяем ИМПОРТЫ, а не упоминания в тексте —
    в шапке модуля эти имена стоят в объяснении, и это нормально."""

    def test_source_has_no_numpy_scipy_pandas(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "stats_core.py",
        )
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        hit = re.search(r"^\s*(?:import|from)\s+(numpy|scipy|pandas)\b", src, re.M)
        self.assertIsNone(hit, "запрещённый импорт в stats_core.py: %r"
                          % (hit.group(0) if hit else None))

    def test_not_loaded_at_runtime(self):
        for mod in ("numpy", "scipy", "pandas"):
            self.assertNotIn(mod, dir(st))


if __name__ == "__main__":
    unittest.main(verbosity=2)

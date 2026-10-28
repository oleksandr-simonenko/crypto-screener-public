#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Селфтест вердикта 15.11 (блоки C1/C2): tests/test_verdict.py

Проверяем ровно то, на чём 15 ноября будет стоять решение о деньгах:

  1. СИНТЕТИКА «ВСЕ СИТА РАВНЫ» — одинаковое распределение у всех дорожек.
     Доля ложных отвержений H0 на 200 повторах обязана лежать в 0.02..0.10
     (номинал alpha = 0.05). Тест, который на равных данных находит победителя
     чаще, чем в одном случае из десяти, — это генератор ложных находок, а не
     арбитр турнира.
  2. СИНТЕТИКА «ОДНО СИТО ЗАВЕДОМО ЛУЧШЕ» — сдвиг 1.5 сигмы. H0 обязана
     отвергаться, а победителем обязано становиться ИМЕННО ЭТО сито, в
     подавляющем большинстве повторов.
  3. ПЕРЕКЛЮЧЕНИЕ ДИЗАЙНА: при N < 10 по токенам скрипт обязан уйти на
     запасной дизайн «блок = прогон» (§3.1) и объявить это.
  4. ИСКЛЮЧЕНИЕ ПОИМЁННО: дорожка без закрытых позиций обязана быть названа
     в отчёте с причиной, а не молча пропасть из таблицы.

Плюс проверки-предохранители: вердикт всегда РОВНО одной из трёх разрешённых
форм; пустой вход не роняет скрипт; A0 не участвует в тесте, но участвует в §6;
таблица чувствительности к издержкам считается при каждом множителе.

ДЕТЕРМИНИЗМ: все сиды зафиксированы. Тест обязан давать один и тот же ответ
на любой машине и в любой день — иначе он не тест, а лотерея.

Запуск: python -m unittest discover -s tests -v
"""

import datetime as _dt
import json
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import costs                      # noqa: E402
import rules_addendum as RA       # noqa: E402
import verdict_1511 as V          # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# =========================================================================
#  СИНТЕТИКА
# =========================================================================

# Шесть сит, контроль A7 — тот же расклад, что ожидается к 15.11 без A6.
SYNTH_TRACKS = ("A1", "A2", "A3", "A4", "A5", "A7")
SYNTH_SIGMA = 10.0                # сигма доходности в процентах
BASE_DATE = _dt.date(2026, 7, 20)


def _cfg():
    """Реальный costs.yaml: net — монотонное преобразование gross, поэтому
    ранги от издержек не зависят, а путь расчёта проверяется настоящий."""
    return costs.load_costs()


def make_trade(track, block_idx, token, gross, liq_class="deep"):
    entry = BASE_DATE + _dt.timedelta(days=7 * block_idx)
    exit_d = entry + _dt.timedelta(days=60)
    return {
        "track": track,
        "sym": token.upper(),
        "cg_id": token,
        "entry_date": entry.isoformat(),
        "exit_date": exit_d.isoformat(),
        "entry_price": 1.0,
        "exit_price": 1.0 + gross / 100.0,
        "last_price": 1.0 + gross / 100.0,
        "peak_price": 1.0 + abs(gross) / 100.0,
        "status": "closed",
        "close_reason": "age",
        "hold_days": 60,
        "ret_gross_pct": gross,
        "vol_usd_24h": 2.0e7,
        "liq_class": liq_class,
        "ret_net_pct": None,
    }


def synth_shared_runs(rng, n_blocks, tracks=SYNTH_TRACKS, effect=0.0, boosted=None):
    """Блоки-ПРОГОНЫ полны по построению, токены у каждой дорожки СВОИ.

    Это в точности та ситуация, которую правила описывают в §3.1: пересечение
    сит по одному токену почти не встречается, зато прогон общий.
    """
    out = []
    for b in range(n_blocks):
        for tr in tracks:
            gross = rng.gauss(0.0, SYNTH_SIGMA)
            if boosted is not None and tr == boosted:
                gross += effect * SYNTH_SIGMA
            out.append(make_trade(tr, b, "tok-%d-%s" % (b, tr), gross))
    return out


def synth_shared_tokens(rng, n_blocks, tracks=SYNTH_TRACKS):
    """Блоки-ТОКЕНЫ полны: один и тот же токен взят всеми ситами."""
    out = []
    for b in range(n_blocks):
        for tr in tracks:
            out.append(make_trade(tr, b, "shared-%d" % b, rng.gauss(0.0, SYNTH_SIGMA)))
    return out


def is_canonical_verdict(line):
    """Вердикт обязан быть РОВНО одной из трёх разрешённых форм (§5 + контракт)."""
    if line == V.VERDICT_NO_DIFF:
        return True
    if line.startswith("победитель ") and " (p=" in line and line.endswith(")"):
        return True
    if line.startswith("данных мало: N=") and line.endswith(", мощность ниже порога"):
        return True
    return False


# =========================================================================
#  1. ВСЕ СИТА РАВНЫ -> ложных отвержений не больше номинала
# =========================================================================

class TestNullHypothesis(unittest.TestCase):
    """Главное свойство арбитра: на равных данных он молчит."""

    REPS = 200
    N_BLOCKS = 15
    SEED = 20260901

    def test_dolya_lozhnyh_otvergenij_v_dopuske(self):
        cfg = _cfg()
        rng = random.Random(self.SEED)
        rejections = 0
        winners = 0
        for _ in range(self.REPS):
            trades = synth_shared_runs(rng, self.N_BLOCKS)
            res = V.analyze(trades, cfg, mult=1.0, use_power_gate=False)
            self.assertEqual(res["N"], self.N_BLOCKS)
            self.assertEqual(res["k"], len(SYNTH_TRACKS))
            if res["iman_davenport"]["p"] < V.ALPHA:
                rejections += 1
            if res["verdict_kind"] == "winner":
                winners += 1
        rate = rejections / float(self.REPS)
        self.assertTrue(
            0.02 <= rate <= 0.10,
            "доля ложных отвержений H0 = %.3f вне допуска 0.02..0.10 "
            "(номинал alpha=%.2f, повторов %d)" % (rate, V.ALPHA, self.REPS),
        )
        # Победитель на равных данных обязан быть РЕДКОСТЬЮ: пост-хок Холма
        # режет ещё сильнее, чем сам главный тест.
        self.assertLessEqual(
            winners / float(self.REPS), 0.10,
            "на равных ситах победитель объявлен в %d случаях из %d" % (winners, self.REPS),
        )

    def test_verdikt_vsegda_kanonicheskoj_formy(self):
        cfg = _cfg()
        rng = random.Random(self.SEED + 1)
        for _ in range(25):
            res = V.analyze(synth_shared_runs(rng, self.N_BLOCKS), cfg,
                            mult=1.0, use_power_gate=False)
            self.assertTrue(is_canonical_verdict(res["verdict_line"]),
                            "недопустимая форма вердикта: %r" % res["verdict_line"])


# =========================================================================
#  2. ОДНО СИТО ЛУЧШЕ НА 1.5 СИГМЫ -> ловится, и это именно оно
# =========================================================================

class TestAlternative(unittest.TestCase):
    """Обратное свойство: настоящее преимущество арбитр обязан увидеть."""

    REPS = 200
    N_BLOCKS = 30          # столько блоков даёт мощность, достаточную для 1.5 сигмы
    EFFECT = 1.5
    BOOSTED = "A3"
    SEED = 20260902

    def test_pobeditel_imenno_usilennoe_sito(self):
        cfg = _cfg()
        rng = random.Random(self.SEED)
        rejected = 0
        correct = 0
        wrong_winner = 0
        for _ in range(self.REPS):
            trades = synth_shared_runs(rng, self.N_BLOCKS,
                                       effect=self.EFFECT, boosted=self.BOOSTED)
            res = V.analyze(trades, cfg, mult=1.0, use_power_gate=False)
            if res["iman_davenport"]["p"] < V.ALPHA:
                rejected += 1
            if res["verdict_kind"] == "winner":
                if res["winner"] == self.BOOSTED:
                    correct += 1
                else:
                    wrong_winner += 1
        self.assertGreaterEqual(
            rejected / float(self.REPS), 0.90,
            "H0 отвергнута лишь в %.3f повторов — эффект 1.5 сигмы обязан ловиться"
            % (rejected / float(self.REPS)),
        )
        self.assertGreaterEqual(
            correct / float(self.REPS), 0.90,
            "верный победитель найден лишь в %.3f повторов" % (correct / float(self.REPS)),
        )
        self.assertEqual(
            wrong_winner, 0,
            "победителем %d раз объявлено НЕ усиленное сито — это ложная находка"
            % wrong_winner,
        )

    def test_pobeditel_luchshe_kontrolya_a_ne_huzhe(self):
        """Сито, значимо ХУДШЕЕ контроля, победителем не объявляется никогда."""
        cfg = _cfg()
        rng = random.Random(self.SEED + 7)
        trades = synth_shared_runs(rng, self.N_BLOCKS, effect=-self.EFFECT, boosted="A3")
        res = V.analyze(trades, cfg, mult=1.0, use_power_gate=False)
        self.assertTrue(res["h0_rejected"], "проваленный контроль: H0 не отвергнута")
        self.assertNotEqual(res["winner"], "A3")
        rows = {r["track"]: r for r in res["posthoc"]}
        self.assertTrue(rows["A3"]["passed_holm"], "A3 обязана пройти Холма по величине z")
        self.assertFalse(rows["A3"]["better_than_control"])
        self.assertFalse(rows["A3"]["is_winner"])


# =========================================================================
#  3. ПЕРЕКЛЮЧЕНИЕ ДИЗАЙНА ПРИ N < 10 ПО ТОКЕНАМ (§3.1)
# =========================================================================

class TestDesignSwitch(unittest.TestCase):

    def test_pri_malom_chisle_tokenov_perehod_na_progony(self):
        cfg = _cfg()
        rng = random.Random(4242)
        trades = synth_shared_runs(rng, 12)        # общих токенов нет вовсе
        res = V.analyze(trades, cfg, mult=1.0)
        self.assertEqual(res["n_blocks_token"], 0)
        self.assertEqual(res["n_blocks_run"], 12)
        self.assertEqual(res["design"], "run")
        self.assertTrue(res["design_switched"])
        self.assertIn("ЗАПАСНОЙ", res["design_reason"])
        self.assertEqual(res["N"], 12)

    def test_granicza_perekluchenia_rovno_10(self):
        """9 общих токенов -> запасной дизайн; 10 -> главный. Порог из §3.1.
        Арифметика проверяется БЕЗ флага С-01 (design_lock=None): сам флаг —
        в TestDesignLock."""
        cfg = _cfg()
        res9 = V.analyze(synth_shared_tokens(random.Random(11), 9), cfg, mult=1.0,
                         design_lock=None)
        self.assertEqual(res9["n_blocks_token"], 9)
        self.assertEqual(res9["design"], "run")
        self.assertTrue(res9["design_switched"])

        res10 = V.analyze(synth_shared_tokens(random.Random(12), 10), cfg, mult=1.0,
                          design_lock=None)
        self.assertEqual(res10["n_blocks_token"], 10)
        self.assertEqual(res10["design"], "token")
        self.assertFalse(res10["design_switched"])
        self.assertEqual(res10["N"], 10)

    def test_oba_n_pechatayutsya_vsegda(self):
        """§3.1 требует печатать оба N независимо от выбранного дизайна."""
        cfg = _cfg()
        res = V.analyze(synth_shared_runs(random.Random(13), 12), cfg, mult=1.0)
        self.assertIn("n_blocks_token", res)
        self.assertIn("n_blocks_run", res)
        art = V.build_verdict(as_of="2026-11-15", n_sims=1,
                              trades_override=synth_shared_runs(random.Random(14), 12),
                              cfg=cfg)
        # Артефакт обязан нести оба числа, а не только победившее.
        self.assertEqual(art["result"]["n_blocks_token"], 0)
        self.assertEqual(art["result"]["n_blocks_run"], 12)


# =========================================================================
#  4. ДОРОЖКА БЕЗ ЗАКРЫТЫХ — ИСКЛЮЧАЕТСЯ ПОИМЁННО (§3.1)
# =========================================================================

class TestNamedExclusion(unittest.TestCase):

    def test_pustaya_dorozhka_nazvana_poimenno(self):
        cfg = _cfg()
        rng = random.Random(777)
        trades = synth_shared_runs(rng, 12, tracks=("A1", "A2", "A4", "A5", "A7"))
        # A3 и A6 существуют, но только с ОТКРЫТЫМИ позициями; A8 пуста совсем.
        for b in range(3):
            for tr in ("A3", "A6"):
                t = make_trade(tr, b, "open-%d-%s" % (b, tr), 5.0)
                t["status"] = "open"
                t["exit_date"] = None
                t["ret_gross_pct"] = None
                trades.append(t)

        res = V.analyze(V.closed_only(trades), cfg, mult=1.0)
        excluded = {row["track"]: row["reason"] for row in res["excluded_tracks"]}
        for tr in ("A3", "A6", "A8"):
            self.assertIn(tr, excluded, "дорожка %s не названа в исключениях" % tr)
            self.assertIn("закрыт", excluded[tr].lower(),
                          "у исключения %s не названа причина: %r" % (tr, excluded[tr]))
        self.assertNotIn("A3", res["participating"])
        self.assertEqual(res["participating"], ["A1", "A2", "A4", "A5", "A7"])
        self.assertEqual(res["k"], 5)

    def test_bez_kontrolya_a7_verdikt_dannyh_malo(self):
        """Нет контроля — нет турнира: §1 знает только A7 как планку."""
        cfg = _cfg()
        trades = synth_shared_runs(random.Random(778), 12, tracks=("A1", "A2", "A3"))
        res = V.analyze(trades, cfg, mult=1.0)
        self.assertEqual(res["verdict_kind"], "insufficient")
        self.assertIn("A7", res["verdict_reason"])
        self.assertTrue(is_canonical_verdict(res["verdict_line"]))


# =========================================================================
#  ПРЕДОХРАНИТЕЛИ: пустой вход, A0, §6, чувствительность, мощность
# =========================================================================

class TestEmptyAndArtifacts(unittest.TestCase):

    def test_pustoj_vhod_ne_padaet(self):
        cfg = _cfg()
        art = V.build_verdict(as_of="2026-09-01", n_sims=1, trades_override=[], cfg=cfg)
        self.assertEqual(art["n_closed_total"], 0)
        self.assertEqual(art["result"]["verdict_kind"], "insufficient")
        self.assertTrue(art["verdict_line"].startswith("данных мало: N=0"))
        self.assertTrue(is_canonical_verdict(art["verdict_line"]))
        self.assertFalse(art["money_allowed"])
        self.assertFalse(art["paragraph6"]["computable"])
        # артефакт обязан быть валидным JSON (inf/nan недопустимы)
        json.dumps(V._json_safe(art), ensure_ascii=False, allow_nan=False)

    def test_otkrytye_pozicii_ne_idut_v_verdikt(self):
        """§2: open_pnl не идёт в вердикт ни в каком виде."""
        cfg = _cfg()
        trades = synth_shared_runs(random.Random(15), 12)
        for t in trades:
            t["status"] = "open"
            t["exit_date"] = None
        art = V.build_verdict(as_of="2026-11-15", n_sims=1, trades_override=trades, cfg=cfg)
        self.assertEqual(art["n_trades_total"], len(trades))
        self.assertEqual(art["n_closed_total"], 0)
        self.assertEqual(art["result"]["N"], 0)

    def test_a0_ne_v_teste_no_v_paragrafe_6(self):
        cfg = _cfg()
        rng = random.Random(16)
        trades = synth_shared_runs(rng, 12)
        for b in range(12):
            trades.append(make_trade("A0", b, "a0-%d" % b, rng.gauss(0.0, SYNTH_SIGMA)))
        res = V.analyze(trades, cfg, mult=1.0)
        self.assertNotIn("A0", res["participating"])
        self.assertNotIn("A0", res["mean_ranks"])
        self.assertEqual(res["a0_closed_n"], 12)
        p6 = V.paragraph6(trades, cfg, mult=1.0)
        self.assertTrue(p6["computable"])
        self.assertIsNotNone(p6["portfolio_mean_net"])

    def test_paragraf6_trebuet_vseh_uslovij(self):
        """Заведомо сильное сито проходит §6; оно же с N<30 закрытых — нет."""
        cfg = _cfg()
        rng = random.Random(17)
        big = synth_shared_runs(rng, 40, effect=1.5, boosted="A3")
        for b in range(40):
            big.append(make_trade("A0", b, "a0-%d" % b, rng.gauss(0.0, SYNTH_SIGMA)))
        p6 = V.paragraph6(big, cfg, mult=1.0)
        self.assertIn("A3", p6["passed_tracks"])
        row = p6["per_track"]["A3"]
        self.assertTrue(row["checks"]["n_closed_ge_30"])
        self.assertTrue(row["checks"]["leads_half1"])
        self.assertTrue(row["checks"]["leads_half2"])
        self.assertGreaterEqual(row["delta_vs_control_pp"], V.P6_EDGE_PP)

        small = synth_shared_runs(random.Random(18), 20, effect=1.5, boosted="A3")
        for b in range(20):
            small.append(make_trade("A0", b, "a0-%d" % b, 0.0))
        p6s = V.paragraph6(small, cfg, mult=1.0)
        self.assertFalse(p6s["per_track"]["A3"]["checks"]["n_closed_ge_30"])
        self.assertNotIn("A3", p6s["passed_tracks"])

    def test_tablica_chuvstvitelnosti_po_kazhdomu_mnozhitelyu(self):
        cfg = _cfg()
        art = V.build_verdict(as_of="2026-11-15", n_sims=1,
                              trades_override=synth_shared_runs(random.Random(19), 12),
                              cfg=cfg)
        mults = [row["mult"] for row in art["sensitivity"]]
        self.assertEqual(mults, [float(m) for m in cfg["sensitivity_multipliers"]])
        for row in art["sensitivity"]:
            self.assertTrue(is_canonical_verdict(row["verdict_line"]))
        self.assertIn("verdict_stable_across_costs", art)
        if art["verdict_stable_across_costs"]:
            self.assertIsNone(art["sensitivity_alarm"])
        else:
            self.assertEqual(art["sensitivity_alarm"], art["sensitivity_alarm"].upper())

    def test_artefakty_pishutsya_v_dve_tochki(self):
        cfg = _cfg()
        art = V.build_verdict(as_of="2026-11-15", n_sims=1, trades_override=[], cfg=cfg)
        with tempfile.TemporaryDirectory() as tmp:
            written = V.write_artifacts(art, repo_dir=tmp)
            self.assertEqual(len(written), 2)
            for path in written:
                self.assertTrue(os.path.isfile(path))
                with open(path, "r", encoding="utf-8") as fh:
                    back = json.load(fh)
                self.assertEqual(back["verdict_line"], art["verdict_line"])
                self.assertIn("git_sha", back["provenance"])
                self.assertIn("rules_sha256", back)
            self.assertTrue(written[0].endswith(os.path.join("artifacts", "verdict_latest.json")))
            self.assertTrue(written[1].endswith(
                os.path.join("2026-11", "verdict_20261115.json")))


class TestPower(unittest.TestCase):
    """C2: мощность зависит только от k и N и обязана быть монотонной по эффекту."""

    def test_moshchnost_rastet_s_effektom(self):
        pw = V.power_estimate(6, 20, [0.0, 0.5, 1.0, 2.0], n_sims=300, seed=V.POWER_SEED)
        self.assertTrue(pw["computable"])
        vals = [pw["by_effect"][e] for e in (0.0, 0.5, 1.0, 2.0)]
        self.assertLessEqual(vals[0], 0.10, "при нулевом эффекте мощность = ложные срабатывания")
        self.assertLess(vals[0], vals[2])
        self.assertLess(vals[1], vals[3])

    def test_moshchnost_determinirovana(self):
        a = V.power_estimate(5, 12, [1.0], n_sims=200, seed=123)
        V._POWER_CACHE.clear()
        b = V.power_estimate(5, 12, [1.0], n_sims=200, seed=123)
        self.assertEqual(a["by_effect"][1.0], b["by_effect"][1.0])

    def test_vyrozhdennye_vhody_ne_padayut(self):
        for k, n in ((0, 0), (1, 5), (6, 1)):
            pw = V.power_estimate(k, n, [1.0], n_sims=10, seed=1)
            self.assertFalse(pw["computable"])
            self.assertEqual(pw["by_effect"][1.0], 0.0)

    def test_sims_ne_menyaet_verdikt(self):
        """Ключ --sims меняет только таблицу мощности, но не вердикт."""
        cfg = _cfg()
        trades = synth_shared_runs(random.Random(20), 12)
        a1 = V.build_verdict(as_of="2026-11-15", n_sims=1, trades_override=trades, cfg=cfg)
        a2 = V.build_verdict(as_of="2026-11-15", n_sims=50, trades_override=trades, cfg=cfg)
        self.assertEqual(a1["verdict_line"], a2["verdict_line"])
        self.assertEqual(a1["result"]["winner"], a2["result"]["winner"])


class TestProjection(unittest.TestCase):
    """Проекция числа блоков к 15.11 — на СИНТЕТИЧЕСКОМ турнире (детерминированно)
    и на настоящем tournament.json (структурно, без привязки к дню)."""

    def _synthetic_repo(self, tmp):
        # A1,A2 входят в один и тот же прогон 20.07 -> общий блок-прогон;
        # A3 входит 21.09 -> к 12.11 ещё нет 60 дней, значит не закроется;
        # A8 пуста.
        data = {
            "tracks": {
                "A1": {"open": [
                    {"sym": "X", "cg_id": "x", "entry_date": "2026-07-20"},
                    {"sym": "Y", "cg_id": "y", "entry_date": "2026-07-23"}], "closed": []},
                "A2": {"open": [
                    {"sym": "X", "cg_id": "x", "entry_date": "2026-07-20"},
                    {"sym": "Z", "cg_id": "z", "entry_date": "2026-07-23"}], "closed": []},
                "A3": {"open": [
                    {"sym": "Q", "cg_id": "q", "entry_date": "2026-09-21"}], "closed": []},
                "A4": {"open": [], "closed": []},
                "A5": {"open": [], "closed": []},
                "A6": {"open": [], "closed": []},
                "A7": {"open": [], "closed": []},
                "A8": {"open": [], "closed": []},
            },
            "meta": {},
        }
        with open(os.path.join(tmp, "tournament.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

    def test_proekciya_na_sintetike(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._synthetic_repo(tmp)
            pr = V.projected_n(as_of="2026-09-01", repo_dir=tmp)
            self.assertTrue(pr["available"])
            self.assertEqual(pr["per_track_closed"]["A1"], 2)
            self.assertEqual(pr["per_track_closed"]["A2"], 2)
            # позиция от 21.09 не доживает до 60 дней к 12.11
            self.assertEqual(pr["per_track_closed"]["A3"], 0)
            self.assertEqual(pr["participating"], ["A1", "A2"])
            self.assertIn("A8", pr["excluded_empty"])
            self.assertIn("A3", pr["excluded_empty"])
            # общий токен ровно один (x), общих прогонов два (20.07 и 23.07)
            self.assertEqual(pr["blocks_token"], 1)
            self.assertEqual(pr["blocks_run"], 2)
            self.assertEqual(pr["design_that_applies"], "run")

    def test_proekciya_na_nastoyashchem_turnire(self):
        pr = V.projected_n(as_of="2026-09-01")
        if not pr.get("available"):
            self.skipTest("tournament.json недоступен")
        self.assertIn("A8", pr["excluded_empty"])       # A8 пуста, это факт протокола
        self.assertLessEqual(pr["blocks_token"], pr["blocks_run"])
        self.assertEqual(pr["design_that_applies"],
                         "token" if pr["blocks_token"] >= V.MIN_BLOCKS_TOKEN else "run")
        for tr, n in pr["per_track_closed"].items():
            self.assertGreaterEqual(n, 0)


# =========================================================================
#  ПОПРАВКА №1 (02.09.2026): A6 — НАБЛЮДАТЕЛЬНАЯ, ВНЕ ПОДТВЕРЖДАЮЩЕГО СЕМЕЙСТВА
# =========================================================================

SEVEN_TRACKS = ("A1", "A2", "A3", "A4", "A5", "A6", "A7")

ADDENDUM_FAKE = """# тестовая поправка

```yaml
addendum: 1
%s
applies_before_first_closures: [2026-09-14, 2026-09-21]
rules_v1_sha256: %s
observational_tracks: %s
```
"""


def _write_fake_repo(tmp, tracks_yaml, pin=None, adopted="2026-09-02"):
    """Каталог-репозиторий: правила v1 (копия настоящих) + поправка с заданным
    списком дорожек (строка yaml, обычно '[A6]'). Без tournament.json/
    positions.json — вердикт обязан честно пережить их отсутствие.
    adopted=None — блок без даты принятия."""
    src = os.path.join(REPO, RA.RULES_V1_FILENAME)
    with open(src, "rb") as fh:
        raw = fh.read()
    with open(os.path.join(tmp, RA.RULES_V1_FILENAME), "wb") as fh:
        fh.write(raw)
    if pin is None:
        pin = RA.file_hashes(src)[1]
    if not tracks_yaml.startswith("["):
        tracks_yaml = "[%s]" % tracks_yaml
    adopted_line = ("adopted: %s" % adopted) if adopted else "# без даты принятия"
    with open(os.path.join(tmp, RA.DEFAULT_ADDENDUM_FILENAME), "w", encoding="utf-8") as fh:
        fh.write(ADDENDUM_FAKE % (adopted_line, pin, tracks_yaml))


class TestAddendumFile(unittest.TestCase):
    """Файл поправки в репозитории: читается, закрепляет правила v1, след в STATE."""

    def test_addendum_is_present_and_pins_rules_v1(self):
        add = RA.load_addendum(repo_dir=REPO)
        self.assertTrue(add["available"], add.get("error"))
        self.assertEqual(add["number"], 1)
        self.assertEqual(add["adopted"], "2026-09-02")
        self.assertEqual(add["observational_tracks"], ["A6"])
        self.assertEqual(add["applies_before"], ["2026-09-14", "2026-09-21"])
        self.assertEqual(len(add["sha256_lf"]), 64)
        self.assertTrue(add["pin_matches"], add["warnings"])
        self.assertEqual([w for w in add["warnings"] if w.startswith("ВНИМАНИЕ")], [])

    def test_addendum_hash_is_recorded_in_state(self):
        """Отпечаток поправки обязан лежать в STATE.md §10 — как у правил."""
        add = RA.load_addendum(repo_dir=REPO)
        with open(os.path.join(REPO, "STATE.md"), encoding="utf-8") as fh:
            state = fh.read()
        self.assertIn(add["sha256_lf"], state,
                      "sha256 поправки (LF) не записан в STATE.md")

    def test_rules_v1_hash_unchanged_by_addendum(self):
        """Поправка — отдельный файл: правила v1 несут тот же отпечаток, что
        закреплён в поправке (§8 правил соблюдён)."""
        add = RA.load_addendum(repo_dir=REPO)
        self.assertEqual(add["rules_v1_sha256_pinned"], add["rules_v1_sha256_actual"])

    def test_missing_file_means_no_addendum(self):
        with tempfile.TemporaryDirectory() as tmp:
            add = RA.load_addendum(repo_dir=tmp)
        self.assertFalse(add["available"])
        self.assertIn("не найден", add["error"])
        self.assertEqual(add["observational_tracks"], [])
        self.assertIsNone(RA.addendum_sha256_or_none(repo_dir=tmp))

    def test_control_portfolio_and_garbage_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_fake_repo(tmp, "A7, A6, X9, A0, a6")
            add = RA.load_addendum(repo_dir=tmp)
        self.assertTrue(add["available"])
        self.assertEqual(add["observational_tracks"], ["A6"], add["warnings"])
        joined = " ".join(add["warnings"])
        self.assertIn("A7", joined)
        self.assertIn("A0", joined)
        self.assertIn("X9", joined)

    def test_pin_mismatch_is_flagged_not_hidden(self):
        for pin in ("0123abcd" * 8, "0" * 64):
            # Второй отпечаток — из одних цифр: YAML прочёл бы его числом и
            # потерял бы ведущие нули; читаться он обязан как строка из 64 hex.
            with tempfile.TemporaryDirectory() as tmp:
                _write_fake_repo(tmp, "A6", pin=pin)
                add = RA.load_addendum(repo_dir=tmp)
            self.assertTrue(add["available"])
            self.assertEqual(add["rules_v1_sha256_pinned"], pin)
            self.assertFalse(add["pin_matches"])
            self.assertTrue(any("НЕ совпадает" in w for w in add["warnings"]), add["warnings"])

    def test_file_without_machine_block_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, RA.DEFAULT_ADDENDUM_FILENAME), "w",
                      encoding="utf-8") as fh:
                fh.write("# поправка без машинного блока\n")
            add = RA.load_addendum(repo_dir=tmp)
        self.assertFalse(add["available"])
        self.assertIn("машинный блок", add["error"])

    def test_non_list_tracks_makes_addendum_unavailable(self):
        """`observational_tracks: A6` без скобок — не «поправка без дорожек»,
        а нечитаемая поправка (ревью 02.09): применить нечего, и это ошибка."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_fake_repo(tmp, "[A6]")
            path = os.path.join(tmp, RA.DEFAULT_ADDENDUM_FILENAME)
            with open(path, encoding="utf-8") as fh:
                text = fh.read().replace("observational_tracks: [A6]",
                                         "observational_tracks: A6")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            add = RA.load_addendum(repo_dir=tmp)
        self.assertFalse(add["available"])
        self.assertIn("списком", add["error"])
        self.assertEqual(add["observational_tracks"], [])

    def test_adoption_date_is_parsed_and_checked_against_own_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_fake_repo(tmp, "A6", adopted="2026-11-10")
            late = RA.load_addendum(repo_dir=tmp)
        self.assertEqual(late["adopted_date"], "2026-11-10")
        self.assertTrue(any("ПОЗЖЕ первых закрытий" in w for w in late["warnings"]),
                        late["warnings"])
        with tempfile.TemporaryDirectory() as tmp:
            _write_fake_repo(tmp, "A6", adopted=None)
            nodate = RA.load_addendum(repo_dir=tmp)
        self.assertIsNone(nodate["adopted_date"])
        self.assertTrue(any("дата принятия" in w and w.startswith("ВНИМАНИЕ")
                            for w in nodate["warnings"]), nodate["warnings"])
        real = RA.load_addendum(repo_dir=REPO)
        self.assertEqual(real["adopted_date"], "2026-09-02")
        self.assertEqual([w for w in real["warnings"] if w.startswith("ВНИМАНИЕ")], [])


class TestObservationalTrack(unittest.TestCase):
    """A6 вне семейства, но описательно — и ровно то, что требует поправка."""

    def test_observational_out_of_family_but_described(self):
        cfg = _cfg()
        trades = synth_shared_runs(random.Random(2609), 12, tracks=SEVEN_TRACKS)
        res = V.analyze(trades, cfg, mult=1.0, observational=["A6"])
        self.assertEqual(res["participating"], ["A1", "A2", "A3", "A4", "A5", "A7"])
        self.assertEqual(res["k"], 6)
        self.assertEqual(res["N"], 12)
        self.assertNotIn("A6", res["mean_ranks"])
        self.assertNotIn("A6", [r["track"] for r in res["excluded_tracks"]],
                         "наблюдательная дорожка — не «исключённая по пустоте»")
        self.assertEqual(res["observational_tracks"], ["A6"])
        row = res["observational"]["A6"]
        self.assertEqual(row["n_closed"], 12)
        self.assertIsNotNone(row["median_net"])
        self.assertLessEqual(row["q1_net"], row["median_net"])
        self.assertLessEqual(row["median_net"], row["q3_net"])
        self.assertAlmostEqual(row["iqr_net"], row["q3_net"] - row["q1_net"])
        self.assertTrue(0.0 <= row["win_rate"] <= 1.0)
        self.assertIn("вне теста", row["note"])
        # Без флага те же данные — семёрка: поправка меняет семейство, не данные.
        res7 = V.analyze(trades, cfg, mult=1.0)
        self.assertEqual(res7["k"], 7)
        self.assertEqual(res7["N"], 12)
        self.assertEqual(res7["observational_tracks"], [])

    def test_observational_track_without_closed_is_described_with_zero(self):
        cfg = _cfg()
        trades = synth_shared_runs(random.Random(2610), 12)          # без A6 вовсе
        res = V.analyze(trades, cfg, mult=1.0, observational=["A6"])
        self.assertEqual(res["observational"]["A6"]["n_closed"], 0)
        self.assertNotIn("A6", [r["track"] for r in res["excluded_tracks"]])
        self.assertEqual(res["k"], 6)

    def test_control_cannot_be_observational(self):
        cfg = _cfg()
        trades = synth_shared_runs(random.Random(2611), 12)
        res = V.analyze(trades, cfg, mult=1.0, observational=["A7", "A6"])
        self.assertIn("A7", res["participating"])
        self.assertEqual(res["observational_tracks"], ["A6"])
        self.assertTrue(any("A7" in n and "контроль" in n for n in res["notes"]))

    def test_describe_quartiles(self):
        d = V.describe([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(d["n_closed"], 4)
        self.assertAlmostEqual(d["median_net"], 2.5)
        self.assertAlmostEqual(d["q1_net"], 1.75)
        self.assertAlmostEqual(d["q3_net"], 3.25)
        self.assertAlmostEqual(d["iqr_net"], 1.5)
        self.assertAlmostEqual(d["win_rate"], 1.0)
        self.assertEqual(V.describe([])["n_closed"], 0)
        self.assertIsNone(V.describe([])["median_net"])
        self.assertAlmostEqual(V.describe([-1.0, 1.0])["win_rate"], 0.5)

    def test_build_verdict_reads_flag_from_repo_dir(self):
        """Флаг — из файла репозитория, не из кода: с поправкой A6 вне семейства,
        без неё — участвует, и артефакт несёт оба факта."""
        cfg = _cfg()
        trades = synth_shared_runs(random.Random(2612), 12, tracks=SEVEN_TRACKS)
        with tempfile.TemporaryDirectory() as tmp:
            _write_fake_repo(tmp, "A6")
            art = V.build_verdict(as_of="2026-11-15", n_sims=1, trades_override=trades,
                                  cfg=cfg, repo_dir=tmp)
        self.assertTrue(art["rules_addendum"]["available"])
        self.assertEqual(art["rules_addendum"]["observational_tracks"], ["A6"])
        self.assertTrue(art["rules_addendum"]["pin_matches"])
        self.assertEqual(len(art["rules_addendum_sha256"]), 64)
        self.assertEqual(art["result"]["k"], 6)
        self.assertNotIn("A6", art["result"]["participating"])
        self.assertIn("A6", art["result"]["observational"])
        for row in art["sensitivity"]:
            self.assertTrue(is_canonical_verdict(row["verdict_line"]))
        json.dumps(V._json_safe(art), ensure_ascii=False, allow_nan=False)

        with tempfile.TemporaryDirectory() as tmp:
            art0 = V.build_verdict(as_of="2026-11-15", n_sims=1, trades_override=trades,
                                   cfg=cfg, repo_dir=tmp)
        self.assertFalse(art0["rules_addendum"]["available"])
        self.assertIsNone(art0["rules_addendum_sha256"])
        self.assertEqual(art0["result"]["k"], 7)
        self.assertIn("A6", art0["result"]["participating"])

    def test_verdict_line_stays_canonical_with_addendum(self):
        cfg = _cfg()
        trades = synth_shared_runs(random.Random(2613), 12, tracks=SEVEN_TRACKS)
        res = V.analyze(trades, cfg, mult=1.0, observational=["A6"])
        self.assertTrue(is_canonical_verdict(res["verdict_line"]))

    def test_post_hoc_addendum_is_not_applied(self):
        """Ревью 02.09: поправка, принятая ПОСЛЕ первой закрытой сделки, — не
        предрегистрация. Семейство остаётся полным, расхождение — в артефакте
        и заглавными буквами в отчёте, а не «принята … до первых закрытий»."""
        cfg = _cfg()
        trades = synth_shared_runs(random.Random(2614), 12, tracks=SEVEN_TRACKS)
        first_exit = min(t["exit_date"] for t in trades)          # 2026-09-18
        self.assertEqual(first_exit, "2026-09-18")
        with tempfile.TemporaryDirectory() as tmp:
            _write_fake_repo(tmp, "A6", adopted="2026-11-10")
            art = V.build_verdict(as_of="2026-11-15", n_sims=1, trades_override=trades,
                                  cfg=cfg, repo_dir=tmp)
        add = art["rules_addendum"]
        self.assertTrue(add["available"])
        self.assertFalse(add["preregistered"])
        self.assertFalse(add["applied"])
        self.assertEqual(add["declared_tracks"], ["A6"])
        self.assertEqual(add["observational_tracks"], [])
        self.assertEqual(add["first_exit_date"], "2026-09-18")
        self.assertIn("не предрегистрация", add["preregistration_note"])
        self.assertEqual(art["result"]["k"], 7)
        self.assertIn("A6", art["result"]["participating"])
        # Та же поправка, но принятая на дату первого выхода — тоже поздно.
        with tempfile.TemporaryDirectory() as tmp:
            _write_fake_repo(tmp, "A6", adopted="2026-09-18")
            art2 = V.build_verdict(as_of="2026-11-15", n_sims=1, trades_override=trades,
                                   cfg=cfg, repo_dir=tmp)
        self.assertFalse(art2["rules_addendum"]["preregistered"])
        # Без даты принятия — тоже не применяется.
        with tempfile.TemporaryDirectory() as tmp:
            _write_fake_repo(tmp, "A6", adopted=None)
            art3 = V.build_verdict(as_of="2026-11-15", n_sims=1, trades_override=trades,
                                   cfg=cfg, repo_dir=tmp)
        self.assertFalse(art3["rules_addendum"]["preregistered"])
        self.assertEqual(art3["result"]["k"], 7)

    def test_addendum_before_first_exit_is_applied(self):
        cfg = _cfg()
        trades = synth_shared_runs(random.Random(2615), 12, tracks=SEVEN_TRACKS)
        with tempfile.TemporaryDirectory() as tmp:
            _write_fake_repo(tmp, "A6", adopted="2026-09-02")
            art = V.build_verdict(as_of="2026-11-15", n_sims=1, trades_override=trades,
                                  cfg=cfg, repo_dir=tmp)
        add = art["rules_addendum"]
        self.assertTrue(add["preregistered"])
        self.assertTrue(add["applied"])
        self.assertEqual(art["result"]["k"], 6)
        # На пустом входе (закрытых нет) первой сделки нет — принята «до результатов».
        with tempfile.TemporaryDirectory() as tmp:
            _write_fake_repo(tmp, "A6", adopted="2026-09-02")
            art0 = V.build_verdict(as_of="2026-09-02", n_sims=1, trades_override=[],
                                   cfg=cfg, repo_dir=tmp)
        self.assertTrue(art0["rules_addendum"]["preregistered"])
        self.assertIsNone(art0["rules_addendum"]["first_exit_date"])

    def test_observational_never_passes_paragraph6(self):
        """§6 — второй порог ДЛЯ ПОБЕДИТЕЛЯ теста; наблюдательная дорожка вне
        теста и пройти его не может, как бы ни считалась её строка."""
        cfg = _cfg()
        rng = random.Random(2616)
        trades = synth_shared_runs(rng, 40, tracks=SEVEN_TRACKS, effect=4.0, boosted="A6")
        for b in range(40):
            trades.append(make_trade("A0", b, "a0-%d" % b, rng.gauss(0.0, SYNTH_SIGMA)))
        p6_plain = V.paragraph6(trades, cfg, mult=1.0)
        self.assertIn("A6", p6_plain["passed_tracks"], "без поправки A6 проходит §6 по числам")
        p6 = V.paragraph6(trades, cfg, mult=1.0, observational=["A6"])
        self.assertNotIn("A6", p6["passed_tracks"])
        self.assertTrue(p6["per_track"]["A6"]["observational"])
        self.assertFalse(p6["per_track"]["A6"]["passed"])
        self.assertEqual(p6["observational_tracks"], ["A6"])
        self.assertIsNotNone(p6["per_track"]["A6"]["mean_net"], "строка считается описательно")

    def test_addendum_without_effective_tracks_is_not_reported_as_missing(self):
        """Поправка есть (только контроль в списке — отвергнут): семейство
        полное, но подпись не выдаёт это за отсутствие файла (ревью 02.09)."""
        cfg = _cfg()
        trades = synth_shared_runs(random.Random(2617), 12, tracks=SEVEN_TRACKS)
        with tempfile.TemporaryDirectory() as tmp:
            _write_fake_repo(tmp, "A7")
            src = os.path.join(REPO, "tournament.json")
            with open(src, "rb") as fh:
                raw = fh.read()
            with open(os.path.join(tmp, "tournament.json"), "wb") as fh:
                fh.write(raw)
            art = V.build_verdict(as_of="2026-09-02", n_sims=1, trades_override=trades,
                                  cfg=cfg, repo_dir=tmp)
        add = art["rules_addendum"]
        self.assertTrue(add["available"])
        self.assertEqual(add["observational_tracks"], [])
        self.assertEqual(art["result"]["k"], 7)
        pr = art["projection"]
        self.assertIsNone(pr["reference"])
        self.assertIn("наблюдательных дорожек не названо", pr["reference_note"])


class TestProjectionWithAddendum(unittest.TestCase):
    """Проекция N к 15.11 на настоящем tournament.json: 6 дорожек -> 12 блоков,
    семёрка с A6 -> 9 (замер 01–02.09.2026, зафиксирован поправкой)."""

    def test_family_of_six_gives_twelve_run_blocks(self):
        pr = V.projected_n(as_of="2026-09-02", observational=["A6"])
        if not pr.get("available"):
            self.skipTest("tournament.json недоступен")
        self.assertEqual(pr["participating"], ["A1", "A2", "A3", "A4", "A5", "A7"])
        self.assertEqual(pr["observational"], ["A6"])
        self.assertIn("A8", pr["excluded_empty"])
        self.assertNotIn("A6", pr["excluded_empty"])
        self.assertEqual(pr["blocks_run"], 12, "замер 01–02.09.2026: N = 12 без A6")
        self.assertEqual(pr["reference"]["blocks_run"], 9, "с A6 — 9 блоков")
        self.assertEqual(sorted(pr["reference"]["tracks"]),
                         ["A1", "A2", "A3", "A4", "A5", "A6", "A7"])
        self.assertIn("A6", pr["reference"]["label"])

    def test_without_addendum_reference_is_legacy_without_a6(self):
        pr = V.projected_n(as_of="2026-09-02")
        if not pr.get("available"):
            self.skipTest("tournament.json недоступен")
        self.assertIn("A6", pr["participating"])
        self.assertEqual(pr["observational"], [])
        self.assertNotIn("A6", pr["reference"]["tracks"])
        self.assertEqual(pr["reference"]["blocks_run"], 12)

    def test_as_of_excludes_entries_after_it(self):
        """as_of — не подпись: вход позже as_of в проекцию не идёт, поэтому
        тесты на живом tournament.json не зависят от завтрашних прогонов."""
        data = {
            "tracks": {
                "A1": {"open": [{"sym": "X", "cg_id": "x", "entry_date": "2026-07-20"},
                                {"sym": "Y", "cg_id": "y", "entry_date": "2026-07-23"}],
                       "closed": []},
                "A7": {"open": [{"sym": "X", "cg_id": "x", "entry_date": "2026-07-20"},
                                {"sym": "Z", "cg_id": "z", "entry_date": "2026-07-23"}],
                       "closed": [{"sym": "W", "cg_id": "w", "entry_date": "2026-08-03"}]},
            },
            "meta": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "tournament.json"), "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            early = V.projected_n(as_of="2026-07-21", repo_dir=tmp)
            late = V.projected_n(as_of="2026-09-01", repo_dir=tmp)
        self.assertEqual(early["per_track_closed"]["A1"], 1)
        self.assertEqual(early["per_track_closed"]["A7"], 1, "закрытая от 03.08 тоже позже as_of")
        self.assertEqual(early["blocks_run"], 1)
        self.assertEqual(late["per_track_closed"]["A1"], 2)
        self.assertEqual(late["per_track_closed"]["A7"], 3)
        self.assertEqual(late["blocks_run"], 2)

    def test_synthetic_projection_unchanged_without_flag(self):
        """Прежний синтетический тест проекции остаётся верным: без флага
        `reference` — «без A6», и он не ломает старые поля."""
        pr = V.projected_n(as_of="2026-09-01")
        if not pr.get("available"):
            self.skipTest("tournament.json недоступен")
        self.assertIn("reference", pr)
        self.assertIn("blocks_run", pr["reference"])


# =========================================================================
#  С-01 (03.09.2026): ДИЗАЙН «БЛОК = ПРОГОН» ЗАФИКСИРОВАН ФЛАГОМ
# =========================================================================

class TestDesignLock(unittest.TestCase):
    """Дизайн подтверждён навигатором 03.09.2026 до первых закрытий и
    зафиксирован флагом DESIGN_LOCK: по фактическому N после первых закрытий
    не пересчитывается; оба N печатаются; расхождение с §3.1 — заглавными
    буквами; предрегистрацию флага проверяет машина, как у поправки №1."""

    def test_module_flag_is_run_and_dated_before_first_closures(self):
        lock = V.DESIGN_LOCK
        self.assertEqual(lock["design"], "run")
        self.assertEqual(lock["confirmed_on"], "2026-09-03")
        self.assertEqual(lock["before_first_closures"], ["2026-09-14", "2026-09-21"])
        self.assertLess(lock["confirmed_on"], min(lock["before_first_closures"]))
        self.assertIn("§3.1", lock["basis"])

    def test_flag_is_recorded_in_state(self):
        """Датированная строка о подтверждении дизайна обязана лежать в STATE.md
        §10 — как отпечатки правил и поправки."""
        with open(os.path.join(REPO, "STATE.md"), encoding="utf-8") as fh:
            state = fh.read()
        y, m, d = V.DESIGN_LOCK["confirmed_on"].split("-")
        self.assertIn("%s.%s.%s" % (d, m, y), state)
        self.assertIn("«блок = прогон» ПОДТВЕРЖДЁН", state)

    def test_locked_design_wins_over_arithmetic_and_conflict_is_loud(self):
        """10 общих токенов: по §3.1 — токен, по флагу — прогон. Действует флаг,
        расхождение названо заглавными буквами; без флага — арифметика."""
        cfg = _cfg()
        res10 = V.analyze(synth_shared_tokens(random.Random(12), 10), cfg, mult=1.0)
        self.assertEqual(res10["n_blocks_token"], 10)
        self.assertEqual(res10["design_by_rule"], "token")
        self.assertEqual(res10["design"], "run", "флаг фиксирует дизайн")
        self.assertTrue(res10["design_locked"])
        self.assertTrue(res10["design_switched"])
        self.assertTrue(res10["design_conflict"])
        self.assertTrue(res10["design_conflict_line"].startswith("ВНИМАНИЕ"))
        self.assertIn("ЗАФИКСИРОВАН", res10["design_reason"])
        self.assertEqual(res10["design_lock"]["confirmed_on"], "2026-09-03")
        self.assertTrue(is_canonical_verdict(res10["verdict_line"]))
        free = V.analyze(synth_shared_tokens(random.Random(12), 10), cfg, mult=1.0,
                         design_lock=None)
        self.assertEqual(free["design"], "token")
        self.assertFalse(free["design_locked"])
        self.assertIsNone(free["design_lock"])
        self.assertFalse(free["design_conflict"])
        self.assertIsNone(free["design_conflict_line"])

    def test_no_conflict_when_arithmetic_agrees(self):
        res = V.analyze(synth_shared_runs(random.Random(4243), 12), _cfg(), mult=1.0)
        self.assertTrue(res["design_locked"])
        self.assertFalse(res["design_conflict"])
        self.assertIsNone(res["design_conflict_line"])
        self.assertEqual(res["design"], "run")
        self.assertEqual(res["design_by_rule"], "run")
        self.assertEqual(res["N"], 12)
        self.assertIn("n_blocks_token", res)     # оба N по-прежнему считаются
        self.assertIn("n_blocks_run", res)

    def test_build_verdict_applies_preregistered_flag(self):
        """Первый выход синтетики — 18.09.2026, флаг подтверждён 03.09 — раньше:
        предрегистрация подтверждена машиной, флаг применён, артефакт несёт статус."""
        cfg = _cfg()
        trades = synth_shared_runs(random.Random(2701), 12)
        art = V.build_verdict(as_of="2026-11-15", n_sims=1, trades_override=trades, cfg=cfg)
        lk = art["design_lock"]
        self.assertTrue(lk["present"])
        self.assertTrue(lk["preregistered"])
        self.assertTrue(lk["applied"])
        self.assertEqual(lk["design"], "run")
        self.assertEqual(lk["confirmed_on"], "2026-09-03")
        self.assertEqual(lk["first_exit_date"], "2026-09-18")
        self.assertIn("до результатов", lk["note"])
        self.assertTrue(art["result"]["design_locked"])
        self.assertEqual(art["result"]["design"], "run")
        for row in art["sensitivity"]:
            self.assertEqual(row["design"], "run")
        if art["projection"].get("available"):
            self.assertEqual(art["projection"]["design_that_applies"], "run")
            self.assertTrue(art["projection"]["design_locked"])
        json.dumps(V._json_safe(art), ensure_ascii=False, allow_nan=False)
        # На пустом входе первой сделки нет — «до результатов».
        art0 = V.build_verdict(as_of="2026-09-03", n_sims=1, trades_override=[], cfg=cfg)
        self.assertTrue(art0["design_lock"]["applied"])
        self.assertIsNone(art0["design_lock"]["first_exit_date"])

    def test_post_hoc_flag_is_not_applied(self):
        """Флаг с датой не раньше первого выхода — не предрегистрация: НЕ
        применяется, дизайн по §3.1, расхождение в артефакте."""
        cfg = _cfg()
        trades = synth_shared_tokens(random.Random(2702), 10)     # по §3.1 — токен
        saved = V.DESIGN_LOCK
        try:
            V.DESIGN_LOCK = dict(saved, confirmed_on="2026-11-10")
            late = V.build_verdict(as_of="2026-11-15", n_sims=1, trades_override=trades, cfg=cfg)
            V.DESIGN_LOCK = dict(saved, confirmed_on="2026-09-18")   # день первого выхода
            same_day = V.build_verdict(as_of="2026-11-15", n_sims=1, trades_override=trades, cfg=cfg)
            V.DESIGN_LOCK = {k: v for k, v in saved.items() if k != "confirmed_on"}
            nodate = V.build_verdict(as_of="2026-11-15", n_sims=1, trades_override=trades, cfg=cfg)
        finally:
            V.DESIGN_LOCK = saved
        for art in (late, same_day, nodate):
            lk = art["design_lock"]
            self.assertTrue(lk["present"])
            self.assertFalse(lk["preregistered"])
            self.assertFalse(lk["applied"])
            self.assertEqual(art["result"]["design"], "token", "флаг не применён — §3.1")
            self.assertFalse(art["result"]["design_locked"])
        self.assertIn("не предрегистрация", late["design_lock"]["note"])
        self.assertIn("дата подтверждения", nodate["design_lock"]["note"])

    def test_status_function_is_pure_and_strict(self):
        st = V.design_lock_status(None)
        self.assertFalse(st["present"])
        self.assertFalse(st["applied"])
        lock = dict(V.DESIGN_LOCK)
        self.assertTrue(V.design_lock_status(lock, None)["applied"])
        self.assertTrue(V.design_lock_status(lock, _dt.date(2026, 9, 18))["applied"])
        self.assertFalse(V.design_lock_status(lock, _dt.date(2026, 9, 3))["applied"],
                         "на дату первого выхода — уже поздно")
        self.assertFalse(V.design_lock_status(dict(lock, design="weird"), None)["applied"])
        self.assertIsNone(V._lock_or_none({"design": "weird"}))
        self.assertIsNone(V._lock_or_none(None))
        self.assertIs(V._lock_or_none(V._LOCK_DEFAULT), V.DESIGN_LOCK)

    def test_projection_takes_flag_and_names_conflict(self):
        data = {
            "tracks": {
                "A1": {"open": [{"sym": "X", "cg_id": "x", "entry_date": "2026-07-20"}],
                       "closed": []},
                "A7": {"open": [{"sym": "X", "cg_id": "x", "entry_date": "2026-07-20"}],
                       "closed": []},
            },
            "meta": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "tournament.json"), "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            plain = V.projected_n(as_of="2026-09-03", repo_dir=tmp)
            locked = V.projected_n(as_of="2026-09-03", repo_dir=tmp, design_lock=V.DESIGN_LOCK)
            forced = V.projected_n(as_of="2026-09-03", repo_dir=tmp,
                                   design_lock=dict(V.DESIGN_LOCK, design="token"))
        self.assertEqual(plain["blocks_token"], 1)
        self.assertEqual(plain["design_by_rule"], "run")
        self.assertEqual(plain["design_that_applies"], "run")
        self.assertFalse(plain["design_locked"])
        self.assertTrue(locked["design_locked"])
        self.assertFalse(locked["design_conflict"])
        self.assertEqual(forced["design_that_applies"], "token")
        self.assertTrue(forced["design_conflict"])
        real = V.projected_n(as_of="2026-09-03", observational=["A6"], design_lock=V.DESIGN_LOCK)
        if real.get("available"):
            self.assertEqual(real["design_by_rule"], "run", "замер: блоков по токену 0")
            self.assertEqual(real["design_that_applies"], "run")
            self.assertFalse(real["design_conflict"])


class TestPowerFamilySix(unittest.TestCase):
    """Мощность для N=12 / k=6 против N=9 / k=7 (фиксированный сид решения)."""

    def test_family_six_beats_seven_at_one_sigma(self):
        fam = V.power_estimate(6, 12, [1.0], n_sims=V.POWER_DECISION_SIMS,
                               seed=V.POWER_SEED)["by_effect"][1.0]
        ref = V.power_estimate(7, 9, [1.0], n_sims=V.POWER_DECISION_SIMS,
                               seed=V.POWER_SEED)["by_effect"][1.0]
        self.assertGreater(fam, ref)
        self.assertTrue(0.15 <= fam <= 0.40, "ожидалось ≈25 %%, получено %.3f" % fam)
        self.assertTrue(0.05 <= ref <= 0.25, "ожидалось ≈14 %%, получено %.3f" % ref)
        # И то, и другое ниже порога 0.5: оговорка «данных мало» остаётся
        # честной при любом семействе — поправка мощности не «покупает».
        self.assertLess(fam, V.POWER_THRESHOLD)


if __name__ == "__main__":
    unittest.main(verbosity=2)

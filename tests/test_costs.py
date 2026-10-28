#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Селфтест модели издержек (блок A1): tests/test_costs.py

Проверяем ровно то, на чём потом стоят все net-числа вердикта:
  * реальный costs.yaml читается, у КАЖДОЙ константы есть непустой source;
  * круг издержек считается арифметически верно (на синтетическом конфиге,
    где числа выбраны руками — тест не должен ломаться от правки тарифа);
  * net всегда хуже gross при положительных издержках;
  * класс unknown трактуется как самый дорогой thin;
  * множитель чувствительности работает и монотонен;
  * кривой конфиг отвергается, а не проглатывается молча.

Запуск: python -m unittest discover -s tests -v
"""

import copy
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import costs  # noqa: E402


def synthetic_cfg(mode="all", funding_applies=False, funding_bps_per_day=0.0, instrument="spot"):
    """Конфиг с РУЧНЫМИ числами: круглые значения, чтобы арифметику можно было
    проверить в уме и чтобы тест не зависел от правки реального тарифа."""
    return {
        "version": 1,
        "instrument": instrument,
        "taker_fee_bps": {"value": 10.0, "source": "синтетика теста"},
        "funding": {
            "applies_to_spot": funding_applies,
            "bps_per_day": {"value": funding_bps_per_day, "source": "синтетика теста"},
        },
        "slippage_bps_by_class": {
            "deep": {"value": 5.0, "source": "синтетика теста"},
            "medium": {"value": 25.0, "source": "синтетика теста"},
            "thin": {"value": 50.0, "source": "синтетика теста"},
        },
        "liquidity_class_thresholds": {
            "deep_min_vol_usd": 10000000,
            "medium_min_vol_usd": 1000000,
            "source": "синтетика теста",
        },
        "position_usd": 100,
        "sensitivity_multipliers": [0.5, 1.0, 2.0],
        "sensitivity_applies_to": mode,
    }


class TestLoadRealYaml(unittest.TestCase):
    """Реальный costs.yaml репозитория."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = costs.load_costs()

    def test_yaml_chitaetsya_i_polya_na_meste(self):
        self.assertEqual(self.cfg["instrument"], "spot")
        self.assertIsInstance(self.cfg["version"], int)
        self.assertGreater(self.cfg["position_usd"], 0)
        self.assertTrue(self.cfg["_source_path"].endswith("costs.yaml"))
        self.assertIn(self.cfg.get("sensitivity_applies_to", "all"), costs.SENSITIVITY_MODES)

    def test_u_kazhdoy_konstanty_est_source(self):
        # Правило блока A1: каждая константа издержек несёт своё происхождение.
        self.assertTrue(self.cfg["taker_fee_bps"]["source"].strip())
        self.assertTrue(self.cfg["funding"]["bps_per_day"]["source"].strip())
        self.assertTrue(self.cfg["liquidity_class_thresholds"]["source"].strip())
        for cls_name in costs.LIQ_CLASSES:
            src = self.cfg["slippage_bps_by_class"][cls_name]["source"]
            self.assertTrue(src.strip())
            # Слиппедж обязан быть назван допущением прямо в источнике —
            # иначе через месяц его начнут цитировать как замер.
            self.assertIn("допущение", src.lower())

    def test_realnye_konstanty_razumny(self):
        self.assertGreater(self.cfg["taker_fee_bps"]["value"], 0)
        slip = self.cfg["slippage_bps_by_class"]
        self.assertLessEqual(slip["deep"]["value"], slip["medium"]["value"])
        self.assertLessEqual(slip["medium"]["value"], slip["thin"]["value"])
        thr = self.cfg["liquidity_class_thresholds"]
        self.assertLess(thr["medium_min_vol_usd"], thr["deep_min_vol_usd"])
        # Спот: фандинга нет ни в каком виде.
        self.assertFalse(self.cfg["funding"]["applies_to_spot"])
        self.assertEqual(self.cfg["funding"]["bps_per_day"]["value"], 0.0)
        self.assertIn(1.0, [float(m) for m in self.cfg["sensitivity_multipliers"]])

    def test_load_costs_po_yavnomu_puti(self):
        cfg2 = costs.load_costs(costs.DEFAULT_COSTS_PATH)
        self.assertEqual(cfg2["taker_fee_bps"]["value"], self.cfg["taker_fee_bps"]["value"])


class TestClassifyLiquidity(unittest.TestCase):
    def setUp(self):
        self.cfg = synthetic_cfg()

    def test_granicy_klassov(self):
        self.assertEqual(costs.classify_liquidity(50_000_000, self.cfg), "deep")
        self.assertEqual(costs.classify_liquidity(10_000_000, self.cfg), "deep")   # граница включительно
        self.assertEqual(costs.classify_liquidity(9_999_999, self.cfg), "medium")
        self.assertEqual(costs.classify_liquidity(1_000_000, self.cfg), "medium")  # граница включительно
        self.assertEqual(costs.classify_liquidity(999_999, self.cfg), "thin")
        self.assertEqual(costs.classify_liquidity(0, self.cfg), "thin")            # ноль объёма — это факт

    def test_dyry_v_dannyh_dayut_unknown(self):
        for bad in (None, "", "нет данных", float("nan"), float("inf"), -1, True, False, [], {}):
            self.assertEqual(costs.classify_liquidity(bad, self.cfg), "unknown", repr(bad))

    def test_chislo_strokoy_prinimaetsya(self):
        self.assertEqual(costs.classify_liquidity("2500000", self.cfg), "medium")


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.cfg = synthetic_cfg()

    def test_arifmetika_kruga(self):
        # medium: комиссия 10 бп * 2 стороны = 20; слиппедж 25 * 2 = 50; фандинг 0.
        rt = costs.round_trip_bps("medium", 60, self.cfg)
        self.assertAlmostEqual(rt["fee_bps"], 20.0, places=9)
        self.assertAlmostEqual(rt["slippage_bps"], 50.0, places=9)
        self.assertAlmostEqual(rt["funding_bps"], 0.0, places=9)
        self.assertAlmostEqual(rt["total_bps"], 70.0, places=9)
        self.assertEqual(set(rt.keys()), {"fee_bps", "slippage_bps", "funding_bps", "total_bps"})

    def test_klassy_dorozhayut_v_pravilnom_poryadke(self):
        deep = costs.round_trip_bps("deep", 60, self.cfg)["total_bps"]
        medium = costs.round_trip_bps("medium", 60, self.cfg)["total_bps"]
        thin = costs.round_trip_bps("thin", 60, self.cfg)["total_bps"]
        self.assertLess(deep, medium)
        self.assertLess(medium, thin)

    def test_unknown_schitaetsya_kak_samyy_dorogoy_thin(self):
        thin = costs.round_trip_bps("thin", 60, self.cfg)
        for bad in ("unknown", None, "", "УНКНОВН", "чушь"):
            self.assertEqual(costs.round_trip_bps(bad, 60, self.cfg), thin, repr(bad))
            self.assertTrue(costs.is_fallback_class(bad), repr(bad))
        # А настоящий класс подменой не считается.
        for good in ("deep", "medium", "thin", "THIN", " Deep "):
            self.assertFalse(costs.is_fallback_class(good), repr(good))

    def test_unknown_ne_deshevle_lyubogo_izvestnogo_klassa(self):
        unknown = costs.round_trip_bps("unknown", 60, self.cfg)["total_bps"]
        for cls_name in costs.LIQ_CLASSES:
            self.assertGreaterEqual(unknown, costs.round_trip_bps(cls_name, 60, self.cfg)["total_bps"])

    def test_hold_days_dlya_spota_nichego_ne_menyaet(self):
        a = costs.round_trip_bps("thin", 0, self.cfg)["total_bps"]
        b = costs.round_trip_bps("thin", 60, self.cfg)["total_bps"]
        c = costs.round_trip_bps("thin", None, self.cfg)["total_bps"]
        d = costs.round_trip_bps("thin", -5, self.cfg)["total_bps"]  # мусорный срок не даёт скидку
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertEqual(a, d)

    def test_funding_schitaetsya_kogda_primenim(self):
        # Каркас на будущее (перпы): ставка * дни, и она попадает в круг.
        cfg = synthetic_cfg(funding_applies=True, funding_bps_per_day=1.5, instrument="perp")
        rt = costs.round_trip_bps("deep", 10, cfg)
        self.assertAlmostEqual(rt["funding_bps"], 15.0, places=9)   # 1.5 бп/день * 10 дней
        self.assertAlmostEqual(rt["total_bps"], 45.0, places=9)     # 10*2 + 5*2 + 15

    def test_otricatelnyy_mult_otvergaetsya(self):
        with self.assertRaises(ValueError):
            costs.round_trip_bps("thin", 60, self.cfg, mult=-1.0)


class TestSensitivity(unittest.TestCase):
    def test_mult_rezhim_all_masshtabiruet_ves_krug(self):
        cfg = synthetic_cfg(mode="all")
        base = costs.round_trip_bps("medium", 60, cfg)["total_bps"]
        half = costs.round_trip_bps("medium", 60, cfg, mult=0.5)["total_bps"]
        double = costs.round_trip_bps("medium", 60, cfg, mult=2.0)["total_bps"]
        self.assertAlmostEqual(half, base * 0.5, places=9)
        self.assertAlmostEqual(double, base * 2.0, places=9)
        self.assertLess(half, base)
        self.assertLess(base, double)

    def test_mult_rezhim_slippage_only_ne_trogaet_komissiyu(self):
        cfg = synthetic_cfg(mode="slippage_only")
        base = costs.round_trip_bps("medium", 60, cfg)
        double = costs.round_trip_bps("medium", 60, cfg, mult=2.0)
        self.assertAlmostEqual(double["fee_bps"], base["fee_bps"], places=9)
        self.assertAlmostEqual(double["slippage_bps"], base["slippage_bps"] * 2.0, places=9)
        self.assertAlmostEqual(double["total_bps"], base["fee_bps"] + base["slippage_bps"] * 2.0, places=9)

    def test_mult_nol_obnulyaet_izderzhki(self):
        cfg = synthetic_cfg()
        rt = costs.round_trip_bps("thin", 60, cfg, mult=0.0)
        self.assertEqual(rt["total_bps"], 0.0)
        self.assertAlmostEqual(costs.net_ret_pct(20.0, "thin", 60, cfg, mult=0.0), 20.0, places=9)

    def test_bolshe_mult_huzhe_net(self):
        cfg = synthetic_cfg()
        vals = [costs.net_ret_pct(20.0, "thin", 60, cfg, mult=m) for m in (0.5, 1.0, 2.0)]
        self.assertGreater(vals[0], vals[1])
        self.assertGreater(vals[1], vals[2])

    def test_tablica_chuvstvitelnosti(self):
        cfg = synthetic_cfg()
        rows = costs.sensitivity_table("medium", 60, cfg, gross_ret_pct=20.0)
        self.assertEqual([r["mult"] for r in rows], [0.5, 1.0, 2.0])
        for row in rows:
            self.assertIn("total_bps", row)
            self.assertIsNotNone(row["net_ret_pct"])
        rows_no_gross = costs.sensitivity_table("medium", 60, cfg)
        self.assertTrue(all(r["net_ret_pct"] is None for r in rows_no_gross))


class TestNetRet(unittest.TestCase):
    def setUp(self):
        self.cfg = synthetic_cfg()

    def test_multiplikativno_a_ne_vychitaniem(self):
        # thin: круг = 10*2 + 50*2 = 120 бп = 1.2%.
        # net = (1+2.00)*(1-0.012) - 1 = 2.964 -> +196.4%, а вычитание дало бы +198.8%.
        net = costs.net_ret_pct(200.0, "thin", 60, self.cfg)
        self.assertAlmostEqual(net, 196.4, places=6)
        self.assertNotAlmostEqual(net, 200.0 - 1.2, places=3)

    def test_net_vsegda_huzhe_gross_pri_polozhitelnyh_izderzhkah(self):
        for gross in (-99.0, -50.0, -10.0, -0.5, 0.0, 0.5, 7.3, 60.0, 250.0, 1000.0):
            for cls_name in ("deep", "medium", "thin", "unknown"):
                net = costs.net_ret_pct(gross, cls_name, 60, self.cfg)
                self.assertLess(net, gross, "gross=%s class=%s" % (gross, cls_name))

    def test_granica_minus_sto_procentov(self):
        # Монета в ноль: платить издержки уже не с чего, хуже -100% не станет.
        self.assertAlmostEqual(costs.net_ret_pct(-100.0, "thin", 60, self.cfg), -100.0, places=9)

    def test_otkrytaya_pozicia_daet_none(self):
        self.assertIsNone(costs.net_ret_pct(None, "thin", 60, self.cfg))

    def test_net_soglasuetsya_s_round_trip(self):
        for cls_name in ("deep", "medium", "thin"):
            total = costs.round_trip_bps(cls_name, 60, self.cfg)["total_bps"]
            expected = ((1 + 12.5 / 100.0) * (1 - total / 10000.0) - 1) * 100.0
            self.assertAlmostEqual(costs.net_ret_pct(12.5, cls_name, 60, self.cfg), expected, places=6)

    def test_musornyy_gross_otvergaetsya(self):
        with self.assertRaises(ValueError):
            costs.net_ret_pct("много", "thin", 60, self.cfg)
        with self.assertRaises(ValueError):
            costs.net_ret_pct(float("nan"), "thin", 60, self.cfg)


class TestConfigValidation(unittest.TestCase):
    """Кривой конфиг обязан падать громко: он бы испортил все net сразу."""

    def _write_and_load(self, cfg_dict):
        import yaml
        fd, path = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(cfg_dict, fh, allow_unicode=True)
            return costs.load_costs(path)
        finally:
            os.unlink(path)

    def test_validnyy_sinteticheskiy_konfig_prohodit(self):
        cfg = self._write_and_load(synthetic_cfg())
        self.assertEqual(cfg["taker_fee_bps"]["value"], 10.0)

    def test_bez_source_ne_prinimaetsya(self):
        bad = synthetic_cfg()
        del bad["slippage_bps_by_class"]["thin"]["source"]
        with self.assertRaises(ValueError):
            self._write_and_load(bad)

    def test_otricatelnaya_komissiya_ne_prinimaetsya(self):
        bad = synthetic_cfg()
        bad["taker_fee_bps"]["value"] = -1.0
        with self.assertRaises(ValueError):
            self._write_and_load(bad)

    def test_perevernutye_granicy_ne_prinimayutsya(self):
        bad = synthetic_cfg()
        bad["liquidity_class_thresholds"]["deep_min_vol_usd"] = 1000
        with self.assertRaises(ValueError):
            self._write_and_load(bad)

    def test_rassinhron_fandinga_lovitsya(self):
        bad = synthetic_cfg(instrument="perp")
        bad["funding"]["applies_to_spot"] = False
        bad["funding"]["bps_per_day"]["value"] = 1.0
        with self.assertRaises(ValueError):
            self._write_and_load(bad)

    def test_spot_s_vklyuchennym_fandingom_lovitsya(self):
        bad = synthetic_cfg(funding_applies=True, funding_bps_per_day=1.0, instrument="spot")
        with self.assertRaises(ValueError):
            self._write_and_load(bad)

    def test_neizvestnyy_rezhim_chuvstvitelnosti_lovitsya(self):
        bad = synthetic_cfg()
        bad["sensitivity_applies_to"] = "как-нибудь"
        with self.assertRaises(ValueError):
            self._write_and_load(bad)

    def test_pustoy_spisok_mnozhiteley_lovitsya(self):
        bad = synthetic_cfg()
        bad["sensitivity_multipliers"] = []
        with self.assertRaises(ValueError):
            self._write_and_load(bad)

    def test_slippedzh_naoborot_lovitsya(self):
        bad = synthetic_cfg()
        bad["slippage_bps_by_class"]["deep"]["value"] = 999.0
        with self.assertRaises(ValueError):
            self._write_and_load(bad)

    def test_net_faila_konfiga(self):
        with self.assertRaises(IOError):
            costs.load_costs(os.path.join(tempfile.gettempdir(), "net_takogo_faila_costs.yaml"))


class TestRealConfigEndToEnd(unittest.TestCase):
    """Сквозная проверка на настоящих константах: числа должны быть в
    правдоподобном диапазоне, а не в другой вселенной."""

    def test_krug_na_realnyh_konstantah(self):
        cfg = costs.load_costs()
        thin = costs.round_trip_bps("thin", 60, cfg)["total_bps"]
        deep = costs.round_trip_bps("deep", 60, cfg)["total_bps"]
        self.assertGreater(thin, deep)
        # Круг издержек микрокапа: заметный, но не абсурдный (0.1%..10%).
        self.assertTrue(10.0 < deep < 1000.0, deep)
        self.assertTrue(10.0 < thin < 1000.0, thin)
        net = costs.net_ret_pct(0.0, "unknown", 60, cfg)
        self.assertLess(net, 0.0)
        self.assertFalse(math.isnan(net))


if __name__ == "__main__":
    unittest.main(verbosity=2)

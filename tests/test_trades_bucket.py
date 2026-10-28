#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Закрытость сделки определяет ВЕДРО, а не наличие полей.

Регрессионный тест на находку ревизии 01.09.2026 (воспроизводилась прогоном):
позиция, оставшаяся в `tracks.A1.open`, но несущая `ret_pct`, приезжала в
вердикт как ЗАКРЫТАЯ — то есть нереализованный PnL входил и в ранги, и в §6,
вопреки §2 замороженных правил: «open_pnl в вердикт не идёт ни в каком виде».

Почему такой вход не выдуман: screener.py сначала делает `p.update({...ret_pct
...})` и только потом переносит запись в `closed` (paper_update_positions и
t_update). Обрыв прогона между этими действиями, ручная правка файла или любое
будущее добавление «текущего результата» к открытой позиции дают ровно его.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trades as trades_mod        # noqa: E402


def _repo(tmp, tournament=None, positions=None):
    with open(os.path.join(tmp, "tournament.json"), "w", encoding="utf-8") as f:
        json.dump(tournament or {"tracks": {}, "age_cache": {}, "meta": {}},
                  f, ensure_ascii=False)
    with open(os.path.join(tmp, "positions.json"), "w", encoding="utf-8") as f:
        json.dump(positions or {"open": [], "closed": [], "meta": {}},
                  f, ensure_ascii=False)
    return tmp


def _pos(**kw):
    base = {"sym": "FOO", "cg_id": "foo", "entry_date": "2026-07-01",
            "entry_price": 1.0, "last_price": 1.5, "peak_price": 1.5, "miss": 0}
    base.update(kw)
    return base


class TestBucketIsAuthority(unittest.TestCase):

    def test_open_position_with_ret_pct_stays_open(self):
        """Главный случай находки: ret_pct у позиции из ведра open."""
        t = {"tracks": {"A1": {"open": [_pos(ret_pct=50.0)], "closed": []}}}
        with tempfile.TemporaryDirectory() as tmp:
            rows = trades_mod.load_all_trades(_repo(tmp, tournament=t))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["status"], "open", "позиция из ведра open обязана быть открытой")
        self.assertIsNone(r["ret_gross_pct"], "§2: нереализованный PnL в вердикт не идёт")
        self.assertIsNone(r["ret_net_pct"])
        self.assertTrue(r["bucket_mismatch"], "расхождение обязано быть ВИДНО, а не проглочено")

    def test_open_position_with_close_reason_stays_open(self):
        t = {"tracks": {"A1": {"open": [_pos(close_reason="60д")], "closed": []}}}
        with tempfile.TemporaryDirectory() as tmp:
            rows = trades_mod.load_all_trades(_repo(tmp, tournament=t))
        self.assertEqual(rows[0]["status"], "open")
        self.assertTrue(rows[0]["bucket_mismatch"])

    def test_a0_open_position_with_ret_pct_stays_open(self):
        """То же для портфеля A0 — он читается другой веткой загрузчика."""
        p = {"open": [_pos(ret_pct=50.0, name="Foo", stake_usd=100)], "closed": []}
        with tempfile.TemporaryDirectory() as tmp:
            rows = trades_mod.load_all_trades(_repo(tmp, positions=p))
        self.assertEqual(rows[0]["track"], "A0")
        self.assertEqual(rows[0]["status"], "open")
        self.assertIsNone(rows[0]["ret_gross_pct"])

    def test_closed_bucket_is_closed(self):
        """Обратная сторона: из ведра closed сделка закрыта и несёт отклик."""
        t = {"tracks": {"A1": {"open": [], "closed": [
            _pos(ret_pct=50.0, close_date="2026-08-30", close_reason="60д")]}}}
        with tempfile.TemporaryDirectory() as tmp:
            rows = trades_mod.load_all_trades(_repo(tmp, tournament=t))
        r = rows[0]
        self.assertEqual(r["status"], "closed")
        self.assertEqual(r["ret_gross_pct"], 50.0)
        self.assertIsNotNone(r["ret_net_pct"])
        self.assertFalse(r["bucket_mismatch"])

    def test_closed_bucket_without_fields_is_flagged(self):
        """Запись в closed без полей закрытия — тоже порча, и тоже видна."""
        t = {"tracks": {"A1": {"open": [], "closed": [_pos()]}}}
        with tempfile.TemporaryDirectory() as tmp:
            rows = trades_mod.load_all_trades(_repo(tmp, tournament=t))
        self.assertEqual(rows[0]["status"], "closed")
        self.assertTrue(rows[0]["bucket_mismatch"])

    def test_healthy_open_position_is_not_flagged(self):
        """Ложных срабатываний быть не должно: нормальная открытая позиция чиста."""
        t = {"tracks": {"A1": {"open": [_pos()], "closed": []}}}
        with tempfile.TemporaryDirectory() as tmp:
            rows = trades_mod.load_all_trades(_repo(tmp, tournament=t))
        self.assertEqual(rows[0]["status"], "open")
        self.assertFalse(rows[0]["bucket_mismatch"])

    def test_live_repo_has_no_mismatches(self):
        """На боевых данных расхождений быть не должно — иначе это тревога."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        rows = trades_mod.load_all_trades(here)
        bad = [r for r in rows if r.get("bucket_mismatch")]
        self.assertEqual(bad, [], "в боевых файлах появилось расхождение ведро/поля")


if __name__ == "__main__":
    unittest.main(verbosity=2)

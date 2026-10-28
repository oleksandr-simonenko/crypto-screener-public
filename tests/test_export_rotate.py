#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Селфтест блоков D3 (export_trades.py) и D4 (rotate_artifacts.py).

Проверяем ровно то, что дорого стоит при ошибке:
  * РОТАЦИЯ переносит плоские датированные файлы в помесячные папки
    и НЕ ТРОГАЕТ `*_latest.*` — на latest ссылается соседний проект;
  * СЖАТИЕ срабатывает только для файлов старше порога, и оригинал исчезает
    ТОЛЬКО после успешного обратного чтения архива (проверяем обе ветки —
    и удачную, и подменённую неудачную);
  * `--dry-run` (режим по умолчанию) не меняет НИ ОДНОГО байта на диске;
  * заголовки csv совпадают с контрактом ДОСЛОВНО — схему читает чужой
    проект, и молчаливая перестановка колонок сломает его расчёты;
  * CSV-ИНЪЕКЦИЯ: имя монеты из CoinGecko, начинающееся с '=', не должно
    исполниться формулой при открытии файла в Excel — и при этом
    отрицательный pnl обязан остаться ЧИСЛОМ, а не текстом с апострофом.

Все тесты работают на временном каталоге (tempfile), боевой репозиторий
не читается и не меняется.

Запуск: python -m unittest discover -s tests -v
"""

import csv
import gzip
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import export_trades  # noqa: E402
import rotate_artifacts  # noqa: E402

# Дата, «сегодня» для всех тестов. Зафиксирована, чтобы возраст файлов не
# зависел от дня прогона: иначе тест сжатия однажды поменял бы вердикт сам.
AS_OF = date(2026, 9, 1)


def write(path, text="данные\n"):
    """Создать файл с текстом, создав каталоги по пути."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def snapshot(root):
    """Слепок дерева: {относительный путь: содержимое в байтах}."""
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as fh:
                out[os.path.relpath(full, root).replace("\\", "/")] = fh.read()
    return out


def rel_exists(root, rel):
    return os.path.exists(os.path.join(root, rel.replace("/", os.sep)))


class RotateBase(unittest.TestCase):
    """Общий временный репозиторий с типичной раскладкой файлов."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rot_")
        self.addCleanup(shutil.rmtree, self.root, True)
        j = lambda *p: os.path.join(self.root, *p)  # noqa: E731

        # свежие плоские файлы недельного экспорта (как в боевом репозитории)
        write(j("export", "flows_20260814.json"), '{"a": 1}\n')
        write(j("export", "flows_20260821.json"), '{"a": 2}\n')
        write(j("export", "flows_20260828.json"), '{"a": 3}\n')
        # защищённый latest — не должен сдвинуться ни на байт
        write(j("export", "flows_latest.json"), '{"a": 3}\n')
        write(j("export", "flows_latest.csv"), "ticker\n")
        # старый файл (возраст 243 дня на AS_OF) — кандидат на сжатие
        write(j("export", "flows_20260101.json"), '{"old": true}\n' * 50)
        # файл БЕЗ даты в имени — трогать нельзя
        write(j("export", "readme_notes.txt"), "без даты\n")
        # уже разложенный по месяцу и свежий — ни переноса, ни сжатия
        write(j("artifacts", "2026-08", "metrics_20260831.json"), "{}\n")
        # уже разложенный, но старый — только сжатие, без переноса
        write(j("artifacts", "2026-06", "metrics_20260601.json"), "{}\n" * 20)
        # вложенный плоский датированный файл в data/
        write(j("data", "funding", "funding_20260830.csv"), "ts_utc,coin\n")
        # защищённый снапшот множеств символов
        write(j("data", "listings", "symbols_latest.json"), "{}\n")


class TestRotatePlan(RotateBase):

    def test_plan_covers_flat_dated_and_spares_latest(self):
        plan = rotate_artifacts.plan_actions(self.root, older_than_days=60, as_of=AS_OF)
        moved = {os.path.basename(i["src"]) for i in plan
                 if i["action"] == rotate_artifacts.ACTION_MOVE}
        self.assertIn("flows_20260814.json", moved)
        self.assertIn("flows_20260821.json", moved)
        self.assertIn("flows_20260828.json", moved)
        self.assertIn("funding_20260830.csv", moved)
        # latest и файл без даты в план не попадают вообще
        touched = {os.path.basename(i["src"]) for i in plan}
        self.assertNotIn("flows_latest.json", touched)
        self.assertNotIn("flows_latest.csv", touched)
        self.assertNotIn("symbols_latest.json", touched)
        self.assertNotIn("readme_notes.txt", touched)

    def test_plan_gzips_only_old(self):
        plan = rotate_artifacts.plan_actions(self.root, older_than_days=60, as_of=AS_OF)
        gz = {os.path.basename(i["src"]) for i in plan
              if i["action"] == rotate_artifacts.ACTION_GZIP}
        self.assertEqual({"flows_20260101.json", "metrics_20260601.json"}, gz)

    def test_plan_moves_then_gzips_same_file_in_order(self):
        """Старый плоский файл сначала переезжает, потом жмётся уже на месте."""
        plan = rotate_artifacts.plan_actions(self.root, older_than_days=60, as_of=AS_OF)
        seq = [(i["action"], os.path.basename(i["src"])) for i in plan
               if "20260101" in i["src"]]
        self.assertEqual(
            [(rotate_artifacts.ACTION_MOVE, "flows_20260101.json"),
             (rotate_artifacts.ACTION_GZIP, "flows_20260101.json")], seq)
        gz_item = [i for i in plan
                   if i["action"] == rotate_artifacts.ACTION_GZIP
                   and "20260101" in i["src"]][0]
        # сжимаем НОВЫЙ путь (в помесячной папке), а не старый плоский
        self.assertIn(os.path.join("export", "2026-01"), gz_item["src"])

    def test_boundary_is_strictly_older(self):
        """Ровно порог — ещё не «старше порога»."""
        root = tempfile.mkdtemp(prefix="rot_edge_")
        self.addCleanup(shutil.rmtree, root, True)
        write(os.path.join(root, "export", "2026-07", "flows_20260703.json"), "x\n")  # 60 дн.
        write(os.path.join(root, "export", "2026-07", "flows_20260702.json"), "x\n")  # 61 день
        plan = rotate_artifacts.plan_actions(root, older_than_days=60, as_of=AS_OF)
        gz = {os.path.basename(i["src"]) for i in plan
              if i["action"] == rotate_artifacts.ACTION_GZIP}
        self.assertEqual({"flows_20260702.json"}, gz)


class TestRotateDryRun(RotateBase):

    def test_dry_run_changes_nothing(self):
        before = snapshot(self.root)
        plan = rotate_artifacts.plan_actions(self.root, older_than_days=60, as_of=AS_OF)
        self.assertTrue(plan, "план обязан быть непустым, иначе тест ничего не проверяет")
        rotate_artifacts.print_plan(plan, base=self.root)
        self.assertEqual(before, snapshot(self.root))

    def test_cli_without_apply_changes_nothing(self):
        before = snapshot(self.root)
        code = rotate_artifacts.main(
            ["--repo", self.root, "--as-of", AS_OF.isoformat()])
        self.assertEqual(0, code)
        self.assertEqual(before, snapshot(self.root))

    def test_cli_refuses_dry_run_with_apply(self):
        before = snapshot(self.root)
        code = rotate_artifacts.main(
            ["--repo", self.root, "--as-of", AS_OF.isoformat(), "--dry-run", "--apply"])
        self.assertEqual(2, code)
        self.assertEqual(before, snapshot(self.root))


class TestRotateApply(RotateBase):

    def test_apply_moves_and_keeps_latest(self):
        plan = rotate_artifacts.plan_actions(self.root, older_than_days=60, as_of=AS_OF)
        res = rotate_artifacts.apply_plan(plan, verbose=False)
        self.assertEqual([], res["errors"])
        self.assertEqual(0, res["failed"])

        self.assertTrue(rel_exists(self.root, "export/2026-08/flows_20260814.json"))
        self.assertTrue(rel_exists(self.root, "export/2026-08/flows_20260821.json"))
        self.assertTrue(rel_exists(self.root, "export/2026-08/flows_20260828.json"))
        self.assertTrue(rel_exists(self.root, "data/funding/2026-08/funding_20260830.csv"))
        self.assertFalse(rel_exists(self.root, "export/flows_20260814.json"))

        # неприкосновенные
        self.assertTrue(rel_exists(self.root, "export/flows_latest.json"))
        self.assertTrue(rel_exists(self.root, "export/flows_latest.csv"))
        self.assertTrue(rel_exists(self.root, "data/listings/symbols_latest.json"))
        self.assertTrue(rel_exists(self.root, "export/readme_notes.txt"))
        # уже лежавший в своём месяце свежий файл не сдвинулся
        self.assertTrue(rel_exists(self.root, "artifacts/2026-08/metrics_20260831.json"))

    def test_apply_compresses_old_and_content_survives(self):
        original = os.path.join(self.root, "export", "flows_20260101.json")
        with open(original, "rb") as fh:
            payload = fh.read()

        plan = rotate_artifacts.plan_actions(self.root, older_than_days=60, as_of=AS_OF)
        res = rotate_artifacts.apply_plan(plan, verbose=False)
        self.assertEqual([], res["errors"])
        self.assertEqual(2, res["gzipped"])

        gz_path = os.path.join(self.root, "export", "2026-01", "flows_20260101.json.gz")
        self.assertTrue(os.path.exists(gz_path))
        self.assertFalse(os.path.exists(original))
        with gzip.open(gz_path, "rb") as fh:
            self.assertEqual(payload, fh.read())

        # свежие файлы НЕ сжаты
        self.assertTrue(rel_exists(self.root, "export/2026-08/flows_20260828.json"))
        self.assertFalse(rel_exists(self.root, "export/2026-08/flows_20260828.json.gz"))

    def test_original_survives_failed_verification(self):
        """Обратное чтение не сошлось -> оригинал НА МЕСТЕ, архива нет."""
        src = write(os.path.join(self.root, "one", "old_20260101.json"), "важное\n")
        dst = src + ".gz"
        real = rotate_artifacts._gz_matches
        rotate_artifacts._gz_matches = lambda *_a, **_k: False
        try:
            ok, msg = rotate_artifacts.compress_file(src, dst)
        finally:
            rotate_artifacts._gz_matches = real
        self.assertFalse(ok)
        self.assertIn("ОРИГИНАЛ ОСТАВЛЕН", msg)
        self.assertTrue(os.path.exists(src))
        self.assertFalse(os.path.exists(dst))
        self.assertFalse(os.path.exists(dst + ".part"))
        with open(src, encoding="utf-8") as fh:
            self.assertEqual("важное\n", fh.read())

    def test_second_apply_is_idempotent(self):
        plan = rotate_artifacts.plan_actions(self.root, older_than_days=60, as_of=AS_OF)
        rotate_artifacts.apply_plan(plan, verbose=False)
        after_first = snapshot(self.root)
        plan2 = rotate_artifacts.plan_actions(self.root, older_than_days=60, as_of=AS_OF)
        self.assertEqual([], [i for i in plan2 if i["action"] != rotate_artifacts.ACTION_SKIP],
                         "повторный прогон не должен находить работу")
        rotate_artifacts.apply_plan(plan2, verbose=False)
        self.assertEqual(after_first, snapshot(self.root))


class TestNameParsing(unittest.TestCase):

    def test_dates_and_latest(self):
        f = rotate_artifacts.file_date_from_name
        self.assertEqual(date(2026, 8, 14), f("flows_20260814.json"))
        self.assertEqual(date(2026, 8, 14), f("flows_20260814.json.gz"))
        self.assertEqual(date(2026, 8, 14), f("unlocks_2026-08-14.csv"))
        self.assertIsNone(f("candidates_log.jsonl"))
        self.assertIsNone(f("flows_20261340.json"))       # календарно невозможная
        self.assertIsNone(f("build_123456789.json"))      # 9 цифр — не дата
        self.assertTrue(rotate_artifacts.is_latest_name("flows_latest.json"))
        self.assertTrue(rotate_artifacts.is_latest_name("trades_latest.csv"))
        self.assertTrue(rotate_artifacts.is_latest_name("symbols_latest.json"))
        self.assertFalse(rotate_artifacts.is_latest_name("flows_20260814.json"))


# --- D3: схемы экспорта ---------------------------------------------------

def sample_trades():
    """Две сделки: закрытая и открытая. Числа круглые, чтобы читались глазом."""
    return [
        {"track": "A1", "sym": "GRVT", "cg_id": "grvt",
         "entry_date": "2026-07-01", "exit_date": "2026-08-30",
         "entry_price": 0.2, "exit_price": 0.25, "last_price": 0.25,
         "peak_price": 0.3, "status": "closed", "close_reason": "age",
         "hold_days": 60, "ret_gross_pct": 25.0, "vol_usd_24h": 1000000.0,
         "liq_class": "medium", "ret_net_pct": 23.5},
        {"track": "A0", "sym": "MOVE", "cg_id": "movement",
         "entry_date": "2026-08-24", "exit_date": None,
         "entry_price": 0.008, "exit_price": None, "last_price": 0.009,
         "peak_price": 0.01, "status": "open", "close_reason": None,
         "hold_days": 8, "ret_gross_pct": None, "vol_usd_24h": None,
         "liq_class": "unknown", "ret_net_pct": None},
    ]


class TestTradesCsv(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="exp_")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_header_is_exactly_the_contract(self):
        info = export_trades.write_trades_csv(sample_trades(), self.root, as_of=AS_OF)
        with open(info["latest"], encoding="utf-8") as fh:
            first = fh.readline().rstrip("\r\n")
        self.assertEqual(
            "token,ts,signal,side,entry,exit,pnl_gross,pnl_net,status,cg_id", first)
        self.assertEqual(
            ["token", "ts", "signal", "side", "entry", "exit",
             "pnl_gross", "pnl_net", "status", "cg_id"],
            export_trades.TRADES_HEADER)

    def test_paths_and_row_values(self):
        info = export_trades.write_trades_csv(sample_trades(), self.root, as_of=AS_OF)
        self.assertEqual(
            os.path.join(self.root, "artifacts", "2026-09", "trades_20260901.csv"),
            info["dated"])
        self.assertEqual(os.path.join(self.root, "artifacts", "trades_latest.csv"),
                         info["latest"])
        with open(info["dated"], encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(2, len(rows))
        by_token = {r["token"]: r for r in rows}

        closed = by_token["GRVT"]
        self.assertEqual("closed", closed["status"])
        self.assertEqual("A1", closed["signal"])
        self.assertEqual("long", closed["side"])
        self.assertEqual("2026-07-01", closed["ts"])
        self.assertEqual("grvt", closed["cg_id"])
        self.assertEqual(25.0, float(closed["pnl_gross"]))
        self.assertEqual(23.5, float(closed["pnl_net"]))
        self.assertEqual(0.25, float(closed["exit"]))

        # у ОТКРЫТОЙ позиции ни pnl, ни цены выхода быть не может (§2 правил)
        op = by_token["MOVE"]
        self.assertEqual("open", op["status"])
        self.assertEqual("", op["exit"])
        self.assertEqual("", op["pnl_gross"])
        self.assertEqual("", op["pnl_net"])
        self.assertEqual("A0", op["signal"])

    def test_empty_input_gives_valid_csv(self):
        """Ноль закрытых и ноль сделок — валидный файл, а не падение."""
        info = export_trades.write_trades_csv([], self.root, as_of=AS_OF)
        self.assertEqual(0, info["n_rows"])
        with open(info["latest"], encoding="utf-8") as fh:
            content = fh.read()
        self.assertEqual(",".join(export_trades.TRADES_HEADER) + "\n", content)

    def test_meta_sidecar_carries_provenance(self):
        info = export_trades.write_trades_csv(sample_trades(), self.root, as_of=AS_OF)
        meta = os.path.splitext(info["dated"])[0] + ".meta.json"
        with open(meta, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertIn("git_sha", payload["provenance"])
        self.assertIn("generated_utc", payload["provenance"])
        self.assertEqual(export_trades.TRADES_HEADER, payload["schema"])

    def test_open_position_never_leaks_pnl_even_if_source_has_it(self):
        """Если загрузчик по ошибке положит ret в открытую — экспорт его не пустит."""
        bad = sample_trades()[1]
        bad["ret_gross_pct"] = 12.5
        bad["ret_net_pct"] = 11.0
        bad["exit_price"] = 0.009
        row = export_trades.trade_row(bad)
        self.assertEqual("", row[5])
        self.assertEqual("", row[6])
        self.assertEqual("", row[7])
        self.assertEqual("open", row[8])


class TestFlowsCsv(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="flw_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.src = os.path.join(self.root, "export", "flows_latest.json")
        write(self.src, json.dumps({
            "generated_utc": "2026-08-28T20:11:40Z",
            "top_contracts": [
                {"ticker": "GRVT", "cg_id": "grvt",
                 "run_timestamp": "2026-08-24T07:35:52Z", "score": 75,
                 "mcap_usd": 24691560, "price_usd": 0.21598, "entered_paper": True,
                 "contracts": {"ethereum": "0xaa", "binance-smart-chain": "0xbb"}},
                {"ticker": "NATIVE", "cg_id": "native-coin",
                 "run_timestamp": "2026-08-27T17:45:57Z", "score": 70,
                 "mcap_usd": None, "price_usd": 1.5, "entered_paper": False,
                 "contracts": {}},
            ],
            "blacklist": {},
        }, ensure_ascii=False))

    def test_header_is_exactly_the_contract(self):
        info = export_trades.write_flows_csv(self.root)
        self.assertEqual("ok", info["status"])
        with open(info["dst"], encoding="utf-8") as fh:
            first = fh.readline().rstrip("\r\n")
        self.assertEqual(
            "ticker,cg_id,run_timestamp,score,mcap_usd,price_usd,entered_paper,chain,contract",
            first)

    def test_one_row_per_chain_and_empty_pair_for_native(self):
        info = export_trades.write_flows_csv(self.root)
        with open(info["dst"], encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(3, len(rows))       # 2 контракта + 1 монета без контрактов
        grvt = [r for r in rows if r["ticker"] == "GRVT"]
        self.assertEqual(2, len(grvt))
        self.assertEqual(["binance-smart-chain", "ethereum"],
                         sorted(r["chain"] for r in grvt))
        self.assertEqual("true", grvt[0]["entered_paper"])
        native = [r for r in rows if r["ticker"] == "NATIVE"][0]
        self.assertEqual("", native["chain"])
        self.assertEqual("", native["contract"])
        self.assertEqual("", native["mcap_usd"])
        self.assertEqual("false", native["entered_paper"])

    def test_missing_source_is_status_not_crash(self):
        empty = tempfile.mkdtemp(prefix="flw_none_")
        self.addCleanup(shutil.rmtree, empty, True)
        info = export_trades.write_flows_csv(empty)
        self.assertIn("не найден", info["status"])
        self.assertFalse(os.path.exists(info["dst"]))


class TestCsvInjection(unittest.TestCase):
    """CSV-инъекция: чужое имя монеты не должно стать формулой Excel.

    Поля token/cg_id/ticker/chain/contract приходят из CoinGecko, то есть
    снаружи. Ячейка, начинающаяся с '=', '+', '-' или '@', в Excel и
    LibreOffice исполняется как формула — то есть чужая строка становится
    кодом на машине пилота. Вторая половина проверки не менее важна: гасить
    ЧИСЛА нельзя, иначе отрицательный pnl станет текстом и колонка перестанет
    складываться — безопасность, купленная порчей измерения, нам не нужна.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="inj_")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_помощники_гасят_текст_и_не_трогают_числа(self):
        for evil in ('=1+1', '+cmd|calc', '-2+3', '@SUM(A1)',
                     '=HYPERLINK("http://evil.invalid")'):
            self.assertTrue(export_trades._txt(evil).startswith("'"), evil)
            self.assertTrue(export_trades._num(evil).startswith("'"), evil)
        for num in ("-12.3", "-0", "+1.5", "-1.5e-3"):
            self.assertEqual(num, export_trades._txt(num), num)
            self.assertEqual(num, export_trades._num(num), num)
        # числа, пришедшие числами, идут своим путём и апострофа не видят
        self.assertEqual("-12.3", export_trades._num(-12.3))
        self.assertEqual("-5", export_trades._num(-5))
        # нормальный текст не трогаем вовсе
        self.assertEqual("GRVT", export_trades._txt("GRVT"))
        self.assertEqual("", export_trades._txt(None))
        # float() глотает 'nan'/'inf'/'1_0' — числами Excel их не считает,
        # поэтому наша проверка числа их не признаёт и гасит как текст
        self.assertEqual("'-nan", export_trades._txt("-nan"))
        self.assertEqual("'-1_0", export_trades._txt("-1_0"))

    def test_строка_сделки_гасит_token_и_cg_id_но_не_pnl(self):
        evil = [{"track": "A1", "sym": "=cmd|' /c calc'!A1", "cg_id": "@evil-coin",
                 "entry_date": "2026-07-01", "exit_date": "2026-08-30",
                 "entry_price": 0.2, "exit_price": 0.25, "status": "closed",
                 "ret_gross_pct": -12.3, "ret_net_pct": -13.4}]
        info = export_trades.write_trades_csv(evil, self.root, as_of=AS_OF)
        with open(info["dated"], encoding="utf-8", newline="") as fh:
            row = list(csv.DictReader(fh))[0]
        self.assertTrue(row["token"].startswith("'"), row["token"])
        self.assertTrue(row["cg_id"].startswith("'"), row["cg_id"])
        # текст не потерян, только обезврежен
        self.assertIn("cmd", row["token"])
        # ГЛАВНОЕ: отрицательный pnl остался числом, а не текстом
        self.assertEqual("-12.3", row["pnl_gross"])
        self.assertEqual("-13.4", row["pnl_net"])
        self.assertEqual(-12.3, float(row["pnl_gross"]))
        self.assertEqual(-13.4, float(row["pnl_net"]))

    def test_поток_гасит_ticker_chain_и_contract(self):
        write(os.path.join(self.root, "export", "flows_latest.json"),
              json.dumps({"top_contracts": [
                  {"ticker": "=HYPERLINK(1)", "cg_id": "-evil",
                   "run_timestamp": "2026-08-24T07:35:52Z", "score": 75,
                   "mcap_usd": 100, "price_usd": 0.2, "entered_paper": True,
                   "contracts": {"=chain": "+0xaa"}},
              ]}, ensure_ascii=False))
        info = export_trades.write_flows_csv(self.root)
        with open(info["dst"], encoding="utf-8", newline="") as fh:
            row = list(csv.DictReader(fh))[0]
        for col in ("ticker", "cg_id", "chain", "contract"):
            self.assertTrue(row[col].startswith("'"), (col, row[col]))
        # соседние числовые колонки не пострадали
        self.assertEqual(75.0, float(row["score"]))
        self.assertEqual(0.2, float(row["price_usd"]))

    def test_ни_одна_ячейка_обоих_файлов_не_начинает_формулу(self):
        """Подметание по всему файлу, а не по заранее известным колонкам.

        Проверка держится и если завтра в схему добавят колонку: правило
        одно — ячейка либо не начинается опасным символом, либо является
        числом (тогда это данные, а не формула).
        """
        write(os.path.join(self.root, "export", "flows_latest.json"),
              json.dumps({"top_contracts": [
                  {"ticker": "@evil", "cg_id": "=bad", "score": "-7",
                   "mcap_usd": "=1+1", "price_usd": -0.5, "entered_paper": False,
                   "contracts": {"-chain": "=0xbb"}},
              ]}, ensure_ascii=False))
        trades = [{"track": "-A1", "sym": "=evil", "cg_id": "+id",
                   "entry_date": "2026-07-01", "exit_date": "2026-08-30",
                   "entry_price": -0.2, "exit_price": "-0.25", "status": "closed",
                   "ret_gross_pct": -12.3, "ret_net_pct": -13.4}]
        report = export_trades.export_all(self.root, trades=trades,
                                          as_of=AS_OF, verbose=False)
        checked = 0
        for path in (report["trades"]["dated"], report["trades"]["latest"],
                     report["flows"]["dst"]):
            with open(path, encoding="utf-8", newline="") as fh:
                for line in csv.reader(fh):
                    for cell in line:
                        checked += 1
                        if cell[:1] in export_trades.FORMULA_LEAD_CHARS:
                            self.assertTrue(
                                export_trades._is_number_text(cell),
                                "ячейка %r в %s начинает формулу" % (cell, path))
        self.assertGreater(checked, 20, "тест обязан был реально что-то прочесть")
        # и отрицательные числа при этом уцелели именно числами
        with open(report["trades"]["latest"], encoding="utf-8", newline="") as fh:
            row = list(csv.DictReader(fh))[0]
        self.assertEqual(-0.2, float(row["entry"]))
        self.assertEqual(-0.25, float(row["exit"]))
        self.assertEqual(-12.3, float(row["pnl_gross"]))


class TestFlowsContract(unittest.TestCase):
    """С-26 (03.09.2026): контракт flows_latest.json для stage0 — отпечаток
    (LF — канон) и проверка формы уходят в meta-спутник; csv пишется всегда,
    даже если форма исходника не та (потребитель читает source_valid)."""

    GOOD = {
        "generated_utc": "2026-08-28T20:11:40Z",
        "source": "pre-BEAT screener (private repo), еженедельный экспорт",
        "week_runs": ["2026-08-24T07:35:52Z-32701845435"],
        "note": "тест",
        "top_contracts": [
            {"ticker": "GRVT", "cg_id": "grvt", "run_timestamp": "2026-08-24T07:35:52Z",
             "score": 75, "mcap_usd": 24691560, "price_usd": 0.21598,
             "entered_paper": True, "contracts": {"ethereum": "0xaa"}},
            {"ticker": "NATIVE", "cg_id": "native", "run_timestamp": "2026-08-27T17:45:57Z",
             "score": None, "mcap_usd": None, "price_usd": None,
             "entered_paper": False, "contracts": {}},
        ],
        "blacklist": {"SLX": {"reason": "разлок", "event_date": "2026-07-25"},
                      "ERA": {"reason": "дата не подтверждена", "event_date": None}},
    }

    def _root_with(self, payload):
        root = tempfile.mkdtemp(prefix="flc_")
        self.addCleanup(shutil.rmtree, root, True)
        path = os.path.join(root, "export", "flows_latest.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False, indent=1).encode("utf-8")
        with open(path, "wb") as fh:
            fh.write(data)
        return root, path

    def _meta(self, root):
        with open(os.path.join(root, "export", "flows_latest.meta.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)

    def test_real_export_validates_as_version_1(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = os.path.join(repo, "export", "flows_latest.json")
        if not os.path.exists(src):
            self.skipTest("export/flows_latest.json нет")
        with open(src, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8"))
        check = export_trades.validate_flows(data)
        self.assertEqual([], check["problems"])
        self.assertTrue(check["valid"])
        self.assertEqual(export_trades.FLOWS_SCHEMA_VERSION, check["schema_version"])

    def test_meta_carries_canonical_sha256_and_validation(self):
        root, path = self._root_with(self.GOOD)
        info = export_trades.write_flows_csv(root)
        self.assertEqual("ok", info["status"])
        self.assertTrue(info["source_valid"])
        self.assertEqual(1, info["source_schema_version"])
        self.assertEqual([], info["source_problems"])
        with open(path, "rb") as fh:
            raw = fh.read()
        meta = self._meta(root)
        self.assertEqual(hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(),
                         meta["source_sha256"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), meta["source_sha256_raw"])
        self.assertEqual(len(raw), meta["source_bytes"])
        self.assertFalse(meta["source_crlf"])
        self.assertTrue(meta["source_valid"])
        self.assertEqual([], meta["source_problems"])
        self.assertEqual(1, meta["source_schema_version"])
        self.assertEqual("2026-08-28T20:11:40Z", meta["source_generated_utc"])
        self.assertEqual("flows_latest.json", meta["source_json"])
        self.assertIn("STATE.md §11", meta["source_contract"])
        self.assertIn("git_sha", meta["provenance"])

    def test_crlf_and_lf_copies_share_the_canonical_hash(self):
        """В дереве пилота (autocrlf) файл CRLF, в git и на раннере — LF:
        канонический отпечаток обязан совпасть, сырой — честно отличаться."""
        lf = json.dumps(self.GOOD, ensure_ascii=False, indent=1).encode("utf-8")
        crlf = lf.replace(b"\n", b"\r\n")
        self.assertNotEqual(lf, crlf)
        root_lf, _ = self._root_with(lf)
        root_crlf, _ = self._root_with(crlf)
        export_trades.write_flows_csv(root_lf)
        export_trades.write_flows_csv(root_crlf)
        m_lf, m_crlf = self._meta(root_lf), self._meta(root_crlf)
        self.assertEqual(m_lf["source_sha256"], m_crlf["source_sha256"])
        self.assertNotEqual(m_lf["source_sha256_raw"], m_crlf["source_sha256_raw"])
        self.assertFalse(m_lf["source_crlf"])
        self.assertTrue(m_crlf["source_crlf"])
        self.assertEqual(m_lf["source_sha256"], m_lf["source_sha256_raw"])

    def test_broken_shape_is_reported_but_csv_still_written(self):
        bad = {"top_contracts": [{"ticker": 5, "cg_id": "", "run_timestamp": "вчера",
                                  "score": "75", "entered_paper": "yes",
                                  "contracts": ["0xaa"]}],
               "blacklist": {"X": "строка", "Y": {"reason": "r", "event_date": "25.07.2026"}},
               "extra_key": 1}
        root, _ = self._root_with(bad)
        info = export_trades.write_flows_csv(root)
        self.assertEqual("ok", info["status"])
        self.assertTrue(os.path.exists(info["dst"]))
        self.assertFalse(info["source_valid"])
        self.assertIsNone(info["source_schema_version"])
        joined = " | ".join(info["source_problems"])
        for frag in ("generated_utc", "extra_key", "cg_id", "ticker", "run_timestamp",
                     "score", "entered_paper", "contracts", "mcap_usd",
                     "blacklist['X']", "blacklist['Y']"):
            self.assertIn(frag, joined, joined)
        meta = self._meta(root)
        self.assertFalse(meta["source_valid"])
        self.assertIsNone(meta["source_schema_version"])
        self.assertEqual(info["source_problems"], meta["source_problems"])

    def test_duplicate_cg_id_and_non_dict_root_are_problems(self):
        dup = json.loads(json.dumps(self.GOOD))
        dup["top_contracts"].append(dict(dup["top_contracts"][0]))
        check = export_trades.validate_flows(dup)
        self.assertFalse(check["valid"])
        self.assertTrue(any("дубль cg_id" in p for p in check["problems"]), check["problems"])
        self.assertFalse(export_trades.validate_flows([])["valid"])
        self.assertFalse(export_trades.validate_flows(None)["valid"])
        good = export_trades.validate_flows(json.loads(json.dumps(self.GOOD)))
        self.assertTrue(good["valid"], good["problems"])

    def test_missing_source_leaves_contract_fields_empty(self):
        empty = tempfile.mkdtemp(prefix="flc_none_")
        self.addCleanup(shutil.rmtree, empty, True)
        info = export_trades.write_flows_csv(empty)
        self.assertIn("не найден", info["status"])
        self.assertIsNone(info["source_sha256"])
        self.assertIsNone(info["source_valid"])


class TestExportAll(unittest.TestCase):

    def test_export_all_works_without_trades_module(self):
        """Список сделок передан снаружи — соседний модуль не нужен вовсе."""
        root = tempfile.mkdtemp(prefix="all_")
        self.addCleanup(shutil.rmtree, root, True)
        write(os.path.join(root, "export", "flows_latest.json"),
              json.dumps({"top_contracts": []}))
        report = export_trades.export_all(root, trades=sample_trades(),
                                          as_of=AS_OF, verbose=False)
        self.assertTrue(os.path.exists(report["trades"]["latest"]))
        self.assertTrue(os.path.exists(report["flows"]["dst"]))
        self.assertEqual(2, report["trades"]["n_rows"])
        self.assertEqual(1, report["trades"]["n_closed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

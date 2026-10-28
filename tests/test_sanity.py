#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Селфтест светофора конвейера (блок E1): tests/test_sanity.py

Проверяем ровно то, ради чего светофор существует:
  * здоровый синтетический репозиторий даёт GREEN и код возврата 0;
  * протухший и обнулённый входной файл дают RED (и именно по П1);
  * открытая позиция старше 75 дней даёт RED по П5 (закрытие не сработало);
  * воронка из ОДНОЙ причины даёт FAIL по П3;
  * ссохшийся прогон ловится П2, а расхождение хеша правил — П6;
  * у КАЖДОЙ причины есть число (иначе причину нельзя перепроверить);
  * artifacts/sanity_history.csv ДОПИСЫВАЕТСЯ, а не перезаписывается —
    иначе история светофора теряется при каждом прогоне и сравнивать
    «вход/выход захода» будет не с чем.

Все тесты работают на ВРЕМЕННОМ каталоге: боевые tournament.json,
positions.json, candidates_log.jsonl и config.yaml не читаются и тем более
не пишутся.

Запуск: python -m unittest discover -s tests -v
"""

import contextlib
import csv
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sanity  # noqa: E402

# Момент, относительно которого считаются все возрасты в тестах.
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
# Та же дата для CLI: --as-of ставит момент проверки на 12:00 UTC этого дня,
# то есть ровно NOW. Тесты не зависят от того, когда их запустили.
AS_OF = "2026-09-01"

RULES_TEXT = "# Замороженные правила турнира (синтетика теста)\nверсия 1\n"

# Причины отказа из канона проекта — воронка здорового прогона.
FUNNEL_OK = {"no_perp": 900, "float_gate": 260, "low_score": 130,
             "below_top_n": 40, "in_top_not_entered": 14, "blacklist": 4,
             "enter": 2}


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def day(offset_days):
    """Дата на offset_days раньше момента проверки."""
    return (NOW.date() - timedelta(days=offset_days)).isoformat()


def make_log(path, runs):
    """Синтетический candidates_log.jsonl.

    runs: список (возраст_прогона_в_днях, {причина: сколько строк}).
    Пишем строку `_run` и по одной строке на каждую монету — ровно тот
    формат, который разбирает боевой скрипт."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"_meta": {"feature_schema_version": 4}},
                           ensure_ascii=False) + "\n")
        for age_days, reasons in runs:
            ts = iso(NOW - timedelta(days=age_days))
            run_id = "%s-test" % ts
            f.write(json.dumps({"_run": {"run_id": run_id, "timestamp_utc": ts}},
                               ensure_ascii=False) + "\n")
            n = 0
            for reason, count in reasons.items():
                for _ in range(count):
                    n += 1
                    f.write(json.dumps({
                        "run_id": run_id, "timestamp_utc": ts,
                        "ticker": "T%d" % n, "cg_id": "coin-%d" % n,
                        "decision": "enter" if reason == "enter" else "reject",
                        "reject_reason": None if reason == "enter" else reason,
                        "score": 50, "features": {"price": 1.0},
                        "features_tier": "cheap_only",
                    }, ensure_ascii=False) + "\n")


def make_repo(root, log_runs=None, open_ages=None, with_artifacts=True,
              rules_sha_in_metrics=None, metrics_age_days=0.5,
              config_text="mcap_min: 5000000\ntop_n: 15\n"):
    """Здоровый синтетический репозиторий; параметрами ломаем что нужно.

    open_ages — возраст открытых позиций дорожки A1 в днях."""
    os.makedirs(root, exist_ok=True)
    log_runs = log_runs if log_runs is not None else [
        (13.0, dict(FUNNEL_OK)), (9.5, dict(FUNNEL_OK)),
        (6.0, dict(FUNNEL_OK)), (2.5, dict(FUNNEL_OK)),
    ]
    open_ages = open_ages if open_ages is not None else [40, 12, 2]

    # --- журнал кандидатов
    make_log(os.path.join(root, "candidates_log.jsonl"), log_runs)

    # --- реестр турнира (дорожки A1..A8, закрытых нет — как на 01.09.2026)
    tracks = {}
    for code in ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8"):
        tracks[code] = {"open": [], "closed": []}
    for i, age in enumerate(open_ages):
        tracks["A1"]["open"].append({
            "sym": "SYM%d" % i, "cg_id": "coin-%d" % i,
            "entry_date": day(age), "entry_price": 1.0,
            "last_price": 1.1, "peak_price": 1.2, "miss": 0})
    with open(os.path.join(root, "tournament.json"), "w", encoding="utf-8") as f:
        json.dump({"tracks": tracks, "age_cache": {},
                   "meta": {"started": "2026-07-20", "freeze_until": "2026-11-15"}},
                  f, ensure_ascii=False)

    # --- основной бумажный портфель (дорожка A0)
    with open(os.path.join(root, "positions.json"), "w", encoding="utf-8") as f:
        json.dump({"open": [{"sym": "AAA", "cg_id": "aaa",
                             "entry_date": day(min(open_ages) if open_ages else 1),
                             "entry_price": 1.0, "last_price": 1.0,
                             "peak_price": 1.0, "miss": 0}],
                   "closed": [], "meta": {"note": "синтетика теста"}},
                  f, ensure_ascii=False)

    # --- конфиг
    with open(os.path.join(root, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(config_text)

    # --- замороженные правила
    rules_path = os.path.join(root, "screener_tournament_rules_v1.md")
    with open(rules_path, "w", encoding="utf-8", newline="") as f:
        f.write(RULES_TEXT)
    with open(rules_path, "rb") as f:
        real_sha = hashlib.sha256(f.read()).hexdigest()

    # --- артефакты чужих блоков (метрики и вердикт)
    if with_artifacts:
        art = os.path.join(root, "artifacts")
        os.makedirs(art, exist_ok=True)
        with open(os.path.join(art, "metrics_latest.json"), "w", encoding="utf-8") as f:
            json.dump({
                "provenance": {"git_sha": "deadbeef1234",
                               "generated_utc": iso(NOW - timedelta(days=metrics_age_days)),
                               "tool_version": "1.0.0", "python": "3.12.0"},
                "rules_sha256": (rules_sha_in_metrics if rules_sha_in_metrics
                                 is not None else real_sha),
                "n_closed_total": 0, "tracks": {}, "funnel": {},
                "status": "нет закрытых сделок",
            }, f, ensure_ascii=False)
        with open(os.path.join(art, "verdict_latest.json"), "w", encoding="utf-8") as f:
            json.dump({"verdict": "данных мало: N=0", "provenance": {}}, f,
                      ensure_ascii=False)
    return {"root": root, "rules_sha256": real_sha}


class SanityTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sanity_test_")
        self.repo = os.path.join(self.tmp, "repo")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args):
        """CLI светофора с проглоченной печатью: возвращаем только код возврата.
        Печать самого скрипта в выводе тестов только мешает читать результат."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = sanity.main(list(args))
        self.assertIn("ИТОГ:", buf.getvalue())
        return code

    def by_id(self, result, cid):
        for c in result["checks"]:
            if c["id"] == cid:
                return c
        raise AssertionError("нет проверки %s" % cid)


class TestHealthyRepo(SanityTestBase):
    """Здоровый набор обязан давать GREEN — иначе светофор бесполезен:
    красный, который горит всегда, перестают читать."""

    def test_green(self):
        make_repo(self.repo)
        res = sanity.run_sanity(self.repo, now=NOW)
        self.assertEqual(res["verdict"], sanity.GREEN,
                         "здоровый репозиторий дал не GREEN: %s" % res["reasons"])
        self.assertEqual(res["counts"]["fail"], 0)
        self.assertEqual(res["counts"]["warn"], 0)
        self.assertEqual(res["reasons"], [])
        self.assertEqual(len(res["checks"]), 6)

    def test_exit_code_zero_and_artifacts_written(self):
        make_repo(self.repo)
        code = self.run_cli("--repo", self.repo, "--as-of", AS_OF)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "artifacts",
                                                    "sanity_latest.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "artifacts",
                                                    "sanity_history.csv")))

    def test_reads_only_its_own_artifacts(self):
        """Светофор не имеет права трогать данные проекта: сверяем побайтно."""
        make_repo(self.repo)
        watched = ["tournament.json", "positions.json", "candidates_log.jsonl",
                   "config.yaml", "screener_tournament_rules_v1.md",
                   os.path.join("artifacts", "metrics_latest.json"),
                   os.path.join("artifacts", "verdict_latest.json")]
        before = {}
        for name in watched:
            with open(os.path.join(self.repo, name), "rb") as f:
                before[name] = hashlib.sha256(f.read()).hexdigest()
        self.run_cli("--repo", self.repo, "--as-of", AS_OF)
        for name in watched:
            with open(os.path.join(self.repo, name), "rb") as f:
                after = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(before[name], after, "светофор изменил %s" % name)


class TestInputFiles(SanityTestBase):
    """П1: устаревший или обнулённый вход = RED."""

    def test_stale_log_is_red(self):
        # последний прогон 20 дней назад: при расписании пн/чт это минимум
        # четыре пропущенных прогона подряд
        make_repo(self.repo, log_runs=[(31.0, dict(FUNNEL_OK)),
                                       (27.0, dict(FUNNEL_OK)),
                                       (24.0, dict(FUNNEL_OK)),
                                       (20.0, dict(FUNNEL_OK))])
        res = sanity.run_sanity(self.repo, now=NOW)
        self.assertEqual(res["verdict"], sanity.RED)
        p1 = self.by_id(res, "П1")
        self.assertEqual(p1["status"], sanity.FAIL)
        self.assertTrue(any("candidates_log.jsonl" in r for r in p1["reasons"]))
        self.assertEqual(self.run_cli("--repo", self.repo, "--as-of", AS_OF), 1)

    def test_zero_size_input_is_red(self):
        make_repo(self.repo)
        open(os.path.join(self.repo, "tournament.json"), "w").close()
        res = sanity.run_sanity(self.repo, now=NOW)
        self.assertEqual(res["verdict"], sanity.RED)
        p1 = self.by_id(res, "П1")
        self.assertEqual(p1["status"], sanity.FAIL)
        self.assertTrue(any("нулевой размер" in r for r in p1["reasons"]),
                        p1["reasons"])

    def test_missing_input_is_red(self):
        make_repo(self.repo)
        os.remove(os.path.join(self.repo, "positions.json"))
        res = sanity.run_sanity(self.repo, now=NOW)
        self.assertEqual(res["verdict"], sanity.RED)
        self.assertEqual(self.by_id(res, "П1")["status"], sanity.FAIL)

    def test_broken_json_is_red(self):
        make_repo(self.repo)
        with open(os.path.join(self.repo, "tournament.json"), "w",
                  encoding="utf-8") as f:
            f.write("{это не json")
        res = sanity.run_sanity(self.repo, now=NOW)
        self.assertEqual(res["verdict"], sanity.RED)
        self.assertEqual(self.by_id(res, "П1")["status"], sanity.FAIL)

    def test_old_config_is_not_a_problem(self):
        """Конфиг заморожен до 15.11 — его возраст НЕ повод для тревоги."""
        make_repo(self.repo)
        cfg = os.path.join(self.repo, "config.yaml")
        old = (NOW - timedelta(days=200)).timestamp()
        os.utime(cfg, (old, old))
        res = sanity.run_sanity(self.repo, now=NOW)
        self.assertEqual(res["verdict"], sanity.GREEN, res["reasons"])
        self.assertFalse(
            self.by_id(res, "П1")["details"]["thresholds"]["config_age_checked"])


class TestRunSize(SanityTestBase):
    """П2: ссохшийся прогон против медианы последних."""

    def test_shrunk_run_is_fail(self):
        small = {"no_perp": 200, "float_gate": 60, "low_score": 30,
                 "below_top_n": 10, "enter": 1}     # ~301 строка против ~1350
        make_repo(self.repo, log_runs=[(13.0, dict(FUNNEL_OK)),
                                       (9.5, dict(FUNNEL_OK)),
                                       (6.0, dict(FUNNEL_OK)),
                                       (2.5, small)])
        res = sanity.run_sanity(self.repo, now=NOW)
        p2 = self.by_id(res, "П2")
        self.assertEqual(p2["status"], sanity.FAIL, p2["details"])
        self.assertEqual(res["verdict"], sanity.RED)
        self.assertLess(p2["details"]["ratio"], sanity.SHRINK_FAIL)

    def test_short_history_does_not_alarm(self):
        """Двух прогонов мало для медианы — молчим, а не выдумываем норму."""
        make_repo(self.repo, log_runs=[(6.0, dict(FUNNEL_OK)),
                                       (2.5, {"no_perp": 100, "enter": 1})])
        res = sanity.run_sanity(self.repo, now=NOW)
        p2 = self.by_id(res, "П2")
        self.assertEqual(p2["status"], sanity.PASS)
        self.assertEqual(p2["details"]["history_n"], 1)


class TestFunnel(SanityTestBase):
    """П3: воронка, схлопнувшаяся до одной причины."""

    def test_single_reason_is_fail(self):
        make_repo(self.repo, log_runs=[(13.0, dict(FUNNEL_OK)),
                                       (9.5, dict(FUNNEL_OK)),
                                       (6.0, dict(FUNNEL_OK)),
                                       (2.5, {"no_perp": 1350})])
        res = sanity.run_sanity(self.repo, now=NOW)
        p3 = self.by_id(res, "П3")
        self.assertEqual(p3["status"], sanity.FAIL, p3["details"])
        self.assertEqual(p3["details"]["reasons_n"], 1)
        self.assertEqual(res["verdict"], sanity.RED)
        self.assertTrue(any("no_perp" in r and "1350" in r for r in p3["reasons"]),
                        p3["reasons"])

    def test_concentration_is_warn(self):
        make_repo(self.repo, log_runs=[(13.0, dict(FUNNEL_OK)),
                                       (9.5, dict(FUNNEL_OK)),
                                       (6.0, dict(FUNNEL_OK)),
                                       (2.5, {"no_perp": 1330, "enter": 20})])
        res = sanity.run_sanity(self.repo, now=NOW)
        p3 = self.by_id(res, "П3")
        self.assertEqual(p3["status"], sanity.WARN, p3["details"])
        self.assertEqual(res["verdict"], sanity.YELLOW)

    def test_healthy_funnel_passes(self):
        make_repo(self.repo)
        p3 = self.by_id(sanity.run_sanity(self.repo, now=NOW), "П3")
        self.assertEqual(p3["status"], sanity.PASS)
        self.assertEqual(p3["details"]["reasons_n"], len(FUNNEL_OK))


class TestArtifacts(SanityTestBase):
    """П4: артефакты чужих блоков."""

    def test_missing_artifacts_are_yellow_not_red(self):
        """Так выглядит 01.09.2026: artifacts/ создаётся впервые."""
        make_repo(self.repo, with_artifacts=False)
        res = sanity.run_sanity(self.repo, now=NOW)
        self.assertEqual(res["verdict"], sanity.YELLOW, res["reasons"])
        self.assertEqual(self.by_id(res, "П4")["status"], sanity.WARN)
        self.assertEqual(self.run_cli("--repo", self.repo, "--as-of", AS_OF), 0)

    def test_broken_metrics_is_red(self):
        make_repo(self.repo)
        with open(os.path.join(self.repo, "artifacts", "metrics_latest.json"),
                  "w", encoding="utf-8") as f:
            f.write("{обрыв")
        res = sanity.run_sanity(self.repo, now=NOW)
        self.assertEqual(res["verdict"], sanity.RED)
        self.assertEqual(self.by_id(res, "П4")["status"], sanity.FAIL)

    def test_stale_metrics_is_warn(self):
        make_repo(self.repo, metrics_age_days=12)
        res = sanity.run_sanity(self.repo, now=NOW)
        p4 = self.by_id(res, "П4")
        self.assertEqual(p4["status"], sanity.WARN)
        self.assertTrue(any("несвежий" in r for r in p4["reasons"]), p4["reasons"])


class TestOpenPositions(SanityTestBase):
    """П5: закрытие позиций."""

    def test_position_older_than_75_days_is_red(self):
        make_repo(self.repo, open_ages=[80, 12, 2])
        res = sanity.run_sanity(self.repo, now=NOW)
        p5 = self.by_id(res, "П5")
        self.assertEqual(p5["status"], sanity.FAIL, p5["details"])
        self.assertEqual(res["verdict"], sanity.RED)
        self.assertEqual(p5["details"]["max_age_days"], 80)
        self.assertEqual(p5["details"]["over_fail_n"], 1)
        self.assertTrue(any("80" in r for r in p5["reasons"]), p5["reasons"])
        self.assertEqual(self.run_cli("--repo", self.repo, "--as-of", AS_OF), 1)

    def test_position_between_65_and_75_is_warn(self):
        make_repo(self.repo, open_ages=[70, 2])
        res = sanity.run_sanity(self.repo, now=NOW)
        p5 = self.by_id(res, "П5")
        self.assertEqual(p5["status"], sanity.WARN, p5["details"])
        self.assertEqual(res["verdict"], sanity.YELLOW)

    def test_position_at_62_days_is_fine(self):
        """60 дней + разрыв прогонов = законный возраст, тревоги быть не должно."""
        make_repo(self.repo, open_ages=[62, 2])
        p5 = self.by_id(sanity.run_sanity(self.repo, now=NOW), "П5")
        self.assertEqual(p5["status"], sanity.PASS, p5["details"])

    def test_miss_overflow_is_warn(self):
        make_repo(self.repo)
        path = os.path.join(self.repo, "tournament.json")
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        obj["tracks"]["A1"]["open"][0]["miss"] = 4
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        p5 = self.by_id(sanity.run_sanity(self.repo, now=NOW), "П5")
        self.assertEqual(p5["status"], sanity.WARN)
        self.assertTrue(any("miss=4" in r for r in p5["reasons"]), p5["reasons"])


class TestRulesHash(SanityTestBase):
    """П6: хеш замороженных правил."""

    def test_mismatch_is_red(self):
        make_repo(self.repo, rules_sha_in_metrics="0" * 64)
        res = sanity.run_sanity(self.repo, now=NOW)
        p6 = self.by_id(res, "П6")
        self.assertEqual(p6["status"], sanity.FAIL)
        self.assertEqual(res["verdict"], sanity.RED)
        self.assertTrue(any("РАСХОЖДЕНИЕ" in r for r in p6["reasons"]))

    def test_match_passes(self):
        made = make_repo(self.repo)
        p6 = self.by_id(sanity.run_sanity(self.repo, now=NOW), "П6")
        self.assertEqual(p6["status"], sanity.PASS)
        self.assertEqual(p6["details"]["sha256_now"], made["rules_sha256"])

    def test_no_metrics_means_warn_not_red(self):
        make_repo(self.repo, with_artifacts=False)
        p6 = self.by_id(sanity.run_sanity(self.repo, now=NOW), "П6")
        self.assertEqual(p6["status"], sanity.WARN)

    def test_missing_rules_with_recorded_hash_is_red(self):
        """Файл, на который сослался артефакт, исчез — это пропажа, не «ещё нет»."""
        make_repo(self.repo)
        os.remove(os.path.join(self.repo, "screener_tournament_rules_v1.md"))
        res = sanity.run_sanity(self.repo, now=NOW)
        self.assertEqual(self.by_id(res, "П6")["status"], sanity.FAIL)
        self.assertEqual(res["verdict"], sanity.RED)


class TestReasonsCarryNumbers(SanityTestBase):
    """У каждой причины обязано быть числовое подтверждение: «что-то не так»
    нельзя ни проверить, ни опровергнуть."""

    def test_every_reason_has_a_digit(self):
        cases = [
            dict(open_ages=[80, 2]),
            dict(with_artifacts=False),
            dict(rules_sha_in_metrics="0" * 64),
            dict(log_runs=[(13.0, dict(FUNNEL_OK)), (9.5, dict(FUNNEL_OK)),
                           (6.0, dict(FUNNEL_OK)), (2.5, {"no_perp": 1350})]),
            dict(metrics_age_days=12),
        ]
        for i, kwargs in enumerate(cases):
            root = os.path.join(self.tmp, "repo_%d" % i)
            make_repo(root, **kwargs)
            res = sanity.run_sanity(root, now=NOW)
            self.assertTrue(res["reasons"], "случай %d не дал ни одной причины" % i)
            for reason in res["reasons"]:
                self.assertTrue(any(ch.isdigit() for ch in reason),
                                "причина без числа: %s" % reason)


class TestHistoryCsv(SanityTestBase):
    """История светофора должна ДОПИСЫВАТЬСЯ: сравнение вход/выход захода
    возможно, только если прошлые строки на месте."""

    def test_history_appends_and_keeps_single_header(self):
        make_repo(self.repo)
        hist = os.path.join(self.repo, "artifacts", "sanity_history.csv")
        for _ in range(3):
            res = sanity.run_sanity(self.repo, now=NOW)
            sanity.write_artifacts(res, self.repo)
        with open(hist, encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows), 4, "ожидались шапка + 3 строки, получено %d" % len(rows))
        self.assertEqual(rows[0], sanity.HISTORY_HEADER)
        for row in rows[1:]:
            self.assertEqual(len(row), len(sanity.HISTORY_HEADER))
            self.assertEqual(row[1], sanity.GREEN)

    def test_history_survives_verdict_change(self):
        """Старые строки не переписываются новым вердиктом."""
        make_repo(self.repo)
        sanity.write_artifacts(sanity.run_sanity(self.repo, now=NOW), self.repo)
        open(os.path.join(self.repo, "positions.json"), "w").close()
        sanity.write_artifacts(sanity.run_sanity(self.repo, now=NOW), self.repo)
        with open(os.path.join(self.repo, "artifacts", "sanity_history.csv"),
                  encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][1], sanity.GREEN)
        self.assertEqual(rows[2][1], sanity.RED)

    def test_reasons_cell_has_no_newlines(self):
        """Многострочная причина разъехалась бы по строкам csv."""
        make_repo(self.repo, open_ages=[80, 2], with_artifacts=False)
        sanity.write_artifacts(sanity.run_sanity(self.repo, now=NOW), self.repo)
        with open(os.path.join(self.repo, "artifacts", "sanity_history.csv"),
                  encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows), 2)
        self.assertNotIn("\n", rows[1][-1])
        self.assertTrue(rows[1][-1])

    def test_latest_json_is_valid_and_stamped(self):
        make_repo(self.repo)
        res = sanity.run_sanity(self.repo, now=NOW)
        sanity.write_artifacts(res, self.repo)
        with open(os.path.join(self.repo, "artifacts", "sanity_latest.json"),
                  encoding="utf-8") as f:
            obj = json.load(f)
        self.assertIn("git_sha", obj["provenance"])
        self.assertIn("generated_utc", obj["provenance"])
        self.assertEqual(obj["verdict"], sanity.GREEN)
        self.assertEqual(len(obj["checks"]), 6)


class TestCheckLogIntegration(SanityTestBase):
    """check_log.py обязан честно печатать итог светофора — или его отсутствие."""

    def test_sanity_line_without_artifact(self):
        import check_log
        line = check_log.sanity_line(os.path.join(self.tmp, "нет-такого.json"))
        self.assertIn("НЕ ПРОГОНЯЛСЯ", line)

    def test_sanity_line_reads_artifact(self):
        import check_log
        make_repo(self.repo)
        res = sanity.run_sanity(self.repo, now=NOW)
        sanity.write_artifacts(res, self.repo)
        line = check_log.sanity_line(os.path.join(self.repo, "artifacts",
                                                  "sanity_latest.json"))
        self.assertIn("GREEN", line)
        self.assertIn("FAIL=0", line)

    def test_sanity_line_on_broken_artifact(self):
        import check_log
        os.makedirs(os.path.join(self.repo, "artifacts"), exist_ok=True)
        path = os.path.join(self.repo, "artifacts", "sanity_latest.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{обрыв")
        line = check_log.sanity_line(path)
        self.assertIn("не читается", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)

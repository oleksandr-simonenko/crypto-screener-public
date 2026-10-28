#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Селфтест воркфлоу GitHub Actions: tests/test_workflows.py (С-22/С-23, 02.09.2026).

Воркфлоу — такой же код проекта, как .py: он решает, что попадёт в репозиторий
и что придёт пилоту в Telegram. До этого файла его проверял только парсер yml
из отчёта пакета, и то руками. Здесь закреплено то, что уже ломалось или
предупреждалось GitHub:

  * С-23 — «Node.js 20 is deprecated» (прогон #5 funding-logger): все
    воркфлоу обязаны сидеть на мажорах действий с Node 24 — actions/checkout
    v5 и actions/setup-python v6 (сверено 02.09.2026 по README и релизам
    действий: checkout v5.0.0 «now supports Node.js 24», setup-python v6.0.0
    «Upgrade to node 24»; оба требуют раннер >= v2.327.1, hosted ubuntu-latest
    это условие выполняет). Новое действие или новый мажор — только через
    правку EXPECTED_MAJORS здесь, осознанно.
  * С-20 — токен Telegram только через env: в тексте `run:` секретов нет,
    `set -x` нет.
  * С-22 — funding.yml: шаг среза имеет id snap и получает токен/чат для ИНФО,
    шаг тревоги стоит под `if: failure() || cancelled()` (коды 1/2 и job,
    убитый по таймауту — ревью 03.09) и читает число недобранных часов И ПАР
    (С-24) из outputs шага snap; отдельного шага «ИНФО при успехе» в воркфлоу
    нет — его шлёт сам скрипт. Шаг коммита имеет id commit: тревога говорит,
    если push не прошёл.

Запуск: python -m unittest discover -s tests -v
"""

import glob
import io
import os
import re
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Public showcase: the production workflows live in ci/workflows (reference copies,
# not scheduled here); only tests.yml is active in .github/workflows.
WF_DIR = os.path.join(ROOT, "ci", "workflows")
if not os.path.isdir(WF_DIR):
    WF_DIR = os.path.join(ROOT, ".github", "workflows")

# Единственные внешние действия проекта и их мажоры с Node 24 (С-23).
EXPECTED_MAJORS = {
    "actions/checkout": 5,
    "actions/setup-python": 6,
}

_USES_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)@v(?P<major>\d+)(\.\d+)*$")


def _read(path):
    with io.open(path, "rb") as fh:
        return fh.read().decode("utf-8")


def _load(path):
    return yaml.safe_load(_read(path))


def _workflows():
    paths = sorted(glob.glob(os.path.join(WF_DIR, "*.yml")))
    assert paths, "в .github/workflows нет ни одного yml"
    return paths


def _steps(wf):
    for job_name, job in (wf.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            yield job_name, step


class TestActionVersions(unittest.TestCase):
    """С-23: мажоры действий с Node 24 во ВСЕХ воркфлоу."""

    def test_all_workflows_parse(self):
        for path in _workflows():
            wf = _load(path)
            self.assertIsInstance(wf, dict, path)
            self.assertIn("jobs", wf, "%s: нет jobs" % path)

    def test_every_uses_is_a_known_action_on_node24_major(self):
        problems = []
        seen = set()
        for path in _workflows():
            for job_name, step in _steps(_load(path)):
                uses = step.get("uses")
                if not uses:
                    continue
                m = _USES_RE.match(str(uses).strip())
                if not m:
                    problems.append("%s/%s: непонятная ссылка uses=%r"
                                    % (os.path.basename(path), job_name, uses))
                    continue
                name, major = m.group("name"), int(m.group("major"))
                seen.add(name)
                if name not in EXPECTED_MAJORS:
                    problems.append("%s/%s: действие %s не в списке разрешённых"
                                    % (os.path.basename(path), job_name, name))
                elif major != EXPECTED_MAJORS[name]:
                    problems.append("%s/%s: %s@v%d, ожидался v%d (Node 24)"
                                    % (os.path.basename(path), job_name, name, major,
                                       EXPECTED_MAJORS[name]))
        self.assertEqual([], problems, "\n  " + "\n  ".join(problems))
        self.assertEqual(set(EXPECTED_MAJORS), seen,
                         "ожидались ровно эти действия: %s" % sorted(EXPECTED_MAJORS))

    def test_gate_runs_on_workflow_changes(self):
        """Ревью 02.09: сторож бесполезен, если гейт не бежит на правку yml.
        PyYAML читает ключ `on` как булево True — учитываем оба варианта."""
        wf = _load(os.path.join(WF_DIR, "tests.yml"))
        on = wf.get("on", wf.get(True)) or {}
        paths = ((on.get("push") or {}).get("paths")) or []
        self.assertIn(".github/workflows/*.yml", paths,
                      "tests.yml обязан запускаться при изменении любого воркфлоу")
        self.assertIn("**/*.py", paths)

    def test_no_stale_majors_left_in_text(self):
        """Строковая проверка поверх разбора: старый мажор не должен остаться
        даже в закомментированной строке — иначе его скопируют обратно."""
        for path in _workflows():
            text = _read(path)
            self.assertNotIn("actions/checkout@v4", text, path)
            self.assertNotIn("actions/setup-python@v5", text, path)


class TestSecretsStayInEnv(unittest.TestCase):
    """С-20: секрет живёт только в env шага; в тексте run его нет; set -x нет."""

    def test_no_secret_expression_inside_run(self):
        problems = []
        for path in _workflows():
            for job_name, step in _steps(_load(path)):
                run = step.get("run")
                if run and "secrets." in str(run):
                    problems.append("%s/%s: `secrets.` внутри run"
                                    % (os.path.basename(path), job_name))
        self.assertEqual([], problems, "\n  " + "\n  ".join(problems))

    def test_no_set_x(self):
        for path in _workflows():
            self.assertNotIn("set -x", _read(path), path)

    def test_telegram_calls_print_only_http_code(self):
        """Каждый curl к Telegram: -o /dev/null и -w "%{http_code}" — тело
        ответа (может цитировать URL с токеном) в лог не попадает."""
        for path in _workflows():
            for job_name, step in _steps(_load(path)):
                run = str(step.get("run") or "")
                if "api.telegram.org" not in run:
                    continue
                self.assertIn("-o /dev/null", run, "%s/%s" % (path, job_name))
                self.assertIn('-w "%{http_code}"', run, "%s/%s" % (path, job_name))
                self.assertIn("--max-time", run, "%s/%s" % (path, job_name))


class TestFundingWorkflowOutcomes(unittest.TestCase):
    """С-22: три исхода логгера отражены в funding.yml."""

    def setUp(self):
        self.path = os.path.join(WF_DIR, "funding.yml")
        self.wf = _load(self.path)
        self.steps = [s for _j, s in _steps(self.wf)]

    def _step(self, predicate, what):
        found = [s for s in self.steps if predicate(s)]
        self.assertEqual(1, len(found), "%s: найдено %d, ожидался один" % (what, len(found)))
        return found[0]

    def test_snapshot_step_has_id_and_telegram_env(self):
        snap = self._step(lambda s: str(s.get("run") or "").strip() == "python funding_logger.py",
                          "шаг среза")
        self.assertEqual(snap.get("id"), "snap", "шаг среза обязан иметь id snap для outputs")
        env = snap.get("env") or {}
        self.assertEqual(env.get("TELEGRAM_BOT_TOKEN"), "${{ secrets.TELEGRAM_BOT_TOKEN }}")
        self.assertEqual(env.get("TELEGRAM_CHAT_ID"), "${{ secrets.TELEGRAM_CHAT_ID }}")

    ALARM_IF = "failure() || cancelled()"

    def test_alarm_step_only_on_failure_and_reads_outputs(self):
        alarm = self._step(lambda s: "api.telegram.org" in str(s.get("run") or ""),
                           "шаг тревоги")
        self.assertEqual(str(alarm.get("if")).strip(), self.ALARM_IF,
                         "тревога — на кодах 1/2 (failure) и на убитом по таймауту job "
                         "(cancelled), не на успехе")
        env = alarm.get("env") or {}
        self.assertEqual(env.get("OUTCOME"), "${{ steps.snap.outputs.outcome }}")
        self.assertEqual(env.get("UNFILLED_HOURS"), "${{ steps.snap.outputs.unfilled_hours }}")
        self.assertEqual(env.get("GAP_HOURS"), "${{ steps.snap.outputs.gap_hours }}")
        self.assertEqual(env.get("SUMMARY"), "${{ steps.snap.outputs.summary }}")
        # С-24: тревога называет пары и монеты, а не только часы.
        self.assertEqual(env.get("PAIRS_MISSING"), "${{ steps.snap.outputs.pairs_missing }}")
        self.assertEqual(env.get("PAIRS_UNRESOLVED"), "${{ steps.snap.outputs.pairs_unresolved }}")
        self.assertEqual(env.get("COINS_FAILED"), "${{ steps.snap.outputs.coins_failed }}")
        self.assertEqual(env.get("COINS_FAILED_NAMES"),
                         "${{ steps.snap.outputs.coins_failed_names }}")
        self.assertEqual(env.get("SNAP_STATUS"), "${{ steps.snap.outcome }}")
        self.assertEqual(env.get("COMMIT_STATUS"), "${{ steps.commit.outcome }}")
        run = str(alarm.get("run"))
        for branch in ("partial)", "error)", "ok|healed)", "*)"):
            self.assertIn(branch, run, "в тревоге нет ветки %s" % branch)
        self.assertIn("${UNFILLED_HOURS}", run, "тревога обязана называть число недобранных часов")
        self.assertIn("${PAIRS_UNRESOLVED}", run, "тревога обязана называть число недобранных пар")
        self.assertIn("${COINS_FAILED_NAMES", run, "тревога обязана называть монеты")
        self.assertIn("cancelled", run, "ветка убитого по таймауту job")
        self.assertIn("КОММИТ/PUSH НЕ ПРОШЁЛ", run, "тревога обязана сказать про упавший push")
        self.assertIn("--data-urlencode", run, "строка итога — данные, уходит с urlencode")
        self.assertNotIn("secrets.", run)

    def test_no_separate_success_info_step(self):
        """ИНФО о залеченном пропуске шлёт сам скрипт — второго канала в yml нет,
        иначе пилот получал бы сообщение дважды."""
        for s in self.steps:
            cond = str(s.get("if") or "")
            self.assertNotIn("success()", cond, "лишний шаг на успехе: %s" % s.get("name"))
            if "api.telegram.org" in str(s.get("run") or ""):
                self.assertEqual(cond.strip(), self.ALARM_IF)

    def test_commit_step_still_runs_always(self):
        commit = self._step(lambda s: "git commit" in str(s.get("run") or ""), "шаг коммита")
        self.assertEqual(str(commit.get("if")).strip(), "always()")
        self.assertEqual(commit.get("id"), "commit", "тревога читает steps.commit.outcome")

    def test_economy_mode_is_only_a_comment(self):
        """С-25: заготовка режима экономии лежит комментарием — действующий cron
        по-прежнему почасовой, пока навигатор не сказал иначе."""
        on = self.wf.get("on", self.wf.get(True)) or {}
        crons = [c.get("cron") for c in (on.get("schedule") or [])]
        self.assertEqual(crons, ["7 * * * *"])
        self.assertIn('"7 */2 * * *"', _read(self.path), "заготовка cron должна быть в комментарии")


if __name__ == "__main__":
    unittest.main(verbosity=2)

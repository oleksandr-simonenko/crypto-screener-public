#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_provenance.py — селфтест блоков E2 и C3.

Проверяем ровно то, на что опираются остальные модули пакета:
  * stamp() отдаёт все четыре обязательных ключа в нужном формате;
  * git_sha() возвращает строку и НЕ ПАДАЕТ ни при каких обстоятельствах —
    ни без переменной окружения, ни без git в системе, ни при мусоре в env;
  * rules_sha256 стабилен между вызовами и меняется от ЛЮБОЙ правки файла
    (это и есть смысл заморозки правил: подмена обязана быть видна).

Запуск из корня репозитория:
    python -m unittest discover -s tests -v
    python -m unittest tests.test_provenance -v
"""

import hashlib
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime

# Тесты лежат в tests/, а модули — в корне репозитория. Добавляем корень в
# путь импорта явно, чтобы тест работал и из корня, и из каталога tests.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import provenance  # noqa: E402
import rules_hash  # noqa: E402

_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PY_VER = re.compile(r"^3\.\d+\.\d+$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class TestStamp(unittest.TestCase):
    """stamp() — штамп, который уходит в каждый артефакт."""

    def test_keys_present(self):
        st = provenance.stamp()
        self.assertIsInstance(st, dict)
        for key in ("git_sha", "generated_utc", "tool_version", "python"):
            self.assertIn(key, st, "в штампе нет обязательного ключа %r" % key)
            self.assertIsInstance(st[key], str)
            self.assertTrue(st[key], "ключ %r пустой" % key)

    def test_no_extra_surprises(self):
        # Лишние ключи не запрещены, но набор должен быть предсказуем:
        # артефакты читает и человек, и diff_metrics.
        st = provenance.stamp()
        self.assertEqual(
            set(st.keys()),
            {"git_sha", "generated_utc", "tool_version", "python"},
        )

    def test_generated_utc_format(self):
        st = provenance.stamp()
        self.assertRegex(st["generated_utc"], _ISO_Z)
        # И это действительно разбираемая дата, а не похожая строка.
        datetime.strptime(st["generated_utc"], "%Y-%m-%dT%H:%M:%SZ")

    def test_python_version_format(self):
        st = provenance.stamp()
        self.assertRegex(st["python"], _PY_VER)
        self.assertTrue(st["python"].startswith("3."))

    def test_tool_version_override(self):
        st = provenance.stamp(tool_version="9.9.9-test")
        self.assertEqual(st["tool_version"], "9.9.9-test")


class TestGitSha(unittest.TestCase):
    """git_sha() обязан быть безотказным: артефакт важнее провенанса."""

    def setUp(self):
        self._saved_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def test_returns_nonempty_string(self):
        sha = provenance.git_sha()
        self.assertIsInstance(sha, str)
        self.assertTrue(sha)

    def test_shape_is_sha_or_unknown(self):
        sha = provenance.git_sha()
        if sha == provenance.UNKNOWN:
            return
        self.assertEqual(len(sha), provenance.SHA_SHORT_LEN)
        self.assertRegex(sha, r"^[0-9a-f]+$")

    def test_env_github_sha_wins(self):
        os.environ["GITHUB_SHA"] = "A" * 40
        self.assertEqual(provenance.git_sha_full(), "a" * 40)
        self.assertEqual(provenance.git_sha(), "a" * provenance.SHA_SHORT_LEN)

    def test_garbage_env_is_rejected_not_propagated(self):
        # Мусор из чужого окружения не должен попасть в артефакт как «sha».
        os.environ["GITHUB_SHA"] = "не-хеш-а-текст"
        sha = provenance.git_sha()
        self.assertNotIn("не-хеш", sha)
        if sha != provenance.UNKNOWN:
            self.assertRegex(sha, r"^[0-9a-f]+$")

    def test_no_git_no_env_gives_unknown_and_does_not_raise(self):
        # Худший случай: переменной нет, git недоступен (пустой PATH).
        os.environ.pop("GITHUB_SHA", None)
        os.environ["PATH"] = ""
        try:
            sha = provenance.git_sha()
        except Exception as exc:  # pragma: no cover — именно этого быть не должно
            self.fail("git_sha() упал без git и без env: %r" % exc)
        self.assertIsInstance(sha, str)
        self.assertTrue(sha)

    def test_stamp_survives_broken_environment(self):
        os.environ.pop("GITHUB_SHA", None)
        os.environ["PATH"] = ""
        st = provenance.stamp()
        self.assertIn("git_sha", st)
        self.assertTrue(st["git_sha"])


class TestRulesSha(unittest.TestCase):
    """C3: отпечаток замороженных правил."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rules_hash_test_")
        self.path = os.path.join(self.tmpdir, "rules_sample.md")
        # bytes-литерал не умеет кириллицу — пишем текст и кодируем явно в utf-8.
        self._write("# ПРАВИЛА\nблок = токен\n".encode("utf-8"))

    def tearDown(self):
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
            os.rmdir(self.tmpdir)
        except OSError:
            pass

    def _write(self, data):
        with open(self.path, "wb") as f:
            f.write(data)

    def test_hex_shape(self):
        self.assertRegex(rules_hash.rules_sha256(self.path), _HEX64)

    def test_stable_between_calls(self):
        first = rules_hash.rules_sha256(self.path)
        second = rules_hash.rules_sha256(self.path)
        self.assertEqual(first, second)

    def test_matches_hashlib_on_raw_bytes(self):
        # Хеш обязан считаться от БАЙТОВ: никакой нормализации переводов строк.
        with open(self.path, "rb") as f:
            expected = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(rules_hash.rules_sha256(self.path), expected)

    def test_changes_when_content_changes(self):
        before = rules_hash.rules_sha256(self.path)
        self._write("# ПРАВИЛА\nблок = прогон\n".encode("utf-8"))
        after = rules_hash.rules_sha256(self.path)
        self.assertNotEqual(before, after, "правки файла не видно по хешу")

    def test_changes_on_single_byte_edit(self):
        before = rules_hash.rules_sha256(self.path)
        self._write("# ПРАВИЛА\nблок = токен\n ".encode("utf-8"))  # добавлен пробел
        self.assertNotEqual(before, rules_hash.rules_sha256(self.path))

    def test_crlf_gives_different_hash(self):
        # Явно фиксируем известную ловушку: CRLF-копия того же текста — другой хеш.
        self._write("# ПРАВИЛА\nблок = токен\n".encode("utf-8"))
        lf = rules_hash.rules_sha256(self.path)
        self._write("# ПРАВИЛА\r\nблок = токен\r\n".encode("utf-8"))
        self.assertNotEqual(lf, rules_hash.rules_sha256(self.path))
        self.assertTrue(rules_hash.has_crlf(self.path))

    def test_missing_file_raises(self):
        missing = os.path.join(self.tmpdir, "нет-такого-файла.md")
        with self.assertRaises(FileNotFoundError):
            rules_hash.rules_sha256(missing)
        self.assertIsNone(rules_hash.rules_sha256_or_none(missing))

    def test_info_fields(self):
        info = rules_hash.rules_info(self.path)
        self.assertEqual(set(info.keys()), {"path", "sha256", "size_bytes", "crlf"})
        self.assertEqual(info["size_bytes"], os.path.getsize(self.path))
        self.assertRegex(info["sha256"], _HEX64)


class TestRepoRulesCopy(unittest.TestCase):
    """Копия правил обязана лежать в самом репозитории скринера (пункт C3)."""

    def test_rules_file_present_and_hashable(self):
        path = os.path.join(_REPO_ROOT, rules_hash.DEFAULT_RULES_FILENAME)
        self.assertTrue(
            os.path.isfile(path),
            "нет копии замороженных правил в корне репозитория: %s" % path,
        )
        self.assertGreater(os.path.getsize(path), 1000, "файл правил подозрительно мал")
        self.assertRegex(rules_hash.rules_sha256(path), _HEX64)

    def test_default_path_resolves_from_tests_dir(self):
        # Вызов без аргумента должен находить правила, даже когда cwd — tests/.
        saved = os.getcwd()
        try:
            os.chdir(os.path.dirname(os.path.abspath(__file__)))
            self.assertRegex(rules_hash.rules_sha256(), _HEX64)
        finally:
            os.chdir(saved)


if __name__ == "__main__":
    unittest.main(verbosity=2)

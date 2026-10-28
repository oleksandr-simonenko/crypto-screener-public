#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Селфтест закрытия утечки ключа в шести оставшихся скриптах (пакет 02.09.2026).

ЧТО ЗА УТЕЧКА
Все скрипты зовут Telegram как `.../bot{TG_TOKEN}/sendMessage` — токен стоит
В URL. При сетевом сбое `requests` кладёт полный URL в текст исключения, и
строка вида

    except Exception as e:
        print(f"Telegram недоступен: {e}")

печатает в stdout ВЕСЬ токен бота. Один таймаут = ключ в логе прогона Actions.
То же с Anthropic (`x-api-key`) и CoinGecko (`x-cg-demo-api-key`).
Закрыто 01.09 в `auditor.py`, `check_log.py`, `export_flows.py`; 02.09 —
в `screener.py`, `autoconfig.py`, `backtest.py`, `exits.py`, `spread.py`,
`revolut_scan.py` через `secrets_guard.redact`.

ЧТО ПРОВЕРЯЕМ ЗДЕСЬ — ДВА НЕЗАВИСИМЫХ СЛОЯ

1. ПОВЕДЕНИЕ. Для каждого из шести файлов подменяем сетевую сессию на такую,
   что бросает настоящее `requests.exceptions.ConnectionError` с URL, внутри
   которого лежит ФАЛЬШИВЫЙ токен вида `123456789:AAF...`. Зовём реальную
   функцию, ловим stdout и требуем трёх вещей разом:
     * токена в выводе НЕТ;
     * заглушка `<КЛЮЧ ВЫРЕЗАН>` в выводе ЕСТЬ (значит, чистка сработала,
       а не «токен случайно не попал»);
     * сообщение об ошибке осталось (сбой не проглочен молча — тихий сбой
       хуже напечатанного ключа без ключа).

2. СТРУКТУРА. Разбираем исходники в AST и требуем, чтобы НИ ОДНА переменная
   исключения не попадала в print() без `redact()` — кроме поимённо
   перечисленных мест, где исключение заведомо не сетевое (битый локальный
   JSON, отказ записи файла, проверка YAML). Список закрытый: новая
   необёрнутая печать исключения уронит тест, даже если её напишут через
   год и в другом файле из шести.

НАСТОЯЩИЕ КЛЮЧИ ЗДЕСЬ НЕ ИСПОЛЬЗУЮТСЯ И НЕ ЧИТАЮТСЯ — только выдуманные
образцы нужной формы.
"""

import ast
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests                    # noqa: E402
import secrets_guard               # noqa: E402
from secrets_guard import PLACEHOLDER, redact   # noqa: E402

import autoconfig                  # noqa: E402
import backtest                    # noqa: E402
import exits                       # noqa: E402
import revolut_scan                # noqa: E402
import screener                    # noqa: E402
import spread                      # noqa: E402
import funding_logger              # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- выдуманные ключи нужной формы ----------------------------------------
# Хвост телеграм-токена намеренно длиннее 30 знаков: так его ловят ОБА
# шаблона secrets_guard — и «после /bot», и «отдельно стоящий».
FAKE_TG = "123456789:AAFakeFakeFakeFakeFakeFakeFakeFake12345"
FAKE_ANTHROPIC = "sk-ant-api03-FAKEfakeFAKEfakeFAKEfakeFAKEfake"
FAKE_CG = "CG-FakeFakeFake123456"
# Форма, которую шаблоны НЕ знают: ловится только точным значением из
# окружения. Нужна, чтобы проверить второй проход redact отдельно от первого.
FAKE_ODD = "totally-unusual-secret-value-0000"


def net_error(url_path, host="api.telegram.org"):
    """Исключение ровно того вида, что отдаёт requests при таймауте —
    с полным URL внутри текста. Это и есть механизм утечки."""
    return requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host=%r, port=443): Max retries exceeded with url: "
        "%s (Caused by ConnectTimeoutError(<urllib3.connection.HTTPSConnection "
        "object at 0x000001>, 'Connection to %s timed out. (connect timeout=30)'))"
        % (host, url_path, host))


TG_URL = "/bot%s/sendMessage" % FAKE_TG
CG_URL = "/api/v3/coins/markets?vs_currency=usd&x_cg_demo_api_key=%s" % FAKE_CG


class BoomSession(object):
    """Сессия, которая на любой запрос бросает заданное исключение."""

    def __init__(self, exc):
        self.exc = exc
        self.headers = {}

    def get(self, *a, **kw):
        raise self.exc

    def post(self, *a, **kw):
        raise self.exc


def run_capturing(module, exc, fn, *args, **kwargs):
    """Подменить module.S на падающую сессию, позвать fn, вернуть stdout.

    Исключение из самой fn не гасим молча: возвращаем и вывод, и ошибку —
    тест сам решит, законно ли падение (backtest.main падает уже ПОСЛЕ
    интересующей нас печати).
    """
    buf = io.StringIO()
    old_S = module.S
    module.S = BoomSession(exc)
    raised = None
    try:
        with redirect_stdout(buf):
            fn(*args, **kwargs)
    except BaseException as err:      # noqa: BLE001 — намеренно широко
        raised = err
    finally:
        module.S = old_S
    return buf.getvalue(), raised


class LeakAssertions(unittest.TestCase):
    """Общая проверка одного места печати."""

    def assert_no_leak(self, out, secret, expect_text):
        self.assertNotIn(secret, out,
                         "секрет напечатан в stdout:\n%s" % out)
        self.assertIn(PLACEHOLDER, out,
                      "чистка не сработала — заглушки нет:\n%s" % out)
        self.assertIn(expect_text, out,
                      "сообщение об ошибке пропало — сбой проглочен молча:\n%s" % out)


# ===========================================================================
# 1. ПОВЕДЕНИЕ — по одному классу на файл
# ===========================================================================

class TestScreenerLeak(LeakAssertions):
    """screener.py — восемь мест печати исключения сетевого вызова."""

    def test_post_tg(self):
        out, _ = run_capturing(
            screener, net_error(TG_URL), screener._post_tg,
            "https://api.telegram.org/bot%s/sendMessage" % FAKE_TG, "тело")
        self.assert_no_leak(out, FAKE_TG, "Telegram недоступен")

    def test_fetch_universe(self):
        out, _ = run_capturing(
            screener, net_error(CG_URL, "api.coingecko.com"),
            screener.fetch_universe, {})
        self.assert_no_leak(out, FAKE_CG, "не отдалась")

    def test_funding_binance(self):
        out, _ = run_capturing(
            screener, net_error(TG_URL), screener.fetch_funding_binance)
        self.assert_no_leak(out, FAKE_TG, "Binance funding недоступен")

    def test_funding_bybit(self):
        out, _ = run_capturing(
            screener, net_error(TG_URL), screener.fetch_funding_bybit)
        self.assert_no_leak(out, FAKE_TG, "Bybit funding недоступен")

    def test_funding_mexc(self):
        out, _ = run_capturing(
            screener, net_error(TG_URL), screener.fetch_funding_mexc)
        self.assert_no_leak(out, FAKE_TG, "MEXC funding недоступен")

    def test_revenue_names(self):
        out, _ = run_capturing(
            screener, net_error(TG_URL), screener.fetch_revenue_names)
        self.assert_no_leak(out, FAKE_TG, "DefiLlama недоступна")

    def test_prices_by_ids(self):
        with mock.patch.object(screener.time, "sleep", lambda *_: None):
            out, _ = run_capturing(
                screener, net_error(CG_URL, "api.coingecko.com"),
                screener.fetch_prices_by_ids, ["bitcoin"])
        self.assert_no_leak(out, FAKE_CG, "simple/price не отдался")

    def test_claude_digest(self):
        rows = [{"sym": "AAA", "name": "Alpha", "score": 9, "mcap": 5e7,
                 "float": 0.2, "funding": -0.01, "funding_avg3d": -0.01,
                 "vol_mcap": 0.5}]
        old = screener.ANTHROPIC_KEY
        screener.ANTHROPIC_KEY = FAKE_ANTHROPIC
        try:
            out, _ = run_capturing(
                screener,
                net_error("/v1/messages", "api.anthropic.com"),
                screener.claude_digest, rows)
        finally:
            screener.ANTHROPIC_KEY = old
        self.assertNotIn(FAKE_ANTHROPIC, out)
        self.assertIn("Claude-шаг не сработал", out)

    def test_anthropic_key_in_exception_text(self):
        """Ключ Anthropic ездит в заголовке, но в текст исключения он попасть
        может — проверяем, что и такую форму вырезает."""
        exc = requests.exceptions.ConnectionError(
            "request failed, headers={'x-api-key': '%s'}" % FAKE_ANTHROPIC)
        rows = [{"sym": "AAA", "name": "Alpha", "score": 9, "mcap": 5e7,
                 "float": 0.2, "funding": -0.01, "funding_avg3d": -0.01,
                 "vol_mcap": 0.5}]
        old = screener.ANTHROPIC_KEY
        screener.ANTHROPIC_KEY = FAKE_ANTHROPIC
        try:
            out, _ = run_capturing(screener, exc, screener.claude_digest, rows)
        finally:
            screener.ANTHROPIC_KEY = old
        self.assert_no_leak(out, FAKE_ANTHROPIC, "Claude-шаг не сработал")


class TestAutoconfigLeak(LeakAssertions):
    """autoconfig.py — два места."""

    def test_post_tg(self):
        old = autoconfig.TG_TOKEN
        autoconfig.TG_TOKEN = FAKE_TG
        try:
            out, _ = run_capturing(
                autoconfig, net_error(TG_URL), autoconfig._post_tg, "тело")
        finally:
            autoconfig.TG_TOKEN = old
        self.assert_no_leak(out, FAKE_TG, "Telegram недоступен")

    def test_ask_claude(self):
        out, _ = run_capturing(
            autoconfig,
            net_error("/v1/messages?key=%s" % FAKE_ANTHROPIC, "api.anthropic.com"),
            autoconfig.ask_claude, {}, {}, [])
        self.assert_no_leak(out, FAKE_ANTHROPIC, "Запрос к Claude не удался")


class TestBacktestLeak(LeakAssertions):
    """backtest.py — три места, включая обработчик внутри main()."""

    def test_tg(self):
        old_t, old_c = backtest.TG_TOKEN, backtest.TG_CHAT
        backtest.TG_TOKEN, backtest.TG_CHAT = FAKE_TG, "1"
        try:
            out, _ = run_capturing(
                backtest, net_error(TG_URL), backtest.tg, "текст")
        finally:
            backtest.TG_TOKEN, backtest.TG_CHAT = old_t, old_c
        self.assert_no_leak(out, FAKE_TG, "Telegram недоступен")

    def test_fetch_mexc_syms(self):
        out, _ = run_capturing(
            backtest, net_error(TG_URL), backtest.fetch_mexc_syms)
        self.assert_no_leak(out, FAKE_TG, "MEXC недоступен")

    def test_main_per_coin_handler(self):
        """Обработчик в цикле main(): исключение прилетает из market_chart,
        а тот идёт в CoinGecko через cg_get. Функцию не выделить — зовём
        main() с подменёнными соседями. Падение ПОСЛЕ печати законно."""
        exc = net_error(CG_URL, "api.coingecko.com")

        def boom(_cid):
            raise exc

        buf = io.StringIO()
        with mock.patch.object(backtest, "fetch_universe",
                               lambda: [("alpha", "AAA", 5e7)]), \
             mock.patch.object(backtest, "fetch_mexc_syms", lambda: {"AAA"}), \
             mock.patch.object(backtest, "market_chart", boom), \
             mock.patch.object(backtest, "tg", lambda *_: None), \
             mock.patch.object(backtest.time, "sleep", lambda *_: None):
            try:
                with redirect_stdout(buf):
                    backtest.main()
            except BaseException:      # noqa: BLE001
                pass
        out = buf.getvalue()
        self.assert_no_leak(out, FAKE_CG, "пропуск")


class TestExitsLeak(LeakAssertions):
    """exits.py — два места."""

    def test_tg(self):
        old_t, old_c = exits.TG_TOKEN, exits.TG_CHAT
        exits.TG_TOKEN, exits.TG_CHAT = FAKE_TG, "1"
        try:
            out, _ = run_capturing(exits, net_error(TG_URL), exits.tg, "текст")
        finally:
            exits.TG_TOKEN, exits.TG_CHAT = old_t, old_c
        self.assert_no_leak(out, FAKE_TG, "Telegram недоступен")

    def test_fetch_mexc_syms(self):
        out, _ = run_capturing(exits, net_error(TG_URL), exits.fetch_mexc_syms)
        self.assert_no_leak(out, FAKE_TG, "MEXC недоступен")


class TestSpreadLeak(LeakAssertions):
    """spread.py — два места."""

    def test_tg(self):
        old_t, old_c = spread.TG_TOKEN, spread.TG_CHAT
        spread.TG_TOKEN, spread.TG_CHAT = FAKE_TG, "1"
        try:
            out, _ = run_capturing(spread, net_error(TG_URL), spread.tg, "текст")
        finally:
            spread.TG_TOKEN, spread.TG_CHAT = old_t, old_c
        self.assert_no_leak(out, FAKE_TG, "Telegram недоступен")

    def test_fetch_mexc_syms(self):
        out, _ = run_capturing(spread, net_error(TG_URL), spread.fetch_mexc_syms)
        self.assert_no_leak(out, FAKE_TG, "MEXC недоступен")


class TestRevolutScanLeak(LeakAssertions):
    """revolut_scan.py — одно место."""

    def test_tg(self):
        old_t, old_c = revolut_scan.TG_TOKEN, revolut_scan.TG_CHAT
        revolut_scan.TG_TOKEN, revolut_scan.TG_CHAT = FAKE_TG, "1"
        try:
            out, _ = run_capturing(
                revolut_scan, net_error(TG_URL), revolut_scan.tg, "текст")
        finally:
            revolut_scan.TG_TOKEN, revolut_scan.TG_CHAT = old_t, old_c
        self.assert_no_leak(out, FAKE_TG, "Telegram недоступен")


class TestFundingLoggerLeak(LeakAssertions):
    """С-22: ИНФО из логгера — новое сетевое место с токеном бота в URL."""

    def test_notify_telegram(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": FAKE_TG,
                                          "TELEGRAM_CHAT_ID": "42"}):
            with redirect_stdout(buf):
                ok = funding_logger.notify_telegram(
                    "ИНФО", session=BoomSession(net_error(TG_URL)))
        self.assertFalse(ok)
        self.assert_no_leak(buf.getvalue(), FAKE_TG, "Telegram недоступен")


class TestEnvVarPath(LeakAssertions):
    """Второй проход redact — точное значение из окружения.

    Нужен отдельно: шаблоны знают ФОРМУ ключа, а окружение знает ЗНАЧЕНИЕ.
    Если завтра токен сменит форму, работать должен хотя бы один проход.
    """

    def test_unusual_form_caught_by_env(self):
        exc = net_error("/bot%s/sendMessage" % FAKE_ODD)
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": FAKE_ODD}):
            out, _ = run_capturing(
                screener, exc, screener._post_tg, "https://x", "тело")
        self.assert_no_leak(out, FAKE_ODD, "Telegram недоступен")

    def test_unusual_form_not_caught_without_env(self):
        """Честная граница модуля, записанная тестом: значение неизвестной
        формы, которого НЕТ в окружении, redact поймать не может. Это не
        дефект — это причина, по которой ключ не суют в URL."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            self.assertIn(FAKE_ODD, redact("сбой: /bot%s/x" % FAKE_ODD))


# ===========================================================================
# 2. СТРУКТУРА — AST-обход всех шести файлов
# ===========================================================================

# Файлы, чьи сетевые вызовы обязаны печатать исключения только через redact,
# и ожидаемое число ОБЁРНУТЫХ мест. Числа заморожены: пропадёт обёртка —
# тест упадёт. Пакет 02.09.2026-2 (С-18) расширил список с шести файлов на
# все ДЕВЯТЬ, что ходят в сеть, — добавлены auditor.py, export_flows.py,
# check_log.py. Пакет ПОЛИРОВКА-02.09 (С-22) добавил ДЕСЯТЫЙ —
# funding_logger.py: он теперь сам шлёт ИНФО в Telegram, то есть держит
# токен в URL (9 мест: 2 чтения файла, outputs, ИНФО, сбой прогона,
# источник, разбор, повторы запроса, добор монеты).
GUARDED_FILES = {
    "screener.py": 8,
    "autoconfig.py": 2,
    "backtest.py": 3,
    "exits.py": 2,
    "spread.py": 2,
    "revolut_scan.py": 1,
    "auditor.py": 2,
    "export_flows.py": 2,
    "check_log.py": 1,
    "funding_logger.py": 9,
}

# Места, где исключение ЗАВЕДОМО не сетевое: битый локальный JSON, отказ
# записи файла, проверка собранного YAML, локальный импорт/расчёт. Ключ —
# начало печатаемого текста. Оставлены как есть сознательно: пакет разрешил
# трогать только вывод исключений СЕТЕВЫХ вызовов, и расширять область молча
# нельзя.
NON_NETWORK_ALLOWED = {
    "screener.py": (
        "не смог записать candidates_log",
        "positions.json повреждён",
        "не смог сохранить леджер",
        "tournament.json повреждён",
        "не смог сохранить tournament.json",
    ),
    "autoconfig.py": (
        "JSON битый",
        "Итоговый YAML не прошёл контроль",
    ),
    "backtest.py": (),
    "exits.py": (),
    "spread.py": (),
    "revolut_scan.py": (),
    "auditor.py": (),
    # export_trades.export_all — локальный импорт и запись csv, сети не
    # касается (проверено: в export_trades.py нет requests/urlopen).
    "export_flows.py": (
        "export_trades не отработал",
    ),
    "check_log.py": (),
    "funding_logger.py": (),
}


def _literal_head(node):
    """Начало печатаемого текста — чтобы узнавать место по смыслу, а не по
    номеру строки (номера съедет первая же будущая правка)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                return part.value
        return ""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _literal_head(node.left)
    if isinstance(node, ast.Call):
        return _literal_head(node.func)
    if isinstance(node, ast.Attribute):
        return _literal_head(node.value)
    return ""


def _is_output_call(node):
    """print(...) или что-то похожее на логирование."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Name):
        return f.id == "print"
    if isinstance(f, ast.Attribute):
        return f.attr in ("info", "warning", "error", "exception", "debug",
                          "critical", "log", "write")
    return False


def _read(path):
    """Прочитать исходник байтами и раскодировать. Отдельной функцией — чтобы
    файл гарантированно закрывался: батарея гоняется с -W и мусорить
    ResourceWarning в лог Actions незачем."""
    with io.open(path, "rb") as fh:
        return fh.read().decode("utf-8")


def scan_handlers(path):
    """Вернуть (обёрнутые, необёрнутые) места печати переменной исключения.

    Каждый элемент — (номер строки, начало текста)."""
    src = _read(path)
    tree = ast.parse(src)
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    wrapped, bare = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not node.name:
            continue
        var = node.name
        for out_call in [n for n in ast.walk(node) if _is_output_call(n)]:
            head = _literal_head(out_call.args[0]) if out_call.args else ""
            for name in ast.walk(out_call):
                if not (isinstance(name, ast.Name) and name.id == var):
                    continue
                # Идём вверх до самого вызова печати: встретили redact — чисто.
                cur, guarded = name, False
                while cur is not out_call and cur in parents:
                    if (isinstance(cur, ast.Call) and isinstance(cur.func, ast.Name)
                            and cur.func.id == "redact"):
                        guarded = True
                        break
                    cur = parents[cur]
                (wrapped if guarded else bare).append((name.lineno, head))
    return wrapped, bare


class TestSourceStructure(unittest.TestCase):
    """Ни одной необёрнутой печати исключения вне закрытого списка."""

    def test_no_bare_exception_prints(self):
        problems = []
        for fname, allowed in NON_NETWORK_ALLOWED.items():
            _, bare = scan_handlers(os.path.join(ROOT, fname))
            for lineno, head in bare:
                if not any(head.startswith(a) for a in allowed):
                    problems.append("%s:%d — %r" % (fname, lineno, head[:60]))
        self.assertEqual([], problems,
                         "печать исключения без redact():\n  " + "\n  ".join(problems))

    def test_wrapped_count_frozen(self):
        for fname, expected in GUARDED_FILES.items():
            wrapped, _ = scan_handlers(os.path.join(ROOT, fname))
            # Одно место = одно упоминание переменной; дублей в этих файлах нет.
            self.assertEqual(expected, len(wrapped),
                             "%s: обёрнутых мест %d, ожидалось %d"
                             % (fname, len(wrapped), expected))

    def test_import_present(self):
        for fname in GUARDED_FILES:
            src = _read(os.path.join(ROOT, fname))
            self.assertIn("from secrets_guard import redact", src,
                          "%s: нет импорта redact" % fname)

    def test_scanner_itself_works(self):
        """Проверка проверки: сканер обязан ЗАМЕТИТЬ голую печать.
        Без этого тест выше зелен и на файле, где утечка есть."""
        import tempfile
        bad = ("def f():\n"
               "    try:\n"
               "        pass\n"
               "    except Exception as e:\n"
               "        print(f'упало: {e}')\n")
        with tempfile.NamedTemporaryFile("wb", suffix=".py", delete=False) as fh:
            fh.write(bad.encode("utf-8"))
            tmp = fh.name
        try:
            wrapped, bare = scan_handlers(tmp)
            self.assertEqual([], wrapped)
            self.assertEqual(1, len(bare))
            self.assertTrue(bare[0][1].startswith("упало"))
        finally:
            os.unlink(tmp)


class TestGuardHoles(unittest.TestCase):
    """С-19 (пакет 02.09.2026-2): дыры формы, названные и закрытые.

    Каждая была записана в отчёте как «не живая на сегодняшнем токене, но
    обязана быть закрыта». Тест на каждую — чтобы «закрыта» стало
    проверяемым фактом, а не строкой в отчёте.
    """

    def test_id_longer_than_12_digits_in_url(self):
        """(а) id длиннее 12 цифр. Прежний \\d{6,12} пропускал такой токен
        ЦЕЛИКОМ — самый опасный из провалов: Telegram уже раздаёт длинные id."""
        token = "1234567890123:AAFakeFakeFakeFakeFakeFakeFakeFake12345"  # 13 цифр
        s = "сбой url: /bot%s/sendMessage" % token
        cleaned = redact(s)
        self.assertNotIn(token, cleaned)
        self.assertIn(PLACEHOLDER, cleaned)
        self.assertIn("/bot", cleaned, "диагностика вызова обязана уцелеть")
        self.assertIn("sendMessage", cleaned)

    def test_id_longer_than_12_digits_outside_url(self):
        token = "1234567890123:AAFakeFakeFakeFakeFakeFakeFakeFakeFake12345"
        cleaned = redact("токен протёк в текст: %s конец" % token)
        self.assertNotIn(token, cleaned)
        self.assertIn(PLACEHOLDER, cleaned)

    def test_url_encoded_colon(self):
        """(б) %3A вместо ':' — так двоеточие приезжает из URL-энкодинга."""
        body = "AAFakeFakeFakeFakeFakeFakeFakeFake12345"
        for sep in ("%3A", "%3a"):
            s = "сбой: /bot123456789%s%s/sendMessage" % (sep, body)
            cleaned = redact(s)
            self.assertNotIn(body, cleaned, "хвост токена с %s не вырезан" % sep)
            self.assertIn(PLACEHOLDER, cleaned)

    def test_token_split_by_newline(self):
        """(в) токен, разорванный переносом строки: лог заворачивает длинные
        строки, и половина токена уезжает на следующую строку."""
        token_head = "123456789:AAFakeFakeFakeFake"
        token_tail = "FakeFakeFakeFake12345"
        s = "сбой url: /bot%s\n%s/sendMessage" % (token_head, token_tail)
        cleaned = redact(s)
        self.assertNotIn(token_tail, cleaned, "хвост за переносом не вырезан")
        self.assertNotIn("AAFakeFakeFakeFake", cleaned)
        self.assertIn(PLACEHOLDER, cleaned)

    def test_proxy_var_value_from_env(self):
        """(г) прокси-переменная с логином:паролем — точным значением из env."""
        proxy = "http://bob:hunter2SECRET@proxy.internal:8080"
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": proxy}):
            cleaned = redact("ProxyError: не достучаться через %s (timeout)" % proxy)
        self.assertNotIn("hunter2SECRET", cleaned)
        self.assertNotIn(proxy, cleaned)
        self.assertIn(PLACEHOLDER, cleaned)

    def test_proxy_lower_case_env_name(self):
        proxy = "http://alice:topsecretPWD@10.0.0.9:3128"
        with mock.patch.dict(os.environ, {"https_proxy": proxy}):
            cleaned = redact("ProxyError через %s" % proxy)
        self.assertNotIn("topsecretPWD", cleaned)
        self.assertIn(PLACEHOLDER, cleaned)

    def test_url_credentials_pattern_without_env(self):
        """Чужой прокси, которого нет в НАШЕМ окружении: креды в любом URL
        вида scheme://логин:пароль@хост вырезаются шаблоном, хост цел."""
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                         "http_proxy", "https_proxy", "all_proxy"):
                os.environ.pop(name, None)
            cleaned = redact("socks5://joe:pa55w0rd@10.0.0.1:1080 отвалился")
        self.assertNotIn("pa55w0rd", cleaned)
        self.assertNotIn("joe:pa55w0rd", cleaned)
        self.assertIn(PLACEHOLDER, cleaned)
        self.assertIn("10.0.0.1", cleaned, "хост прокси обязан остаться виден")

    def test_gh_token_env_var(self):
        """GH_TOKEN (его читает gh CLI, а не GITHUB_TOKEN) — вырезается по
        значению из окружения даже в неизвестной форме."""
        val = "plain-gh-token-not-a-known-form-000"
        with mock.patch.dict(os.environ, {"GH_TOKEN": val}):
            cleaned = redact("git push отклонён с токеном %s в url" % val)
        self.assertNotIn(val, cleaned)
        self.assertIn(PLACEHOLDER, cleaned)

    def test_redact_before_slice_is_the_safe_order(self):
        """(д) redact ДО среза. redact(s)[:300] чист; redact(s[:300]) — течёт,
        потому что срез режет токен пополам, и обрубок мимо шаблонов."""
        token = "123456789:AAFakeFakeFakeFakeFakeFakeFakeFake12345"
        # Токен садится на границу 300: префикс подобран так, чтобы срез [:300]
        # рассёк токен по середине хвоста (после двоеточия) — тогда обрубок
        # слишком короток для шаблонов и в неправильном порядке утёк бы.
        s = "x" * 275 + " /bot%s/sendMessage" % token
        safe = redact(s)[:300]           # правильный порядок
        unsafe = redact(s[:300])         # неправильный: срез до чистки
        self.assertNotIn(token, safe)
        self.assertNotIn("AAFake", safe, "в правильном порядке обрубка нет")
        # А неправильный порядок оставляет обрубок токена — ровно поэтому
        # правило в secrets_guard: redact(r.text)[:300], а не redact(r.text[:300]).
        self.assertIn("AAFake", unsafe,
                      "демонстрация дыры: срез до чистки печатает обрубок")

    def test_call_sites_slice_after_redact(self):
        """Ни один боевой файл не зовёт redact(r.text[:...]) — только
        redact(r.text)[:...]. Иначе дыра (д) вернулась бы в код."""
        import re as _re
        bad = _re.compile(r"redact\(\s*r\.text\s*\[")
        for fname in GUARDED_FILES:
            src = _read(os.path.join(ROOT, fname))
            self.assertIsNone(bad.search(src),
                              "%s: срез ВНУТРИ redact(r.text[:...]) — "
                              "дыра С-19" % fname)


class TestRedactOnRealRequestsError(unittest.TestCase):
    """Отдельно от файлов: redact против НАСТОЯЩЕГО текста requests."""

    def test_connection_error_text(self):
        s = str(net_error(TG_URL))
        self.assertIn(FAKE_TG, s, "образец обязан содержать токен — иначе тест пуст")
        cleaned = redact(s)
        self.assertNotIn(FAKE_TG, cleaned)
        self.assertIn(PLACEHOLDER, cleaned)
        # Диагностика уцелела: видно, КУДА не достучались.
        self.assertIn("api.telegram.org", cleaned)
        self.assertIn("/bot", cleaned)
        self.assertIn("sendMessage", cleaned)

    def test_exception_object_accepted(self):
        """redact() зовут прямо на объекте исключения, не на строке."""
        self.assertNotIn(FAKE_TG, redact(net_error(TG_URL)))

    def test_placeholder_is_what_tests_expect(self):
        self.assertEqual(PLACEHOLDER, secrets_guard.PLACEHOLDER)


if __name__ == "__main__":
    unittest.main()

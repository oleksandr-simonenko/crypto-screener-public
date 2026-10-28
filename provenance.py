#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""provenance.py — штамп происхождения артефактов (блок E2 пакета СКРИНЕР-ПОЛНЫЙ).

ЗАЧЕМ ЭТО НУЖНО ДЛЯ ДЕНЕГ
Каждый json/csv-артефакт репозитория обязан нести `git_sha` и `generated_utc`.
Без них через месяц невозможно сказать, КАКИМ кодом посчитано число в отчёте,
и спор о цифре упирается в память, а не в факт. Вердикт 15.11.2026 должен
опираться на воспроизводимый расчёт, иначе он не стоит ничего.

ЖЕЛЕЗНОЕ ТРЕБОВАНИЕ: git_sha() НИКОГДА не падает и не висит.
На GitHub Actions каталог `.git` бывает подрезан (`fetch-depth: 1`), локально
git может отсутствовать в PATH, а вызов внешнего процесса — зависнуть.
Поэтому порядок жёсткий:
    1) переменная окружения GITHUB_SHA (Actions выставляет её всегда);
    2) `git rev-parse HEAD` — с таймаутом и полным подавлением ошибок;
    3) честное 'unknown' — но НЕ исключение и НЕ пустая строка.

Зависимости: только stdlib (никаких requests/PyYAML тут не нужно).
"""

import os
import subprocess
import sys
from datetime import datetime, timezone

# Версия набора инструментов пакета (метрики/вердикт/логгеры).
# Меняется руками при смене формата артефактов — по ней читатель понимает,
# каким поколением кода сделан файл, даже если git_sha == 'unknown'.
TOOL_VERSION = "1.0.0"

# Сколько символов sha считать «коротким». 12 — стандарт git для длинных историй.
SHA_SHORT_LEN = 12

# Таймаут на вызов git. Штамп не имеет права задерживать прогон:
# лучше 'unknown' за 5 секунд, чем повисший воркфлоу.
GIT_TIMEOUT_SEC = 5

# Кладём 'unknown' одной константой, чтобы её нельзя было опечатать в трёх местах.
UNKNOWN = "unknown"

_HEX = set("0123456789abcdefABCDEF")


def _looks_like_sha(value):
    """Похоже ли на git sha: непустая шестнадцатеричная строка длиной 7..64.

    Проверка нужна потому, что GITHUB_SHA может прийти мусором из чужого
    окружения, а `git rev-parse` в некоторых состояниях печатает текст ошибки
    в stdout. Мусор в артефакте хуже честного 'unknown'.
    """
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not (7 <= len(value) <= 64):
        return False
    return all(ch in _HEX for ch in value)


def git_sha_full():
    """Полный sha (как отдал источник) либо 'unknown'. Никогда не падает."""
    # (1) окружение GitHub Actions — самый надёжный источник, git не нужен.
    env_sha = os.getenv("GITHUB_SHA")
    if _looks_like_sha(env_sha):
        return env_sha.strip().lower()

    # (2) локальный git. Любая беда — молча вниз по списку.
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=here,                  # чтобы sha был про ЭТОТ репозиторий, а не про cwd
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SEC,
        )
        if proc.returncode == 0:
            out = proc.stdout.decode("utf-8", "replace").strip()
            if _looks_like_sha(out):
                return out.lower()
    except Exception:
        # Ловим ВСЁ намеренно: FileNotFoundError (нет git), TimeoutExpired,
        # PermissionError, OSError. Провенанс не имеет права уронить прогон.
        pass

    # (3) честное незнание.
    return UNKNOWN


def git_sha():
    """Короткий sha кода.

    Порядок источников: env GITHUB_SHA -> `git rev-parse HEAD` -> 'unknown'.
    Никогда не падает, всегда возвращает непустую строку.
    Длина — SHA_SHORT_LEN символов (полный доступен через git_sha_full()).
    """
    full = git_sha_full()
    if full == UNKNOWN:
        return UNKNOWN
    return full[:SHA_SHORT_LEN]


def utc_now_iso():
    """Текущее время UTC в формате 'YYYY-MM-DDTHH:MM:SSZ' (без микросекунд)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def python_version():
    """'3.x.y' — версия интерпретатора, которым посчитан артефакт.

    Локально 3.14, на Actions 3.12; расхождение результатов между ними должно
    быть видно по артефакту, а не выясняться догадками.
    """
    v = sys.version_info
    return "%d.%d.%d" % (v.major, v.minor, v.micro)


def stamp(tool_version=TOOL_VERSION):
    """Штамп происхождения для любого артефакта.

    -> {'git_sha': str, 'generated_utc': 'YYYY-MM-DDTHH:MM:SSZ',
        'tool_version': str, 'python': '3.x.y'}

    Вызывается без аргументов; параметр tool_version — только для случая,
    когда конкретный скрипт версионируется отдельно от набора.
    """
    return {
        "git_sha": git_sha(),
        "generated_utc": utc_now_iso(),
        "tool_version": str(tool_version),
        "python": python_version(),
    }


def main():
    st = stamp()
    print("git_sha:       %s" % st["git_sha"])
    print("generated_utc: %s" % st["generated_utc"])
    print("tool_version:  %s" % st["tool_version"])
    print("python:        %s" % st["python"])
    if st["git_sha"] == UNKNOWN:
        print("ВНИМАНИЕ: sha не определён (нет GITHUB_SHA и нет доступного git).")
        print("Артефакт всё равно будет записан, но проследить код по нему нельзя.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

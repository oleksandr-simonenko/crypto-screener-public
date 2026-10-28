#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rules_hash.py — sha256 замороженных правил турнира (блок C3).

ЗАЧЕМ ЭТО НУЖНО ДЛЯ ДЕНЕГ
`screener_tournament_rules_v1.md` — предрегистрация: способ счёта зафиксирован
ДО того, как стал известен исход турнира. Ценность предрегистрации ровно в том,
что её нельзя подправить задним числом. Отпечаток файла, впечатанный в каждый
артефакт (metrics_latest.json, verdict_latest.json, еженедельный аудит),
превращает «правила не менялись» из обещания в проверяемый факт: изменилась
хоть одна буква — хеш другой, и это видно в истории артефактов.

ЧЕСТНАЯ ГРАНИЦА, которую надо называть вслух: хеш ловит ПОДМЕНУ ФАЙЛА между
снимками. Он не мешает автору переписать и правила, и все артефакты разом.
Против такого работает только внешняя копия (git-история на GitHub), а не
этот модуль. Первый слой не притворяется вторым.

ВАЖНО ПРО ПЕРЕВОДЫ СТРОК: хеш считается от БАЙТОВ файла, как есть.
Это значит, что LF-версия и CRLF-версия одного и того же текста дают РАЗНЫЕ
хеши. На Windows-машинах git обычно стоит с core.autocrlf=true и выдаёт при
клоне CRLF, тогда как ubuntu-раннер Actions получает LF. Чтобы хеш совпадал
везде, файл правил должен быть закреплён в `.gitattributes` (см. README/STATE);
CLI этого модуля печатает предупреждение, если нашёл CRLF.

Зависимости: только stdlib.
"""

import hashlib
import os
import sys

# Имя файла замороженных правил. Канон обоих проектов (stage0 и скринера).
DEFAULT_RULES_FILENAME = "screener_tournament_rules_v1.md"

# Читаем кусками: файл маленький, но одна и та же функция может однажды
# получить большой путь, а память на раннере не резиновая.
_CHUNK = 1024 * 1024


def resolve_rules_path(path=DEFAULT_RULES_FILENAME):
    """Превратить относительное имя в существующий путь.

    Порядок: абсолютный путь — как есть; иначе сначала относительно текущего
    каталога, потом относительно каталога этого модуля. Второе нужно для
    вызовов из tests/ и из воркфлоу, где cwd не равен корню репозитория.
    Если не нашли нигде — возвращаем вариант относительно cwd, чтобы в тексте
    ошибки было видно, где файл искали первым делом.
    """
    if os.path.isabs(path):
        return path
    cwd_candidate = os.path.abspath(path)
    if os.path.exists(cwd_candidate):
        return cwd_candidate
    here = os.path.dirname(os.path.abspath(__file__))
    module_candidate = os.path.join(here, path)
    if os.path.exists(module_candidate):
        return module_candidate
    return cwd_candidate


def rules_sha256(path=DEFAULT_RULES_FILENAME):
    """sha256 файла правил в шестнадцатеричном виде (64 символа, нижний регистр).

    Хеш считается от БАЙТОВ файла — никакой нормализации переводов строк,
    никакого перекодирования. Файла нет -> FileNotFoundError с внятным текстом:
    молча вернуть заглушку нельзя, иначе артефакт унесёт в отчёт хеш пустоты.
    """
    full = resolve_rules_path(path)
    if not os.path.isfile(full):
        raise FileNotFoundError(
            "файл замороженных правил не найден: %s "
            "(ожидается копия screener_tournament_rules_v1.md в корне репозитория)" % full
        )
    h = hashlib.sha256()
    with open(full, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def rules_sha256_or_none(path=DEFAULT_RULES_FILENAME):
    """То же, но при любой беде отдаёт None вместо исключения.

    Для мест, где отсутствие правил не должно ронять прогон целиком
    (светофор sanity.py, аудит): пусть в артефакте честно стоит null и
    отдельная строка «правила не найдены», чем упавший воркфлоу без данных.
    """
    try:
        return rules_sha256(path)
    except Exception:
        return None


def rules_size_bytes(path=DEFAULT_RULES_FILENAME):
    """Размер файла правил в байтах (полезно печатать рядом с хешем)."""
    full = resolve_rules_path(path)
    return os.path.getsize(full)


def has_crlf(path=DEFAULT_RULES_FILENAME):
    """Есть ли в файле переводы строк CRLF.

    Не влияет на расчёт, но CLI обязан предупредить: CRLF-копия того же текста
    даёт ДРУГОЙ хеш, и сверка П6 в sanity.py выдаст ложный MISMATCH.
    """
    full = resolve_rules_path(path)
    with open(full, "rb") as f:
        return b"\r\n" in f.read()


def rules_info(path=DEFAULT_RULES_FILENAME):
    """Сводка одним словарём — удобно класть прямо в артефакт.

    -> {'path', 'sha256', 'size_bytes', 'crlf'}
    """
    full = resolve_rules_path(path)
    return {
        "path": os.path.basename(full),
        "sha256": rules_sha256(full),
        "size_bytes": rules_size_bytes(full),
        "crlf": has_crlf(full),
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    path = argv[0] if argv else DEFAULT_RULES_FILENAME
    full = resolve_rules_path(path)
    try:
        info = rules_info(full)
    except FileNotFoundError as exc:
        print("ОШИБКА: %s" % exc)
        return 1
    except OSError as exc:
        print("ОШИБКА чтения файла правил: %s" % exc)
        return 1

    print("файл:   %s" % full)
    print("sha256: %s" % info["sha256"])
    print("размер: %d байт" % info["size_bytes"])
    if info["crlf"]:
        print("ВНИМАНИЕ: в файле есть CRLF. На ubuntu-раннере Actions тот же текст")
        print("будет с LF и даст ДРУГОЙ хеш — сверка правил выдаст ложное расхождение.")
        print("Лечится закреплением файла в .gitattributes (eol=lf), не правкой хеша.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

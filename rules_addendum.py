#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rules_addendum.py — поправки к замороженным правилам турнира (02.09.2026).

ЗАЧЕМ ЭТО НУЖНО ДЛЯ ДЕНЕГ
Правила турнира v1 заморожены (§8): правка на месте запрещена, любое изменение
— отдельный файл с датой и подтверждением, что оно сделано ДО просмотра
результатов. Поправка №1 (`tournament_rules_addendum_1.md`) выводит дорожку A6
из подтверждающего семейства в наблюдательную. Чтобы это было решением
ПРОТОКОЛА, а не строчкой в коде, которую можно тихо поменять 14 ноября,
`verdict_1511.py` читает флаг ИЗ ФАЙЛА поправки через этот модуль:
  * нет файла — семейство прежнее (все непустые дорожки), и это печатается;
  * файл есть — его sha256 попадает в артефакты и в аудит рядом с отпечатком
    правил, а внутри файла закреплён sha256 правил v1: если правила изменятся,
    поправка ссылается уже не на те правила, и модуль это скажет.

ФОРМАТ. Поправка — markdown с ОДНИМ машинным блоком ```yaml ... ``` внутри:
    addendum: 1
    adopted: 2026-09-02
    rules_v1_sha256: <64 hex>
    observational_tracks: [A6]
Прочие ключи блока — справочные и не влияют на счёт. Контроль A7 и портфель
A0 наблюдательными быть не могут: такая запись игнорируется с пометкой.

ПЕРЕВОДЫ СТРОК — та же оговорка, что у rules_hash.py: sha256 считается от
байтов. Здесь отдаются ОБА отпечатка — сырых байтов и LF-нормализованного
текста; канон — LF (так файл лежит в git и на ubuntu-раннере).

Зависимости: stdlib + PyYAML (есть в requirements.txt).
CLI: python rules_addendum.py [--repo-dir DIR]
"""

import argparse
import datetime as _dt
import hashlib
import io
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_ADDENDUM_FILENAME = "tournament_rules_addendum_1.md"
RULES_V1_FILENAME = "screener_tournament_rules_v1.md"

# Дорожки турнира A0..A9; контроль и портфель наблюдательными не бывают.
_TRACK_RE = re.compile(r"^A[0-9]$")
CONTROL_TRACK = "A7"
PORTFOLIO_TRACK = "A0"

# Первый блок ```yaml ... ``` файла. Дополнительных блоков быть не должно;
# если их несколько, берётся первый, у которого есть ключ addendum.
_FENCE_RE = re.compile(r"```yaml[ \t]*\r?\n(.*?)\r?\n```", re.S)

# Отпечаток правил читается РЕГУЛЯРКОЙ из сырого текста блока, а не из
# разобранного YAML: хеш, случайно состоящий из одних цифр, YAML превратил
# бы в число и потерял бы ведущие нули — и сверка молча сломалась бы.
_PIN_RE = re.compile(r"^\s*rules_v1_sha256\s*:\s*[\"']?([0-9a-fA-F]{64})[\"']?\s*$", re.M)


def resolve_path(filename=DEFAULT_ADDENDUM_FILENAME, repo_dir=None):
    """Путь к файлу в корне репозитория (repo_dir или каталог модуля)."""
    if os.path.isabs(filename):
        return filename
    return os.path.join(repo_dir if repo_dir is not None else HERE, filename)


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def file_hashes(path):
    """-> (sha256 сырых байтов, sha256 LF-нормализованного текста, есть ли CRLF)."""
    with io.open(path, "rb") as fh:
        raw = fh.read()
    crlf = b"\r\n" in raw
    lf = raw.replace(b"\r\n", b"\n")
    return sha256_hex(raw), sha256_hex(lf), crlf


def parse_machine_block(text):
    """Первый машинный блок поправки -> (dict, сырой текст блока).

    Нет блока или он не объект -> ValueError.
    """
    for m in _FENCE_RE.finditer(text):
        data = yaml.safe_load(m.group(1))
        if isinstance(data, dict) and "addendum" in data:
            return data, m.group(1)
    raise ValueError("в файле нет машинного блока ```yaml с ключом addendum")


def pinned_rules_hash(block_text):
    """sha256 правил v1 из сырого текста блока (64 hex) или None."""
    m = _PIN_RE.search(block_text or "")
    return m.group(1).lower() if m else None


def _as_iso_date(value):
    """date | datetime | 'YYYY-MM-DD…' -> 'YYYY-MM-DD'; мусор -> None."""
    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if value is None:
        return None
    text = str(value).strip()
    try:
        return _dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _clean_tracks(values, warnings):
    """Список дорожек из блока -> нормализованный список без контроля/портфеля.

    Не-список сюда не попадает: load_addendum отвергает такой файл целиком
    (available=False) — «поправка есть, но применить нечего» выглядело бы
    как отсутствие файла, а это разные состояния.
    """
    if values is None:
        return []
    out = []
    for item in values:
        tr = str(item).strip().upper()
        if not _TRACK_RE.match(tr):
            warnings.append("не дорожка турнира: %r — запись проигнорирована" % (item,))
            continue
        if tr == CONTROL_TRACK:
            warnings.append("контроль %s наблюдательным быть не может — запись "
                            "проигнорирована" % tr)
            continue
        if tr == PORTFOLIO_TRACK:
            warnings.append("портфель %s в тесте не участвует и наблюдательным не "
                            "назначается — запись проигнорирована" % tr)
            continue
        if tr not in out:
            out.append(tr)
    return out


def load_addendum(repo_dir=None, filename=DEFAULT_ADDENDUM_FILENAME,
                  rules_filename=RULES_V1_FILENAME):
    """Прочитать поправку и сверить отпечаток правил v1. Никогда не бросает.

    -> {'available', 'file', 'path', 'sha256', 'sha256_lf', 'crlf', 'number',
        'adopted', 'adopted_date', 'decided_by', 'applies_before',
        'observational_tracks', 'rules_v1_sha256_pinned',
        'rules_v1_sha256_actual', 'pin_matches', 'warnings', 'error'}
    available=False с текстом в error — файла нет, блок не разобран или
    observational_tracks не список; тогда observational_tracks пуст и
    семейство вердикта остаётся прежним.
    Дата принятия (adopted_date, 'YYYY-MM-DD') сверяется с датами первых
    закрытий, названными в самой поправке; сверку с ФАКТИЧЕСКИМИ закрытиями
    делает verdict_1511.build_verdict — у него есть сделки.
    """
    out = {
        "available": False,
        "file": os.path.basename(filename),
        "path": None,
        "sha256": None,
        "sha256_lf": None,
        "crlf": None,
        "number": None,
        "adopted": None,
        "adopted_date": None,
        "decided_by": None,
        "applies_before": [],
        "observational_tracks": [],
        "rules_v1_sha256_pinned": None,
        "rules_v1_sha256_actual": None,
        "pin_matches": None,
        "warnings": [],
        "error": None,
    }
    path = resolve_path(filename, repo_dir)
    out["path"] = path
    if not os.path.isfile(path):
        out["error"] = "файл поправки не найден: %s" % path
        return out
    try:
        out["sha256"], out["sha256_lf"], out["crlf"] = file_hashes(path)
        with io.open(path, "rb") as fh:
            text = fh.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        out["error"] = "файл поправки не прочитан: %s" % exc
        return out
    try:
        block, block_text = parse_machine_block(text)
    except (ValueError, yaml.YAMLError) as exc:
        out["error"] = "машинный блок поправки не разобран: %s" % exc
        return out

    warnings = out["warnings"]
    try:
        out["number"] = int(block.get("addendum"))
    except (TypeError, ValueError):
        warnings.append("ключ addendum не число: %r" % (block.get("addendum"),))
    adopted_raw = block.get("adopted")
    out["adopted"] = str(adopted_raw).strip() if adopted_raw is not None else None
    out["adopted_date"] = _as_iso_date(adopted_raw)
    if out["adopted_date"] is None:
        warnings.append("ВНИМАНИЕ: дата принятия (adopted) отсутствует или не разобрана — "
                        "предрегистрацию проверить нельзя")
    out["decided_by"] = str(block.get("decided_by")).strip() if block.get("decided_by") else None
    before = block.get("applies_before_first_closures") or []
    if isinstance(before, (list, tuple)):
        out["applies_before"] = [str(d) for d in before]
    before_dates = [d for d in (_as_iso_date(x) for x in out["applies_before"]) if d]
    if out["adopted_date"] and before_dates and out["adopted_date"] > min(before_dates):
        warnings.append("ВНИМАНИЕ: дата принятия %s ПОЗЖЕ первых закрытий, названных "
                        "в самой поправке (%s) — это не предрегистрация"
                        % (out["adopted_date"], min(before_dates)))
    tracks_raw = block.get("observational_tracks")
    if tracks_raw is not None and not isinstance(tracks_raw, (list, tuple)):
        out["error"] = ("observational_tracks должен быть списком вида [A6], получено %r"
                        % (tracks_raw,))
        return out
    out["observational_tracks"] = _clean_tracks(tracks_raw, warnings)

    pinned = pinned_rules_hash(block_text)
    if pinned is None and block.get("rules_v1_sha256") is not None:
        warnings.append("rules_v1_sha256 в поправке не похож на sha256 (64 hex): %r"
                        % (block.get("rules_v1_sha256"),))
    out["rules_v1_sha256_pinned"] = pinned
    rules_path = resolve_path(rules_filename, repo_dir)
    try:
        rules_hashes = file_hashes(rules_path) if os.path.isfile(rules_path) else None
    except OSError as exc:
        rules_hashes = None
        warnings.append("файл правил v1 не прочитан (%s) — сверка отпечатка невозможна"
                        % type(exc).__name__)
    if rules_hashes is not None:
        r_raw, r_lf, _crlf = rules_hashes
        out["rules_v1_sha256_actual"] = r_lf
        out["pin_matches"] = bool(pinned) and pinned in (r_raw, r_lf)
        if pinned and not out["pin_matches"]:
            warnings.append(
                "ВНИМАНИЕ: sha256 правил v1 в поправке (%s…) НЕ совпадает с файлом "
                "правил (%s…) — правила или поправка менялись" % (pinned[:12], r_lf[:12]))
        elif not pinned:
            warnings.append("в поправке нет rules_v1_sha256 — отпечаток правил не закреплён")
    elif not os.path.isfile(rules_path):
        warnings.append("файл правил v1 не найден — сверка отпечатка невозможна")

    out["available"] = True
    return out


def addendum_sha256_or_none(repo_dir=None, filename=DEFAULT_ADDENDUM_FILENAME):
    """Канонический (LF) sha256 поправки; None, если файла нет или он не читается."""
    try:
        path = resolve_path(filename, repo_dir)
        if not os.path.isfile(path):
            return None
        return file_hashes(path)[1]
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Поправки к замороженным правилам турнира")
    ap.add_argument("--repo-dir", default=None, help="корень репозитория")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    info = load_addendum(repo_dir=args.repo_dir)
    print("файл:            %s" % info["path"])
    if not info["available"]:
        print("НЕДОСТУПНА: %s" % info["error"])
        return 1
    print("sha256 (LF):     %s" % info["sha256_lf"])
    print("sha256 (байты):  %s%s" % (info["sha256"], "  [CRLF]" if info["crlf"] else ""))
    print("поправка №%s, принята %s, решение: %s"
          % (info["number"], info["adopted"], info["decided_by"] or "—"))
    print("до первых закрытий: %s" % (", ".join(info["applies_before"]) or "—"))
    print("наблюдательные дорожки (вне теста): %s"
          % (", ".join(info["observational_tracks"]) or "нет"))
    print("правила v1: закреплено %s, фактически %s -> %s"
          % ((info["rules_v1_sha256_pinned"] or "—")[:16],
             (info["rules_v1_sha256_actual"] or "—")[:16],
             "СОВПАДАЕТ" if info["pin_matches"] else "НЕ СОВПАДАЕТ"))
    for w in info["warnings"]:
        print("  предупреждение: %s" % w)
    return 0 if info["pin_matches"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

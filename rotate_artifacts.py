#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rotate_artifacts.py — блок D4 пакета СКРИНЕР-ПОЛНЫЙ: ротация артефактов.

ЗАЧЕМ ЭТО НУЖНО ДЛЯ ДЕНЕГ
Датированные выгрузки копятся в git вечно: почасовой фандинг — это ~24 строки
в день на 233 перпа, ежедневные разлоки — сотни строк, недельные потоки — свой
файл каждую пятницу. Плоский каталог на тысячу файлов делает две вещи, обе
плохие: репозиторий тяжелеет (а Actions клонирует его каждый прогон), и нужный
файл перестаёт находиться глазами. Раскладка по месяцам и сжатие старого —
дешёвая гигиена, которая держит воркфлоу быстрыми до вердикта 15.11.2026.

ЧТО ДЕЛАЕТ
1) ПЕРЕНОС: плоский датированный файл `<что-то>_YYYYMMDD.<ext>` из корня
   наблюдаемого каталога уезжает в помесячную папку `YYYY-MM/` рядом.
   Пример: export/flows_20260814.json -> export/2026-08/flows_20260814.json
2) СЖАТИЕ: файл старше N дней (по умолчанию 60, СТРОГО больше) пакуется в .gz.
   Оригинал удаляется ТОЛЬКО после того, как архив записан И прочитан обратно,
   и sha256 распакованного совпал с sha256 оригинала. Не совпал — архив
   удаляется, оригинал остаётся, ошибка печатается.
3) `*_latest.*` НЕ ТРОГАЕТСЯ НИКОГДА: ни переносом, ни сжатием. На эти имена
   ссылаются другие скрипты и соседний проект; уехавший latest — это тихая
   поломка всех читателей сразу.

БЕЗОПАСНОСТЬ ПО УМОЛЧАНИЮ
Без ключей скрипт печатает ПЛАН и не делает ничего (`--dry-run` — режим по
умолчанию). Файлы двигаются только по явному `--apply`. Так и задумано:
инструмент, который удаляет файлы, обязан по умолчанию быть безвредным.

ВОЗРАСТ СЧИТАЕТСЯ ПО ДАТЕ В ИМЕНИ, а не по mtime файла. Причина: git не хранит
время изменения, и после свежего клона (каждый прогон Actions) mtime у всех
файлов одинаков и равен времени клонирования — по нему «старых» файлов не
бывает вообще, и сжатие не срабатывало бы никогда.

ЗАВИСИМОСТИ: только stdlib.
Запуск: python rotate_artifacts.py              # план, ничего не меняет
        python rotate_artifacts.py --apply      # выполнить план
"""

import argparse
import gzip
import hashlib
import os
import re
import shutil
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

# Каталоги, за которыми присматриваем. Корень репозитория сюда НЕ входит:
# там лежат tournament.json, positions.json, candidates_log.jsonl, config.yaml —
# рабочие файлы скринера, которые двигать нельзя ни при каких условиях.
DEFAULT_ROOTS = ("export", "artifacts", "data")

# Служебные каталоги пропускаем целиком.
SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv"}

DEFAULT_OLDER_THAN_DAYS = 60

MONTH_DIR_RE = re.compile(r"^\d{4}-\d{2}$")
DATE_COMPACT_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")
DATE_DASHED_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")

# `*_latest.*` и любые варианты со словом latest как отдельным куском имени.
# Специально шире буквы контракта: цена ложного пропуска — ноль, цена
# случайно уехавшего latest — сломанные читатели.
LATEST_RE = re.compile(r"(^|[_.\-])latest([_.\-]|$)", re.IGNORECASE)

CHUNK = 1 << 20  # 1 МиБ: читаем кусками, файл фандинга за месяц не мал

ACTION_MOVE = "move"
ACTION_GZIP = "gzip"
ACTION_SKIP = "skip"


# --- разбор имён ---------------------------------------------------------

def _coerce_date(value):
    """date | 'YYYY-MM-DD' | None -> date | None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()


def file_date_from_name(name):
    """Дата из имени файла или None.

    Понимает `..._YYYYMMDD.ext` и `..._YYYY-MM-DD.ext`. Строгая проверка
    календарём: '20261340' не дата, и такой файл считается недатированным —
    трогать его нельзя, раз мы не поняли, к какому месяцу он относится.
    """
    if name.endswith(".gz"):
        name = name[:-3]
    m = DATE_COMPACT_RE.search(name)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            return None
    m = DATE_DASHED_RE.search(name)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def is_latest_name(name):
    """Имя защищённого «свежего» файла (`*_latest.*`)."""
    return bool(LATEST_RE.search(name))


def iter_files(root_abs):
    """Все файлы под каталогом, кроме служебных подкаталогов."""
    for dirpath, dirnames, filenames in os.walk(root_abs):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for fname in sorted(filenames):
            yield os.path.join(dirpath, fname)


def _target_month_dir(path, file_date):
    """Куда файл должен лежать по своей дате.

    Если он уже в папке `YYYY-MM` — проверяем, что месяц ТОТ. Файл в чужом
    месяце (такое бывает после ручного разбора) переезжает в правильный,
    а не остаётся лежать там, где его никто не найдёт.
    """
    parent = os.path.dirname(path)
    month = file_date.strftime("%Y-%m")
    base = os.path.basename(parent)
    if MONTH_DIR_RE.match(base):
        if base == month:
            return parent
        return os.path.join(os.path.dirname(parent), month)
    return os.path.join(parent, month)


# --- план ----------------------------------------------------------------

def plan_actions(repo_dir=".", roots=DEFAULT_ROOTS,
                 older_than_days=DEFAULT_OLDER_THAN_DAYS, as_of=None):
    """Построить план работ. НИЧЕГО не меняет на диске.

    -> список словарей:
       {'action': 'move'|'gzip'|'skip', 'src': путь, 'dst': путь|None,
        'file_date': 'YYYY-MM-DD', 'age_days': int, 'reason': str}
    План линейный и упорядоченный: сначала перенос файла, сразу за ним — его
    сжатие УЖЕ ПО НОВОМУ пути. Порядок важен, apply идёт по списку сверху вниз.
    """
    repo_abs = os.path.abspath(repo_dir)
    today = _coerce_date(as_of) or datetime.now(timezone.utc).date()
    plan = []
    planned_dst = set()   # чтобы два файла не целились в одно имя молча

    for root in roots:
        root_abs = os.path.join(repo_abs, root)
        if not os.path.isdir(root_abs):
            continue
        for path in iter_files(root_abs):
            name = os.path.basename(path)
            if is_latest_name(name):
                continue
            fdate = file_date_from_name(name)
            if fdate is None:
                continue
            age = (today - fdate).days
            common = {"file_date": fdate.isoformat(), "age_days": age}
            current = path

            # (1) перенос в помесячную папку
            target_dir = _target_month_dir(path, fdate)
            if os.path.normpath(target_dir) != os.path.normpath(os.path.dirname(path)):
                dst = os.path.join(target_dir, name)
                if os.path.exists(dst) or dst in planned_dst:
                    plan.append(dict(common, action=ACTION_SKIP, src=path, dst=dst,
                                     reason="цель уже существует — перенос не делаем"))
                    continue
                plan.append(dict(common, action=ACTION_MOVE, src=path, dst=dst,
                                 reason="плоский датированный файл -> помесячная папка"))
                planned_dst.add(dst)
                current = dst

            # (2) сжатие старого
            if name.endswith(".gz"):
                continue
            if age <= older_than_days:
                continue
            gz = current + ".gz"
            if os.path.exists(gz) or gz in planned_dst:
                plan.append(dict(common, action=ACTION_SKIP, src=current, dst=gz,
                                 reason="архив уже существует — сжатие не делаем"))
                continue
            plan.append(dict(common, action=ACTION_GZIP, src=current, dst=gz,
                             reason="старше порога в %d дней" % older_than_days))
            planned_dst.add(gz)

    return plan


# --- исполнение ----------------------------------------------------------

def _sha256_file(path, opener=open):
    h = hashlib.sha256()
    with opener(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _gz_matches(gz_path, expected_sha):
    """Читается ли архив обратно и совпадает ли содержимое с оригиналом.

    Отдельной функцией — чтобы селфтест мог подменить её и проверить, что при
    неудачной проверке оригинал действительно ОСТАЁТСЯ на месте.
    """
    try:
        return _sha256_file(gz_path, opener=gzip.open) == expected_sha
    except Exception:
        return False


def compress_file(src, dst):
    """Сжать src в dst и удалить оригинал ТОЛЬКО после обратной проверки.

    -> (ok: bool, message: str). Исключений наружу не выпускает: ротация не
    имеет права уронить прогон, но обязана честно сказать, что не вышло.
    """
    tmp = dst + ".part"
    try:
        src_sha = _sha256_file(src)
        with open(src, "rb") as fin, gzip.open(tmp, "wb", compresslevel=9) as fout:
            shutil.copyfileobj(fin, fout, CHUNK)
        os.replace(tmp, dst)
    except Exception as exc:
        for leftover in (tmp,):
            if os.path.exists(leftover):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
        return False, "сжатие не удалось (%s: %s)" % (type(exc).__name__, exc)

    if not _gz_matches(dst, src_sha):
        # Архив не читается обратно или отличается — он бесполезен и опасен
        # (создаёт иллюзию сохранённых данных). Убираем его, оригинал живёт.
        try:
            os.remove(dst)
        except OSError:
            pass
        return False, "архив не прошёл обратное чтение — ОРИГИНАЛ ОСТАВЛЕН"

    try:
        stat = os.stat(src)
        os.utime(dst, (stat.st_atime, stat.st_mtime))
    except OSError:
        pass   # время файла — удобство, не условие корректности

    try:
        os.remove(src)
    except OSError as exc:
        return False, "архив цел, но оригинал не удалён (%s) — дубль на диске" % exc
    return True, "сжат и проверен обратным чтением"


def apply_plan(plan, verbose=True):
    """Выполнить план. -> {'moved','gzipped','skipped','failed','errors':[...]}"""
    result = {"moved": 0, "gzipped": 0, "skipped": 0, "failed": 0, "errors": []}
    for item in plan:
        action, src, dst = item["action"], item["src"], item.get("dst")
        if action == ACTION_SKIP:
            result["skipped"] += 1
            continue
        if not os.path.exists(src):
            result["failed"] += 1
            result["errors"].append("%s: исходник исчез до выполнения" % src)
            continue
        if dst and os.path.exists(dst):
            result["skipped"] += 1
            if verbose:
                print("  ПРОПУСК %s: цель появилась до выполнения" % _short(src))
            continue

        if action == ACTION_MOVE:
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                result["moved"] += 1
                if verbose:
                    print("  ПЕРЕНЁС %s -> %s" % (_short(src), _short(dst)))
            except Exception as exc:
                result["failed"] += 1
                result["errors"].append("перенос %s: %s: %s" % (src, type(exc).__name__, exc))
        elif action == ACTION_GZIP:
            ok, msg = compress_file(src, dst)
            if ok:
                result["gzipped"] += 1
                if verbose:
                    print("  СЖАЛ    %s -> %s" % (_short(src), _short(dst)))
            else:
                result["failed"] += 1
                result["errors"].append("сжатие %s: %s" % (src, msg))
                if verbose:
                    print("  ОШИБКА  %s: %s" % (_short(src), msg))
    return result


# --- печать --------------------------------------------------------------

def _short(path, base=None):
    """Путь покороче — от корня репозитория, если получается."""
    base = base or HERE
    try:
        rel = os.path.relpath(path, base)
    except ValueError:
        return path
    return rel if not rel.startswith("..") else path


def print_plan(plan, base=None, older_than_days=DEFAULT_OLDER_THAN_DAYS, applying=False):
    head = "ПЛАН РОТАЦИИ" if not applying else "ВЫПОЛНЯЮ РОТАЦИЮ"
    print("%s (порог сжатия: старше %d дней; *_latest.* не трогаем)"
          % (head, older_than_days))
    if not plan:
        print("  делать нечего: датированных файлов вне помесячных папок нет, "
              "старых для сжатия нет")
        return
    counts = {ACTION_MOVE: 0, ACTION_GZIP: 0, ACTION_SKIP: 0}
    for item in plan:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
        mark = {ACTION_MOVE: "ПЕРЕНОС", ACTION_GZIP: "СЖАТИЕ ", ACTION_SKIP: "ПРОПУСК"}[item["action"]]
        print("  %s %s -> %s  [%s, возраст %d дн.]"
              % (mark, _short(item["src"], base), _short(item.get("dst") or "-", base),
                 item["reason"], item["age_days"]))
    print("  итого: переносов %d, сжатий %d, пропусков %d"
          % (counts[ACTION_MOVE], counts[ACTION_GZIP], counts[ACTION_SKIP]))


# --- CLI -----------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="D4: раскладка датированных артефактов по месяцам и сжатие старого.")
    parser.add_argument("--repo", default=HERE,
                        help="каталог репозитория (по умолчанию каталог скрипта)")
    parser.add_argument("--roots", default=",".join(DEFAULT_ROOTS),
                        help="каталоги через запятую (по умолчанию %s)" % ",".join(DEFAULT_ROOTS))
    parser.add_argument("--older-than-days", type=int, default=DEFAULT_OLDER_THAN_DAYS,
                        help="сжимать файлы СТРОГО старше этого числа дней (по умолчанию 60)")
    parser.add_argument("--as-of", default=None,
                        help="считать возраст на эту дату YYYY-MM-DD (по умолчанию сегодня UTC)")
    parser.add_argument("--dry-run", action="store_true",
                        help="только план (режим по умолчанию, флаг для явности)")
    parser.add_argument("--apply", action="store_true",
                        help="ВЫПОЛНИТЬ план: переносы и сжатие с удалением оригиналов")
    args = parser.parse_args(argv)

    if args.dry_run and args.apply:
        print("--dry-run и --apply вместе бессмысленны: непонятно, менять файлы или нет.")
        return 2
    if args.older_than_days < 0:
        print("--older-than-days не может быть отрицательным")
        return 2

    roots = tuple(r.strip() for r in args.roots.split(",") if r.strip())
    try:
        plan = plan_actions(repo_dir=args.repo, roots=roots,
                            older_than_days=args.older_than_days, as_of=args.as_of)
    except ValueError as exc:
        print("не разобрал аргументы: %s" % exc)
        return 2

    base = os.path.abspath(args.repo)
    print_plan(plan, base=base, older_than_days=args.older_than_days, applying=args.apply)

    if not args.apply:
        print("режим наблюдения: НИ ОДИН файл не тронут. "
              "Выполнить: python rotate_artifacts.py --apply")
        return 0

    result = apply_plan(plan)
    print("выполнено: переносов %d, сжатий %d, пропусков %d, ошибок %d"
          % (result["moved"], result["gzipped"], result["skipped"], result["failed"]))
    for err in result["errors"]:
        print("  ОШИБКА: %s" % err)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

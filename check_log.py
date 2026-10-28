#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CHECK_LOG v1.3 — еженедельная проверка здоровья candidates_log.jsonl.
ТОЛЬКО ЧТЕНИЕ: ничего не пишет и не меняет.

Зачем: журнал копится вслепую до сентября. Молчаливая поломка
(признак всегда null, отвалился сэмплер контроля, схлопнулась воронка)
обнаружилась бы, когда данные уже потеряны. Этот скрипт печатает
по каждому прогону контрольные числа и кричит о тревогах.

Запуск: кнопкой в GitHub Actions (workflow check-log), раз в неделю.
"""

import json
import os
import statistics
from collections import Counter
from datetime import datetime, timezone

import requests
from secrets_guard import redact

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "candidates_log.jsonl")
# Итог светофора конвейера (sanity.py, блок E1) — ЧИТАЕМ готовый артефакт и
# ничего не пересчитываем: два независимых счёта одного числа рано или поздно
# разойдутся, и тогда непонятно, какому верить.
SANITY_PATH = os.path.join(HERE, "artifacts", "sanity_latest.json")
SANITY_STALE_DAYS = 7     # артефакт старше недели — итог в сводке уже не про сегодня
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Дешёвые признаки, которые НЕ имеют права быть пустыми у всех строк прогона
CHEAP_MUST_HAVE = ["price", "mcap", "vol", "vm", "p7", "p30",
                   "venues_n", "funding_min"]
EXPENSIVE = ["funding_avg3d", "dist_ath", "rng60", "chg30"]
NULL_DRIFT_ALARM = 0.20   # скачок доли null на +20 п.п. к прошлому прогону = тревога
SHRINK_VS_MEDIAN = 0.6    # прогон меньше 60% медианы последних = тревога
MEDIAN_WINDOW = 10        # окно медианы; сверяем при минимум 3 прошлых прогонах


def read_log(path=None):
    """Разбираем журнал: служебные строки и строки кандидатов по прогонам."""
    path = path or LOG_PATH
    runs, rows_by_run, run_order = {}, {}, []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "_meta" in obj:
                    continue
                if "_run" in obj:
                    rid = obj["_run"].get("run_id")
                    runs[rid] = obj["_run"]
                    if rid not in rows_by_run:
                        rows_by_run[rid] = []
                        run_order.append(rid)
                    continue
                rid = obj.get("run_id")
                rows_by_run.setdefault(rid, []).append(obj)
                if rid not in run_order:
                    run_order.append(rid)
    except FileNotFoundError:
        pass
    return runs, rows_by_run, run_order


def check_run(rows, run_info, prev_keys, prev_nulls=None):
    """Проверки одного прогона.
    Возвращает (текст-блок, тревоги, ключи, null-доли) — доли отдаём
    наружу, чтобы следующий прогон мог сверить дрейф."""
    alarms = []
    rows_n = len(rows)
    scored = [r for r in rows if r.get("score") is not None]
    cnt = Counter(r["score"] for r in scored)
    n_scored, ties_n = len(scored), sum(c for c in cnt.values() if c > 1)

    # сверка с заявленным в _run — расхождение это тревога, не заметка
    claimed = (run_info or {}).get("rank_stats") or {}
    if claimed:
        if claimed.get("n_scored") != n_scored or claimed.get("ties_n") != ties_n:
            alarms.append(f"РАСХОЖДЕНИЕ rank_stats: _run говорит "
                          f"{claimed}, пересчёт даёт n_scored={n_scored}, ties_n={ties_n}")

    tiers = Counter(r.get("features_tier", "?") for r in rows)
    if rows_n and tiers.get("random_control", 0) == 0:
        alarms.append(f"контрольная группа ПУСТАЯ: random_control=0 из "
                      f"{rows_n} строк прогона — сэмплер сломан?")

    reasons = Counter((r.get("reject_reason") or "enter") for r in rows)
    if rows_n and len(reasons) <= 1:
        only_name, only_n = reasons.most_common(1)[0]
        alarms.append(f"воронка схлопнулась: 1 причина на весь прогон "
                      f"({only_name}={only_n} из {rows_n} строк, 100%)")

    # доля null по каждому признаку. Для 4 дорогих — знаменатель НЕ весь
    # прогон (там 90% строк не обогащались по замыслу и порог тонет), а
    # обогащённая подвыборка top30+random_control: база ~0%, обычный
    # дрейф-порог работает без исключений.
    enriched = [r for r in rows
                if r.get("features_tier") in ("top30", "random_control")]
    enr_n = len(enriched)
    feat_keys = sorted({k for r in rows for k in (r.get("features") or {})})
    nulls = {}
    # сырые счётчики рядом с долями: у каждой тревоги должно быть число, по
    # которому её можно перепроверить руками, а не одна доля в процентах
    null_counts = {}
    for k in feat_keys:
        base = enriched if k in EXPENSIVE else rows
        denom = enr_n if k in EXPENSIVE else rows_n
        if denom == 0:
            nulls[k] = None
            null_counts[k] = (0, 0)
            continue
        n_null = sum(1 for r in base if (r.get("features") or {}).get(k) is None)
        nulls[k] = n_null / denom
        null_counts[k] = (n_null, denom)
    for k in CHEAP_MUST_HAVE:
        if nulls.get(k, 0) >= 0.999 and rows_n:
            n_null, denom = null_counts.get(k, (0, rows_n))
            alarms.append(f"дешёвый признак «{k}» пуст у ВСЕХ строк: "
                          f"{n_null} из {denom} ({nulls[k]:.1%})")
    # Дорогие признаки: 100% null при живом обогащении = конвейер сломан.
    # Порог полного отказа ловится здесь, потому что дрейф-порог его не
    # видит: базовая null-доля дорогих ~90%, до 100% всего +10 п.п.
    if enr_n > 0:
        for k in EXPENSIVE:
            v = nulls.get(k)
            if v is not None and v >= 0.999:
                n_null, denom = null_counts.get(k, (0, enr_n))
                alarms.append(f"дорогой признак «{k}» пуст у всех обогащённых: "
                              f"{n_null} из {denom} ({v:.1%}) — обогащение сломано?")

    # Ссохшиеся группы: фактический размер против ОЖИДАЕМОГО из конфига
    # прогона (не против прошлого прогона — иначе деградация станет нормой)
    exp = (run_info or {}).get("enrichment") or {}
    thr = ((run_info or {}).get("params") or {}).get("thresholds") or {}
    exp_ctrl = exp.get("expected_control")
    if exp_ctrl is None:
        cfg_n, pool = thr.get("ml_control_n"), exp.get("pool_n")
        exp_ctrl = min(cfg_n, pool) if (cfg_n and pool) else cfg_n
    if exp_ctrl is None:
        # старые прогоны без ml_control_n в конфиге: сверяем хотя бы
        # с ЗАЯВЛЕННЫМ размером выборки — ловим потерю строк лога
        exp_ctrl = exp.get("control_n")
    if exp_ctrl:
        actual = tiers.get("random_control", 0)
        if actual < 0.8 * exp_ctrl:
            alarms.append(f"контроль ссохся: {actual} при ожидаемых ~{exp_ctrl}")
    exp_top = exp.get("expected_top30") or (min(30, n_scored) if n_scored else None)
    if exp_top:
        actual = tiers.get("top30", 0)
        if actual < 0.8 * exp_top:
            alarms.append(f"top30 ссохся: {actual} при ожидаемых ~{exp_top}")
    # Дрейф: резкий рост доли null против прошлого прогона ловит
    # ЧАСТИЧНУЮ поломку источника до того, как она съест месяц данных
    for k, cur in nulls.items():
        prev = (prev_nulls or {}).get(k)
        if cur is None or prev is None:
            continue
        if cur - prev >= NULL_DRIFT_ALARM:
            alarms.append(f"скачок null у «{k}»: {prev:.0%} -> {cur:.0%} "
                          f"(+{(cur - prev) * 100:.0f} п.п.)")

    # джойн с предыдущим прогоном: cg_id, для старых строк — ticker
    key = "cg_id" if any(r.get("cg_id") for r in rows) else "ticker"
    cur_keys = {r.get(key) for r in rows if r.get(key)}
    joined = (len(cur_keys & prev_keys), len(prev_keys)) if prev_keys else None

    null_line = "  null-доли: " + (", ".join(
        f"{k}{'*' if k in EXPENSIVE else ''}={v:.0%}"
        for k, v in nulls.items() if v) or "нет") + \
        "   (* — по обогащённой подвыборке)"
    block = [
        f"rows={rows_n} | n_scored={n_scored} | ties_n={ties_n}"
        + (f" | джойн({key}) с пред.: {joined[0]}/{joined[1]}" if joined else ""),
        f"  ярусы: " + ", ".join(f"{k}={v}" for k, v in tiers.most_common()),
        f"  воронка: " + ", ".join(f"{k}={v}" for k, v in reasons.most_common()),
        null_line,
    ]
    return "\n".join(block), alarms, cur_keys, nulls


def sanity_line(path=None):
    """Итог светофора конвейера (sanity.py) одной строкой + причины.

    Артефакт ЧИТАЕТСЯ как есть, ничего не пересчитывается. Нет файла — так и
    пишем: «санити не прогонялся». Молчание в этом месте хуже плохой новости —
    читатель сводки решит, что светофор зелёный, хотя его просто никто не включал.
    """
    path = path or SANITY_PATH
    try:
        with open(path, encoding="utf-8") as f:
            res = json.load(f)
    except FileNotFoundError:
        return ("Санити (sanity.py): НЕ ПРОГОНЯЛСЯ — файла "
                "artifacts/sanity_latest.json нет.")
    except (OSError, json.JSONDecodeError) as e:
        return f"Санити (sanity.py): артефакт не читается ({e}) — итог неизвестен."

    verdict = res.get("verdict", "?")
    counts = res.get("counts") or {}
    ts = (res.get("provenance") or {}).get("generated_utc") or res.get("as_of_utc")
    line = (f"Санити (sanity.py): {verdict} "
            f"(FAIL={counts.get('fail', '?')}, WARN={counts.get('warn', '?')}, "
            f"PASS={counts.get('pass', '?')}), снят {ts or '?'}")
    # Возраст артефакта печатается всегда: зелёный светофор недельной давности
    # рассказывает о прошлой неделе, а выглядит как сегодняшний.
    try:
        when = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - when).total_seconds() / 86400.0
        line += f", возраст {age:.1f} дн"
        if age > SANITY_STALE_DAYS:
            line += (f" — УСТАРЕЛ (порог {SANITY_STALE_DAYS} дн), "
                     f"прогнать sanity.py заново")
    except (ValueError, TypeError):
        line += ", возраст неизвестен (время снятия не разобрано)"

    reasons = res.get("reasons") or []
    if reasons:
        line += "\n" + "\n".join("• " + str(r) for r in reasons[:5])
        if len(reasons) > 5:
            line += f"\n• …ещё причин: {len(reasons) - 5}"
    return line


def main():
    runs, rows_by_run, order = read_log()
    if not order:
        print("Журнал пуст или отсутствует — проверять нечего.")
        print("\n" + sanity_line())   # итог светофора нужен и здесь: пустой
        return 0                      # журнал — сам по себе повод его прочесть
    print(f"Журнал: прогонов {len(order)}\n" + "=" * 60)
    all_alarms, prev_keys, prev_nulls = [], set(), {}
    hist_rows, hist_pool = [], []   # история размеров для медианной эвристики
    last_block = ""
    for rid in order:
        info = runs.get(rid) or {}
        ts = info.get("timestamp_utc") or (rows_by_run[rid][0].get("timestamp_utc")
                                           if rows_by_run[rid] else "?")
        block, alarms, prev_keys, prev_nulls = check_run(
            rows_by_run.get(rid, []), info, prev_keys, prev_nulls)
        # Схлопнувшаяся вселенная/пруд: конфиг-якоря нет — размер законно
        # плавает (окно капы, листинги). Сверяем с МЕДИАНОЙ последних
        # прогонов, не с предыдущим — один ссохшийся прогон не сдвинет
        # норму. Это честная эвристика, а не якорь: медленную усадку за
        # месяцы она пропустит, окончательно закрывает только разметка.
        cur_rows = len(rows_by_run.get(rid, []))
        cur_pool = (info.get("enrichment") or {}).get("pool_n")
        for label, cur, hist in (("вселенная (rows_n)", cur_rows, hist_rows),
                                 ("пруд скоринга (pool_n)", cur_pool, hist_pool)):
            if cur is None:
                continue
            if len(hist) >= 3:
                med = statistics.median(hist[-MEDIAN_WINDOW:])
                if med and cur < SHRINK_VS_MEDIAN * med:
                    alarms.append(f"{label} схлопнулась: {cur} при медиане "
                                  f"~{med:.0f} последних прогонов (эвристика)")
            hist.append(cur)
        print(f"\n[{ts}] run={rid}\n{block}")
        for a in alarms:
            print(f"  !! {a}")
            all_alarms.append(f"[{ts}] {a}")
        last_block = f"[{ts}]\n{block}"

    print("\n" + "=" * 60)
    verdict = ("Тревог нет — журнал здоров." if not all_alarms
               else f"ТРЕВОГ: {len(all_alarms)}")
    print(verdict)

    # итог светофора конвейера — последней строкой и в консоли, и в Telegram
    sanity = sanity_line()
    print("\n" + sanity)

    # короткая сводка в Telegram: последний прогон + тревоги + санити
    if TG_TOKEN and TG_CHAT:
        msg = (f"Проверка журнала кандидатов\n{verdict}\n\nПоследний прогон:\n{last_block}")
        if all_alarms:
            msg += "\n\nТревоги:\n" + "\n".join("• " + a for a in all_alarms[:10])
        # Режем ТЕЛО сводки, а не хвост: иначе при длинном списке тревог итог
        # светофора молча пропадёт ровно в тот раз, когда он важнее всего.
        tail = "\n\n" + sanity
        msg = msg[:max(0, 4000 - len(tail))] + tail
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": msg}, timeout=30)
        except Exception as e:
            # redact: в тексте исключения лежит URL с токеном бота.
            print("Telegram недоступен: %s" % redact(e))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

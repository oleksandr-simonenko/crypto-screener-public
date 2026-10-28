#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
СИМУЛЯЦИЯ ВЫХОДОВ v1 — сколько стоит неправильная продажа.

Сегодня система держит позицию ровно 60 дней и продаёт «по звонку».
Этот скрипт берёт ТЕ ЖЕ исторические сделки и прогоняет их через
разные правила продажи, день за днём по реальному пути цены.

Правила в сетке (перебираем все, сравниваем с пассивным удержанием):
  - тейк: продать всё при росте в 1.5x / 2x / 3x
  - скользящий стоп: продать при откате на 20/30/40/50% от максимума
  - комбинация: часть на тейке, остаток под скользящим стопом
  - стоп-лосс: жёсткий выход при падении на 30/50% от входа

Важно: считается на дневных ценах закрытия. Внутридневные проколы не
видны — реальный результат стопов будет чуть хуже расчётного.
Никаких предсказаний: правила механические, срабатывают по факту цены.

Запуск: кнопкой в Actions (workflow exits). ~10 минут.
"""

import os
import sys
import time
from datetime import date

import requests
from secrets_guard import redact

CG_KEY = os.getenv("COINGECKO_API_KEY", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MCAP_MIN, MCAP_MAX = 5_000_000, 150_000_000
MAX_COINS = int(os.getenv("BT_MAX_COINS", "300"))
HOLD = 60
BASE_WIN = 60
STEP = 7
VM_MIN = 0.3
BASE_RANGE = 1.6
COOLDOWN = 56

S = requests.Session()
S.headers.update({"User-Agent": "pre-beat-exits/1.0"})


def tg(text):
    if not TG_TOKEN or not TG_CHAT:
        print(text)
        return
    for i in range(0, len(text), 3800):
        try:
            r = S.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                       json={"chat_id": TG_CHAT, "text": text[i:i + 3800],
                             "disable_web_page_preview": True}, timeout=30)
            if r.status_code != 200:
                # redact ДО среза (С-17/С-19): ответ Telegram может процитировать
                # запрошенный URL с токеном, а срез разрезал бы токен пополам.
                print(f"ТЕЛЕГРАМ ОТКЛОНИЛ ({r.status_code}): {redact(r.text)[:300]}")
        except Exception as e:
            print(f"Telegram недоступен: {redact(e)}")


def cg_get(path, params):
    headers = {"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}
    for attempt in range(3):
        try:
            r = S.get(f"https://api.coingecko.com/api/v3{path}",
                      params=params, headers=headers, timeout=40)
            if r.status_code == 429:
                time.sleep(65)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5)
    return {}


def fetch_universe():
    out, page = [], 1
    while page <= 8:
        js = cg_get("/coins/markets", {"vs_currency": "usd",
                    "order": "market_cap_desc", "per_page": 250,
                    "page": page, "sparkline": "false"})
        if not js:
            break
        stop = False
        for c in js:
            m = c.get("market_cap") or 0
            if m and m < MCAP_MIN:
                stop = True
                break
            if m and MCAP_MIN <= m <= MCAP_MAX:
                out.append((c["id"], (c.get("symbol") or "").upper()))
        if stop:
            break
        page += 1
        time.sleep(1.0 if CG_KEY else 12)
    return out


def fetch_mexc_syms():
    out = set()
    try:
        r = S.get("https://contract.mexc.com/api/v1/contract/ticker", timeout=30)
        r.raise_for_status()
        for row in r.json().get("data", []):
            sym = row.get("symbol", "")
            if sym.endswith("_USDT"):
                out.add(sym.split("_")[0])
    except Exception as e:
        print(f"MEXC недоступен: {redact(e)}")
    return out


def market_chart(cid):
    d = cg_get(f"/coins/{cid}/market_chart",
               {"vs_currency": "usd", "days": 365, "interval": "daily"})
    prices = [p[1] or 0 for p in d.get("prices", [])]
    mcaps = [p[1] or 0 for p in d.get("market_caps", [])]
    vols = [p[1] or 0 for p in d.get("total_volumes", [])]
    n = min(len(prices), len(mcaps), len(vols))
    return prices[:n], mcaps[:n], vols[:n]


def collect_paths(prices, mcaps, vols):
    """Возвращает ПУТИ цены сигнальных сделок: список списков
    [1.0, p1/p0, p2/p0, ...] длиной HOLD+1. Путь нужен, чтобы
    проигрывать правила выхода день за днём, а не по итогу."""
    paths = []
    n = len(prices)
    last_sig = -10**9
    for t in range(BASE_WIN, n - HOLD, STEP):
        p, m, v = prices[t], mcaps[t], vols[t]
        if not p or not m or not (MCAP_MIN <= m <= MCAP_MAX):
            continue
        win = prices[t - BASE_WIN:t]
        lo = min(win)
        if lo <= 0:
            continue
        hi, med = max(win), sorted(win)[len(win) // 2]
        if not ((hi / lo < BASE_RANGE) and (0.75 * med <= p <= 1.3 * med)):
            continue
        if (v / m) < VM_MIN or (t - last_sig) < COOLDOWN:
            continue
        seg = prices[t:t + HOLD + 1]
        if any(x <= 0 for x in seg):
            continue
        paths.append([x / p for x in seg])   # путь в разах от входа
        last_sig = t
    return paths


# --- Правила выхода. Каждое получает путь и возвращает итог в разах ---

def rule_hold(path, **kw):
    """Пассивно: держим все 60 дней."""
    return path[-1]


def rule_take(path, mult=2.0, **kw):
    """Тейк: продали всё, как только цена достигла mult."""
    for x in path:
        if x >= mult:
            return mult
    return path[-1]


def rule_trail(path, drop=0.30, **kw):
    """Скользящий стоп: выходим при откате на drop от достигнутого максимума.
    Победителям даёт бежать сколько угодно."""
    peak = path[0]
    for x in path:
        peak = max(peak, x)
        if x <= peak * (1 - drop):
            return x
    return path[-1]


def rule_stop(path, loss=0.50, **kw):
    """Жёсткий стоп-лосс от цены входа."""
    for x in path:
        if x <= 1 - loss:
            return x
    return path[-1]


def rule_combo(path, part=0.25, mult=2.0, drop=0.40, **kw):
    """Комбинация: долю part продаём на тейке mult,
    остаток ведём скользящим стопом drop."""
    taken, rest = 0.0, 1.0
    peak = path[0]
    for x in path:
        peak = max(peak, x)
        if rest == 1.0 and x >= mult:
            taken = part * mult
            rest = 1 - part
        if rest < 1.0 and x <= peak * (1 - drop):
            return taken + rest * x
        if rest == 1.0 and x <= peak * (1 - drop):
            return x
    return taken + rest * path[-1]


def stats(results):
    if not results:
        return None
    xs = sorted(results)
    n = len(xs)
    return {
        "n": n,
        "mean": (sum(xs) / n - 1) * 100,
        "median": (xs[n // 2] - 1) * 100,
        "win": sum(1 for x in xs if x > 1) / n * 100,
        "worst": (xs[0] - 1) * 100,
        "best": (xs[-1] - 1) * 100,
    }


def main():
    print("1/3 вселенная и перпы...")
    uni = fetch_universe()
    mexc = fetch_mexc_syms()
    sample = [(cid, sym) for cid, sym in uni if sym in mexc][:MAX_COINS]
    print(f"   монет к обработке: {len(sample)}")

    print("2/3 качаю пути цен (~8 мин)...")
    paths, ok = [], 0
    for i, (cid, sym) in enumerate(sample, 1):
        try:
            pr, mc, vl = market_chart(cid)
            if len(pr) < BASE_WIN + HOLD + STEP:
                continue
            paths += collect_paths(pr, mc, vl)
            ok += 1
        except Exception:
            pass
        if i % 50 == 0:
            print(f"   {i}/{len(sample)}, сделок {len(paths)}")
        time.sleep(1.0 if CG_KEY else 8)

    print(f"3/3 проигрываю правила на {len(paths)} сделках...")
    grid = [("пассивно 60 дней (как сейчас)", rule_hold, {})]
    for m in (1.5, 2.0, 3.0):
        grid.append((f"тейк на x{m}", rule_take, {"mult": m}))
    for d in (0.20, 0.30, 0.40, 0.50):
        grid.append((f"скользящий стоп −{d:.0%} от пика", rule_trail, {"drop": d}))
    for l in (0.30, 0.50):
        grid.append((f"стоп-лосс −{l:.0%} от входа", rule_stop, {"loss": l}))
    for part, m, d in ((0.25, 2.0, 0.40), (0.50, 2.0, 0.40), (0.50, 1.5, 0.30)):
        grid.append((f"{part:.0%} на x{m} + стоп −{d:.0%}", rule_combo,
                     {"part": part, "mult": m, "drop": d}))

    base = None
    rows = []
    for label, fn, kw in grid:
        st = stats([fn(p, **kw) for p in paths])
        if st is None:
            continue
        if base is None:
            base = st["mean"]
        rows.append((label, st))

    lines = [f"СИМУЛЯЦИЯ ВЫХОДОВ — {date.today():%d.%m.%Y}",
             f"Монет с историей: {ok} | сделок: {len(paths)}",
             "Каждое правило проиграно день за днём по реальному пути цены.",
             ""]
    for label, st in rows:
        delta = st["mean"] - base
        mark = "" if abs(delta) < 0.05 else f" | к пассиву {delta:+.1f} п.п."
        lines.append(f"{label}: среднее {st['mean']:+.1f}% | "
                     f"медиана {st['median']:+.1f}% | в плюсе {st['win']:.0f}%{mark}")
        lines.append(f"   худшая {st['worst']:+.0f}% | лучшая {st['best']:+.0f}%")

    best = max(rows, key=lambda r: r[1]["mean"]) if rows else None
    lines.append("")
    if best:
        gain = best[1]["mean"] - base
        lines.append(f"ЛУЧШЕЕ: {best[0]} — среднее {best[1]['mean']:+.1f}%, "
                     f"это {gain:+.1f} п.п. к пассивному удержанию.")
        if gain < 2:
            lines.append("Разница мала — правило выхода на этих данных не решает.")
    lines.append("")
    lines.append("Ограничения: дневные цены (внутридневные проколы стопов не видны, "
                 "реальность будет чуть хуже); ошибка выжившего — умершие монеты "
                 "выпали, цифры оптимистичны; комиссии и проскальзывание не учтены.")

    report = "\n".join(lines)
    print(report)
    tg(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

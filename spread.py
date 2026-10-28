#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
СПРЕД-АНАЛИЗ v1 — можно ли зарабатывать независимо от направления рынка.

Считает на тех же данных, что и backtest.py, но отвечает на другие вопросы:
  1) СТАБИЛЬНОСТЬ. Спред (сигнал минус контроль) по периодам: он ровный
     или это одна удачная неделя, вытянувшая среднее?
  2) РЕЖИМЫ. Спред отдельно в падающем, боковом и растущем рынке —
     работает ли идея в обе стороны или только на отскоках?
  3) ФИЛЬТР. Что даёт правило «торгуем только когда BTC выше своей
     50-дневной средней» — сколько убытка оно срезает?
  4) ВЫХОДЫ. Что даёт правило «половину продаём при удвоении» против
     пассивного удержания 60 дней.

Спред = разница медиан. Экономический смысл: столько заработала бы
рыночно-нейтральная связка (купил сигнальные монеты, продал в шорт
столько же «средних»), то есть заработок, очищенный от направления рынка.

Ничего не пишет и не меняет. Только читает данные и печатает отчёт.
Запуск: кнопкой в Actions (workflow spread). ~10 минут.
"""

import os
import sys
import time
from datetime import date, timedelta

import requests
from secrets_guard import redact

CG_KEY = os.getenv("COINGECKO_API_KEY", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MCAP_MIN, MCAP_MAX = 5_000_000, 150_000_000
BT_MAX_COINS = int(os.getenv("BT_MAX_COINS", "300"))
HOLD = 60          # форвард-горизонт, дней
BASE_WIN = 60      # длина «базы» перед сигналом
STEP = 7           # шаг прохода по истории
VM_MIN = 0.3       # порог объём/капа
BASE_RANGE = 1.6   # «плоская» база: max/min < 1.6
COOLDOWN = 56      # не дублируем сигнал 8 недель
MA_WIN = 50        # окно средней BTC для режимного фильтра
BUCKET = 42        # длина периода для проверки стабильности, дней (~6 недель)

S = requests.Session()
S.headers.update({"User-Agent": "pre-beat-spread/1.0"})


def tg(text):
    if not TG_TOKEN or not TG_CHAT:
        print(text)
        return
    # Шлём ПРОСТЫМ текстом: в отчёте есть символы < и >, а в HTML-режиме
    # Telegram считает их тегами и отклоняет сообщение с ошибкой 400.
    # Ответ сервера проверяем — молчаливых сбоев быть не должно.
    for i in range(0, len(text), 3800):
        try:
            r = S.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                       json={"chat_id": TG_CHAT, "text": text[i:i + 3800],
                             "disable_web_page_preview": True},
                       timeout=30)
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
        except Exception as e:
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
        time.sleep(2.5 if CG_KEY else 12)
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


def analyze(prices, mcaps, vols):
    """Как в backtest.py, но КАЖДАЯ точка помечена днём решения t —
    без этого нельзя разложить результат по периодам и режимам."""
    sig, ctrl = [], []
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
        fwd = prices[t + HOLD] / p - 1
        peak = max(prices[t:t + HOLD + 1]) / p - 1
        rec = (t, fwd, peak)
        if (v / m) >= VM_MIN:
            if (t - last_sig) >= COOLDOWN:
                sig.append(rec)
                last_sig = t
        else:
            ctrl.append(rec)
    return sig, ctrl


def median(xs):
    if not xs:
        return None
    s = sorted(xs)
    return s[len(s) // 2]


def spread(sig, ctrl):
    """Спред = медиана сигнала минус медиана контроля, в п.п."""
    ms, mc = median([x[1] for x in sig]), median([x[1] for x in ctrl])
    if ms is None or mc is None:
        return None
    return (ms - mc) * 100


def btc_context(n_days):
    """История BTC: состояние тренда на день решения (выше/ниже средней)
    и движение рынка за форвардное окно."""
    d = cg_get("/coins/bitcoin/market_chart",
               {"vs_currency": "usd", "days": 365, "interval": "daily"})
    px = [p[1] or 0 for p in d.get("prices", [])]
    above, fwd_ret = {}, {}
    for t in range(len(px)):
        if t >= MA_WIN and px[t]:
            ma = sum(px[t - MA_WIN:t]) / MA_WIN
            above[t] = px[t] > ma          # известно В МОМЕНТ решения
        if t + HOLD < len(px) and px[t]:
            fwd_ret[t] = px[t + HOLD] / px[t] - 1   # известно ПОСЛЕ
    return above, fwd_ret, len(px)


def main():
    print("1/4 вселенная и перпы MEXC...")
    uni = fetch_universe()
    mexc = fetch_mexc_syms()
    sample = [(cid, sym) for cid, sym in uni if sym in mexc][:BT_MAX_COINS]
    print(f"   в окне {len(uni)}, с перпами {len(sample)}")

    print("2/4 контекст рынка (BTC)...")
    btc_above, btc_fwd, btc_len = btc_context(365)

    print("3/4 качаю историю монет (~10 мин)...")
    sig, ctrl, ok = [], [], 0
    for i, (cid, sym) in enumerate(sample, 1):
        try:
            prices, mcaps, vols = market_chart(cid)
            if len(prices) < BASE_WIN + HOLD + STEP:
                continue
            s_, c_ = analyze(prices, mcaps, vols)
            sig += s_
            ctrl += c_
            ok += 1
        except Exception:
            pass
        if i % 50 == 0:
            print(f"   {i}/{len(sample)}, сигналов {len(sig)}")
        time.sleep(1.6 if CG_KEY else 8)

    print("4/4 считаю...")
    lines = [f"СПРЕД-АНАЛИЗ — {date.today():%d.%m.%Y}",
             f"Монет с историей: {ok} | сигналов {len(sig)}, контроль {len(ctrl)}",
             "Спред = медиана сигнала − медиана контроля. Это заработок "
             "рыночно-нейтральной связки: купил сигнал, продал столько же «среднего».",
             ""]

    base = spread(sig, ctrl)
    lines.append(f"1. ОБЩИЙ СПРЕД: {base:+.1f} п.п. за 60 дней"
                 if base is not None else "1. Спред: данных мало")

    # --- 2. Стабильность по периодам ---
    lines.append("")
    lines.append("2. СТАБИЛЬНОСТЬ (спред по периодам)")
    buckets = {}
    for name, pts in (("s", sig), ("c", ctrl)):
        for t, fwd, peak in pts:
            buckets.setdefault(t // BUCKET, {"s": [], "c": []})[name].append((t, fwd, peak))
    pos = tot = 0
    for b in sorted(buckets):
        v = spread(buckets[b]["s"], buckets[b]["c"])
        ns = len(buckets[b]["s"])
        if v is None or ns < 3:
            continue
        tot += 1
        pos += 1 if v > 0 else 0
        lines.append(f"  период {b + 1} (n={ns}): {v:+.1f} п.п.")
    if tot:
        lines.append(f"  → положительных периодов: {pos} из {tot}. "
                     + ("Спред устойчив." if pos / tot >= 0.7 else
                        "Спред неровный — вытянут отдельными периодами."))

    # --- 3. Режимы рынка (по движению BTC за форвардное окно) ---
    lines.append("")
    lines.append("3. РЕЖИМЫ РЫНКА (по движению BTC за те же 60 дней)")
    regs = {"падение (BTC < −10%)": (-9, -0.10),
            "боковик (−10..+10%)": (-0.10, 0.10),
            "рост (BTC > +10%)": (0.10, 9)}
    for label, (lo, hi) in regs.items():
        s_ = [x for x in sig if lo <= btc_fwd.get(x[0], 0) < hi]
        c_ = [x for x in ctrl if lo <= btc_fwd.get(x[0], 0) < hi]
        v = spread(s_, c_)
        ms = median([x[1] for x in s_])
        if v is None or len(s_) < 3:
            lines.append(f"  {label}: точек мало")
            continue
        lines.append(f"  {label}: спред {v:+.1f} п.п. | сам сигнал "
                     f"{ms * 100:+.0f}% | n={len(s_)}")

    # --- 4. Фильтр тренда BTC (известен в момент решения) ---
    lines.append("")
    lines.append("4. ФИЛЬТР «BTC выше 50-дневной средней» (решение принимаемо в моменте)")
    on = [x for x in sig if btc_above.get(x[0]) is True]
    off = [x for x in sig if btc_above.get(x[0]) is False]
    for label, pts in (("торгуем (BTC в аптренде)", on), ("пропускаем (даунтренд)", off)):
        m = median([x[1] for x in pts])
        if m is None or len(pts) < 3:
            lines.append(f"  {label}: точек мало")
            continue
        wr = sum(1 for x in pts if x[1] > 0) / len(pts) * 100
        lines.append(f"  {label}: медиана {m * 100:+.1f}% | winrate {wr:.0f}% | n={len(pts)}")

    # --- 5. Правило выхода: половина при удвоении ---
    lines.append("")
    lines.append("5. ПРАВИЛО ВЫХОДА (половину продаём при +100%)")
    hold_r = [x[1] for x in sig]
    take_r = [(0.5 * 1.0 + 0.5 * x[1]) if x[2] >= 1.0 else x[1] for x in sig]
    if hold_r:
        lines.append(f"  пассивно 60д: медиана {median(hold_r) * 100:+.1f}% | "
                     f"среднее {sum(hold_r) / len(hold_r) * 100:+.1f}%")
        lines.append(f"  с тейком x2: медиана {median(take_r) * 100:+.1f}% | "
                     f"среднее {sum(take_r) / len(take_r) * 100:+.1f}%")
        lines.append(f"  сделок, ходивших в +100%: "
                     f"{sum(1 for x in sig if x[2] >= 1.0)} из {len(sig)}")

    lines.append("")
    lines.append("Ограничения: ошибка выжившего (умершие монеты выпали из данных) — "
                 "цифры оптимистичны; спред не учитывает стоимость шорта (фандинг) "
                 "и комиссии; год истории = мало независимых периодов. "
                 "Это диагностика направления, не готовая стратегия.")

    report = "\n".join(lines)
    print(report)
    tg(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

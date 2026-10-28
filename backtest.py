#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
БЭКТЕСТ v1 — исторический тест усечённого сигнала
===================================================
Что проверяем: компоненту сигнала, которую МОЖНО проверить на бесплатной
истории — «монета в окне $5–150M + плоская база 60 дней + всплеск
объём/капа ≥ 0.3». Форвард-доходность меряем через 60 дней.

Как устроен эксперимент (это важно понимать):
  СИГНАЛ   = база + объёмный триггер сработал
  КОНТРОЛЬ = та же база, но БЕЗ объёмного триггера
  Сравниваем распределения форвардов. Если сигнал не лучше контроля —
  объёмная компонента скоринга не добавляет ничего.

Честные ограничения (не мелкий шрифт, а суть):
  1) ОШИБКА ВЫЖИВШЕГО: делистнутые и умершие монеты исчезли из данных
     CoinGecko — результат является ОПТИМИСТИЧНОЙ ВЕРХНЕЙ ГРАНИЦЕЙ.
  2) Фандинг исторически не проверяется (бесплатной глубокой истории
     нет) — эту компоненту закрывает форвард-тест бумажного портфеля.
  3) Флоат исторически недоступен — не тестируется.
Запуск: кнопкой в Actions (workflow "backtest"). Идёт ~10–15 минут.
"""

import os
import sys
import time
from datetime import datetime, timezone

import requests
from secrets_guard import redact

CG_KEY = os.getenv("COINGECKO_API_KEY", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# --- Параметры эксперимента (можно крутить через env в workflow) -----------
BT_MAX_COINS = int(os.getenv("BT_MAX_COINS", "300"))  # размер выборки
MCAP_MIN, MCAP_MAX = 5e6, 150e6   # окно капитализации — как у скринера
HOLD = 60          # форвард-горизонт, дней (= выдержке бумажного портфеля)
BASE_WIN = 60      # длина «базы» перед сигналом, дней
STEP = 7           # шаг прохода по истории, дней (недельный, как прогоны)
VM_MIN = 0.3       # порог объём/капа — как в скоринге
BASE_RANGE = 1.6   # max/min цены в базе < 1.6 = «плоско» (диапазон < 60%)
COOLDOWN = 56      # после сигнала 8 недель не дублируем его же

S = requests.Session()
S.headers.update({"User-Agent": "pre-beat-backtest/1.0"})


def tg(text):
    if not TG_TOKEN or not TG_CHAT:
        print(text)
        return
    try:
        S.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
               json={"chat_id": TG_CHAT, "text": text[:4000],
                     "parse_mode": "HTML",
                     "disable_web_page_preview": True}, timeout=30)
    except Exception as e:
        print(f"Telegram недоступен: {redact(e)}")


def cg_get(path, params):
    """GET к CoinGecko с ключом и обработкой 429."""
    headers = {"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}
    for _ in range(3):
        r = S.get(f"https://api.coingecko.com/api/v3{path}",
                  params=params, headers=headers, timeout=40)
        if r.status_code == 429:
            print("429 от CoinGecko — жду 65 сек...")
            time.sleep(65)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("CoinGecko не отвечает")


def fetch_universe():
    """Монеты в окне капы СЕГОДНЯ (та самая ошибка выжившего — признаём)."""
    coins, page = [], 1
    while page <= 20:
        batch = cg_get("/coins/markets", {
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": 250, "page": page, "sparkline": "false"})
        if not batch:
            break
        page_min = None
        for c in batch:
            m = c.get("market_cap")
            if not m:
                continue
            page_min = m
            if MCAP_MIN <= m <= MCAP_MAX:
                coins.append((c["id"], (c.get("symbol") or "").upper(), m))
        if page_min is not None and page_min < MCAP_MIN:
            break
        page += 1
        time.sleep(2.2 if CG_KEY else 15)
    return coins


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
    """Годовая дневная история: цены, капы, объёмы (списки чисел)."""
    d = cg_get(f"/coins/{cid}/market_chart",
               {"vs_currency": "usd", "days": 365, "interval": "daily"})
    prices = [p[1] or 0 for p in d.get("prices", [])]
    mcaps = [p[1] or 0 for p in d.get("market_caps", [])]
    vols = [p[1] or 0 for p in d.get("total_volumes", [])]
    n = min(len(prices), len(mcaps), len(vols))
    return prices[:n], mcaps[:n], vols[:n]


def analyze(prices, mcaps, vols):
    """Проход по истории одной монеты. Возвращает (сигналы, контроль):
    каждый элемент = (форвард_60д, пик_внутри_60д)."""
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
        hi = max(win)
        med = sorted(win)[len(win) // 2]
        in_base = (hi / lo < BASE_RANGE) and (0.75 * med <= p <= 1.3 * med)
        if not in_base:
            continue
        fwd = prices[t + HOLD] / p - 1
        peak = max(prices[t:t + HOLD + 1]) / p - 1
        if (v / m) >= VM_MIN:
            if (t - last_sig) >= COOLDOWN:
                sig.append((fwd, peak))
                last_sig = t
            # сигнал в кулдауне — не считаем НИКУДА: в контроле ему не место
        else:
            ctrl.append((fwd, peak))
    return sig, ctrl


def stats(points):
    if not points:
        return None
    fwds = sorted(x[0] for x in points)
    peaks = [x[1] for x in points]
    n = len(fwds)
    return {
        "n": n,
        "winrate": sum(1 for f in fwds if f > 0) / n * 100,
        "median": fwds[n // 2] * 100,
        "mean": sum(fwds) / n * 100,
        "p90": fwds[min(n - 1, int(n * 0.9))] * 100,
        "worst": fwds[0] * 100,
        "x2_share": sum(1 for p in peaks if p >= 1.0) / n * 100,
    }


def fmt(s, label):
    if not s:
        return f"{label}: точек нет"
    return (f"<b>{label}</b> (n={s['n']}): winrate {s['winrate']:.0f}% | "
            f"медиана {s['median']:+.1f}% | среднее {s['mean']:+.1f}% | "
            f"p90 {s['p90']:+.0f}% | пик≥x2 у {s['x2_share']:.0f}% | "
            f"худший {s['worst']:+.0f}%")


def main():
    print("1/3 вселенная и перпы MEXC...")
    uni = fetch_universe()
    mexc = fetch_mexc_syms()
    sample = [(cid, sym) for cid, sym, _ in uni if sym in mexc][:BT_MAX_COINS]
    print(f"   в окне: {len(uni)}, с перпами MEXC: {len(sample)} (лимит {BT_MAX_COINS})")

    print("2/3 качаю годовую историю по монетам (~10 мин)...")
    all_sig, all_ctrl, ok = [], [], 0
    for i, (cid, sym) in enumerate(sample, 1):
        try:
            prices, mcaps, vols = market_chart(cid)
            if len(prices) < BASE_WIN + HOLD + STEP:
                continue
            s_, c_ = analyze(prices, mcaps, vols)
            all_sig += s_
            all_ctrl += c_
            ok += 1
        except Exception as e:
            print(f"   {sym}: пропуск ({redact(e)})")
        if i % 25 == 0:
            print(f"   {i}/{len(sample)} монет, сигналов {len(all_sig)}")
        time.sleep(2.1 if CG_KEY else 12)

    print("3/3 считаю статистику...")
    s_sig, s_ctrl = stats(all_sig), stats(all_ctrl)
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    lines = [
        f"<b>Бэктест v1 — {now}</b>",
        f"Усечённый сигнал: окно $5–150M + база {BASE_WIN}д + объём/капа ≥ {VM_MIN}. "
        f"Форвард {HOLD}д. Монет с историей: {ok}.",
        "",
        fmt(s_sig, "СИГНАЛ (база+объём)"),
        fmt(s_ctrl, "КОНТРОЛЬ (база без объёма)"),
    ]
    if s_sig and s_ctrl:
        diff = s_sig["median"] - s_ctrl["median"]
        lines.append("")
        lines.append(f"Разница медиан: {diff:+.1f} п.п. → объёмный триггер "
                     f"{'ДОБАВЛЯЕТ преимущество' if diff > 0 else 'преимущества НЕ показал'} "
                     f"на этой выборке.")
    lines += [
        "",
        "Честно: это ОПТИМИСТИЧНАЯ ВЕРХНЯЯ ГРАНИЦА — делистнутые монеты из "
        "данных исчезли (ошибка выжившего). Фандинг и флоат исторически не "
        "проверяемы — их меряет форвард-тест бумажного портфеля. "
        "Решения о размере ставок на бэктесте не строим.",
    ]
    tg("\n".join(lines))
    print("Готово.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

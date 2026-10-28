#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
REVOLUT-СКАН v1 — первый машинный проход по ВСЕМУ каталогу Revolut
(минус портфель пользователя, он разобран отдельно).

Что делает: берёт список revolut_symbols из config.yaml (~300 тикеров),
исключает портфель и стейблы/обёртки, по каждой монете собирает данные
CoinGecko и раскладывает на ярусы:
  ЯДРО-КАНДИДАТ  — прошла все ворота, высокий балл: сюда смотрим глубоко
  ПРОВЕРИТЬ      — прошла ворота, балл средний
  НЕТ            — с конкретной причиной (мёртвая ликвидность, нож,
                   навес нераспределённых токенов, мелочь, не найдена)

Это КОЛИЧЕСТВЕННЫЙ отсев, не финальный вердикт: выжившие идут на
качественную проверку (новости, разлоки, команда) отдельным шагом.

ЧЕКПОИНТ: прогресс пишется в scan_state.json после каждой пачки.
Упал/прерван — следующий запуск продолжает с места, ничего не пересчитывая.
Полный отчёт: revolut_scan_report.md (коммитится в репо) + сводка в Telegram.
"""

import json
import os
import sys
import time
from datetime import date, datetime, timezone

import requests
import yaml
from secrets_guard import redact

CG_KEY = os.getenv("COINGECKO_API_KEY", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "scan_state.json")
REPORT_PATH = os.path.join(HERE, "revolut_scan_report.md")

# Портфель пользователя — разобран отдельно, из скана исключаем
PORTFOLIO = {"ICP", "XRP", "ONDO", "LCX", "WLD", "XCN", "TON", "GRAM", "AI",
             "AKT", "AIOZ", "SOL", "IOTX", "TIA", "FET", "SUI", "RENDER",
             "STRK", "ZK", "ZRO", "HFT", "XLM", "USDC"}
# Стейблы и обёртки — не инвестиция по определению
NON_INVEST = {"USDT", "DAI", "TUSD", "USDP", "PYUSD", "EURC", "EURT", "GUSD",
              "FDUSD", "USDE", "WBTC", "WETH", "STETH", "WSTETH", "CBETH",
              "RETH", "USDS", "USD1"}

# Ворота отсева (обоснование: философия «доживёт до 2027»)
MIN_MCAP = 30_000_000       # мельче — лотерея вне Revolut-логики
MIN_VOL = 1_500_000         # $/сутки; тоньше — не выйти без проскальзывания
KNIFE_P30 = -35.0           # падение за месяц глубже — нож, не ловим
MAX_FDV_RATIO = 4.0         # навес нераспределённых токенов больше 4x
ENRICH_TOP = 45             # скольким финалистам тянуть годовой график

S = requests.Session()
S.headers.update({"User-Agent": "pre-beat-revolut-scan/1.0"})


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


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"done": {}, "enriched": {}, "meta": {}}


def save_state(st):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def load_catalog():
    with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    syms = [s.upper() for s in (cfg.get("revolut_symbols") or [])]
    return [s for s in syms if s and s not in PORTFOLIO]


def fetch_market_map(pages=13):
    """Топ ~3250 монет по капе: symbol -> строка markets. При коллизии
    тикеров берём БОЛЬШУЮ по капе (Revolut листит крупные версии)."""
    m = {}
    for page in range(1, pages + 1):
        js = cg_get("/coins/markets", {
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": 250, "page": page, "sparkline": "false",
            "price_change_percentage": "7d,30d"})
        if not js:
            break
        for c in js:
            sym = (c.get("symbol") or "").upper()
            cur = m.get(sym)
            if cur is None or (c.get("market_cap") or 0) > (cur.get("market_cap") or 0):
                m[sym] = c
        time.sleep(1.2 if CG_KEY else 10)
    return m


def evaluate(sym, c):
    """Ворота + балл. Возвращает строку результата для одной монеты."""
    if sym in NON_INVEST:
        return {"sym": sym, "tier": "НЕТ", "reason": "стейбл/обёртка — не инвестиция"}
    if c is None:
        return {"sym": sym, "tier": "НЕТ",
                "reason": "не найдена в топ-3250 CoinGecko (мёртвая/переименована?)"}
    mcap = c.get("market_cap") or 0
    vol = c.get("total_volume") or 0
    fdv = c.get("fully_diluted_valuation")
    p7 = c.get("price_change_percentage_7d_in_currency")
    p30 = c.get("price_change_percentage_30d_in_currency")
    ath_dd = c.get("ath_change_percentage")   # % от исторического пика
    row = {"sym": sym, "cg_id": c.get("id"), "mcap": mcap, "vol": vol,
           "vm": (vol / mcap) if mcap else 0,
           "fdv_ratio": (fdv / mcap) if (fdv and mcap) else None,
           "p7": p7, "p30": p30, "ath_dd": ath_dd}
    if mcap < MIN_MCAP:
        row.update(tier="НЕТ", reason=f"капа ${mcap/1e6:.0f}M < $30M — мелочь")
        return row
    if vol < MIN_VOL:
        row.update(tier="НЕТ", reason=f"объём ${vol/1e6:.1f}M/сут — мёртвая ликвидность")
        return row
    if p30 is not None and p30 <= KNIFE_P30:
        row.update(tier="НЕТ", reason=f"нож: {p30:+.0f}% за 30 дней")
        return row
    if row["fdv_ratio"] and row["fdv_ratio"] > MAX_FDV_RATIO:
        row.update(tier="НЕТ",
                   reason=f"навес: FDV/капа {row['fdv_ratio']:.1f}x — впереди разлоки")
        return row

    # Балл выжившим: ликвидность, глубина просадки (ставка на 2027),
    # умеренный навес, размер как якорь надёжности
    score = 0
    vm = row["vm"]
    score += 25 if vm >= 0.08 else 15 if vm >= 0.03 else 5
    if ath_dd is not None:
        score += 25 if ath_dd <= -80 else 15 if ath_dd <= -60 else 5 if ath_dd <= -40 else 0
    fr = row["fdv_ratio"]
    score += 20 if (fr is None or fr <= 1.2) else 10 if fr <= 2.0 else 0
    score += 20 if mcap >= 1e9 else 12 if mcap >= 3e8 else 6
    if p30 is not None and p30 > 0 and (p7 is None or p7 > -10):
        score += 10   # стабилизация/разворот против рынка
    row["score"] = score
    row["tier"] = "ЯДРО-КАНДИДАТ" if score >= 70 else \
                  "ПРОВЕРИТЬ" if score >= 50 else "СЛАБО"
    row["reason"] = (f"балл {score}: капа ${mcap/1e6:.0f}M, объём {vm:.0%}/сут, "
                     f"{('от пика ' + format(ath_dd, '+.0f') + '%') if ath_dd is not None else ''}")
    return row


def enrich_shape(row):
    """Годовой график для финалистов: база или лестница вниз."""
    try:
        d = cg_get(f"/coins/{row['cg_id']}/market_chart",
                   {"vs_currency": "usd", "days": 365, "interval": "daily"})
        prices = [p[1] or 0 for p in d.get("prices", [])]
        if len(prices) >= 60 and min(prices[-60:]) > 0:
            rng60 = max(prices[-60:]) / min(prices[-60:]) - 1
            row["rng60"] = round(rng60, 2)
            row["base"] = rng60 <= 0.5
    except Exception:
        pass
    return row


def build_report(results):
    tiers = {"ЯДРО-КАНДИДАТ": [], "ПРОВЕРИТЬ": [], "СЛАБО": [], "НЕТ": []}
    for r in results.values():
        tiers.setdefault(r["tier"], []).append(r)
    for k in ("ЯДРО-КАНДИДАТ", "ПРОВЕРИТЬ", "СЛАБО"):
        tiers[k].sort(key=lambda x: -x.get("score", 0))

    reasons = {}
    for r in tiers["НЕТ"]:
        key = r["reason"].split(":")[0].split("—")[0].strip()
        reasons[key] = reasons.get(key, 0) + 1

    md = [f"# Revolut-скан — {date.today():%d.%m.%Y}",
          f"Проверено {len(results)} монет каталога (портфель исключён).",
          "Это машинный отсев по воротам «доживёт до 2027»: ликвидность, "
          "не-нож, умеренный навес FDV, размер. Ярусы ЯДРО/ПРОВЕРИТЬ идут "
          "на качественную проверку (новости, разлоки, команда).", ""]
    for k in ("ЯДРО-КАНДИДАТ", "ПРОВЕРИТЬ"):
        md.append(f"## {k} ({len(tiers[k])})")
        for r in tiers[k]:
            base = " | база 60д" if r.get("base") else ""
            md.append(f"- **{r['sym']}** — {r['reason']}{base}")
        md.append("")
    md.append(f"## СЛАБО ({len(tiers['СЛАБО'])})")
    md.append(", ".join(r["sym"] for r in tiers["СЛАБО"]) or "—")
    md.append("")
    md.append(f"## НЕТ ({len(tiers['НЕТ'])}) — по причинам")
    for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
        syms = [r["sym"] for r in tiers["НЕТ"] if r["reason"].startswith(k)]
        md.append(f"- {k} ({v}): " + ", ".join(syms))
    report = "\n".join(md)

    summary = [f"REVOLUT-СКАН — {date.today():%d.%m.%Y}",
               f"Проверено: {len(results)} | ЯДРО: {len(tiers['ЯДРО-КАНДИДАТ'])} | "
               f"проверить: {len(tiers['ПРОВЕРИТЬ'])} | слабо: {len(tiers['СЛАБО'])} | "
               f"нет: {len(tiers['НЕТ'])}", ""]
    if tiers["ЯДРО-КАНДИДАТ"]:
        summary.append("ЯДРО-КАНДИДАТЫ (глубокая проверка следующим шагом):")
        for r in tiers["ЯДРО-КАНДИДАТ"]:
            summary.append(f"  {r['sym']} — {r['reason']}")
    if tiers["ПРОВЕРИТЬ"]:
        summary.append("")
        summary.append("ПРОВЕРИТЬ: " + ", ".join(r["sym"] for r in tiers["ПРОВЕРИТЬ"]))
    summary.append("")
    summary.append("Полный разбор с причинами по каждой: revolut_scan_report.md в репо.")
    return report, "\n".join(summary)


def main():
    st = load_state()
    catalog = load_catalog()
    print(f"каталог после исключений: {len(catalog)} тикеров; "
          f"уже сделано: {len(st['done'])}")

    todo = [s for s in catalog if s not in st["done"]]
    if todo:
        if "market_map_ts" not in st["meta"]:
            print("тяну карту рынка (топ-3250)...")
        mm = fetch_market_map()
        st["meta"]["market_map_ts"] = datetime.now(timezone.utc).isoformat()
        for i, sym in enumerate(todo, 1):
            st["done"][sym] = evaluate(sym, mm.get(sym))
            if i % 20 == 0:
                save_state(st)
                print(f"  {i}/{len(todo)} (чекпоинт)")
        save_state(st)

    # Обогащение финалистов годовым графиком — тоже с чекпоинтом
    finalists = sorted(
        (r for r in st["done"].values()
         if r["tier"] in ("ЯДРО-КАНДИДАТ", "ПРОВЕРИТЬ") and r.get("cg_id")),
        key=lambda x: -x.get("score", 0))[:ENRICH_TOP]
    todo_e = [r for r in finalists if r["sym"] not in st["enriched"]]
    print(f"обогащение графиком: {len(todo_e)} из {len(finalists)}")
    for i, r in enumerate(todo_e, 1):
        st["done"][r["sym"]] = enrich_shape(dict(r))
        st["enriched"][r["sym"]] = True
        if i % 10 == 0:
            save_state(st)
            print(f"  график {i}/{len(todo_e)} (чекпоинт)")
        time.sleep(1.4 if CG_KEY else 8)
    save_state(st)

    report, summary = build_report(st["done"])
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)
    print(summary)
    tg(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EXPORT_FLOWS v1.0 — еженедельная выгрузка для соседнего DEX-проекта.
Собирает две вещи (строго по их запросу, ничего лишнего):
  1) пары тикер-контракт всех монет, попавших в топ-15 скринера за неделю;
  2) снапшот чёрного списка с датами событий (event_date).
Пишет export/flows_ГГГГММДД.json + export/flows_latest.json (история в git)
и шлёт файл документом в Telegram — оттуда его удобно переслать в другой
проект с телефона. Claude API не используется, стоит ноль.
Запуск: пятницы 08:17 UTC (после автоконфига) + кнопка.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta

import requests
import yaml
from secrets_guard import redact

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "candidates_log.jsonl")
CFG_PATH = os.path.join(HERE, "config.yaml")
OUT_DIR = os.path.join(HERE, "export")

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
CG_KEY = os.getenv("COINGECKO_API_KEY", "").strip()

S = requests.Session()
S.headers.update({"User-Agent": "pre-beat-export/1.0 (personal research tool)"})


def load_week_tops(log_path, days=7):
    """Топы скринера за последние N дней из журнала кандидатов.
    Топ = строки с решением enter или причиной in_top_not_entered
    (это и есть топ-15 каждого прогона). Дедуп по cg_id: если монета
    была в обоих прогонах недели, берём более свежую строку."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    runs_seen = []
    tops = {}   # cg_id -> строка
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "_meta" in d:
                    continue
                if "_run" in d:
                    ts = d["_run"].get("timestamp_utc", "")
                    try:
                        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                            tzinfo=timezone.utc)
                        if t >= cutoff:
                            runs_seen.append(d["_run"]["run_id"])
                    except ValueError:
                        pass
                    continue
                # обычная строка монеты
                ts = d.get("timestamp_utc", "")
                try:
                    t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc)
                except ValueError:
                    continue
                if t < cutoff:
                    continue
                is_top = (d.get("decision") == "enter"
                          or d.get("reject_reason") == "in_top_not_entered")
                if not is_top or not d.get("cg_id"):
                    continue
                prev = tops.get(d["cg_id"])
                if prev is None or ts > prev.get("timestamp_utc", ""):
                    tops[d["cg_id"]] = d
    except FileNotFoundError:
        print("candidates_log.jsonl не найден — экспорт топов пуст")
    return runs_seen, list(tops.values())


def fetch_contracts_cg(cg_id):
    """Адреса контрактов монеты по сетям (CoinGecko /coins/{id})."""
    headers = {"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}
    try:
        r = S.get(
            f"https://api.coingecko.com/api/v3/coins/{cg_id}",
            params={"localization": "false", "tickers": "false",
                    "market_data": "false", "community_data": "false",
                    "developer_data": "false", "sparkline": "false"},
            headers=headers, timeout=20,
        )
        r.raise_for_status()
        plats = r.json().get("platforms") or {}
        return {ch: ad for ch, ad in plats.items() if ch and ad}
    except Exception as e:
        # redact (С-18): текст исключения requests цитирует URL CoinGecko,
        # а в живом вызове рядом ходит ключ x-cg-demo-api-key.
        print(f"контракты {cg_id} не отдались: {redact(e)}")
        return {}


def load_blacklist(cfg_path):
    """Снапшот чёрного списка. Строки-легаси приводим к одному виду,
    чтобы потребителю не разбирать два формата."""
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        print("config.yaml не найден — blacklist пуст")
        return {}
    out = {}
    for sym, v in (cfg.get("blacklist") or {}).items():
        if isinstance(v, dict):
            out[str(sym).upper()] = {"reason": v.get("reason"),
                                     "event_date": v.get("event_date")}
        else:
            out[str(sym).upper()] = {"reason": str(v), "event_date": None}
    return out


def build_export(log_path=LOG_PATH, cfg_path=CFG_PATH,
                 fetch_fn=fetch_contracts_cg, pause=1.2):
    """Сборка полного экспорта. fetch_fn подменяется в тестах."""
    runs, tops = load_week_tops(log_path)
    top_out = []
    for d in sorted(tops, key=lambda x: -(x.get("score") or 0)):
        contracts = fetch_fn(d["cg_id"])
        top_out.append({
            "ticker": d.get("ticker"),
            "cg_id": d.get("cg_id"),
            "run_timestamp": d.get("timestamp_utc"),
            "score": d.get("score"),
            "mcap_usd": (d.get("features") or {}).get("mcap"),
            "price_usd": (d.get("features") or {}).get("price"),
            "entered_paper": d.get("decision") == "enter",
            "contracts": contracts,   # {сеть: адрес}; пусто = нативная монета
        })
        time.sleep(pause)
    return {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "pre-BEAT screener (private repo), еженедельный экспорт",
        "week_runs": runs,
        "note": ("top_contracts: монеты из топ-15 прогонов за 7 дней, дедуп "
                 "по cg_id (тикеры не уникальны!). blacklist: снапшот на "
                 "момент экспорта; event_date=null = дата не подтверждена "
                 "источниками, карантин бессрочный."),
        "top_contracts": top_out,
        "blacklist": load_blacklist(cfg_path),
    }


def send_telegram_document(path, caption):
    """Шлём файл документом. Подпись — только plain text (грабли HTML)."""
    if not TG_TOKEN or not TG_CHAT:
        print("Telegram не настроен — файл лежит в репо, не отправлен")
        return
    try:
        with open(path, "rb") as f:
            r = S.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                data={"chat_id": TG_CHAT, "caption": caption},
                files={"document": (os.path.basename(path), f,
                                    "application/json")},
                timeout=60,
            )
        if r.status_code != 200:
            # redact ДО среза (С-19): срез разрезал бы токен пополам и
            # спрятал бы обрубок от шаблонов чистки.
            print("Telegram отклонил документ (%s): %s"
                  % (r.status_code, redact(r.text)[:300]))
    except Exception as e:
        # redact: в тексте исключения лежит URL с токеном бота.
        print("Telegram недоступен: %s" % redact(e))


def main():
    data = build_export()
    os.makedirs(OUT_DIR, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    dated = os.path.join(OUT_DIR, f"flows_{day}.json")
    latest = os.path.join(OUT_DIR, "flows_latest.json")
    payload = json.dumps(data, ensure_ascii=False, indent=1)
    for p in (dated, latest):
        with open(p, "w", encoding="utf-8") as f:
            f.write(payload)
    n_top = len(data["top_contracts"])
    n_bl = len(data["blacklist"])
    print(f"экспорт: топов {n_top}, blacklist {n_bl}, прогонов недели "
          f"{len(data['week_runs'])}")
    try:   # D3: дубль в csv-схему соседнего проекта; сбой не валит экспорт
        import export_trades
        export_trades.export_all(HERE)
    except Exception as e:
        print(f"export_trades не отработал (json-экспорт не пострадал): {e}")
    send_telegram_document(
        latest,
        f"Экспорт для DEX-проекта: топ-контрактов {n_top}, "
        f"blacklist {n_bl}. Перешли этот файл в тот чат.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
COSTS v1.0 — модель издержек круга для бумажных сделок скринера (блок A1).

ЧТО ЗДЕСЬ ИЗМЕРЕНО, А ЧТО ДОПУЩЕНО — читать до того, как цитировать net.
Комиссия — единственная измеримая часть модели: это публичный спотовый тариф
площадки, и в costs.yaml взята худшая из наблюдаемых публичных ставок, а не
средняя. Фандинг — ноль по устройству инструмента: спотовая покупка монеты не
платит фандинг, он существует только у бессрочных фьючерсов; поле оставлено
пустым каркасом на случай перпов, а не заполнено оценкой. Слиппедж — ЧИСТОЕ
ДОПУЩЕНИЕ: стакана у проекта нет, реальных исполнений нет ни одного (все сделки
бумажные), поэтому числа по классам ликвидности назначены консервативно, в
худшую для нас сторону, и замером не являются. Границы классов по суточному
объёму — тоже назначены, а не выведены из данных. Отсюда прямое следствие,
которое обязан соблюдать любой отчёт: ВЕРДИКТ И ЛЮБОЕ net-число ПУБЛИКУЮТСЯ
ТОЛЬКО ВМЕСТЕ С ТАБЛИЦЕЙ ЧУВСТВИТЕЛЬНОСТИ по costs.yaml:sensitivity_multipliers
(см. sensitivity_table ниже). Одиночное net без таблицы — ложная точность: оно
выглядит замером, а внутри сидит догадка о спреде. Если знак результата меняется
между множителями 0.5 и 2.0, честный вывод — «издержки решают», а не «сито
работает».

Издержки применяются МУЛЬТИПЛИКАТИВНО (с оборота), а не вычитанием из процента:
    net = (1 + g/100) * (1 - total_bps/10000) - 1
Разница не косметическая: на сделке +200% вычитание 2.4% занижает издержки втрое
против реальности, потому что комиссия и спред платятся и с выросшей суммы тоже.

Класс ликвидности unknown (нет суточного объёма) трактуется как самый дорогой
thin. Число таких сделок обязано попадать в артефакт полем liq_class_fallback_n
(см. is_fallback_class), иначе допущение спрячется в среднем.

Зависимости: stdlib + PyYAML. Ключей и сети не требует.
CLI: python costs.py — печатает конфиг, круг издержек по классам и таблицу
чувствительности на демонстрационном gross.
"""

import math
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_COSTS_PATH = os.path.join(HERE, "costs.yaml")

# Классы ликвидности в порядке удорожания.
LIQ_CLASSES = ("deep", "medium", "thin")
# Куда сваливается неизвестный/непонятный класс — в самый дорогой.
FALLBACK_CLASS = "thin"
# Режимы применения множителя чувствительности.
SENSITIVITY_MODES = ("all", "slippage_only")


# --- служебное -----------------------------------------------------------

def _as_number(value, where):
    """Число или внятная ошибка с указанием места. bool числом НЕ считается."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("costs.yaml: %s должно быть числом, получено %r" % (where, value))
    val = float(value)
    if math.isnan(val) or math.isinf(val):
        raise ValueError("costs.yaml: %s не число (nan/inf)" % where)
    return val


def _need_source(node, where):
    """У каждой константы издержек обязано быть непустое поле source."""
    if not isinstance(node, dict):
        raise ValueError("costs.yaml: %s должно быть блоком {value, source}" % where)
    src = node.get("source")
    if not isinstance(src, str) or not src.strip():
        raise ValueError("costs.yaml: у %s нет непустого поля source" % where)
    return src


def _value_with_source(node, where, min_value=0.0):
    """Достать value из блока {value, source}, проверив source и знак."""
    _need_source(node, where)
    val = _as_number(node.get("value"), where + ".value")
    if val < min_value:
        raise ValueError("costs.yaml: %s.value = %s, ожидалось >= %s" % (where, val, min_value))
    return val


# --- загрузка конфига ----------------------------------------------------

def load_costs(path=None):
    """Прочитать и ПРОВЕРИТЬ costs.yaml. Возвращает dict конфига.

    Проверяется не только наличие ключей, но и согласованность: у каждой
    константы издержек есть непустой source, границы классов упорядочены,
    множители положительны, и объявленная неприменимость фандинга не
    противоречит ненулевой ставке. Молча принять кривой конфиг нельзя:
    он бы тихо испортил все net-числа сразу.
    """
    cfg_path = path or DEFAULT_COSTS_PATH
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("costs.yaml: ожидался словарь верхнего уровня, получено %r" % type(cfg))

    # версия и инструмент
    version = cfg.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("costs.yaml: version должно быть целым, получено %r" % (version,))
    instrument = cfg.get("instrument")
    if not isinstance(instrument, str) or not instrument.strip():
        raise ValueError("costs.yaml: instrument должен быть непустой строкой")

    # комиссия
    _value_with_source(cfg.get("taker_fee_bps"), "taker_fee_bps")

    # фандинг
    funding = cfg.get("funding")
    if not isinstance(funding, dict):
        raise ValueError("costs.yaml: блок funding отсутствует")
    applies = funding.get("applies_to_spot")
    if not isinstance(applies, bool):
        raise ValueError("costs.yaml: funding.applies_to_spot должно быть true/false")
    fund_rate = _value_with_source(funding.get("bps_per_day"), "funding.bps_per_day")
    if instrument == "spot" and applies:
        raise ValueError(
            "costs.yaml: instrument=spot, но funding.applies_to_spot=true — "
            "у спота фандинга нет, рассинхрон конфига")
    if not applies and fund_rate != 0.0:
        raise ValueError(
            "costs.yaml: фандинг объявлен неприменимым (applies_to_spot=false), "
            "но bps_per_day.value=%s — рассинхрон конфига" % fund_rate)

    # слиппедж по классам
    slip = cfg.get("slippage_bps_by_class")
    if not isinstance(slip, dict):
        raise ValueError("costs.yaml: блок slippage_bps_by_class отсутствует")
    values = {}
    for cls in LIQ_CLASSES:
        if cls not in slip:
            raise ValueError("costs.yaml: в slippage_bps_by_class нет класса %s" % cls)
        values[cls] = _value_with_source(slip[cls], "slippage_bps_by_class.%s" % cls)
    if not (values["deep"] <= values["medium"] <= values["thin"]):
        raise ValueError(
            "costs.yaml: слиппедж обязан не убывать deep <= medium <= thin, получено %r" % values)

    # границы классов
    thr = cfg.get("liquidity_class_thresholds")
    if not isinstance(thr, dict):
        raise ValueError("costs.yaml: блок liquidity_class_thresholds отсутствует")
    _need_source(thr, "liquidity_class_thresholds")
    deep_min = _as_number(thr.get("deep_min_vol_usd"), "liquidity_class_thresholds.deep_min_vol_usd")
    med_min = _as_number(thr.get("medium_min_vol_usd"), "liquidity_class_thresholds.medium_min_vol_usd")
    if not (0 < med_min < deep_min):
        raise ValueError(
            "costs.yaml: ожидалось 0 < medium_min_vol_usd < deep_min_vol_usd, "
            "получено %s и %s" % (med_min, deep_min))

    # размер позиции
    pos = _as_number(cfg.get("position_usd"), "position_usd")
    if pos <= 0:
        raise ValueError("costs.yaml: position_usd должен быть > 0")

    # множители чувствительности
    mults = cfg.get("sensitivity_multipliers")
    if not isinstance(mults, list) or not mults:
        raise ValueError("costs.yaml: sensitivity_multipliers должен быть непустым списком")
    for i, m in enumerate(mults):
        if _as_number(m, "sensitivity_multipliers[%d]" % i) <= 0:
            raise ValueError("costs.yaml: множитель чувствительности должен быть > 0")

    mode = cfg.get("sensitivity_applies_to", "all")
    if mode not in SENSITIVITY_MODES:
        raise ValueError(
            "costs.yaml: sensitivity_applies_to должно быть одним из %r, получено %r"
            % (list(SENSITIVITY_MODES), mode))

    cfg["_source_path"] = os.path.abspath(cfg_path)
    return cfg


# --- классификация ликвидности ------------------------------------------

def classify_liquidity(vol_usd_24h, cfg):
    """Класс ликвидности по суточному объёму в USD.

    Возвращает 'deep' | 'medium' | 'thin' | 'unknown'. unknown — это честное
    «объёма нет»: None, пустая строка, нечисло, отрицательное, nan/inf.
    Именно unknown, а не тихая подстановка нуля: ноль объёма и отсутствие
    данных об объёме — разные вещи, и путать их нельзя (первое — факт, второе —
    дыра в данных, которую надо считать в liq_class_fallback_n).
    """
    if vol_usd_24h is None or isinstance(vol_usd_24h, bool):
        return "unknown"
    try:
        vol = float(vol_usd_24h)
    except (TypeError, ValueError):
        return "unknown"
    if math.isnan(vol) or math.isinf(vol) or vol < 0:
        return "unknown"

    thr = cfg["liquidity_class_thresholds"]
    if vol >= float(thr["deep_min_vol_usd"]):
        return "deep"
    if vol >= float(thr["medium_min_vol_usd"]):
        return "medium"
    return "thin"


def is_fallback_class(liq_class):
    """True, если класс неизвестен/нераспознан и будет посчитан как thin.

    Нужен metrics/verdict, чтобы заполнить поле liq_class_fallback_n: сделка,
    посчитанная по худшему классу из-за отсутствия объёма, обязана быть видна
    отдельным числом, а не растворяться в среднем.
    """
    return _effective_class(liq_class) != _normalize_class(liq_class)


def _normalize_class(liq_class):
    if isinstance(liq_class, str):
        return liq_class.strip().lower()
    return liq_class


def _effective_class(liq_class):
    """Класс, по которому реально считаются издержки.

    unknown, None и любая нераспознанная строка -> самый дорогой thin.
    Исключения не бросаем сознательно: метрики не должны падать из-за одной
    сделки с дырой в данных — но и дешевле от дыры не становятся.
    """
    cls = _normalize_class(liq_class)
    if cls in LIQ_CLASSES:
        return cls
    return FALLBACK_CLASS


# --- круг издержек -------------------------------------------------------

def round_trip_bps(liq_class, hold_days, cfg, mult=1.0):
    """Издержки полного круга в базисных пунктах.

    Круг = комиссия вход + комиссия выход + слиппедж вход + слиппедж выход
    + фандинг за hold_days (для спота = 0).

    -> {'fee_bps', 'slippage_bps', 'funding_bps', 'total_bps'}

    mult — множитель таблицы чувствительности. По умолчанию (режим 'all' в
    costs.yaml) множится весь круг, поэтому total_bps масштабируется линейно;
    в режиме 'slippage_only' множится только допущение о слиппедже, комиссия
    остаётся как факт тарифа.
    """
    m = _as_number(mult, "mult")
    if m < 0:
        raise ValueError("mult должен быть >= 0, получено %s" % m)

    cls = _effective_class(liq_class)
    fee_one_side = float(cfg["taker_fee_bps"]["value"])
    slip_one_side = float(cfg["slippage_bps_by_class"][cls]["value"])

    if hold_days is None:
        days = 0.0
    else:
        days = _as_number(hold_days, "hold_days")
        if days < 0:
            days = 0.0

    funding = cfg["funding"]
    rate_per_day = float(funding["bps_per_day"]["value"]) if funding["applies_to_spot"] else 0.0

    mode = cfg.get("sensitivity_applies_to", "all")
    if mode == "slippage_only":
        fee_bps = fee_one_side * 2.0
        slippage_bps = slip_one_side * 2.0 * m
        funding_bps = rate_per_day * days
    else:
        fee_bps = fee_one_side * 2.0 * m
        slippage_bps = slip_one_side * 2.0 * m
        funding_bps = rate_per_day * days * m

    total_bps = fee_bps + slippage_bps + funding_bps
    return {
        "fee_bps": round(fee_bps, 6),
        "slippage_bps": round(slippage_bps, 6),
        "funding_bps": round(funding_bps, 6),
        "total_bps": round(total_bps, 6),
    }


def net_ret_pct(gross_ret_pct, liq_class, hold_days, cfg, mult=1.0):
    """Чистая доходность в процентах: издержки МУЛЬТИПЛИКАТИВНО, с оборота.

        net = (1 + g/100) * (1 - total_bps/10000) - 1,   в процентах.

    gross_ret_pct = None -> None (у открытой позиции результата нет; §2
    замороженных правил: open_pnl в вердикт не идёт ни в каком виде).

    Граничный случай, который стоит знать: при g = -100% (монета в ноль)
    net = -100% тоже — платить издержки уже не с чего, хуже нуля не станет.
    Во всех остальных случаях при положительных издержках net строго хуже gross.
    """
    if gross_ret_pct is None:
        return None
    gross = _as_number(gross_ret_pct, "gross_ret_pct")
    total_bps = round_trip_bps(liq_class, hold_days, cfg, mult=mult)["total_bps"]
    net = (1.0 + gross / 100.0) * (1.0 - total_bps / 10000.0) - 1.0
    return round(net * 100.0, 6)


def sensitivity_table(liq_class, hold_days, cfg, gross_ret_pct=None):
    """Строки таблицы чувствительности по costs.yaml:sensitivity_multipliers.

    -> [{'mult', 'fee_bps', 'slippage_bps', 'funding_bps', 'total_bps',
         'net_ret_pct'}] — net_ret_pct = None, если gross не задан.
    Вспомогательная функция: вердикт обязан печатать эту таблицу рядом с
    любым net-числом (см. шапку модуля).
    """
    rows = []
    for m in cfg["sensitivity_multipliers"]:
        mult = float(m)
        row = round_trip_bps(liq_class, hold_days, cfg, mult=mult)
        row["mult"] = mult
        row["net_ret_pct"] = net_ret_pct(gross_ret_pct, liq_class, hold_days, cfg, mult=mult)
        rows.append(row)
    return rows


# --- CLI -----------------------------------------------------------------

def _main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    cfg = load_costs()
    print("COSTS v1.0 — модель издержек круга (блок A1)")
    print("конфиг: %s (version=%s, instrument=%s)" % (cfg["_source_path"], cfg["version"], cfg["instrument"]))
    print("комиссия тейкера: %.2f бп на сторону — ИЗМЕРИМО (публичный тариф)"
          % cfg["taker_fee_bps"]["value"])
    print("фандинг: %s (спот фандинг не платит)"
          % ("применяется" if cfg["funding"]["applies_to_spot"] else "не применяется, 0.0 бп/день"))
    print("слиппедж на сторону — ДОПУЩЕНИЕ, не замер (стакана нет): deep %.1f / medium %.1f / thin %.1f бп"
          % (cfg["slippage_bps_by_class"]["deep"]["value"],
             cfg["slippage_bps_by_class"]["medium"]["value"],
             cfg["slippage_bps_by_class"]["thin"]["value"]))
    thr = cfg["liquidity_class_thresholds"]
    print("границы классов (суточный объём, USD): deep >= %s, medium >= %s, ниже — thin; нет объёма -> unknown -> считается как thin"
          % (thr["deep_min_vol_usd"], thr["medium_min_vol_usd"]))
    print("позиция: $%s; множитель чувствительности применяется к: %s"
          % (cfg["position_usd"], cfg.get("sensitivity_applies_to", "all")))

    print("")
    print("КРУГ ИЗДЕРЖЕК (удержание 60 дней, множитель 1.0):")
    print("  %-8s %10s %12s %11s %11s" % ("класс", "комиссия", "слиппедж", "фандинг", "ИТОГО"))
    for cls in list(LIQ_CLASSES) + ["unknown"]:
        rt = round_trip_bps(cls, 60, cfg)
        mark = "  <- считается как thin" if is_fallback_class(cls) else ""
        print("  %-8s %10.2f %12.2f %11.2f %11.2f бп%s"
              % (cls, rt["fee_bps"], rt["slippage_bps"], rt["funding_bps"], rt["total_bps"], mark))

    demo_gross = 20.0
    print("")
    print("ТАБЛИЦА ЧУВСТВИТЕЛЬНОСТИ (демонстрация: gross = %+.1f%%, удержание 60 дней)" % demo_gross)
    print("  без этой таблицы ни одно net-число не публикуется — слиппедж это допущение")
    for cls in LIQ_CLASSES:
        parts = []
        for row in sensitivity_table(cls, 60, cfg, gross_ret_pct=demo_gross):
            parts.append("x%.1f: %.2f бп -> net %+.2f%%" % (row["mult"], row["total_bps"], row["net_ret_pct"]))
        print("  %-8s %s" % (cls, " | ".join(parts)))
    return 0


if __name__ == "__main__":
    sys.exit(_main())

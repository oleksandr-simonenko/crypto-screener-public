#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
metrics.py — ВСЕ числа турнира считаются здесь, кодом (блоки A2/A3/A4).

ЗАЧЕМ ЭТО НУЖНО ДЛЯ ДЕНЕГ
Аудитор-LLM НЕ СЧИТАЕТ НИЧЕГО. Он читает готовый json и цитирует его дословно.
Причина простая и проверенная на соседнем проекте: число, посчитанное моделью
в тексте, невозможно свести обратно с данными — оно выглядит настоящим и
переживает любые пересказы. Поэтому здесь один источник чисел: артефакт
artifacts/metrics_latest.json, посчитанный этим файлом.

ЧТО СЧИТАЕТСЯ
  track_metrics  — метрики одной дорожки по ЗАКРЫТЫМ сделкам (A2/A3);
  funnel_metrics — воронка отказов скринера по прогонам, с разрезом no_perp (A4);
  build_metrics  — весь артефакт целиком;
  diff_metrics   — «что изменилось с прошлой недели» для еженедельного аудита.

ГРАНИЦЫ, КОТОРЫЕ НАЗЫВАЮТСЯ ВСЛУХ В САМОМ АРТЕФАКТЕ
  * Открытые позиции в метрики не идут НИКАК (§2 замороженных правил:
    open_pnl не участвует ни как метрика, ни как «предварительный срез»).
    На 01.09.2026 закрытых сделок ноль, поэтому честный артефакт этого дня —
    артефакт с нулями и status «нет закрытых сделок». Это НЕ сбой.
  * net-числа содержат ДОПУЩЕНИЕ о слиппедже (стакана у проекта нет), поэтому
    рядом всегда лежит таблица чувствительности по costs.yaml. Одиночное net
    без неё — ложная точность.
  * Сделки без суточного объёма на дату входа считаются по худшему классу
    (thin) и отдельно пересчитаны ДВУМЯ полями: liq_class_fallback_n_all (по
    ВСЕМ загруженным сделкам, включая открытые) и liq_class_fallback_n_closed
    (только по закрытым). Одного числа тут мало: до 01.09.2026 поле считалось
    по закрытым и показывало 0 в день, когда подстановка шла по 151 сделке
    из 421 — то есть ровно тогда, когда его и надо было увидеть.
  * max_drawdown_pct считается на кривой «одна ставка за раз». Дорожка держит
    до 60 позиций одновременно, поэтому это НЕ просадка портфеля дорожки;
    рядом лежит max_drawdown_pct_equal_weight (равновзвешенный портфель по
    датам выхода) и поле equity_curve_order с полным порядком сортировки —
    без него число зависело бы от порядка строк в json, а такое число не
    измерение.
  * Пути в артефакте — ТОЛЬКО относительно корня репозитория: одинаковый
    расчёт локально и на Actions обязан давать одинаковый файл, а имени
    пользователя машины-исполнителя в json быть не должно.

Зависимости: stdlib + PyYAML (через costs.py). Ни numpy, ни pandas.
CLI: python metrics.py [repo_dir] — печатает сводку и пишет
     artifacts/metrics_latest.json + artifacts/YYYY-MM/metrics_YYYYMMDD.json.
"""

import json
import math
import os
import sys
from datetime import datetime, timezone

import costs
import provenance
import rules_hash
import rules_addendum
import trades as trades_mod

__all__ = [
    "track_metrics",
    "funnel_metrics",
    "build_metrics",
    "diff_metrics",
    "sensitivity_rows",
    "write_artifacts",
    "max_drawdown_pct",
    "max_drawdown_pct_equal_weight",
    "curve_sort_key",
    "rel_to_repo",
    "NO_CLOSED_STATUS",
    "EQUITY_CURVE_ORDER_FIELDS",
]

# Дословная строка статуса пустого входа. Держим константой: её ищет селфтест,
# её цитирует аудитор, и опечатка в одном из трёх мест была бы незаметна.
NO_CLOSED_STATUS = "нет закрытых сделок"
HAS_CLOSED_STATUS = "есть закрытые сделки"

ARTIFACTS_DIR = "artifacts"
LATEST_NAME = "metrics_latest.json"

# Числовые поля дорожки, по которым diff_metrics считает дельты.
_TRACK_NUMERIC_FIELDS = (
    "n_closed",
    "n_open",
    "mean_gross",
    "median_gross",
    "mean_net",
    "median_net",
    "iqr_net_q1",
    "iqr_net_q3",
    "share_profitable_net",
    "max_drawdown_pct",
    "max_drawdown_pct_equal_weight",
    "top3_contribution_share",
    "worst_trade_net",
    "best_trade_net",
    "liq_class_fallback_n",
    "liq_class_fallback_n_all",
    "liq_class_fallback_n_closed",
)

# --- ПОРЯДОК КРИВОЙ КАПИТАЛА -------------------------------------------
# Сортировка ТОЛЬКО по exit_date неполна: в один день закрывается сразу
# несколько позиций, и внутри дня их порядок задавался тем, как они лежали в
# tournament.json/positions.json. Просадка от этого порядка зависит, а число,
# зависящее от порядка строк в файле, — не измерение. Поэтому ключ полный и
# доводится до однозначности: даты, потом дорожка, потом идентификатор монеты,
# потом сам результат. Список публикуется в артефакте (equity_curve_order),
# чтобы порядок можно было воспроизвести чужими руками, а не угадать.
EQUITY_CURVE_ORDER_FIELDS = (
    "exit_date", "entry_date", "track", "cg_id", "sym", "ret_net_pct",
)

# Чем заменяется пустая дата при сортировке. Пустая строка сортируется ПЕРВОЙ,
# то есть сделка без даты выхода уехала бы в начало кривой; она должна быть в
# конце — «дата неизвестна» это не «дата самая ранняя».
_MISSING_DATE = "9999-12-31"

# Оговорка, которая обязана стоять РЯДОМ с числом просадки, а не в шапке файла:
# число без неё читается как просадка портфеля, которой оно не является.
MAX_DD_NOTE = ("«одна ставка за раз», а дорожка держит до 60 позиций сразу — это "
               "НЕ просадка портфеля; портфельная оценка рядом: "
               "max_drawdown_pct_equal_weight")

LIQ_FALLBACK_NOTE = ("liq_class_fallback_n_all — по ВСЕМ загруженным сделкам "
                     "(закрытым и открытым), liq_class_fallback_n_closed — только "
                     "по закрытым; liq_class_fallback_n = ..._all")


# =========================================================================
#  ЭЛЕМЕНТАРНАЯ СТАТИСТИКА (чистый Python; numpy в проекте запрещён)
# =========================================================================

def _quantile(sorted_vals, q):
    """Квантиль по ЛИНЕЙНОЙ интерполяции между порядковыми статистиками.

    Метод назван явно, потому что «квартиль» — это семь разных определений,
    и молчаливый выбор одного из них однажды превратится в спор о цифре.
    Здесь: позиция = (n-1)*q, значение = линейная интерполяция соседей
    (тип 7 по классификации Hyndman–Fan; то же, что numpy.percentile по
    умолчанию и quantile() в R). Проверено сверкой с numpy — см.
    tests/test_metrics.py, там значения выписаны руками.
    """
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    pos = (n - 1) * float(q)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    frac = pos - lo
    return float(sorted_vals[lo]) + (float(sorted_vals[hi]) - float(sorted_vals[lo])) * frac


def _median(values):
    """Медиана. Пусто -> None (а не ноль: ноль это результат, пусто — его отсутствие)."""
    if not values:
        return None
    return _quantile(sorted(values), 0.5)


def _mean(values):
    if not values:
        return None
    return sum(values) / float(len(values))


def _round(value, digits=6):
    if value is None:
        return None
    return round(float(value), digits)


def curve_sort_key(trade):
    """ПОЛНЫЙ детерминированный ключ порядка сделки на кривой капитала.

    Поля — EQUITY_CURVE_ORDER_FIELDS. Даты сравниваются строками (ISO-формат
    YYYY-MM-DD сортируется как текст правильно), пустая дата уезжает в конец
    через _MISSING_DATE, остальные поля приводятся к строке, а ret_net_pct —
    к float. Приведение обязательно: None и число в одном кортеже роняют
    сравнение с TypeError, а падать на неполной записи метрики не имеют права.

    Зачем: сортировка только по exit_date оставляла порядок внутри дня на
    усмотрение того, как строки лежали в json. Просадка от этого порядка
    зависит — значит она была свойством файла, а не сделок.
    """
    trade = trade if isinstance(trade, dict) else {}

    def text(name):
        value = trade.get(name)
        return "" if value is None else str(value)

    try:
        net = float(trade.get("ret_net_pct"))
    except (TypeError, ValueError):
        net = 0.0
    return (
        trade.get("exit_date") or _MISSING_DATE,
        trade.get("entry_date") or _MISSING_DATE,
        text("track"),
        text("cg_id"),
        text("sym"),
        net,
    )


def max_drawdown_pct(nets):
    """Максимальная просадка кривой капитала по закрытым сделкам, в % от пика.

    МОДЕЛЬ КРИВОЙ (называется прямо, потому что от неё зависит число):
    сделки идут ПОСЛЕДОВАТЕЛЬНО, каждая на одинаковую ставку $100, банкролл
    равен той же ставке (одна позиция за раз). Тогда
        E_0 = 1, E_i = E_{i-1} + net_i/100,
    и просадка (peak - E_i)/peak * 100 не зависит от размера ставки — модель
    масштабно-инвариантна, поэтому «$100» тут не подгоночный параметр.

    Порядок сделок задаёт вызывающий — track_metrics сортирует их полным
    ключом curve_sort_key. Просадки нет -> 0.0.

    ЧЕСТНАЯ ОГОВОРКА 1: при сумме убытков хуже −100% аддитивная кривая уходит
    ниже нуля, и число может превысить 100 % — это сигнал, что банкролл
    в один размер ставки кончился, а не «просадка 137 %».

    ЧЕСТНАЯ ОГОВОРКА 2 (MAX_DD_NOTE, едет рядом с числом в артефакт): модель
    «одна ставка за раз» к дорожке турнира не применима буквально — дорожка
    держит до 60 позиций одновременно. Это просадка ПОСЛЕДОВАТЕЛЬНОСТИ
    результатов, а не портфеля; портфельную считает
    max_drawdown_pct_equal_weight.
    """
    if not nets:
        return None
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for net in nets:
        equity += float(net) / 100.0
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100.0
        if dd > worst:
            worst = dd
    return worst


def max_drawdown_pct_equal_weight(closed_trades):
    """Просадка РАВНОВЗВЕШЕННОГО портфеля по датам выхода, в % от пика.

    Честная вторая метрика рядом с max_drawdown_pct. Модель:
      * капитал делится поровну между ВСЕМИ N закрытыми сделками дорожки,
        доля каждой = 1/N банкролла (это и есть «равновзвешенный портфель»);
      * реализованный результат приходит В ДЕНЬ ВЫХОДА, все выходы одного дня
        складываются в одно движение капитала:
            E(d) = 1 + сумма(net_i)/100/N по всем сделкам с exit_date <= d;
      * просадка меряется по последовательности ДАТ, а не сделок.

    Отсюда главное свойство: число НЕ ЗАВИСИТ от порядка сделок внутри дня —
    внутри дня они складываются, а сложение от порядка не зависит. Именно оно
    и чинит находку: у max_drawdown_pct порядок пришлось задать явно, здесь
    его не требуется задавать вовсе. (Слагаемые всё равно суммируются в
    порядке curve_sort_key — чтобы совпадал последний бит float, а не только
    смысл.)

    ГРАНИЦА, которую называем вслух: это просадка РЕАЛИЗОВАННОГО результата.
    Переоценки открытых позиций тут нет и быть не может — §2 замороженных
    правил запрещает open_pnl в любом виде, — поэтому настоящая внутридневная
    просадка портфеля глубже этой. Число читать как «нижнюю оценку».

    -> % от пика, либо None если считать не из чего.
    """
    rows = [t for t in (closed_trades or [])
            if isinstance(t, dict) and t.get("ret_net_pct") is not None]
    if not rows:
        return None
    n = len(rows)
    by_day = {}
    for t in sorted(rows, key=curve_sort_key):
        day = t.get("exit_date") or _MISSING_DATE
        by_day[day] = by_day.get(day, 0.0) + float(t["ret_net_pct"])

    equity = 1.0
    peak = 1.0
    worst = 0.0
    for day in sorted(by_day):
        equity += by_day[day] / 100.0 / n
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100.0
        if dd > worst:
            worst = dd
    return worst


def rel_to_repo(path, repo_dir=None):
    """Путь ОТНОСИТЕЛЬНО корня репозитория, всегда прямыми слешами.

    Артефакт обязан быть одинаковым локально и на GitHub Actions, иначе один
    и тот же расчёт даёт два разных файла и diff недели показывает изменение
    там, где менялась только машина. Плюс абсолютный путь Windows тащит в json
    имя пользователя — этого в публикуемом файле быть не должно.

    Файл вне репозитория (или на другом диске — на Windows relpath на это
    бросает ValueError) сводится к basename: относительного пути к нему нет,
    а печатать абсолютный нельзя.
    """
    if not path:
        return None
    full = os.path.abspath(path)
    base = os.path.abspath(repo_dir or ".")
    try:
        rel = os.path.relpath(full, base)
    except ValueError:
        return os.path.basename(full)
    rel = rel.replace("\\", "/")
    if rel == ".." or rel.startswith("../"):
        return os.path.basename(full)
    return rel


def top3_contribution(nets):
    """Доля суммы трёх лучших net-сделок в общей сумме net.

    -> (share|None, note). Если общая сумма <= 0, доля НЕ ОПРЕДЕЛЕНА: деление
    на отрицательное число даёт красивое число с обратным смыслом (лидеры дают
    «-180 % вклада»), и такое число в отчёте хуже пустоты. Тогда None и
    пояснение строкой — как требует контракт.
    """
    if not nets:
        return None, "нет закрытых сделок"
    total = sum(nets)
    if total <= 0:
        return None, ("сумма net <= 0 (%.4f) — доля вклада не определена, "
                      "деление на неположительную сумму меняет смысл числа" % total)
    top = sorted(nets, reverse=True)[:3]
    return sum(top) / total, ("доля суммы %d лучших сделок в общей сумме net" % len(top))


# =========================================================================
#  A2/A3 — МЕТРИКИ ДОРОЖКИ
# =========================================================================

def _liq_class_counts(rows):
    """Счётчик классов ликвидности. Не строка -> 'unknown' (класса нет)."""
    out = {}
    for t in rows:
        cls = t.get("liq_class")
        key = cls if isinstance(cls, str) else "unknown"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _fallback_n(rows):
    """Сколько сделок посчитано по ПОДСТАВЛЕННОМУ худшему классу (thin)."""
    return sum(1 for t in rows if costs.is_fallback_class(t.get("liq_class")))


def track_metrics(trades_of_track):
    """Метрики одной дорожки. Считаются ТОЛЬКО по закрытым сделкам.

    -> {'n_closed','n_open','mean_gross','median_gross','mean_net','median_net',
        'iqr_net':[q1,q3],'share_profitable_net','max_drawdown_pct',
        'max_drawdown_pct_equal_weight','max_drawdown_note','equity_curve_order',
        'top3_contribution_share','top3_contribution_note',
        'worst_trade_net','best_trade_net','liq_class_fallback_n',
        'liq_class_fallback_n_all','liq_class_fallback_n_closed',
        'liq_class_fallback_note','liq_class_counts','liq_class_counts_all',
        'n_closed_without_ret','status'}

    Пустой вход — законный вход: n_closed = 0, числа = None, status =
    «нет закрытых сделок». Никаких нулей вместо отсутствующих чисел: ноль это
    результат, отсутствие — не результат, и путать их нельзя.

    ИСКЛЮЧЕНИЕ из этого правила — счётчики подстановки класса ликвидности.
    Они считаются по ВСЕМ загруженным сделкам, а не только по закрытым, и
    поэтому осмысленны даже при нуле закрытых. Смысл поля — «сколько сделок
    посчитано по самому дорогому классу из-за незнания объёма»; закрытых
    01.09.2026 ноль, а подстановка идёт по 151 сделке из 421, и поле,
    считавшее только закрытые, показывало 0 ровно в тот день, когда его и
    надо было прочитать.
    """
    rows = list(trades_of_track or [])
    closed_all = [t for t in rows if t.get("status") == "closed"]
    open_rows = [t for t in rows if t.get("status") != "closed"]

    # Закрытая сделка без ret_pct — дефект данных, а не повод молча её потерять.
    closed = [t for t in closed_all if t.get("ret_net_pct") is not None
              and t.get("ret_gross_pct") is not None]
    n_without_ret = len(closed_all) - len(closed)

    # Подстановка класса считается ДВАЖДЫ и публикуется двумя полями:
    # по всем сделкам (издержки подставлены уже сейчас) и по закрытым
    # (сколько подстановки попало в опубликованные net-числа).
    liq_counts = _liq_class_counts(closed)
    liq_counts_all = _liq_class_counts(rows)
    fallback_closed = _fallback_n(closed)
    fallback_all = _fallback_n(rows)
    order_str = ", ".join(EQUITY_CURVE_ORDER_FIELDS)

    if not closed:
        return {
            "n_closed": 0,
            "n_open": len(open_rows),
            "mean_gross": None,
            "median_gross": None,
            "mean_net": None,
            "median_net": None,
            "iqr_net": [None, None],
            "share_profitable_net": None,
            "max_drawdown_pct": None,
            "max_drawdown_pct_equal_weight": None,
            "max_drawdown_note": MAX_DD_NOTE,
            "equity_curve_order": order_str,
            "top3_contribution_share": None,
            "top3_contribution_note": "нет закрытых сделок",
            "worst_trade_net": None,
            "best_trade_net": None,
            "liq_class_fallback_n": fallback_all,
            "liq_class_fallback_n_all": fallback_all,
            "liq_class_fallback_n_closed": fallback_closed,
            "liq_class_fallback_note": LIQ_FALLBACK_NOTE,
            "liq_class_counts": liq_counts,
            "liq_class_counts_all": liq_counts_all,
            "n_closed_without_ret": n_without_ret,
            "status": NO_CLOSED_STATUS,
        }

    # Порядок кривой капитала — ПОЛНЫЙ ключ curve_sort_key, а не одна дата
    # выхода: в один день закрывается несколько позиций, и по одной дате их
    # порядок задавал бы порядок строк в json (см. EQUITY_CURVE_ORDER_FIELDS).
    ordered = sorted(closed, key=curve_sort_key)
    nets_ordered = [float(t["ret_net_pct"]) for t in ordered]

    grosses = [float(t["ret_gross_pct"]) for t in closed]
    nets = [float(t["ret_net_pct"]) for t in closed]
    nets_sorted = sorted(nets)

    share_top3, note_top3 = top3_contribution(nets)
    n_profit = sum(1 for v in nets if v > 0)

    return {
        "n_closed": len(closed),
        "n_open": len(open_rows),
        "mean_gross": _round(_mean(grosses)),
        "median_gross": _round(_median(grosses)),
        "mean_net": _round(_mean(nets)),
        "median_net": _round(_median(nets)),
        "iqr_net": [_round(_quantile(nets_sorted, 0.25)), _round(_quantile(nets_sorted, 0.75))],
        "share_profitable_net": _round(n_profit / float(len(nets))),
        "max_drawdown_pct": _round(max_drawdown_pct(nets_ordered)),
        "max_drawdown_pct_equal_weight": _round(max_drawdown_pct_equal_weight(closed)),
        "max_drawdown_note": MAX_DD_NOTE,
        "equity_curve_order": order_str,
        "top3_contribution_share": _round(share_top3),
        "top3_contribution_note": note_top3,
        "worst_trade_net": _round(min(nets)),
        "best_trade_net": _round(max(nets)),
        "liq_class_fallback_n": fallback_all,
        "liq_class_fallback_n_all": fallback_all,
        "liq_class_fallback_n_closed": fallback_closed,
        "liq_class_fallback_note": LIQ_FALLBACK_NOTE,
        "liq_class_counts": liq_counts,
        "liq_class_counts_all": liq_counts_all,
        "n_closed_without_ret": n_without_ret,
        "status": HAS_CLOSED_STATUS,
    }


def sensitivity_rows(trades_of_track, cfg):
    """Таблица чувствительности net к множителю издержек (costs.yaml).

    Печатается ВСЕГДА и рядом с любым net-числом: слиппедж — допущение, и
    если знак результата меняется между множителями 0.5 и 2.0, честный вывод
    «издержки решают», а не «сито работает».

    -> [{'mult','n_closed','mean_net','median_net','share_profitable_net'}]
    Строки существуют и при нуле закрытых — с None вместо чисел, чтобы таблицу
    нельзя было «потерять» на пустых данных.
    """
    out = []
    closed = [t for t in (trades_of_track or [])
              if t.get("status") == "closed" and t.get("ret_gross_pct") is not None]
    for mult in cfg.get("sensitivity_multipliers", [1.0]):
        m = float(mult)
        nets = []
        for t in closed:
            val = costs.net_ret_pct(t["ret_gross_pct"], t.get("liq_class"),
                                    t.get("hold_days"), cfg, mult=m)
            if val is not None:
                nets.append(float(val))
        if nets:
            n_profit = sum(1 for v in nets if v > 0)
            out.append({
                "mult": m,
                "n_closed": len(nets),
                "mean_net": _round(_mean(nets)),
                "median_net": _round(_median(nets)),
                "share_profitable_net": _round(n_profit / float(len(nets))),
            })
        else:
            out.append({
                "mult": m,
                "n_closed": 0,
                "mean_net": None,
                "median_net": None,
                "share_profitable_net": None,
            })
    return out


# =========================================================================
#  A4 — ВОРОНКА ОТКАЗОВ
# =========================================================================

def _iso_week(day):
    """'2026-W32' по ISO-календарю. day = 'YYYY-MM-DD'."""
    try:
        dt = datetime.strptime(day[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    year, week, _ = dt.isocalendar()
    return "%04d-W%02d" % (year, week)


def funnel_metrics(log_path, repo_dir=None):
    """A4. Воронка отказов скринера по прогонам журнала кандидатов.

    По каждому прогону: счётчики reject_reason и разрез отказа no_perp
    ПО ИСТОЧНИКАМ:
      no_venue_at_all    — у монеты вообще нет площадок (features.venues_n == 0);
      vanished_since_prev — в ПРЕДЫДУЩЕМ прогоне перп у монеты был, в текущем
                            нет: кандидат в делистинг, самый интересный случай;
      new_coin           — монеты в прошлом прогоне не было вовсе (пополнение
                            вселенной, а не потеря площадки).
    Разрезы НЕ образуют разбиения: первый — про признак монеты, второй и
    третий — про её историю. Это сказано прямо, чтобы никто не складывал их
    в надежде получить общее число.

    Плюс тренд доли no_perp: список (run_ts, share) по прогонам, агрегат по
    ISO-неделям и дельта последней недели к предыдущей в процентных пунктах.

    Журнала нет / он пуст -> валидный словарь с пустыми списками и честным
    status. Падать нельзя: воронка не должна ронять весь артефакт метрик.

    repo_dir задаёт корень, ОТНОСИТЕЛЬНО которого печатается log_path. Раньше
    сюда шёл абсолютный путь, и один и тот же расчёт давал разные json локально
    и на Actions, а в файл попадало имя пользователя Windows.
    """
    out = {
        "log_path": rel_to_repo(log_path, repo_dir),
        "status": "ok",
        "n_runs": 0,
        "n_lines": 0,
        "bad_lines": 0,
        "runs": [],
        "no_perp_trend": [],
        "weekly": [],
        "delta_last_week_pp": None,
        "note": ("разрезы no_perp не складываются в общее число: no_venue_at_all — "
                 "признак монеты в текущем прогоне, vanished_since_prev и new_coin — "
                 "её история относительно предыдущего прогона"),
    }
    if not log_path or not os.path.isfile(log_path):
        out["status"] = "журнал кандидатов не найден"
        return out

    order = []            # run_id в порядке появления
    runs = {}             # run_id -> накопитель
    bad = 0
    n_lines = 0

    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                bad += 1
                continue
            if not isinstance(obj, dict) or "_meta" in obj:
                continue
            if "_run" in obj:
                meta = obj.get("_run") or {}
                rid = meta.get("run_id")
                if rid and rid not in runs:
                    order.append(rid)
                    runs[rid] = _new_run_node(rid, meta.get("timestamp_utc"))
                elif rid:
                    runs[rid]["run_ts"] = runs[rid]["run_ts"] or meta.get("timestamp_utc")
                continue

            rid = obj.get("run_id")
            if not rid:
                bad += 1
                continue
            if rid not in runs:
                order.append(rid)
                runs[rid] = _new_run_node(rid, obj.get("timestamp_utc"))
            node = runs[rid]
            if not node["run_ts"]:
                node["run_ts"] = obj.get("timestamp_utc")
            n_lines += 1
            node["n_lines"] += 1

            cg_id = obj.get("cg_id")
            if isinstance(cg_id, str) and cg_id:
                node["ids"].add(cg_id)

            if obj.get("decision") == "enter":
                node["enter_n"] += 1

            reason = obj.get("reject_reason")
            key = "none" if reason is None else str(reason)
            node["reject_counts"][key] = node["reject_counts"].get(key, 0) + 1

            if reason == "no_perp":
                node["no_perp_n"] += 1
                if isinstance(cg_id, str) and cg_id:
                    node["no_perp_ids"].add(cg_id)
                features = obj.get("features")
                venues = features.get("venues_n") if isinstance(features, dict) else None
                if isinstance(venues, (int, float)) and not isinstance(venues, bool):
                    if float(venues) == 0.0:
                        node["no_venue_at_all"] += 1
                    else:
                        node["venues_but_no_perp"] += 1
                else:
                    node["venues_unknown"] += 1

    out["bad_lines"] = bad
    out["n_lines"] = n_lines
    out["n_runs"] = len(order)
    if not order:
        out["status"] = "в журнале нет строк прогонов"
        return out

    prev_rid = None
    for rid in order:
        node = runs[rid]
        share = (node["no_perp_n"] / float(node["n_lines"])) if node["n_lines"] else None
        vanished = new_coin = None
        if prev_rid is not None:
            prev = runs[prev_rid]
            prev_ids = prev["ids"]
            prev_no_perp = prev["no_perp_ids"]
            vanished = 0
            new_coin = 0
            for cid in node["no_perp_ids"]:
                if cid not in prev_ids:
                    new_coin += 1
                elif cid not in prev_no_perp:
                    # монета была в прошлом прогоне и НЕ была no_perp -> перп исчез
                    vanished += 1

        denom = float(node["no_perp_n"]) if node["no_perp_n"] else None
        out["runs"].append({
            "run_id": rid,
            "run_ts": node["run_ts"],
            "n_lines": node["n_lines"],
            "enter_n": node["enter_n"],
            "reject_counts": dict(sorted(node["reject_counts"].items())),
            "no_perp_n": node["no_perp_n"],
            "no_perp_share": _round(share),
            "no_perp_sources": {
                "prev_run_id": prev_rid,
                "no_venue_at_all": node["no_venue_at_all"],
                "venues_but_no_perp": node["venues_but_no_perp"],
                "venues_field_missing": node["venues_unknown"],
                "vanished_since_prev": vanished,
                "new_coin": new_coin,
                "share_no_venue_at_all": _round(node["no_venue_at_all"] / denom) if denom else None,
                "share_vanished_since_prev": (_round(vanished / denom)
                                              if (denom and vanished is not None) else None),
                "share_new_coin": (_round(new_coin / denom)
                                   if (denom and new_coin is not None) else None),
            },
        })
        if share is not None:
            out["no_perp_trend"].append([node["run_ts"], _round(share)])
        prev_rid = rid

    # --- недельный агрегат ---------------------------------------------
    weeks = []
    by_week = {}
    for rid in order:
        node = runs[rid]
        week = _iso_week(node["run_ts"] or "")
        if week is None:
            continue
        if week not in by_week:
            by_week[week] = {"week": week, "n_runs": 0, "n_lines": 0, "no_perp_n": 0}
            weeks.append(week)
        acc = by_week[week]
        acc["n_runs"] += 1
        acc["n_lines"] += node["n_lines"]
        acc["no_perp_n"] += node["no_perp_n"]
    for week in weeks:
        acc = by_week[week]
        acc["share"] = _round(acc["no_perp_n"] / float(acc["n_lines"])) if acc["n_lines"] else None
        out["weekly"].append(acc)

    if len(out["weekly"]) >= 2:
        last = out["weekly"][-1]["share"]
        prev = out["weekly"][-2]["share"]
        if last is not None and prev is not None:
            # в ПРОЦЕНТНЫХ ПУНКТАХ: доли -> проценты -> разность
            out["delta_last_week_pp"] = _round((last - prev) * 100.0, 4)

    return out


def _new_run_node(run_id, run_ts):
    return {
        "run_id": run_id,
        "run_ts": run_ts,
        "n_lines": 0,
        "enter_n": 0,
        "reject_counts": {},
        "no_perp_n": 0,
        "no_venue_at_all": 0,
        "venues_but_no_perp": 0,
        "venues_unknown": 0,
        "ids": set(),
        "no_perp_ids": set(),
    }


# =========================================================================
#  СБОРКА АРТЕФАКТА
# =========================================================================

def _costs_config_used(cfg):
    """Слепок конфига издержек для артефакта.

    Полные тексты source сюда не кладём намеренно: они длинные, а артефакт
    целиком уходит в промпт аудитора. Вместо них — числа, путь к файлу и
    прямое указание, что слиппедж это ДОПУЩЕНИЕ.
    """
    slip = cfg.get("slippage_bps_by_class", {})
    thr = cfg.get("liquidity_class_thresholds", {})
    return {
        "path": os.path.basename(cfg.get("_source_path", "costs.yaml")),
        "version": cfg.get("version"),
        "instrument": cfg.get("instrument"),
        "taker_fee_bps": cfg.get("taker_fee_bps", {}).get("value"),
        "funding_applies_to_spot": cfg.get("funding", {}).get("applies_to_spot"),
        "funding_bps_per_day": cfg.get("funding", {}).get("bps_per_day", {}).get("value"),
        "slippage_bps_by_class": {k: slip.get(k, {}).get("value") for k in ("deep", "medium", "thin")},
        "liquidity_class_thresholds": {
            "deep_min_vol_usd": thr.get("deep_min_vol_usd"),
            "medium_min_vol_usd": thr.get("medium_min_vol_usd"),
        },
        "position_usd": cfg.get("position_usd"),
        "sensitivity_multipliers": list(cfg.get("sensitivity_multipliers", [])),
        "sensitivity_applies_to": cfg.get("sensitivity_applies_to", "all"),
        "slippage_is_assumption": True,
        "honesty_note": ("слиппедж и границы классов ликвидности — консервативные "
                         "ДОПУЩЕНИЯ, а не измерения (стакана у проекта нет); "
                         "любое net-число читать только вместе с таблицей "
                         "чувствительности sensitivity"),
    }


def _equity_curve_model():
    """Паспорт обеих просадок: модель, порядок сортировки, границы.

    Лежит в артефакте отдельным полем, потому что число просадки без модели
    нечитаемо: «18.2 %» ничего не значит, пока не сказано, чем двигали капитал
    и в каком порядке шли сделки. Порядок публикуется списком полей, чтобы
    расчёт можно было повторить чужими руками и получить тот же бит.
    """
    return {
        "order_fields": list(EQUITY_CURVE_ORDER_FIELDS),
        "missing_date_sorts_as": _MISSING_DATE,
        "order_note": ("полный детерминированный порядок; сортировки только по "
                       "exit_date НЕ ХВАТАЕТ — в один день закрывается несколько "
                       "позиций, и их порядок внутри дня задавал бы порядок строк "
                       "в tournament.json/positions.json, а с ним менялась бы и "
                       "просадка"),
        "max_drawdown_pct": ("последовательная кривая «одна ставка за раз»: "
                             "E_0 = 1, E_i = E_(i-1) + net_i/100. Масштабно-"
                             "инвариантна (размер ставки не влияет)"),
        "max_drawdown_pct_equal_weight": ("равновзвешенный портфель: капитал "
                                          "делится поровну между всеми N закрытыми "
                                          "сделками, результат приходит в день "
                                          "выхода, просадка меряется по датам; "
                                          "от порядка внутри дня не зависит вовсе"),
        "limitation": ("ни одно из двух чисел не является просадкой портфеля "
                       "дорожки: дорожка держит до 60 позиций одновременно, а "
                       "переоценки открытых позиций в расчёте нет и быть не может "
                       "(§2 замороженных правил запрещает open_pnl в любом виде). "
                       "max_drawdown_pct — просадка последовательности исходов, "
                       "max_drawdown_pct_equal_weight — просадка РЕАЛИЗОВАННОГО "
                       "результата портфеля, то есть нижняя оценка настоящей"),
    }


def build_metrics(repo_dir=".", as_of=None):
    """Полный артефакт метрик. Ничего не пишет на диск — только считает.

    Обязательные поля верхнего уровня (контракт пакета): provenance,
    costs_config_used, tracks, funnel, rules_sha256, n_closed_total, note.
    Плюс status, sources, sensitivity, totals — они нужны аудиту и светофору.
    """
    repo_dir = os.path.abspath(repo_dir or ".")
    cfg = trades_mod.costs_cfg_for(repo_dir)
    all_trades = trades_mod.load_all_trades(repo_dir, as_of=as_of, costs_cfg=cfg)

    by_track = {}
    for t in all_trades:
        by_track.setdefault(t["track"], []).append(t)

    tracks = {}
    for track in sorted(by_track):
        node = track_metrics(by_track[track])
        node["sensitivity"] = sensitivity_rows(by_track[track], cfg)
        node["in_statistical_test"] = (track != trades_mod.A0_TRACK)
        tracks[track] = node

    n_closed_total = sum(node["n_closed"] for node in tracks.values())
    n_open_total = sum(node["n_open"] for node in tracks.values())
    fallback_total = sum(node["liq_class_fallback_n_all"] for node in tracks.values())
    fallback_closed_total = sum(node["liq_class_fallback_n_closed"] for node in tracks.values())

    overall = track_metrics(all_trades)
    overall["sensitivity"] = sensitivity_rows(all_trades, cfg)

    # Дорожки без единой закрытой сделки называются ПОИМЁННО: §3.1 правил
    # исключает их из теста по факту пустоты, и этот факт должен быть в
    # артефакте до 15.11, а не выясняться в день вердикта.
    tracks_empty = [t for t in sorted(tracks) if tracks[t]["n_closed"] == 0]

    funnel = funnel_metrics(os.path.join(repo_dir, trades_mod.CANDIDATES_LOG),
                            repo_dir=repo_dir)

    rules_path = os.path.join(repo_dir, rules_hash.DEFAULT_RULES_FILENAME)
    sha = rules_hash.rules_sha256_or_none(rules_path)
    # Поправка №1 (02.09.2026): отпечаток лежит рядом с отпечатком правил —
    # чтобы в артефакте было видно, что A6 выведена из теста поправкой, а не
    # молчаливой правкой кода. Нет файла — null, и это тоже факт.
    addendum_sha = rules_addendum.addendum_sha256_or_none(repo_dir)

    fallback_line = ("Класс ликвидности подставлен худшим (thin) у %d сделок из "
                     "%d загруженных, из них закрытых %d — поля "
                     "liq_class_fallback_n_all и liq_class_fallback_n_closed."
                     % (fallback_total, len(all_trades), fallback_closed_total))

    if n_closed_total == 0:
        status = NO_CLOSED_STATUS
        note = ("Закрытых сделок ноль — это ожидаемое состояние, а не сбой: по "
                "правилам скринера позиция закрывается на первом прогоне с "
                "возрастом >= 60 дней, первые закрытия 12-22.09.2026. Открытые "
                "позиции в метрики не идут вообще (§2 замороженных правил: "
                "open_pnl не участвует ни как метрика, ни как предварительный "
                "срез). Все числовые поля дорожек = null; это отсутствие "
                "данных, а не ноль результата. " + fallback_line)
    else:
        status = HAS_CLOSED_STATUS
        note = ("Числа посчитаны кодом (metrics.py) по ЗАКРЫТЫМ сделкам. "
                "Открытые позиции не участвуют (§2 замороженных правил). "
                "net содержит допущение о слиппедже — читать вместе с "
                "таблицей sensitivity. " + fallback_line)

    return {
        "schema_version": 1,
        "status": status,
        "provenance": provenance.stamp(),
        "rules_sha256": sha,
        "rules_file": os.path.basename(rules_path) if sha else None,
        "rules_addendum_sha256": addendum_sha,
        "rules_addendum_file": (rules_addendum.DEFAULT_ADDENDUM_FILENAME
                                if addendum_sha else None),
        "costs_config_used": _costs_config_used(cfg),
        "sources": trades_mod.sources_report(repo_dir),
        "paths_are_relative_to": "корень репозитория (repo_dir)",
        "equity_curve_model": _equity_curve_model(),
        "n_closed_total": n_closed_total,
        "n_open_total": n_open_total,
        "n_trades_total": len(all_trades),
        "liq_class_fallback_n": fallback_total,
        "liq_class_fallback_n_all": fallback_total,
        "liq_class_fallback_n_closed": fallback_closed_total,
        "liq_class_fallback_note": LIQ_FALLBACK_NOTE,
        "tracks_without_closed": tracks_empty,
        "tracks": tracks,
        "overall": overall,
        "funnel": funnel,
        "note": note,
    }


def write_artifacts(payload, repo_dir=".", as_of=None):
    """Записать metrics_latest.json и датированную копию artifacts/YYYY-MM/.

    -> (latest_path, dated_path). Каталоги создаются через makedirs(exist_ok).
    Дата берётся из provenance.generated_utc самого артефакта, а не из часов
    в момент записи: файл и его имя обязаны говорить об одном и том же дне.
    """
    repo_dir = os.path.abspath(repo_dir or ".")
    stamp_day = (payload.get("provenance", {}) or {}).get("generated_utc") or ""
    day = stamp_day[:10] if len(stamp_day) >= 10 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if as_of:
        day = str(as_of)[:10]

    art_dir = os.path.join(repo_dir, ARTIFACTS_DIR)
    month_dir = os.path.join(art_dir, day[:7])
    os.makedirs(month_dir, exist_ok=True)

    latest = os.path.join(art_dir, LATEST_NAME)
    dated = os.path.join(month_dir, "metrics_%s.json" % day.replace("-", ""))
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    for path in (latest, dated):
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.write("\n")
    return latest, dated


# =========================================================================
#  ДИФФ ДЛЯ ЕЖЕНЕДЕЛЬНОГО АУДИТА
# =========================================================================

def _flat_track(node):
    """Числовые поля дорожки одним плоским словарём (iqr разложен на два)."""
    if not isinstance(node, dict):
        return {}
    flat = {}
    for field in _TRACK_NUMERIC_FIELDS:
        if field == "iqr_net_q1":
            iqr = node.get("iqr_net") or [None, None]
            flat[field] = iqr[0] if len(iqr) > 0 else None
        elif field == "iqr_net_q3":
            iqr = node.get("iqr_net") or [None, None]
            flat[field] = iqr[1] if len(iqr) > 1 else None
        else:
            flat[field] = node.get(field)
    return flat


def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def diff_metrics(prev, cur):
    """«Что изменилось с прошлой недели» — для раздела аудита (B2).

    prev — предыдущий датированный артефакт (или None, если его нет),
    cur — текущий. Считает по КАЖДОЙ дорожке дельты числовых полей и отдельно
    списками — что появилось и что исчезло.

    «Появилось» = поле было null, стало числом (например, дорожка получила
    первую закрытую сделку). «Исчезло» = наоборот. Это важнее дельт: первое
    закрытие в дорожке — событие, а дельта от null не считается вовсе.

    -> {'status','prev_generated_utc','cur_generated_utc','rules_sha256_changed',
        'totals','tracks','tracks_appeared','tracks_disappeared','funnel',
        'summary'}
    Никогда не падает на неполном/чужом артефакте: чего нет, того нет.
    """
    cur = cur if isinstance(cur, dict) else {}
    cur_prov = cur.get("provenance") or {}

    if not isinstance(prev, dict) or not prev:
        return {
            "status": "предыдущего артефакта нет — сравнивать не с чем",
            "prev_generated_utc": None,
            "cur_generated_utc": cur_prov.get("generated_utc"),
            "prev_git_sha": None,
            "cur_git_sha": cur_prov.get("git_sha"),
            "rules_sha256_changed": None,
            "totals": {},
            "tracks": {},
            "tracks_appeared": sorted((cur.get("tracks") or {}).keys()),
            "tracks_disappeared": [],
            "funnel": {},
            "summary": ["Предыдущего артефакта метрик нет: это первый срез, "
                        "сравнивать не с чем."],
        }

    prev_prov = prev.get("provenance") or {}
    prev_tracks = prev.get("tracks") or {}
    cur_tracks = cur.get("tracks") or {}

    out = {
        "status": "ok",
        "prev_generated_utc": prev_prov.get("generated_utc"),
        "cur_generated_utc": cur_prov.get("generated_utc"),
        "prev_git_sha": prev_prov.get("git_sha"),
        "cur_git_sha": cur_prov.get("git_sha"),
        "rules_sha256_changed": (prev.get("rules_sha256") != cur.get("rules_sha256")),
        "prev_rules_sha256": prev.get("rules_sha256"),
        "cur_rules_sha256": cur.get("rules_sha256"),
        "totals": {},
        "tracks": {},
        "tracks_appeared": sorted(set(cur_tracks) - set(prev_tracks)),
        "tracks_disappeared": sorted(set(prev_tracks) - set(cur_tracks)),
        "funnel": {},
        "summary": [],
    }

    for field in ("n_closed_total", "n_open_total", "n_trades_total",
                  "liq_class_fallback_n", "liq_class_fallback_n_all",
                  "liq_class_fallback_n_closed"):
        pv, cv = prev.get(field), cur.get(field)
        delta = (cv - pv) if (_is_num(pv) and _is_num(cv)) else None
        out["totals"][field] = {"prev": pv, "cur": cv, "delta": delta}

    for track in sorted(set(prev_tracks) | set(cur_tracks)):
        pflat = _flat_track(prev_tracks.get(track))
        cflat = _flat_track(cur_tracks.get(track))
        fields = {}
        appeared, disappeared = [], []
        for name in _TRACK_NUMERIC_FIELDS:
            pv, cv = pflat.get(name), cflat.get(name)
            if _is_num(pv) and _is_num(cv):
                delta = round(cv - pv, 6)
            else:
                delta = None
                if pv is None and _is_num(cv):
                    appeared.append(name)
                elif _is_num(pv) and cv is None:
                    disappeared.append(name)
            if pv != cv:
                fields[name] = {"prev": pv, "cur": cv, "delta": delta}
        out["tracks"][track] = {
            "changed_fields": fields,
            "appeared": appeared,
            "disappeared": disappeared,
            "present_prev": track in prev_tracks,
            "present_cur": track in cur_tracks,
        }
        if appeared:
            out["summary"].append(
                "%s: появились числа там, где их не было (%s)" % (track, ", ".join(appeared)))
        n_delta = fields.get("n_closed", {}).get("delta")
        if _is_num(n_delta) and n_delta != 0:
            out["summary"].append("%s: закрытых сделок %+d" % (track, n_delta))

    # --- воронка ---------------------------------------------------------
    pf = prev.get("funnel") or {}
    cf = cur.get("funnel") or {}
    prev_runs = [r.get("run_id") for r in (pf.get("runs") or [])]
    cur_runs = [r.get("run_id") for r in (cf.get("runs") or [])]
    prev_last = (pf.get("runs") or [{}])[-1] if pf.get("runs") else {}
    cur_last = (cf.get("runs") or [{}])[-1] if cf.get("runs") else {}
    pv, cv = prev_last.get("no_perp_share"), cur_last.get("no_perp_share")
    out["funnel"] = {
        "runs_appeared": [r for r in cur_runs if r not in set(prev_runs)],
        "runs_disappeared": [r for r in prev_runs if r not in set(cur_runs)],
        "n_runs": {"prev": pf.get("n_runs"), "cur": cf.get("n_runs")},
        "no_perp_share_last_run": {
            "prev": pv,
            "cur": cv,
            "delta_pp": (round((cv - pv) * 100.0, 4) if (_is_num(pv) and _is_num(cv)) else None),
        },
        "delta_last_week_pp": {"prev": pf.get("delta_last_week_pp"),
                               "cur": cf.get("delta_last_week_pp")},
    }
    if out["funnel"]["runs_appeared"]:
        out["summary"].append("новых прогонов в журнале: %d"
                              % len(out["funnel"]["runs_appeared"]))
    dpp = out["funnel"]["no_perp_share_last_run"]["delta_pp"]
    if _is_num(dpp) and abs(dpp) >= 0.5:
        out["summary"].append("доля no_perp в последнем прогоне изменилась на %+.2f п.п." % dpp)
    if out["rules_sha256_changed"]:
        out["summary"].append(
            "ВНИМАНИЕ: sha256 замороженных правил ИЗМЕНИЛСЯ — правила правились "
            "после заморозки, это требует объяснения пилоту.")
    if not out["summary"]:
        out["summary"].append("Значимых изменений против прошлого среза не найдено.")
    return out


def load_previous_artifact(repo_dir=".", exclude_path=None):
    """Последний ДАТИРОВАННЫЙ артефакт метрик, кроме указанного.

    Нужен CLI и аудитору: diff считается против предыдущего датированного
    файла, а не против latest (latest уже перезаписан текущим прогоном).
    -> (payload|None, path|None). Битый файл пропускается молча — он не должен
    ронять запись свежего артефакта.
    """
    repo_dir = os.path.abspath(repo_dir or ".")
    art_dir = os.path.join(repo_dir, ARTIFACTS_DIR)
    if not os.path.isdir(art_dir):
        return None, None
    found = []
    for month in os.listdir(art_dir):
        month_dir = os.path.join(art_dir, month)
        if not os.path.isdir(month_dir):
            continue
        for name in os.listdir(month_dir):
            if name.startswith("metrics_") and name.endswith(".json"):
                found.append(os.path.join(month_dir, name))
    excl = os.path.abspath(exclude_path) if exclude_path else None
    found = [p for p in sorted(found) if os.path.abspath(p) != excl]
    for path in reversed(found):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh), path
        except (OSError, ValueError):
            continue
    return None, None


# =========================================================================
#  CLI
# =========================================================================

def _fmt(value, spec="%.2f"):
    return "—" if value is None else (spec % value)


def _print_summary(payload, latest, dated, diff):
    print("МЕТРИКИ ТУРНИРА СИТ (blocks A2/A3/A4) — все числа посчитаны кодом")
    prov = payload["provenance"]
    print("git_sha %s | %s | python %s | tool %s"
          % (prov["git_sha"], prov["generated_utc"], prov["python"], prov["tool_version"]))
    print("sha256 замороженных правил: %s" % (payload["rules_sha256"] or "НЕ НАЙДЕН"))
    print("sha256 поправки №1 (A6 — наблюдательная, вне теста): %s"
          % (payload.get("rules_addendum_sha256") or "НЕТ ФАЙЛА"))
    print("статус: %s" % payload["status"])
    print("")

    print("ДОРОЖКИ (метрики только по ЗАКРЫТЫМ сделкам, §2 правил)")
    print("  %-5s %7s %7s %10s %10s %10s %10s %9s %9s"
          % ("дор.", "закр", "откр", "ср.gross", "мед.gross", "ср.net", "мед.net",
             "прос.%", "прос.EW"))
    for track in sorted(payload["tracks"]):
        node = payload["tracks"][track]
        print("  %-5s %7d %7d %10s %10s %10s %10s %9s %9s"
              % (track, node["n_closed"], node["n_open"],
                 _fmt(node["mean_gross"]), _fmt(node["median_gross"]),
                 _fmt(node["mean_net"]), _fmt(node["median_net"]),
                 _fmt(node["max_drawdown_pct"]),
                 _fmt(node["max_drawdown_pct_equal_weight"])))
    print("  всего закрытых: %d, открытых: %d, сделок в файлах: %d"
          % (payload["n_closed_total"], payload["n_open_total"], payload["n_trades_total"]))
    print("  порядок кривой капитала: %s"
          % ", ".join(payload["equity_curve_model"]["order_fields"]))
    # Строка без %-форматирования: «%%» здесь напечаталось бы дословно.
    print("  ОГОВОРКА: прос.% — кривая «одна ставка за раз», прос.EW —")
    print("  равновзвешенный портфель по датам выхода. Ни одно из двух чисел НЕ")
    print("  является просадкой портфеля дорожки: дорожка держит до 60 позиций")
    print("  одновременно, а переоценки открытых позиций в расчёте нет (§2 правил).")
    print("  подстановка худшего класса издержек (нет объёма на дату входа):")
    print("    по всем сделкам %d, из них по закрытым %d"
          % (payload["liq_class_fallback_n_all"], payload["liq_class_fallback_n_closed"]))
    if payload["tracks_without_closed"]:
        print("  без единой закрытой сделки: %s" % ", ".join(payload["tracks_without_closed"]))
        print("  (§3.1 правил: такая дорожка исключается из теста ПОИМЁННО, по факту пустоты)")

    print("")
    print("ТАБЛИЦА ЧУВСТВИТЕЛЬНОСТИ (издержки — допущение, см. costs.yaml)")
    for row in payload["overall"]["sensitivity"]:
        print("  множитель x%.1f: n=%d, ср.net %s, мед.net %s, доля прибыльных %s"
              % (row["mult"], row["n_closed"], _fmt(row["mean_net"]),
                 _fmt(row["median_net"]), _fmt(row["share_profitable_net"], "%.3f")))

    funnel = payload["funnel"]
    print("")
    print("ВОРОНКА ОТКАЗОВ (A4): прогонов %d, строк %d, статус %s"
          % (funnel["n_runs"], funnel["n_lines"], funnel["status"]))
    if funnel["runs"]:
        last = funnel["runs"][-1]
        print("  последний прогон %s: строк %d, вошло %d, no_perp %d (%s)"
              % (last["run_ts"], last["n_lines"], last["enter_n"],
                 last["no_perp_n"], _fmt(last["no_perp_share"], "%.3f")))
        src = last["no_perp_sources"]
        print("    источники no_perp: нет площадок вовсе %d, площадки есть но перпа нет %d,"
              % (src["no_venue_at_all"], src["venues_but_no_perp"]))
        print("    исчез с прошлого прогона %s, новая монета %s"
              % (src["vanished_since_prev"] if src["vanished_since_prev"] is not None else "—",
                 src["new_coin"] if src["new_coin"] is not None else "—"))
        print("  причины отказов последнего прогона: %s"
              % ", ".join("%s=%d" % (k, v) for k, v in last["reject_counts"].items()))
    if funnel["weekly"]:
        tail = funnel["weekly"][-4:]
        print("  доля no_perp по неделям: %s"
              % ", ".join("%s %s" % (w["week"], _fmt(w["share"], "%.3f")) for w in tail))
        print("  дельта последней недели к предыдущей: %s п.п."
              % _fmt(funnel["delta_last_week_pp"], "%+.2f"))

    print("")
    print("ЧТО ИЗМЕНИЛОСЬ С ПРОШЛОГО СРЕЗА (%s)" % diff.get("status"))
    for line in diff.get("summary", []):
        print("  - %s" % line)

    print("")
    print("записано: %s" % latest)
    print("записано: %s" % dated)
    if payload["status"] == NO_CLOSED_STATUS:
        print("")
        print("ЗАКРЫТЫХ СДЕЛОК НОЛЬ — артефакт валиден и честен: числовые поля null,")
        print("а не нули. Первые закрытия 12-22.09.2026; до них измерять нечего,")
        print("и открытые позиции подставлять запрещено (§2 замороженных правил).")


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    argv = list(sys.argv[1:] if argv is None else argv)

    repo_dir = "."
    as_of = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--as-of" and i + 1 < len(argv):
            as_of = argv[i + 1]
            i += 2
            continue
        if arg in ("-h", "--help"):
            print("использование: python metrics.py [repo_dir] [--as-of YYYY-MM-DD]")
            return 0
        repo_dir = arg
        i += 1

    payload = build_metrics(repo_dir, as_of=as_of)
    latest, dated = write_artifacts(payload, repo_dir, as_of=as_of)
    prev, _prev_path = load_previous_artifact(repo_dir, exclude_path=dated)
    diff = diff_metrics(prev, payload)
    _print_summary(payload, latest, dated, diff)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

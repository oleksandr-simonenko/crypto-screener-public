#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""funding_logger.py — почасовой архив фандинга и открытого интереса Hyperliquid
(блок D2 пакета СКРИНЕР-ПОЛНЫЙ).

ЗАЧЕМ ЭТО НУЖНО ДЛЯ ДЕНЕГ
`metaAndAssetCtxs` — фотография «прямо сейчас»: истории ОТКРЫТОГО ИНТЕРЕСА,
цен и объёмов по всем перпам одним запросом у Hyperliquid нет. OI, который
мы не записали сегодня, завтра взять будет неоткуда ни за какие деньги —
единственная причина, по которой логгер бежит КАЖДЫЙ час: не потому что
час — правильная частота (она ещё не проверена ни одной гипотезой), а
потому что пропущенный срез OI не восстановим.
УТОЧНЕНИЕ 02.09.2026: у ФАНДИНГА история ЕСТЬ — {"type": "fundingHistory"}
отдаёт помонетно почасовые ставки задним числом. Пропущенные часы фандинга
скрипт теперь ДОБИРАЕТ сам (см. «ДОБОР ПРОПУСКОВ» ниже); слово
«невосстановимо» осталось только за OI и прочими срезовыми полями.

Что с этим потом делать — вопрос ОТКРЫТЫЙ, и честно: на 01.09.2026 ни одна
гипотеза на фандинге в этом проекте не подтверждена, и данный файл никакого
сигнала не даёт. Он даёт ВОЗМОЖНОСТЬ проверить гипотезы вида «перед разлоком
растёт открытый интерес» или «экстремальный фандинг предшествует сливу» —
через пару месяцев, когда накопится ряд. Без архива такая проверка невозможна
в принципе, с архивом — стоит один прогон скрипта.

ИСТОЧНИК (проверен фактом 01.09.2026, не менять на выдуманный)
    POST https://api.hyperliquid.xyz/info   тело {"type": "metaAndAssetCtxs"}
    -> HTTP 200, JSON — список РОВНО из двух элементов:
       [0]["universe"][i] = {name, szDecimals, maxLeverage, marginTableId,
                             (иногда) isDelisted, marginMode, onlyIsolated}
       [1][i]             = {funding, openInterest, prevDayPx, dayNtlVlm,
                             premium, oraclePx, markPx, midPx, impactPxs,
                             dayBaseVlm}
    Два списка выравниваются ПО ИНДЕКСУ — общего ключа в них нет, имя монеты
    живёт только в universe. 233 перпа на 01.09.2026. Ключей API не требует.

ЧЕСТНАЯ ОГОВОРКА ПРО ДЛИНЫ СПИСКОВ
Выравнивание по индексу — это допущение о протоколе, а не о нашем коде: если
биржа однажды отдаст списки разной длины, «пара i-й к i-му» может поехать.
Молча писать такое в архив нельзя (испорченный ряд хуже отсутствующего),
падать — тоже нельзя (потеряем час из-за одной кривой отдачи). Поэтому:
режем по МИНИМУМУ длин, пишем то, что заведомо соответствует друг другу,
и печатаем предупреждение в stdout, которое видно в логе Actions.

ЧТО ЛЕЖИТ В АРХИВЕ
56 из 233 инструментов на 01.09.2026 помечены `isDelisted` — у них НЕТ полей
`midPx` и `premium`. Такие строки пишутся всё равно (делистинг сам по себе
факт, который потом захочется видеть), с ПУСТЫМИ ячейками mid_px и premium.
Пустая ячейка = «источник не дал значения», а не ноль. Числа кладутся
СТРОКАМИ ровно так, как их отдал сервер: перевод в float потерял бы точность
и добавил бы к данным нашу ошибку округления там, где её не было.

Файл: data/funding/YYYY-MM/funding_YYYYMMDD.csv — ДОЗАПИСЬ, заголовок пишется
при создании файла.

ИДЕМПОТЕНТНОСТЬ СЧИТАЕТСЯ ПО ПЛАНОВОМУ СЛОТУ CRON, А НЕ ПО ЧАСУ СТАРТА
(находка ревизии 01.09.2026 — до неё ключом был час фактического запуска).
GitHub Actions регулярно задерживает старт на десятки минут. Прогон слота
14:07, стартовавший в 15:02, писался как «час 15»; следом приходил настоящий
слот 15:07, видел час 15 уже снятым и ТИХО не писал ничего. Терялось два
часа сразу — один не снят, второй объявлен дублем, и ни строчки в лог.
Теперь прогон сам вычисляет, КАКОЙ слот он обслуживает: последнее плановое
время `HH:SLOT_MINUTE_UTC`, не превосходящее момент старта. Тогда прогон в
15:02 — это слот 14:07, а прогон в 15:10 — слот 15:07, и оба пишут своё.
`ts_utc` в строке остаётся ФАКТИЧЕСКИМ временем среза (данные сняты именно
тогда, врать про это нельзя), а слот выводится из него той же функцией при
чтении файла — отдельной колонки для этого не нужно.
День файла тоже берётся ПО СЛОТУ, а не по часам старта: иначе слот 23:07,
отработавший в 00:02, лёг бы в файл следующих суток, где дедуп его не увидел
бы рядом с собственной первой записью.

ДОБОР ПРОПУСКОВ (добавлен 02.09.2026; ПО ПАРАМ (слот, монета) — С-24, 03.09.2026)
Каждый прогон сверяет окно последних BACKFILL_WINDOW_HOURS часов ПО ПАРАМ
(слот, монета) для монет ТЕКУЩЕЙ вселенной (живой ответ этого прогона). Пол
у каждой монеты свой — слот её первой записи в файлах окна: монете,
листнутой посреди окна, дырок не выдумываем; монете вселенной без единой
записи при непустом архиве делается ОДИН пробный запрос истории за окно
(что пришло — записывается, пол = первая запись биржи). Свежий архив
(файлов нет вовсе) дырок не выдумывает; архив, молчавший ДОЛЬШЕ окна (файлы
старше окна есть, в окне ни строки), добирает всё окно и краснеет — о такой
потере молчать нельзя. Недостающие пары ФАНДИНГА добираются из истории
биржи: по каждой монете с долгом один запрос fundingHistory на её окно
пропуска, очередь — старейший долг первым (иначе после обрыва бюджета хвост
вселенной голодал бы). Строки пишутся сразу по монете, дедуп по (слот,
монета); оборванный добор продолжается следующим прогоном ровно с
недобранных монет. У добранной строки заполнены ts_utc (момент расчёта
ставки), coin, funding, premium и git_sha добравшего кода; ОСТАЛЬНЫЕ ячейки
ПУСТЫЕ, а в колонке note стоит пометка 'missing_oi' — открытый интерес
задним числом биржа не отдаёт, и нолём его подменять нельзя.
Пара, за которую история монеты пришла, а записи за этот час в ней нет
(«без истории у биржи»), — НЕ дефект и НЕ метка: строка-метка стала бы
необратимым замком на паре (дедуп отбросил бы настоящую строку, отданную
биржей позже, — ревью 03.09), поэтому такая пара просто переспрашивается
каждым прогоном, пока не выйдет из окна, и считается в outputs
(pairs_no_history). Добор живёт в бюджете BACKFILL_DEADLINE_SEC и в общем
бюджете прогона RUN_BUDGET_SEC (job убивается через timeout-minutes: 10, а
убитый шаг не оставляет ни outputs, ни тревоги).

ПОТЕРЯ ЧАСА НИКОГДА НЕ МОЛЧИТ — НО И ЗАЛЕЧЕННЫЙ ПРОПУСК НЕ КРАСИТ ПРОГОН
(С-22, 02.09.2026). До С-22 любая дырка окна давала код 2, даже если фандинг
за неё был добран полностью: залеченный пропуск красил прогон, тревога
приходила на уже исправленное, и красный цвет переставал что-либо значить.
Теперь у прогона РОВНО ТРИ исхода, и код возврата честно называет каждый:

  ok      — пропуска в окне нет                                  -> код 0
  healed  — пропуск был И добран: строки фандинга записаны, ни одна
            монета не отказала, бюджет не исчерпан, ни одного часа
            без единой строки; пары «без истории у биржи» не дефект -> код 0
            + сообщение класса ИНФО в Telegram из самого скрипта
            («пропуск N ч / M пар залечен: фандинг добран, OI пуст
            (missing_oi)») — при пропуске от INFO_GAP_HOURS часов;
            рутинный пропуск (короче — обычная задержка расписания
            GitHub, замер 03.09: cron исполняется раз в 4–5 ч) даёт
            `::notice::` на странице прогона и outputs, без Telegram
  ok      — также: пропуск есть, дефектов нет, записать нечего (все
            пары «без истории», переспрос)                          -> код 0
  partial — пропуск был, добор НЕПОЛНЫЙ (монеты с ошибкой, исчерпан
            бюджет, остались часы без единой строки, архив молчал
            дольше окна)                                            -> код 2
            + тревога (шаг воркфлоу `if: failure() || cancelled()`)
            с числом недобранных часов И пар (и монет поимённо)
  error   — источник недоступен, ответ не разобран, ноль строк,
            любое исключение                                       -> код 1
            + тревога (тот же шаг воркфлоу)

Итог прогона печатается строкой «ИТОГ: …» и, если задан $GITHUB_OUTPUT,
уходит воркфлоу парами key=value (outcome, exit_code, gap_hours,
unfilled_hours, pairs_missing, pairs_filled, pairs_unresolved,
pairs_no_history, coins_failed, coins_failed_names, timed_out, summary,
info_sent) — шаг тревоги берёт число недобранных часов и пар оттуда, а не
гадает; outputs пишутся ДО отправки ИНФО. ИНФО шлёт сам скрипт:
токен и чат — ТОЛЬКО из окружения (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID),
без них сообщение печатается в лог и это не ошибка; тело ответа Telegram не
печатается (только HTTP-код), ошибки — через redact; попыток 1 + TG_RETRIES
с растущей паузой, как у запросов к бирже. Недоставленное ИНФО прогон НЕ
красит (код — функция исхода), но и не молчит: аннотация `::warning::` на
странице прогона и `info_sent=false` в outputs (ревью 02.09). Тревоги
классов partial/error скрипт НЕ шлёт: их шлёт шаг `if: failure()`
воркфлоу, иначе пилот получал бы две тревоги на одну дырку.

Зависимости: stdlib + requests. Ключей для источника не требует; тексты
ошибок проходят через redact() из secrets_guard прежде чем попасть в лог.
Запуск: python funding_logger.py [--data-dir DIR] [--dry-run] [--slot-minute N]
                                 [--no-backfill] [--backfill-deadline SEC]
"""

import argparse
import csv
import glob
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

from provenance import git_sha
from secrets_guard import redact

# --- источник -------------------------------------------------------------

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HL_BODY = {"type": "metaAndAssetCtxs"}

# История фандинга по ОДНОЙ монете (добор пропусков задним числом):
#   POST /info {"type": "fundingHistory", "coin": "BTC",
#               "startTime": ms, "endTime": ms}
#   -> [{"coin": "BTC", "fundingRate": "...", "premium": "...", "time": ms}, ...]
# `time` — момент РАСЧЁТА ставки, конец часа: запись за 14:00 — это ставка
# часа 13:00–14:00, то есть слот 13:07 нашего архива. Поэтому слот добранной
# строки считается ТОЙ ЖЕ функцией slot_key, что и у живого среза, — двух
# правил соответствия «час истории -> слот архива» нет.
HL_FUNDING_HISTORY_TYPE = "fundingHistory"

HTTP_TIMEOUT_SEC = 60
HTTP_RETRIES = 3          # повторов после первой попытки; всего попыток = 1 + HTTP_RETRIES
HTTP_PAUSE_SEC = 2.0      # база паузы между попытками; пауза РАСТЁТ: base*1, base*2, ...

# Запросы ИСТОРИИ (добор) — свой, короткий профиль повторов: одна «ядовитая»
# монета с четырьмя таймаутами по 60 с съедала бы 252 с из бюджета добора и
# могла перелететь timeout-minutes job (ревью 03.09). Худший случай теперь
# 20 + 20 + 2 = 42 с на монету; повтор всё равно будет на следующем прогоне.
HISTORY_TIMEOUT_SEC = 20
HISTORY_RETRIES = 1

# 429 (Too Many Requests) = биржа просит сбавить темп (25.09.2026: DYM, MAVIA, PIXEL
# падали 429 на обеих попытках при паузе 2 с). На 429 ждём не меньше этого срока
# (или Retry-After, если биржа его прислала, но не дольше потолка).
RATE_LIMIT_WAIT_SEC = 10.0
RATE_LIMIT_WAIT_MAX_SEC = 30.0

# `br` в Accept-Encoding ломает requests без brotli (факт разведки 01.09.2026),
# поэтому список сжатий задаём явно, а не оставляем на усмотрение библиотеки.
HTTP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
    "User-Agent": "crypto-screener-funding-logger/1.0",
}

# --- формат архива --------------------------------------------------------

DEFAULT_DATA_DIR = "data/funding"

CSV_HEADER = [
    "ts_utc",
    "coin",
    "funding",
    "open_interest",
    "mark_px",
    "oracle_px",
    "mid_px",
    "premium",
    "day_ntl_vlm",
    "day_base_vlm",
    "max_leverage",
    "git_sha",
    # note: '' у живого среза; 'missing_oi' у строки, добранной из истории
    # фандинга — честная пометка, что OI и цены этого часа потеряны.
    "note",
]

# Заголовок до 02.09.2026 — без колонки note. Файл старой схемы дополняется
# колонкой ПРИ ПЕРВОЙ записи добранной строки (см. append_rows): значения
# старых строк не трогаются, дифф правки виден в git обычным образом.
LEGACY_CSV_HEADER = CSV_HEADER[:-1]

# Соответствие «колонка csv -> ключ в контексте инструмента». Порядок колонок
# задаёт CSV_HEADER, здесь только перевод имён — чтобы схема правилась в одном
# месте, а не в трёх.
CTX_FIELDS = [
    ("funding", "funding"),
    ("open_interest", "openInterest"),
    ("mark_px", "markPx"),
    ("oracle_px", "oraclePx"),
    ("mid_px", "midPx"),
    ("premium", "premium"),
    ("day_ntl_vlm", "dayNtlVlm"),
    ("day_base_vlm", "dayBaseVlm"),
]

# --- расписание -----------------------------------------------------------

# Минута часа, на которую поставлен cron воркфлоу (.github/workflows/funding.yml,
# `cron: "7 * * * *"`). Это ЕДИНСТВЕННОЕ место, где код знает расписание, и
# знать его он обязан: без планового времени прогон не может отличить «меня
# задержали на час» от «наступил следующий час». При смене cron менять здесь
# (или передавать --slot-minute), иначе привязка прогонов к слотам поедет —
# ровно на величину рассинхрона.
SLOT_MINUTE_UTC = 7

# Формат ключа слота: 'YYYY-MM-DDTHH' — 13 символов, лексикографический
# порядок таких ключей совпадает с хронологическим (нули не срезаны).
SLOT_KEY_FMT = "%Y-%m-%dT%H"

# Минимальная длина разбираемой отметки времени: 'YYYY-MM-DDTHH:MM:SS'.
TS_MIN_LEN = 19

# Сколько пропущенных слотов печатать поимённо, прежде чем свернуть в счётчик.
MAX_LISTED_GAPS = 6

# Сколько монет называть поимённо в тревоге/ИНФО, прежде чем свернуть в «+N».
MAX_LISTED_COINS = 5

# Предел длины строки summary: она уходит в outputs, в текст тревоги воркфлоу
# (curl --data-urlencode) и в ИНФО; Telegram режет сообщение на 4096 знаках,
# а шаг тревоги печатает только HTTP-код — длинная строка потеряла бы тревогу.
SUMMARY_LIMIT = 700

# --- добор пропусков ------------------------------------------------------

# Глубина окна сверки «ожидаемые слоты vs записанные». 48 часов — компромисс:
# дырка старше двух суток означает, что автоматика молчала двое суток и
# смотреть надо руками, а не догонять втихую.
BACKFILL_WINDOW_HOURS = 48

# Бюджет времени на добор за один прогон. Воркфлоу живёт в timeout-minutes: 10;
# 5 минут добора + минута среза + git-цикл укладываются с запасом. Не успели —
# следующий прогон продолжит с недобранных монет (старейший долг первым, дедуп
# по (слот, монета) не даст дублей). Замер 03.09.2026: 233 запроса = 91 с.
BACKFILL_DEADLINE_SEC = 300

# Общий бюджет прогона до ИНФО и git-цикла: живой срез в худшем случае 252 с
# (4 попытки × 60 с + паузы), после него добор получает не больше
# RUN_BUDGET_SEC - прошедшее. Худший хвост: 360 + 42 (перелёт одной монеты) +
# 96 (ИНФО с повторами) + git ≈ 9 мин < timeout-minutes: 10 (ревью 03.09).
RUN_BUDGET_SEC = 360

# ИНФО о залеченном пропуске уходит в Telegram только при пропуске от стольких
# часов. GitHub исполняет почасовой cron примерно раз в 4–5 ч (замер 03.09.2026:
# 8 живых слотов из 24), и ИНФО на каждый прогон — 4–6 одинаковых сообщений в
# сутки. 0 = слать всегда (поведение С-22 до 03.09).
INFO_GAP_HOURS = 6

# Пауза между помонетными запросами истории — щадим лимиты биржи.
BACKFILL_PAUSE_SEC = 0.5   # 25.09.2026: было 0.15 -> 429 от Hyperliquid на добор 234 монет

# Пометка добранной строки в колонке note: фандинг есть, OI за этот час нет.
BACKFILL_NOTE = "missing_oi"

# --- три исхода прогона (С-22, 02.09.2026) ---------------------------------

OUTCOME_OK = "ok"            # пропуска в окне нет
OUTCOME_HEALED = "healed"    # пропуск был и добран ПОЛНОСТЬЮ -> код 0 + ИНФО
OUTCOME_PARTIAL = "partial"  # пропуск был, добор неполный    -> код 2 + тревога воркфлоу
OUTCOME_ERROR = "error"      # источник/разбор/исключение     -> код 1 + тревога воркфлоу

# Код возврата — функция исхода и ТОЛЬКО его. Одна таблица на весь скрипт:
# «зелёный, но с пропуском» существует ровно в одном виде — healed, и он
# объявляется ИНФО-сообщением, а не молчанием.
EXIT_BY_OUTCOME = {
    OUTCOME_OK: 0,
    OUTCOME_HEALED: 0,
    OUTCOME_PARTIAL: 2,
    OUTCOME_ERROR: 1,
}

# Пары key=value, которые уходят воркфлоу через $GITHUB_OUTPUT: шаг тревоги
# читает gap_hours/unfilled_hours/pairs_*/summary и называет число часов и
# пар, а не гадает.
OUTPUT_KEYS = ("outcome", "exit_code", "gap_hours", "unfilled_hours",
               "pairs_missing", "pairs_filled", "pairs_unresolved", "pairs_no_history",
               "coins_failed", "coins_failed_names", "timed_out", "summary", "info_sent")

# Значение info_sent, когда ИНФО не отправлялось намеренно (рутинный пропуск
# короче INFO_GAP_HOURS): отличимо и от true/false, и от пустого (partial/error).
INFO_SKIPPED = "skipped"

# ИНФО в Telegram — из самого скрипта. Токен и чат только из окружения; тело
# ответа не печатается (оно может процитировать URL запроса, а в нём токен).
# Повторы — как у запросов к бирже: одна попытка на единственное сообщение,
# которое несёт весть о потере OI, была бы слабее, чем у любого среза.
TG_URL_TMPL = "https://api.telegram.org/bot%s/sendMessage"
TG_TIMEOUT_SEC = 30
TG_TEXT_LIMIT = 3800
TG_RETRIES = 2            # повторов после первой попытки
TG_PAUSE_SEC = 2.0        # база паузы; пауза растёт: base*1, base*2
INFO_PREFIX = "ИНФО funding-logger"


# ==========================================================================
# Мелкие чистые помощники
# ==========================================================================

def utc_now():
    """Текущее время UTC. Отдельной функцией — чтобы тест мог подставить своё."""
    return datetime.now(timezone.utc)


def iso_z(dt):
    """'YYYY-MM-DDTHH:MM:SSZ' — единый формат времени во всех артефактах."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def slot_start(dt, slot_minute=SLOT_MINUTE_UTC):
    """Плановый слот cron, который ОБСЛУЖИВАЕТ прогон, стартовавший в `dt`.

    Это последнее время вида `HH:slot_minute`, не превосходящее `dt`:
        15:02 -> 14:07   (Actions задержал слот 14:07 на 55 минут)
        15:07 -> 15:07   (пришли вовремя)
        15:41 -> 15:07   (тот же слот, повторный запуск — писать нечего)
        00:02 -> 23:07 предыдущих суток
    Сдвигом на slot_minute назад и обрезкой до часа, а не сравнением минут:
    так переход через полночь и через месяц считает календарь, а не мы.
    """
    shifted = dt - timedelta(minutes=slot_minute)
    return shifted.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=slot_minute)


def slot_key(dt, slot_minute=SLOT_MINUTE_UTC):
    """Ключ слота из момента старта: 'YYYY-MM-DDTHH' планового времени."""
    return slot_start(dt, slot_minute).strftime(SLOT_KEY_FMT)


def parse_ts(ts_utc):
    """'YYYY-MM-DDTHH:MM:SSZ' -> datetime в UTC. Любой хлам -> None.

    Строки архива читаются этой же функцией, поэтому «слот строки» считается
    ровно тем же правилом, что и «слот прогона» — двух источников истины у
    дедупа нет.
    """
    if ts_utc is None:
        return None
    s = ts_utc.strip() if isinstance(ts_utc, str) else str(ts_utc).strip()
    if len(s) < TS_MIN_LEN:
        return None
    try:
        dt = datetime.strptime(s[:TS_MIN_LEN], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc)


def slot_key_from_ts(ts_utc, slot_minute=SLOT_MINUTE_UTC):
    """Ключ слота из отметки времени уже записанной строки.

    Идемпотентность строится именно на нём: прогон в 21:07:03 и повторный
    прогон в 21:41:55 — это ОДИН слот, второй раз писать нельзя; а прогон в
    22:02, обслуживающий слот 21:07, — тот же слот 21, и он не имеет права
    занять собой час 22.
    Кривую/короткую отметку возвращаем как пустой ключ: пусть лучше строка
    из битого файла не совпадёт ни с чем (и будет дописана заново), чем
    совпадёт со всем подряд и заглушит запись целого часа.
    """
    dt = parse_ts(ts_utc)
    if dt is None:
        return ""
    return slot_key(dt, slot_minute)


def missing_slots(prev_key, cur_key):
    """Дырка в архиве между двумя ключами слотов.

    -> (сколько слотов пропущено, первые MAX_LISTED_GAPS ключей поимённо).
    Соседние слоты и любой мусор на входе дают (0, []): тревога на пустом
    месте обесценивает тревогу настоящую.
    """
    prev = _key_to_dt(prev_key)
    cur = _key_to_dt(cur_key)
    if prev is None or cur is None:
        return 0, []
    gap = int((cur - prev).total_seconds() // 3600) - 1
    if gap <= 0:
        return 0, []
    listed = [(prev + timedelta(hours=i)).strftime(SLOT_KEY_FMT)
              for i in range(1, min(gap, MAX_LISTED_GAPS) + 1)]
    return gap, listed


def _key_to_dt(key):
    """'YYYY-MM-DDTHH' -> datetime UTC (минуты слота роли не играют)."""
    if not key:
        return None
    try:
        return datetime.strptime(key, SLOT_KEY_FMT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def clean_cell(value):
    """Строка, пригодная для одной ячейки csv.

    Внешние тексты — ДАННЫЕ, а не команды и не разметка: имя инструмента
    приходит от биржи, и хотя сегодня это 'BTC', завтра там может оказаться
    что угодно. Переводы строк и табуляции выпиливаем (иначе одна строка
    архива станет двумя и сломает всех читателей), запятые и кавычки не
    трогаем — их корректно экранирует модуль csv.

    Плюс гасим ведущие '=', '+', '-', '@' у НЕЧИСЛОВЫХ полей: Excel и
    LibreOffice исполняют такую ячейку как формулу, а имя инструмента идёт
    к нам от биржи и нашей проверки не проходило. Числа НЕ трогаем — иначе
    отрицательный фандинг перестал бы быть числом для любого читателя.
    """
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    for bad in ("\r", "\n", "\t"):
        s = s.replace(bad, " ")
    s = s.strip()
    if s[:1] in ("=", "+", "-", "@") and not _looks_numeric(s):
        return "'" + s
    return s


def _looks_numeric(s):
    """Похоже ли на число.

    Свой набор символов проверяется ДО float(), потому что float() глотает
    'nan', 'inf' и '1_0' — а таблица числом их не считает, и именно они
    пролезли бы мимо защиты, прикинувшись числами.
    """
    if not s:
        return False
    if any(ch not in "0123456789+-.eE" for ch in s):
        return False
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def csv_path_for(now=None, base_dir=DEFAULT_DATA_DIR, repo_dir=None):
    """Путь дневного файла: <base_dir>/YYYY-MM/funding_YYYYMMDD.csv.

    ВАЖНО: `run_once` передаёт сюда время СЛОТА, а не время старта. День
    файла обязан совпадать с днём слота, иначе слот 23:07, отработавший в
    00:02 следующих суток, лёг бы в файл следующего дня — и дедуп, который
    читает ровно один дневной файл, не увидел бы там своей же первой записи
    за тот же слот. Строки одного слота обязаны лежать в одном файле.
    """
    now = now or utc_now()
    if repo_dir is None:
        repo_dir = os.path.dirname(os.path.abspath(__file__))
    root = base_dir if os.path.isabs(base_dir) else os.path.join(repo_dir, base_dir)
    month_dir = os.path.join(root, now.strftime("%Y-%m"))
    return os.path.join(month_dir, "funding_%s.csv" % now.strftime("%Y%m%d"))


# ==========================================================================
# Разбор ответа биржи (чистая функция, сети не касается)
# ==========================================================================

def parse_payload(payload, ts_utc, sha=None):
    """Ответ `metaAndAssetCtxs` -> строки csv.

    -> {'rows': [[...], ...], 'n_universe': int, 'n_ctx': int,
        'n_delisted': int, 'warnings': [str, ...],
        'coins_raw': {имя в архиве: имя у биржи}}

    Правила:
      * universe[i] и ctxs[i] — ОДИН инструмент (общего ключа нет, только индекс);
      * при разной длине списков режем по минимуму и кладём предупреждение,
        НЕ падаем: потерять хвост списка лучше, чем потерять весь час;
      * запись без имени инструмента пропускается (без имени строка бесполезна);
      * отсутствующее числовое поле -> пустая ячейка, а не ноль.

    Структурно неверный вход (не список из двух элементов, нет universe и т.п.)
    -> ValueError. Это НЕ то же самое, что рассогласование длин: там мы спасаем
    что можем, здесь спасать нечего, и молчаливый пустой архив был бы враньём.
    """
    if sha is None:
        sha = git_sha()

    if not isinstance(payload, (list, tuple)):
        raise ValueError("ответ Hyperliquid не список: %s" % type(payload).__name__)
    if len(payload) < 2:
        raise ValueError("ответ Hyperliquid короче двух элементов: len=%d" % len(payload))

    meta, ctxs = payload[0], payload[1]
    if not isinstance(meta, dict):
        raise ValueError("первый элемент ответа не объект: %s" % type(meta).__name__)
    universe = meta.get("universe")
    if not isinstance(universe, list):
        raise ValueError("нет списка universe в первом элементе ответа")
    if not isinstance(ctxs, list):
        raise ValueError("второй элемент ответа не список: %s" % type(ctxs).__name__)

    warnings = []
    n_u, n_c = len(universe), len(ctxs)
    n = min(n_u, n_c)
    if n_u != n_c:
        # Именно этот случай нельзя ни проглотить, ни уронить: пишем минимум
        # и громко сообщаем, чтобы расхождение было видно в логе Actions.
        warnings.append(
            "РАССОГЛАСОВАНИЕ ДЛИН: universe=%d, contexts=%d -> пишем первые %d "
            "(выравнивание по индексу, хвост отброшен)" % (n_u, n_c, n)
        )
    if n == 0:
        warnings.append("пустой ответ: ни одного инструмента")

    rows = []
    n_delisted = 0
    coins_raw = {}
    for i in range(n):
        u = universe[i]
        c = ctxs[i]
        if not isinstance(u, dict):
            warnings.append("universe[%d] не объект (%s) — пропуск" % (i, type(u).__name__))
            continue
        if not isinstance(c, dict):
            warnings.append("contexts[%d] не объект (%s) — пропуск" % (i, type(c).__name__))
            continue

        coin = clean_cell(u.get("name"))
        if not coin:
            warnings.append("universe[%d] без имени — пропуск" % i)
            continue
        # Имя для запросов к бирже — СЫРОЕ: clean_cell гасит ведущие '=+-@'
        # апострофом, а биржа такого имени не знает (ревью 03.09).
        coins_raw.setdefault(coin, str(u.get("name")))
        if u.get("isDelisted"):
            n_delisted += 1

        row = [ts_utc, coin]
        for _, api_key in CTX_FIELDS:
            row.append(clean_cell(c.get(api_key)))
        row.append(clean_cell(u.get("maxLeverage")))
        row.append(clean_cell(sha))
        row.append("")  # note: живой срез, пометки нет
        rows.append(row)

    return {
        "rows": rows,
        "n_universe": n_u,
        "n_ctx": n_c,
        "n_delisted": n_delisted,
        "warnings": warnings,
        "coins_raw": coins_raw,
    }


# ==========================================================================
# Файл архива: чтение уже записанных ключей и дозапись
# ==========================================================================

def read_existing_keys(path, slot_minute=SLOT_MINUTE_UTC):
    """Множество ключей (слот, монета), уже лежащих в дневном файле.

    Колонки ищем ПО ЗАГОЛОВКУ, а не по номеру: если завтра схема получит
    новую колонку, дедуп не должен молча начать сравнивать не те поля.
    Нет файла — пустое множество. Битый файл не должен ронять прогон:
    хуже дубля в архиве только потерянный час, поэтому при ошибке чтения
    печатаем предупреждение и считаем, что записанного нет.
    """
    keys = set()
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return keys
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if header is None:
                return keys
            try:
                i_ts = header.index("ts_utc")
                i_coin = header.index("coin")
            except ValueError:
                # Заголовка нет (файл писан не нами) — работаем по канону схемы.
                i_ts, i_coin = 0, 1
                print("ПРЕДУПРЕЖДЕНИЕ: в %s нет ожидаемого заголовка, "
                      "дедуп идёт по позициям колонок 0 и 1" % path)
                row = header
                if len(row) > max(i_ts, i_coin):
                    keys.add((slot_key_from_ts(row[i_ts], slot_minute), row[i_coin]))
            for row in reader:
                if len(row) <= max(i_ts, i_coin):
                    continue
                keys.add((slot_key_from_ts(row[i_ts], slot_minute), row[i_coin]))
    except OSError as exc:
        print("ПРЕДУПРЕЖДЕНИЕ: не прочитать %s (%s) — считаем файл пустым"
              % (path, redact(exc)))
        return set()
    return keys


def _file_header(path):
    """Первая строка csv-файла как список ячеек; None, если не прочитать."""
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return next(csv.reader(fh), None)
    except OSError as exc:
        print("ПРЕДУПРЕЖДЕНИЕ: не прочитать заголовок %s (%s)" % (path, redact(exc)))
        return None


def _upgrade_legacy_file(path):
    """Дописывает колонку note файлу старой 12-колоночной схемы.

    Значения существующих строк НЕ меняются — к заголовку добавляется имя
    колонки, к каждой строке пустая ячейка. Файл переписывается через
    временный с os.replace, чтобы упавший на середине прогон не оставил
    архив половинным. Правка видна в git обычным диффом — задним числом
    ничего не подменяется.
    """
    with open(path, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(CSV_HEADER)
        for row in rows[1:]:
            writer.writerow(row + [""] * max(0, len(CSV_HEADER) - len(row)))
    os.replace(tmp, path)


def append_rows(path, rows):
    """Дозапись строк в дневной csv. Заголовок — только при создании файла.

    -> число фактически записанных строк.
    Пустой список строк файла НЕ создаёт: пустой файл с одним заголовком потом
    неотличим от «в этот день логгер не бегал».
    Файл старой схемы (без note): живые строки дозаписываются в его же 12
    колонок (пустую note незачем навязывать истории), но первая же строка с
    НЕПУСТОЙ note дополняет файл колонкой — пометка missing_oi терять смысл
    не имеет права.
    """
    if not rows:
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    need_header = (not os.path.exists(path)) or os.path.getsize(path) == 0
    out_rows = rows
    if not need_header:
        header = _file_header(path)
        if header == LEGACY_CSV_HEADER:
            i_note = CSV_HEADER.index("note")
            if any(len(r) > i_note and r[i_note] for r in rows):
                _upgrade_legacy_file(path)
            else:
                out_rows = [r[:len(LEGACY_CSV_HEADER)] for r in rows]
    # newline='' — требование модуля csv (иначе на Windows появятся пустые
    # строки между записями); lineterminator фиксируем, чтобы файл был
    # побайтно одинаков на Windows и на ubuntu-latest в Actions.
    with open(path, "a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        if need_header:
            writer.writerow(CSV_HEADER)
        writer.writerows(out_rows)
    return len(out_rows)


def filter_new_rows(rows, existing_keys, slot_minute=SLOT_MINUTE_UTC):
    """Отсев строк, чей ключ (слот, монета) уже в файле.

    -> (новые_строки, число_отсеянных). Внутри одной пачки дубли тоже
    отсекаются: биржа теоретически может отдать один инструмент дважды.
    Слот считается ИЗ ts строки, тем же правилом, что и при чтении файла:
    правило одно на запись и на чтение, иначе дедуп сравнивал бы ключи,
    посчитанные по-разному, и «совпадений» не было бы никогда.
    """
    i_ts = CSV_HEADER.index("ts_utc")
    i_coin = CSV_HEADER.index("coin")
    seen = set(existing_keys)
    fresh = []
    skipped = 0
    for row in rows:
        key = (slot_key_from_ts(row[i_ts], slot_minute), row[i_coin])
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        fresh.append(row)
    return fresh, skipped


# ==========================================================================
# Прогон
# ==========================================================================

def _post_info(body, session=None, url=HL_INFO_URL, timeout=HTTP_TIMEOUT_SEC,
               retries=HTTP_RETRIES, pause=HTTP_PAUSE_SEC, label="запрос"):
    """Один POST к /info с ретраями. -> разобранный JSON.

    Ретраи нужны против сетевой ряби, а не против ошибки в протоколе: если
    сервер честно ответил 4xx, повтор ничего не изменит, но час стоит того,
    чтобы попробовать ещё раз. Пауза между попытками РАСТЁТ (base*1, base*2,
    base*3): мгновенный повтор бьёт в ту же перегрузку, что уронила первую
    попытку. Последнее исключение пробрасывается наверх — молчаливый пустой
    прогон запрещён, воркфлоу обязан покраснеть. Текст исключения печатается
    только через redact: сегодня в запросе секретов нет, но правило «ошибки
    в лог — чистыми» едино для всего проекта, а не для файлов, где уже горело.
    """
    sess = session or requests.Session()
    last = None
    for attempt in range(1, retries + 2):
        try:
            resp = sess.post(url, json=body, headers=HTTP_HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:      # сеть, таймаут, кривой json, 5xx
            last = exc
            print("%s: попытка %d/%d не удалась: %s"
                  % (label, attempt, retries + 1, redact(exc)))
            if attempt <= retries:
                time.sleep(_retry_wait(exc, pause * attempt))
    raise last


def _retry_wait(exc, base_wait):
    """Пауза перед повтором: обычная растущая, но на 429 - не меньше
    RATE_LIMIT_WAIT_SEC (или Retry-After биржи, с потолком RATE_LIMIT_WAIT_MAX_SEC)."""
    resp = getattr(exc, "response", None)
    if resp is None or getattr(resp, "status_code", None) != 429:
        return base_wait
    wait = RATE_LIMIT_WAIT_SEC
    try:
        wait = max(wait, float(resp.headers.get("Retry-After", 0) or 0))
    except (TypeError, ValueError, AttributeError):
        pass
    return max(base_wait, min(wait, RATE_LIMIT_WAIT_MAX_SEC))


def fetch_payload(session=None, url=HL_INFO_URL, timeout=HTTP_TIMEOUT_SEC,
                  retries=HTTP_RETRIES, pause=HTTP_PAUSE_SEC):
    """Живой срез metaAndAssetCtxs (сигнатура сохранена для тестов и стабов)."""
    return _post_info(HL_BODY, session=session, url=url, timeout=timeout,
                      retries=retries, pause=pause, label="metaAndAssetCtxs")


def fetch_funding_history(coin, start_ms, end_ms, session=None, url=HL_INFO_URL,
                          timeout=HISTORY_TIMEOUT_SEC, retries=HISTORY_RETRIES,
                          pause=HTTP_PAUSE_SEC):
    """История ставок фандинга одной монеты за [start_ms, end_ms].

    -> список записей биржи {coin, fundingRate, premium, time}; time в мс.
    Профиль повторов короткий (HISTORY_*): монета одна из сотен, повтор будет
    следующим прогоном, а бюджет прогона общий.
    """
    body = {"type": HL_FUNDING_HISTORY_TYPE, "coin": coin,
            "startTime": int(start_ms), "endTime": int(end_ms)}
    return _post_info(body, session=session, url=url, timeout=timeout,
                      retries=retries, pause=pause,
                      label="fundingHistory %s" % coin)


def window_slot_keys(slot_dt, window_hours=BACKFILL_WINDOW_HOURS):
    """Ключи всех слотов окна сверки: window_hours подряд ДО текущего.

    Текущий слот в окно не входит — его пишет сам прогон.
    """
    return [(slot_dt - timedelta(hours=i)).strftime(SLOT_KEY_FMT)
            for i in range(window_hours, 0, -1)]


def window_paths(slot_dt, base_dir=DEFAULT_DATA_DIR, repo_dir=None,
                 window_hours=BACKFILL_WINDOW_HOURS):
    """Дневные файлы, накрывающие окно, без повторов — по календарю, не по нашей
    арифметике: границы суток и месяца считает datetime."""
    paths = []
    seen = set()
    for i in range(window_hours + 1):
        path = csv_path_for(now=slot_dt - timedelta(hours=i), base_dir=base_dir,
                            repo_dir=repo_dir)
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def known_pairs_for_window(slot_dt, base_dir=DEFAULT_DATA_DIR, repo_dir=None,
                           slot_minute=SLOT_MINUTE_UTC,
                           window_hours=BACKFILL_WINDOW_HOURS):
    """Записанные пары окна ПО МОНЕТАМ: {coin: set(slot_key)} (С-24).

    Читаются все дневные файлы, накрывающие окно, каждый один раз. Ключи
    старше окна (файл дневной, окно нет) в набор входят намеренно: по
    min(keys[coin]) считается ПОЛ монеты — слот её первой записи; раньше
    пола дырок у монеты нет (монета, листнутая посреди окна, не получает
    выдуманных пропусков).
    """
    known = {}
    for path in window_paths(slot_dt, base_dir=base_dir, repo_dir=repo_dir,
                             window_hours=window_hours):
        for key, coin in read_existing_keys(path, slot_minute=slot_minute):
            if key:
                known.setdefault(coin, set()).add(key)
    return known


def known_slot_keys_for_window(slot_dt, base_dir=DEFAULT_DATA_DIR, repo_dir=None,
                               slot_minute=SLOT_MINUTE_UTC,
                               window_hours=BACKFILL_WINDOW_HOURS):
    """Слот-ключи, у которых в файлах окна есть хоть одна строка (любой монеты).

    Объединение known_pairs_for_window по монетам; нужно для счёта «часов без
    единой строки» (С-22) и для совместимости.
    """
    known = set()
    for keys in known_pairs_for_window(slot_dt, base_dir=base_dir, repo_dir=repo_dir,
                                       slot_minute=slot_minute,
                                       window_hours=window_hours).values():
        known |= keys
    return known


def missing_window_slots(slot_dt, known, window_hours=BACKFILL_WINDOW_HOURS):
    """Пропущенные слоты окна для ОДНОГО набора ключей: ожидаемые минус записанные.

    Пустой набор дырок не выдумывает: ждать записей от часов, когда записей
    не существовало, — тревога на пустом месте. По той же причине слоты
    РАНЬШЕ самой первой записи набора пропуском не считаются (пол).
    Под С-24 вызывается на ключах одной монеты — пол у каждой монеты свой.
    """
    if not known:
        return []
    floor_key = min(known)
    return [k for k in window_slot_keys(slot_dt, window_hours)
            if k > floor_key and k not in known]


def last_archive_slot_before_window(slot_dt, base_dir=DEFAULT_DATA_DIR, repo_dir=None,
                                    slot_minute=SLOT_MINUTE_UTC,
                                    window_hours=BACKFILL_WINDOW_HOURS):
    """Последний ключ слота в файлах архива СТАРШЕ окна; '' — таких файлов нет.

    Отличает «свежий архив» (файлов нет вовсе — дырок не выдумываем) от
    «архив молчал дольше окна» (файлы есть, а в окне ни строки — это
    потеря, о которой нельзя молчать, и добор всего окна).
    """
    if repo_dir is None:
        repo_dir = os.path.dirname(os.path.abspath(__file__))
    root = base_dir if os.path.isabs(base_dir) else os.path.join(repo_dir, base_dir)
    inside = set(os.path.abspath(p) for p in window_paths(
        slot_dt, base_dir=base_dir, repo_dir=repo_dir, window_hours=window_hours))
    files = sorted(glob.glob(os.path.join(root, "*", "funding_*.csv")))
    for path in reversed(files):
        if os.path.abspath(path) in inside:
            continue
        keys = {k for k, _ in read_existing_keys(path, slot_minute=slot_minute) if k}
        if keys:
            return max(keys)
    return ""


def missing_window_pairs(slot_dt, known_pairs, coins, window_hours=BACKFILL_WINDOW_HOURS,
                         silent=False):
    """Пропуск по парам (слот, монета) для монет ТЕКУЩЕЙ вселенной (С-24).

    -> (plan, new_coins): plan = {coin: [slot_key, ...]} в порядке вселенной,
       только монеты с пропусками; new_coins = монеты вселенной без единой
       записи в файлах окна при непустом архиве — им дырки не выдумываются,
       но история за окно запрашивается один раз (пол = первая запись биржи).

    Пол монеты = слот её первой записи (missing_window_slots на её ключах).
    silent=True — в файлах окна нет ни строки, а архив старше окна есть:
    полом для всех монет считается начало окна (добор всех 48 ч).
    """
    plan = {}
    new_coins = []
    archive_has_keys = any(known_pairs.values()) if known_pairs else False
    for coin in coins:
        keys = known_pairs.get(coin) if known_pairs else None
        if keys:
            miss = missing_window_slots(slot_dt, keys, window_hours)
        elif silent:
            miss = list(window_slot_keys(slot_dt, window_hours))
        else:
            if archive_has_keys:
                new_coins.append(coin)
            miss = []
        if miss:
            plan[coin] = miss
    return plan, new_coins


def gap_slots(plan):
    """Отсортированное объединение слотов плана — «часы пропуска» для лога."""
    out = set()
    for slots in (plan or {}).values():
        out.update(slots)
    return sorted(out)


def gap_pairs(plan):
    return sum(len(v) for v in (plan or {}).values())


def backfill_queue(plan, coins, new_coins=()):
    """Порядок добора: старейший долг первым, при равенстве — порядок вселенной;
    новые монеты (пробный запрос за окно) — в конце.

    Ревью 03.09: порядок «как во вселенной» голодал — новые дырки между
    прогонами есть у всех монет, и после обрыва бюджета голова очереди
    снова та же, а хвост (мелкие монеты, которыми торгует скринер) копил
    долг, пока слоты не выходили из окна.
    """
    index = {coin: i for i, coin in enumerate(coins)}
    debts = sorted(plan, key=lambda c: (min(plan[c]), index.get(c, len(index))))
    return [(c, list(plan[c]), False) for c in debts] + \
           [(c, [], True) for c in new_coins if c not in plan]


def _entries_to_rows(hist, coin, sha, slot_minute, want_keys):
    """Записи fundingHistory -> строки архива для слотов из want_keys.

    -> {slot_key: row}. Запись за слот H лежит в истории под временем расчёта
    H+1:00 — соответствие считает та же slot_key, что и у живого среза.
    """
    rows = {}
    for entry in hist:
        if not isinstance(entry, dict):
            continue
        try:
            dt = datetime.fromtimestamp(float(entry.get("time")) / 1000.0, timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        key = slot_key(dt, slot_minute)
        if key not in want_keys or key in rows:
            continue
        vals = {"ts_utc": iso_z(dt), "coin": clean_cell(coin),
                "funding": clean_cell(entry.get("fundingRate")),
                "premium": clean_cell(entry.get("premium")),
                "git_sha": clean_cell(sha), "note": BACKFILL_NOTE}
        rows[key] = [vals.get(col, "") for col in CSV_HEADER]
    return rows


def backfill_funding(plan, coins=None, new_coins=(), raw_names=None, known_slots=None,
                     base_dir=DEFAULT_DATA_DIR, repo_dir=None, sha=None, session=None,
                     slot_minute=SLOT_MINUTE_UTC, deadline_sec=BACKFILL_DEADLINE_SEC,
                     dry_run=False, pause_between=BACKFILL_PAUSE_SEC, slot_dt=None,
                     window_hours=BACKFILL_WINDOW_HOURS):
    """Добор пропущенных ПАР (слот, монета) фандинга из истории биржи (С-24).

    plan — {coin: [slot_key, ...]} от missing_window_pairs; coins — порядок
    вселенной (для очереди); new_coins — монеты без записей (пробный запрос
    за окно); raw_names — {имя в архиве: имя у биржи} (clean_cell могла
    изменить имя, запрашивать надо сырое); known_slots — слоты, где до
    прогона была хоть одна строка (для счёта «часов без единой строки»).

    По каждой монете — ОДИН запрос на её долг [min слот, max слот + 2 ч].
    Строки пишутся СРАЗУ по монете (ревью 03.09: убитый по таймауту job
    раньше терял весь добор, потому что строки копились до конца цикла);
    дедуп — по кешу ключей файла, прочитанному один раз и пополняемому.
    Пара после добора в одном из состояний:
      filled     — строка фандинга записана (note=missing_oi);
      no_history — история монеты пришла (список, возможно пустой), а записи
                   за этот час в ней нет: не дефект, метка НЕ пишется
                   (метка стала бы необратимым замком на паре — ревью 03.09),
                   пара переспрашивается каждым прогоном, пока в окне;
      unresolved — монета отказала или не запрошена (бюджет): дефект С-22,
                   продолжит следующий прогон ровно с этих монет.

    -> {'requested_pairs', 'coins_total', 'coins_ok', 'coins_failed',
        'coins_failed_names', 'coins_unattempted', 'coins_new', 'rows_written',
        'would_write', 'pairs_filled', 'pairs_no_history', 'pairs_unresolved',
        'slots_filled', 'unfilled_slots', 'empty_slots', 'no_history_slots',
        'oldest_unresolved_slot', 'timed_out', 'stopped_at'}
    """
    plan = dict(plan or {})
    coins = list(coins) if coins is not None else list(plan)
    new_coins = [c for c in (new_coins or ()) if c not in plan]
    raw_names = raw_names or {}
    known_slots = set(known_slots or ())
    out = {"requested_pairs": gap_pairs(plan), "coins_total": len(plan) + len(new_coins),
           "coins_ok": 0, "coins_failed": 0, "coins_failed_names": [],
           "coins_unattempted": 0, "coins_new": len(new_coins),
           "rows_written": 0, "would_write": 0,
           "pairs_filled": 0, "pairs_no_history": 0, "pairs_unresolved": 0,
           "slots_filled": 0, "unfilled_slots": [], "empty_slots": [],
           "no_history_slots": [], "oldest_unresolved_slot": "",
           "timed_out": False, "stopped_at": None}
    if not plan and not new_coins:
        return out
    if sha is None:
        sha = git_sha()
    if slot_dt is None:
        slot_dt = utc_now()
    window_keys = set(window_slot_keys(slot_start(slot_dt, slot_minute), window_hours))
    queue = backfill_queue(plan, coins, new_coins)
    sess = session or requests.Session()
    cache = {}                      # путь -> множество ключей (слот, монета)
    filled_slots = set()
    unresolved_slots = set()
    no_history_slots = set()
    t0 = time.monotonic()
    for idx, (coin, slots, is_new) in enumerate(queue):
        if time.monotonic() - t0 > deadline_sec:
            out["timed_out"] = True
            out["stopped_at"] = idx + 1          # номер монеты (с 1), перед которой встали
            rest = queue[idx:]
            out["coins_unattempted"] = len(rest)
            for _c, _slots, _new in rest:
                out["pairs_unresolved"] += len(_slots)
                unresolved_slots.update(_slots)
            print("ДОБОР: бюджет %d с исчерпан на монете %d/%d — остальное "
                  "доберёт следующий прогон, начиная с самого старого долга"
                  % (deadline_sec, idx + 1, len(queue)))
            break
        if is_new:
            want = window_keys
            dts = sorted(_key_to_dt(k) for k in window_keys)
        else:
            want = set(slots)
            dts = [d for d in (_key_to_dt(k) for k in slots) if d is not None]
        if not dts:
            continue
        # Запись за слот H лежит в истории под временем расчёта H+1:00 —
        # правый край окна запроса сдвигаем на два часа, чтобы накрыть её с запасом.
        start_ms = int(min(dts).timestamp() * 1000)
        end_ms = int((max(dts) + timedelta(hours=2)).timestamp() * 1000)
        try:
            hist = fetch_funding_history(raw_names.get(coin, coin), start_ms, end_ms,
                                         session=sess)
            if not isinstance(hist, list):
                raise ValueError("история пришла не списком (%s)" % type(hist).__name__)
        except Exception as exc:
            out["coins_failed"] += 1
            out["coins_failed_names"].append(coin)
            out["pairs_unresolved"] += len(slots)
            unresolved_slots.update(slots)
            print("ДОБОР: история %s не отдалась: %s" % (coin, redact(exc)))
            if pause_between and idx + 1 < len(queue):
                time.sleep(pause_between)
            continue
        out["coins_ok"] += 1
        rows_by_key = _entries_to_rows(hist, coin, sha, slot_minute, want)
        by_path = {}
        for key, row in rows_by_key.items():
            # Файл — по дню СЛОТА, как у живого среза: слот 23:07 с временем
            # расчёта 00:00 следующих суток обязан лечь в файл своих суток.
            path = csv_path_for(now=_key_to_dt(key), base_dir=base_dir, repo_dir=repo_dir)
            by_path.setdefault(path, []).append(row)
        written_keys = set()
        for path in sorted(by_path):
            if path not in cache:
                cache[path] = read_existing_keys(path, slot_minute=slot_minute)
            fresh, _ = filter_new_rows(by_path[path], cache[path], slot_minute=slot_minute)
            if not fresh:
                continue
            out["would_write"] += len(fresh)
            if not dry_run:
                out["rows_written"] += append_rows(path, fresh)
            for r in fresh:
                k = slot_key_from_ts(r[0], slot_minute)
                cache[path].add((k, r[1]))
                written_keys.add(k)
        filled_slots |= written_keys
        if is_new:
            out["pairs_filled"] += len(written_keys)
        else:
            out["pairs_filled"] += len(written_keys)
            missing_here = want - set(rows_by_key)
            out["pairs_no_history"] += len(missing_here)
            no_history_slots |= missing_here
        if pause_between and idx + 1 < len(queue):
            time.sleep(pause_between)
    out["slots_filled"] = len(filled_slots)
    # Часы без единой строки после прогона (С-22): слот пропуска, где ни одна
    # монета не имела строки до прогона и ни одной не добрано сейчас.
    empty = [k for k in gap_slots(plan) if k not in known_slots and k not in filled_slots]
    out["empty_slots"] = empty
    out["unfilled_slots"] = sorted(set(empty) | unresolved_slots)
    out["no_history_slots"] = sorted(no_history_slots)
    if unresolved_slots:
        out["oldest_unresolved_slot"] = min(unresolved_slots)
    return out


# ==========================================================================
# Итог прогона: исход, outputs воркфлоу, ИНФО в Telegram (С-22, С-24)
# ==========================================================================

def _names_short(names, limit=MAX_LISTED_COINS):
    names = list(names or [])
    if not names:
        return ""
    head = ", ".join(names[:limit])
    return head + (" +%d" % (len(names) - limit) if len(names) > limit else "")


def classify_outcome(plan, bf=None, no_backfill=False, dry_run=False, silent_since=None,
                     slot_key_now=None):
    """Исход прогона из плана пропуска (пары) и результата добора. Чистая функция.

    -> {'outcome', 'exit_code', 'gap_hours', 'unfilled_hours', 'pairs_missing',
        'pairs_filled', 'pairs_no_history', 'pairs_unresolved', 'slots_filled',
        'coins_failed', 'coins_total', 'coins_failed_names', 'timed_out', 'rows',
        'reasons', 'summary', 'slots', 'oldest_unresolved_slot',
        'oldest_unresolved_age_h', 'silent_since'}

    Правила (С-22 + С-24):
      ok      — пропуска нет; ЛИБО пропуск есть, дефектов нет и записать было
                нечего (все пары — «без истории у биржи», переспрос);
      healed  — пропуск был, строки добраны, дефектов нет; пары «без истории»
                не дефект (переспрашиваются, пока в окне);
      partial — дефект: монета с ошибкой, бюджет исчерпан (продолжит следующий
                прогон), час без единой строки после добора, архив молчал
                дольше окна.
    Отказавшая монета красит прогон каждый раз, пока её пары в окне, — это
    семантика С-22, эскалация по возрасту пары вынесена в очередь (С-27).
    """
    plan = dict(plan or {})
    slots = gap_slots(plan)
    n = len(slots)
    out = {"outcome": OUTCOME_OK, "gap_hours": n, "unfilled_hours": 0,
           "pairs_missing": gap_pairs(plan), "pairs_filled": 0, "pairs_no_history": 0,
           "pairs_unresolved": 0, "slots_filled": 0, "coins_failed": 0, "coins_total": 0,
           "coins_failed_names": [], "timed_out": False, "rows": 0, "reasons": [],
           "summary": "", "slots": slots, "oldest_unresolved_slot": "",
           "oldest_unresolved_age_h": None, "silent_since": silent_since or ""}
    if n == 0:
        out["exit_code"] = EXIT_BY_OUTCOME[OUTCOME_OK]
        return out
    if no_backfill or bf is None:
        out["outcome"] = OUTCOME_PARTIAL
        out["unfilled_hours"] = n
        out["pairs_unresolved"] = out["pairs_missing"]
        out["reasons"] = ["добор выключен: не добрано часов %d из %d (пар %d)"
                          % (n, n, out["pairs_missing"])]
    else:
        out["pairs_filled"] = int(bf.get("pairs_filled") or 0)
        out["pairs_no_history"] = int(bf.get("pairs_no_history") or 0)
        out["pairs_unresolved"] = int(bf.get("pairs_unresolved") or 0)
        out["slots_filled"] = int(bf.get("slots_filled") or 0)
        out["coins_failed"] = int(bf.get("coins_failed") or 0)
        out["coins_total"] = int(bf.get("coins_total") or 0)
        out["coins_failed_names"] = list(bf.get("coins_failed_names") or [])
        out["timed_out"] = bool(bf.get("timed_out"))
        out["unfilled_hours"] = len(bf.get("unfilled_slots") or [])
        out["oldest_unresolved_slot"] = bf.get("oldest_unresolved_slot") or ""
        rows = bf.get("would_write") if dry_run else bf.get("rows_written")
        out["rows"] = int(rows or 0)
        if out["oldest_unresolved_slot"] and slot_key_now:
            d0, d1 = _key_to_dt(out["oldest_unresolved_slot"]), _key_to_dt(slot_key_now)
            if d0 is not None and d1 is not None:
                out["oldest_unresolved_age_h"] = int((d1 - d0).total_seconds() // 3600)
        reasons = []
        if out["pairs_unresolved"]:
            age = out["oldest_unresolved_age_h"]
            tail = ""
            if age is not None:
                tail = "; старейший слот %s (возраст %d ч, из окна выйдет через %d ч)" % (
                    out["oldest_unresolved_slot"], age, max(0, BACKFILL_WINDOW_HOURS - age))
            reasons.append("не добрано пар %d из %d (монет %d из %d: %s; часов %d из %d)%s"
                           % (out["pairs_unresolved"], out["pairs_missing"],
                              out["coins_failed"] + int(bf.get("coins_unattempted") or 0),
                              out["coins_total"],
                              _names_short(out["coins_failed_names"]) or "не запрошены",
                              out["unfilled_hours"], n, tail))
        empty = list(bf.get("empty_slots") or [])
        if empty:
            reasons.append("часов без единой строки после добора %d из %d (%s)"
                           % (len(empty), n, _slots_span(empty)))
        if out["coins_failed"]:
            reasons.append("монет с ошибкой %d из %d (%s)"
                           % (out["coins_failed"], out["coins_total"],
                              _names_short(out["coins_failed_names"])))
        if out["timed_out"]:
            reasons.append("бюджет добора исчерпан на монете %s из %d — продолжит "
                           "следующий прогон" % (bf.get("stopped_at") if bf.get("stopped_at")
                                                 is not None else "?", out["coins_total"]))
        if silent_since:
            reasons.append("архив молчал дольше окна %d ч (последняя запись до окна — %s): "
                           "часы до окна не восстановлены" % (BACKFILL_WINDOW_HOURS, silent_since))
        out["reasons"] = reasons
        if reasons:
            out["outcome"] = OUTCOME_PARTIAL
        elif out["rows"] > 0:
            out["outcome"] = OUTCOME_HEALED
        else:
            out["outcome"] = OUTCOME_OK
    out["exit_code"] = EXIT_BY_OUTCOME[out["outcome"]]
    out["summary"] = _outcome_summary(out)[:SUMMARY_LIMIT]
    return out


def error_outcome(summary):
    """Исход error (код 1) с одной строкой причины для строки ИТОГ и outputs."""
    out = classify_outcome({})
    out["outcome"] = OUTCOME_ERROR
    out["exit_code"] = EXIT_BY_OUTCOME[OUTCOME_ERROR]
    out["summary"] = summary
    return out


def _slots_span(slots):
    if not slots:
        return "—"
    if len(slots) == 1:
        return slots[0]
    return "%s … %s" % (slots[0], slots[-1])


def _outcome_summary(oc):
    """Одна строка про исход — она же уходит в outputs и в текст ИНФО/тревоги."""
    n = oc["gap_hours"]
    if n == 0:
        return ""
    nh = oc.get("pairs_no_history") or 0
    no_hist = (" без истории у биржи %d пар (переспрос, пока в окне);" % nh) if nh else ""
    if oc["outcome"] == OUTCOME_HEALED:
        return ("пропуск %d ч / %d пар залечен: фандинг добран (%d строк, слоты %s);%s "
                "OI пуст (%s)" % (n, oc["pairs_missing"], oc["rows"], _slots_span(oc["slots"]),
                                  no_hist, BACKFILL_NOTE))
    if oc["outcome"] == OUTCOME_PARTIAL:
        return ("пропуск %d ч / %d пар, добор НЕПОЛНЫЙ: %s;%s OI за пропуск не восстановим (%s)"
                % (n, oc["pairs_missing"], "; ".join(oc["reasons"]), no_hist, BACKFILL_NOTE))
    if oc["outcome"] == OUTCOME_OK:
        return ("пропуск %d ч / %d пар: записать нечего —%s добрано %d строк"
                % (n, oc["pairs_missing"], no_hist or " истории нет;", oc["rows"]))
    return ""


def run_url():
    """Ссылка на прогон Actions из переменных раннера; локально — пустая строка."""
    server = os.getenv("GITHUB_SERVER_URL", "").strip()
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    run_id = os.getenv("GITHUB_RUN_ID", "").strip()
    if server and repo and run_id:
        return "%s/%s/actions/runs/%s" % (server, repo, run_id)
    return ""


def info_text(oc):
    """Текст сообщения класса ИНФО для исхода healed (это не тревога)."""
    text = "%s: %s." % (INFO_PREFIX, oc["summary"])
    url = run_url()
    if url:
        text += " Лог: %s" % url
    return text


def info_wanted(oc):
    """Слать ли ИНФО о залеченном пропуске (С-24, ревью 03.09).

    GitHub исполняет почасовой cron примерно раз в 4–5 ч (замер 03.09.2026):
    почти каждый прогон — healed с пропуском 3–5 ч. ИНФО на каждый такой
    прогон — 4–6 одинаковых сообщений в сутки, и канал глохнет. Поэтому ИНФО
    уходит только при пропуске от INFO_GAP_HOURS часов (длиннее обычной
    задержки расписания); рутинный healed оставляет `::notice::` на странице
    прогона и outputs. INFO_GAP_HOURS = 0 возвращает поведение С-22.
    """
    return oc.get("outcome") == OUTCOME_HEALED and int(oc.get("gap_hours") or 0) >= INFO_GAP_HOURS


def github_output(pairs, path=None):
    """key=value в файл $GITHUB_OUTPUT (outputs шага для воркфлоу).

    Локально переменной нет — и делать нечего. Значение приводится к одной
    строке: перевод строки сломал бы формат файла outputs (многострочный
    синтаксис с разделителем тут не нужен — все значения короткие). Булевы
    пишутся как true/false — так их сравнивает выражение `if:` воркфлоу.
    Повторная запись ключа побеждает прежнюю — так читает файл GitHub.
    -> число записанных пар.
    """
    path = path if path is not None else os.getenv("GITHUB_OUTPUT", "")
    if not path:
        return 0
    lines = []
    for key, value in pairs:
        if isinstance(value, bool):
            val = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            val = ", ".join(str(v) for v in value)
        else:
            val = "" if value is None else str(value)
        val = val.replace("\r", " ").replace("\n", " ")
        lines.append("%s=%s\n" % (key, val))
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.writelines(lines)
    except OSError as exc:
        print("ПРЕДУПРЕЖДЕНИЕ: outputs воркфлоу не записаны (%s)" % redact(exc))
        return 0
    return len(lines)


def tg_configured():
    """Есть ли в окружении токен и чат Telegram (значения не печатаются)."""
    return bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
                and os.getenv("TELEGRAM_CHAT_ID", "").strip())


def notify_telegram(text, session=None, timeout=TG_TIMEOUT_SEC,
                    retries=TG_RETRIES, pause=TG_PAUSE_SEC):
    """Сообщение класса ИНФО в Telegram. -> True, если Telegram ответил 200.

    Токен и чат — только из окружения (в Actions — env шага из secrets). Без
    них текст печатается в лог с пометкой, и это НЕ ошибка: локальный прогон
    и dry-run обязаны работать без ключей. Попыток 1 + retries, пауза растёт
    (base*1, base*2): сетевая рябь и 429 — обычное дело, а сообщение одно.
    Печатается только HTTP-код ответа (тело может процитировать URL с
    токеном), текст исключения — через redact: токен стоит в URL, и requests
    кладёт URL в текст ошибки.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("[Telegram не настроен — сообщение только в лог] %s" % text)
        return False
    sess = session or requests.Session()
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        try:
            r = sess.post(TG_URL_TMPL % token,
                          json={"chat_id": chat, "text": text[:TG_TEXT_LIMIT],
                                "disable_web_page_preview": True},
                          timeout=timeout)
            code = r.status_code
        except Exception as exc:
            code = None
            print("Telegram недоступен (попытка %d/%d): %s"
                  % (attempt, attempts, redact(exc)))
        if code is not None:
            print("Telegram ответил кодом %s (попытка %d/%d)" % (code, attempt, attempts))
            if code == 200:
                return True
        if attempt < attempts:
            time.sleep(pause * attempt)
    print("ИНФО не доставлено после %d попыток" % attempts)
    return False


def finish(oc, dry_run=False):
    """Единая точка выхода main(): строка ИТОГ, outputs, ИНФО при healed.

    -> код возврата (из EXIT_BY_OUTCOME). Других путей выхода с кодом у
    main() нет — иначе таблица исходов перестала бы быть единственной.
    Outputs пишутся ДО отправки ИНФО (ревью 03.09: убитый во время ИНФО job
    оставлял шаг тревоги без outputs); info_sent дописывается после — у
    GitHub последняя запись ключа побеждает.
    """
    code = oc["exit_code"]
    print("ИТОГ: исход=%s, код возврата=%d, пропуск=%d ч, не добрано=%d ч%s"
          % (oc["outcome"], code, oc["gap_hours"], oc["unfilled_hours"],
             (" — " + oc["summary"]) if oc.get("summary") else ""))
    values = dict(oc)
    values["exit_code"] = code
    values["info_sent"] = None
    github_output([(k, values.get(k)) for k in OUTPUT_KEYS])
    sent = None
    if oc["outcome"] == OUTCOME_HEALED:
        text = info_text(oc)
        if dry_run:
            print("СУХОЙ ПРОГОН: ИНФО в Telegram не отправляется. Текст был бы: %s" % text)
        elif not info_wanted(oc):
            sent = INFO_SKIPPED
            print("ИНФО не отправлялось: пропуск %d ч короче порога %d ч (рутинная "
                  "задержка расписания GitHub) — след в outputs и на странице прогона"
                  % (oc["gap_hours"], INFO_GAP_HOURS))
            print("::notice title=funding-logger — пропуск залечен::%s" % oc["summary"])
        else:
            sent = bool(notify_telegram(text))
            if not sent and tg_configured():
                # Решение навигатора 03.09.2026 (С-22): недоставленное ИНФО прогон
                # не красит — код есть функция исхода, — но и не молчит: аннотация
                # видна на странице прогона, info_sent=false — следующему шагу и пилоту.
                print("::warning title=funding-logger — ИНФО не доставлено::%s" % text)
    if sent is not None:
        github_output([("info_sent", sent)])
    return code


def run_once(payload, now=None, base_dir=DEFAULT_DATA_DIR, sha=None, repo_dir=None,
             dry_run=False, slot_minute=SLOT_MINUTE_UTC):
    """Разбор + дедуп + дозапись за один слот. Сети не касается.

    Вынесено отдельно от main() намеренно: так весь путь «ответ биржи -> файл»
    проверяется селфтестом БЕЗ сети, на заглушке.

    `now` — момент фактического старта. Из него выводится ДВА разных времени,
    и путать их нельзя: `ts_utc` в строках = когда данные реально сняты
    (правда о данных), `slot` = какой плановый час обслужен (правда о
    расписании). Дедуп и день файла идут по слоту, содержимое строк — по ts.

    -> {'path', 'written', 'skipped', 'parsed', 'ts_utc', 'slot', 'slot_utc',
        'delay_sec', 'last_slot', 'n_missed', 'missed_slots', 'missing_window',
        'gap' (план пар {coin: [slots]}), 'gap_pairs', 'new_coins',
        'silent_since', 'known_slots', 'coins', 'coins_raw', 'n_universe',
        'n_ctx', 'n_delisted', 'warnings'}
    """
    now = now or utc_now()
    ts = iso_z(now)
    slot_dt = slot_start(now, slot_minute)
    slot = slot_dt.strftime(SLOT_KEY_FMT)
    delay_sec = int((now - slot_dt).total_seconds())

    parsed = parse_payload(payload, ts, sha=sha)
    path = csv_path_for(now=slot_dt, base_dir=base_dir, repo_dir=repo_dir)
    existing = read_existing_keys(path, slot_minute=slot_minute)
    fresh, skipped = filter_new_rows(parsed["rows"], existing, slot_minute=slot_minute)

    # Пропуск часов ищем ДО записи, по слотам строго раньше текущего. Если в
    # сегодняшнем файле раньше нас никого нет, смотрим вчерашний: иначе дырка,
    # накрывшая полночь, не была бы видна ни одному прогону — а это самая
    # обычная дырка, ночь как раз то время, когда никто не смотрит.
    known = {k for k, _ in existing if k and k < slot}
    if not known:
        prev_path = csv_path_for(now=slot_dt - timedelta(days=1), base_dir=base_dir,
                                 repo_dir=repo_dir)
        if prev_path != path:
            known = {k for k, _ in read_existing_keys(prev_path, slot_minute=slot_minute)
                     if k and k < slot}
    last_slot = max(known) if known else ""
    n_missed, missed = missing_slots(last_slot, slot)

    i_coin = CSV_HEADER.index("coin")
    coins = list(dict.fromkeys(r[i_coin] for r in parsed["rows"]))

    # Окно сверки для добора — ПО ПАРАМ (слот, монета) для монет текущей
    # вселенной (С-24): пол у каждой монеты свой (её первая запись). Файлы
    # окна читаются ДО записи живого среза, иначе новая монета получила бы
    # пол = текущий слот и её история за окно не запрашивалась бы никогда.
    known_pairs = known_pairs_for_window(slot_dt, base_dir=base_dir, repo_dir=repo_dir,
                                         slot_minute=slot_minute)
    known_slots = set()
    for keys in known_pairs.values():
        known_slots |= keys
    silent_since = ""
    if not known_slots:
        silent_since = last_archive_slot_before_window(slot_dt, base_dir=base_dir,
                                                       repo_dir=repo_dir,
                                                       slot_minute=slot_minute)
    gap, new_coins = missing_window_pairs(slot_dt, known_pairs, coins,
                                          silent=bool(silent_since))

    written = 0 if dry_run else append_rows(path, fresh)
    return {
        "path": path,
        "written": written,
        "would_write": len(fresh),
        "skipped": skipped,
        "parsed": len(parsed["rows"]),
        "ts_utc": ts,
        "slot": slot,
        "slot_utc": iso_z(slot_dt),
        "slot_dt": slot_dt,
        "delay_sec": delay_sec,
        "last_slot": last_slot,
        "n_missed": n_missed,
        "missed_slots": missed,
        "missing_window": gap_slots(gap),
        "gap": gap,
        "gap_pairs": gap_pairs(gap),
        "new_coins": new_coins,
        "silent_since": silent_since,
        "known_slots": known_slots,
        "coins": coins,
        "coins_raw": parsed["coins_raw"],
        "n_universe": parsed["n_universe"],
        "n_ctx": parsed["n_ctx"],
        "n_delisted": parsed["n_delisted"],
        "warnings": parsed["warnings"],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Почасовой архив фандинга и открытого интереса Hyperliquid."
    )
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                    help="каталог архива (по умолчанию %s)" % DEFAULT_DATA_DIR)
    ap.add_argument("--dry-run", action="store_true",
                    help="сходить в источник и напечатать, что было бы записано")
    ap.add_argument("--slot-minute", type=int, default=SLOT_MINUTE_UTC,
                    help="минута часа, на которую поставлен cron (по умолчанию %d; "
                         "менять вместе с .github/workflows/funding.yml)"
                         % SLOT_MINUTE_UTC)
    ap.add_argument("--no-backfill", action="store_true",
                    help="не добирать пропуски из истории биржи (для тестов "
                         "и ручных прогонов; в Actions добор всегда включён)")
    ap.add_argument("--backfill-deadline", type=int, default=BACKFILL_DEADLINE_SEC,
                    help="бюджет добора в секундах (по умолчанию %d; сверх него "
                         "действует общий бюджет прогона %d с)"
                         % (BACKFILL_DEADLINE_SEC, RUN_BUDGET_SEC))
    args = ap.parse_args(argv)
    if not 0 <= args.slot_minute <= 59:
        print("--slot-minute вне диапазона 0..59: %d" % args.slot_minute)
        return 1
    if args.backfill_deadline < 0:
        print("--backfill-deadline не может быть отрицательным: %d"
              % args.backfill_deadline)
        return 1

    print("=" * 66)
    print("ФАНДИНГ И ОТКРЫТЫЙ ИНТЕРЕС — почасовой срез Hyperliquid")
    print("=" * 66)

    try:
        return _run(args)
    except Exception as exc:
        # Любое исключение, не пойманное ниже, — исход error: код 1, строка
        # ИТОГ и outputs для тревоги воркфлоу. Трассировка нужна для разбора,
        # но и она идёт через redact: правило «ошибки в лог — чистыми» едино.
        print("СБОЙ ПРОГОНА (исключение): %s" % redact(exc))
        print(redact(traceback.format_exc()))
        return finish(error_outcome("исключение: %s" % redact(exc)),
                      dry_run=args.dry_run)


def _run(args):
    """Тело прогона; каждый выход — через finish() с одним из трёх исходов."""
    t_run0 = time.monotonic()
    try:
        payload = fetch_payload()
    except Exception as exc:
        print("ИСТОЧНИК НЕДОСТУПЕН: %s" % redact(exc))
        print("Срез часа пропущен: OI восстановить позже НЕЛЬЗЯ, фандинг "
              "доберёт следующий успешный прогон из истории биржи.")
        return finish(error_outcome("источник недоступен: %s" % redact(exc)),
                      dry_run=args.dry_run)

    try:
        res = run_once(payload, base_dir=args.data_dir, dry_run=args.dry_run,
                       slot_minute=args.slot_minute)
    except ValueError as exc:
        print("ОТВЕТ ИСТОЧНИКА НЕ РАЗОБРАН: %s" % redact(exc))
        print("Формат ответа изменился — правку делает пилот, подмены "
              "источника молча не будет.")
        return finish(error_outcome("ответ источника не разобран: %s" % redact(exc)),
                      dry_run=args.dry_run)

    for w in res["warnings"]:
        print("ПРЕДУПРЕЖДЕНИЕ: %s" % w)

    print("слот cron   : %s (ключ %s, cron на :%02d)"
          % (res["slot_utc"], res["slot"], args.slot_minute))
    # Опоздание всегда меньше часа ПО ПОСТРОЕНИЮ: слот — последнее плановое
    # время до старта. Опоздание больше часа выглядит иначе — как дырка в
    # архиве, и ловит его блок «ПРОПУСК ЧАСОВ» ниже, а не эта строка.
    print("время среза : %s (старт позже слота на %d мин)"
          % (res["ts_utc"], res["delay_sec"] // 60))
    print("инструментов: universe=%d, contexts=%d, разобрано строк=%d"
          % (res["n_universe"], res["n_ctx"], res["parsed"]))
    print("из них помечены делистингом: %d (у них нет mid_px и premium)"
          % res["n_delisted"])
    print("файл        : %s" % res["path"])
    if args.dry_run:
        print("СУХОЙ ПРОГОН: записано бы %d строк, дублей за этот слот %d"
              % (res["would_write"], res["skipped"]))
    else:
        print("записано    : %d строк; пропущено как дубль (тот же слот) : %d"
              % (res["written"], res["skipped"]))

    if res["parsed"] == 0:
        print("НИ ОДНОЙ СТРОКИ не разобрано — это сбой источника, а не тишина.")
        return finish(error_outcome("ни одной строки не разобрано — сбой источника"),
                      dry_run=args.dry_run)
    if not args.dry_run and res["written"] == 0:
        print("Новых строк нет: этот слот уже записан. Повторный прогон архив "
              "не портит — так и задумано.")

    gap = res["gap"]
    missing_win = res["missing_window"]
    bf = None
    if res["silent_since"]:
        print("АРХИВ МОЛЧАЛ ДОЛЬШЕ ОКНА %d ч: последняя запись до окна — %s; добираем "
              "всё окно, часы до него потеряны — прогон краснеет"
              % (BACKFILL_WINDOW_HOURS, res["silent_since"]))
    if res["new_coins"]:
        print("новых монет во вселенной без записей в окне: %d (%s) — один пробный "
              "запрос истории на монету, дырок им не выдумываем"
              % (len(res["new_coins"]), _names_short(res["new_coins"])))
    if gap or res["new_coins"]:
        # Дырка не молчит никогда: залеченная — ИНФО-сообщением (при пропуске
        # от INFO_GAP_HOURS) или ::notice::, незалеченная — красным прогоном
        # и тревогой. Зелёный прогон с предупреждением только в логе Actions
        # равносилен молчанию, а молчащая потеря часа — ровно тот дефект,
        # из-за которого переписан ключ дедупа.
        n = len(missing_win)
        if gap:
            tail = "" if n <= MAX_LISTED_GAPS else \
                " ... и ещё %d" % (n - MAX_LISTED_GAPS)
            print("ПРОПУСК ЧАСОВ: за окно %d ч не снят(о) %d слот(ов) / %d пар (монет %d из %d): %s%s"
                  % (BACKFILL_WINDOW_HOURS, n, res["gap_pairs"], len(gap), len(res["coins"]),
                     ", ".join(missing_win[:MAX_LISTED_GAPS]), tail))
        if args.no_backfill:
            print("ДОБОР ВЫКЛЮЧЕН (--no-backfill): фандинг доберёт следующий "
                  "прогон; открытый интерес за эти часы не восстановим.")
        else:
            # Общий бюджет прогона: job живёт в timeout-minutes: 10, и убитый
            # по таймауту шаг не оставляет ни outputs, ни тревоги (ревью 03.09).
            elapsed = time.monotonic() - t_run0
            deadline = max(0, min(args.backfill_deadline, int(RUN_BUDGET_SEC - elapsed)))
            if deadline < args.backfill_deadline:
                print("бюджет добора урезан до %d с: общий бюджет прогона %d с, "
                      "прошло %d с" % (deadline, RUN_BUDGET_SEC, int(elapsed)))
            bf = backfill_funding(gap, coins=res["coins"], new_coins=res["new_coins"],
                                  raw_names=res["coins_raw"], known_slots=res["known_slots"],
                                  base_dir=args.data_dir, slot_minute=args.slot_minute,
                                  deadline_sec=deadline, dry_run=args.dry_run,
                                  slot_dt=res["slot_dt"])
            got = bf["would_write"] if args.dry_run else bf["rows_written"]
            verb = "добран был бы" if args.dry_run else "добран"
            if got:
                print("пропуск %d ч / %d пар: фандинг %s из истории биржи (%d строк, "
                      "пар %d, слотов закрыто %d из %d; монет опрошено %d, новых %d), "
                      "OI нет — открытый интерес задним числом биржа не отдаёт (пометка %s)"
                      % (n, res["gap_pairs"], verb, got, bf["pairs_filled"],
                         bf["slots_filled"], n, bf["coins_ok"], bf["coins_new"],
                         BACKFILL_NOTE))
            elif gap:
                print("пропуск %d ч / %d пар: фандинг добрать не удалось (источник "
                      "не отдал ни строки), OI нет; добор повторит следующий "
                      "прогон" % (n, res["gap_pairs"]))
            if bf["pairs_no_history"]:
                print("без истории у биржи: %d пар (слоты %s) — не дефект, "
                      "переспрашиваются каждым прогоном, пока в окне; метка в архив "
                      "НЕ пишется (замок на паре был бы необратим)"
                      % (bf["pairs_no_history"], _slots_span(bf["no_history_slots"])))
            if bf["coins_failed"] or bf["timed_out"]:
                print("ДОБОР НЕПОЛНЫЙ: монет с ошибкой %d из %d (%s)%s — не добрано пар %d, "
                      "прогон краснеет кодом 2; следующий прогон продолжит с этих монет "
                      "(старейший долг первым)"
                      % (bf["coins_failed"], bf["coins_total"],
                         _names_short(bf["coins_failed_names"]) or "—",
                         ", бюджет исчерпан" if bf["timed_out"] else "",
                         bf["pairs_unresolved"]))
    # Три исхода — одна таблица кодов: залеченный пропуск больше не красит
    # прогон (код 0 + ИНФО/notice), незалеченный — код 2 и тревога с числом
    # часов и пар.
    return finish(classify_outcome(gap, bf, no_backfill=args.no_backfill,
                                   dry_run=args.dry_run, silent_since=res["silent_since"],
                                   slot_key_now=res["slot"]),
                  dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

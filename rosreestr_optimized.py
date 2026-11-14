"""
Проверка домов и помещений в сервисе Росреестра
«Справочная информация по объектам недвижимости в режиме online»
(https://lk.rosreestr.ru/eservices/real-estate-objects-online).

ОПТИМИЗИРОВАННАЯ ВЕРСИЯ (rosreestr_optimized.py).

Главное отличие от исходной версии (rosreestr_online.py):
программа работает через ПРЯМОЙ API сервиса, которым пользуется сам сайт,
а не через имитацию кликов по странице:

  GET  /account-back/address/search?term=<адрес>&objType=all
       -> JSON-массив [{"cadnum", "full_name", "actual", "type"}, ...]
       Бесплатный (без капчи), ОДИН запрос возвращает до 100 объектов.
       Ответ ровно из 100 строк — это ВЫБОРКА из большего множества,
       причём нестабильная: два одинаковых запроса возвращают разные наборы.

  POST /account-back/on  {"filterType": "cadastral", "cadNumbers": ["..."]}
       -> {"elements": [<полная карточка объекта>], "count": N}
       Полная карточка (права, обременения, площадь, этаж, назначение кодом,
       кадастровая стоимость, статус) одним запросом (~1.5 с) вместо
       5-6 с ожиданий экрана. В залогиненной сессии капча не требуется.

  GET  /account-back/dictionary/<NAME>?sortKey=code
       -> [{"code", "value"}] — официальные справочники кодов.

Запросы выполняются из открытой страницы браузера (page.evaluate + fetch),
поэтому идут с cookie и заголовками вашей залогиненной сессии — ровно так,
как их отправляет сам сайт. Ничего не обходится: вход в профиль и капча
проходятся вручную в браузере, сессия хранится в rosreestr_profile.

Если API недоступен или отвечает ошибкой (капча, 401/403, 5xx, не-JSON),
программа автоматически переключается на старый UI-движок (заполнение
поисковой строки, клики по结果ам) — он сохранён полностью.

Читает «Дома в обслуживание.xlsx» (лист «Подходящие»), сохраняет в
«Дома Росреестр.xlsx»: листы «Дома» (полная объединённая таблица),
«Квартиры», «Частных меньше 50%», «Неполные данные», «Ошибки и пропуски»,
«Диагностика». main.py и его Excel не изменяются.

Лимит «не больше 30» считается ТОЛЬКО по квартирам (кв.). Комнаты (ком.) и
нежилые помещения (пом.) в лимит не входят: общежития и дома с нежилыми
помещениями проходят.

Запуск:  python rosreestr_optimized.py
Из main.py:  from rosreestr_optimized import run_check; run_check()
"""

import json
import random
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


# ==========================================
# НАСТРОЙКИ
# ==========================================

URL = "https://lk.rosreestr.ru/eservices/real-estate-objects-online"
API_BASE = "/account-back"

INPUT_FILE = "Дома в обслуживание.xlsx"
INPUT_SHEET = "Подходящие"

OUTPUT_FILE = "Дома Росреестр.xlsx"
CACHE_FILE = "rosreestr_cache.json"
PROFILE_DIR = "rosreestr_profile"
DEBUG_DIR = "rosreestr_debug"
STATS_FILE = "rosreestr_stats.json"
LOG_FILE = "rosreestr_log.txt"

# --- движок ---
USE_API = True            # False: работать только через UI (как старая версия)
AUTO_FALLBACK_UI = True   # API сломался (капча/401/не-JSON) — переключаться на UI
API_TIMEOUT = 25          # секунд на один API-запрос (AbortController внутри страницы)
API_RETRIES = 3           # повторов API-запроса при временной ошибке
API_CARD_BATCH = 1        # сколько карточек запрашивать за один POST /on (1 = бережно)
ADDRESS_PASSES = 3        # проходов поиска по адресу, когда выдача упёрлась в 100
API_PAUSE = 0.4           # пауза между API-запросами, сек (умножается на PACE)

# --- ожидания UI-движка (фолбэк; значения исходной версии) ---
SEARCH_TIMEOUT = 30
CARD_WAIT = 30
PAUSE_AFTER_TYPING = 0.8
CARD_CLOSE_PAUSE = 0.8
QUICK_RETRY_WAIT = 1.5
SITE_LOAD_WAIT = 25
CARD_TIMEOUT = 15
POLL_MS = 150

PAUSE_BEFORE_SEARCH = 0
PAUSE_BEFORE_CARD = 0
PAUSE_AFTER_CARD = 0.2
RATE_LIMIT_WAIT = 90
RATE_LIMIT_RETRIES = 3
MAX_FLAT_FALLBACK = 60
MAX_RESULT_PAGES = 12
PAUSE_NEXT_PAGE = 1.0
SPACE_BEFORE_HOUSE_COMMA = True

AUTO_RETRIES = 5
AUTO_RECOVER_WAITS = (5, 10, 20, 30)
SITE_DOWN_MAX_MINUTES = 90
MAX_CONSECUTIVE_FAILS = 5
MAX_HOUSE_PASSES = 3

SEARCH_STAGES = (
    ("кв.", 1),
    ("пом.", 1),
    ("ком.", 1),
    ("пом.", 1001),
)
STAGE_ONLY_IF_SEEN = True
ALWAYS_PROBE_NEXT = False
ALWAYS_CHECK_1001 = False
MAX_FLATS = 30
SKIP_BY_GIS_COUNT = False
RULES_VERSION = 6
BIG_PROBE_FROM = 20
EMPTY_WAIT = 5
STRICT_SAME_QUARTER = True
EMPTY_STREAK_LIMIT = 3
RESULTS_CAP = 100

MISSING_EXTRA = 6
MAX_MISSING_PROBES = 45
ALT_QUERY_VARIANTS = True

# Коды назначения (официальный справочник Росреестра, подтверждено HAR и
# классификатором; используется при разборе карточки из API).
PURPOSE_CODES = {
    "204001000000": "Нежилое здание",
    "204002000000": "Жилой дом",
    "204003000000": "Многоквартирный дом",
    "206001000000": "Нежилое помещение",
    "206002000000": "Жилое помещение",
}
BUILDING_OBJTYPE = "204"   # здания: 204xxx; помещения: 206xxx

# Кодировка поля ownershipType в карточке API по наблюдениям НЕ подтверждена:
# одна карточка дала «01». Пока карта пуста — форма собственности берётся из
# прав (rightTypeDesc), а сырой код пишется в лист «Квартиры».
# Как заполнить: откройте карточку заведомо муниципальной квартиры, посмотрите
# её ownershipType и добавьте сюда, например {"02": "Муниципальная"}.
OWNERSHIP_CODE_MAP = {}

KN_RE = r"\d{1,2}:\d{1,3}:\d{1,8}:\d+"

INPUT_COLUMNS = [
    "Адрес", "Тип дома", "Кадастровый номер", "Год постройки", "Этажность",
    "Состояние", "Общая площадь здания, м²",
    "Общая площадь жилых помещений, м²", "Способ управления",
    "Управляющая организация", "Количество квартир", "Жилых помещений",
    "Нежилых помещений", "Подходит",
]

_RR_HEADERS = [
    "Найден в Росреестре", "Адрес в Росреестре", "Адрес совпал",
    "Вид объекта", "Назначение", "Площадь дома, кв.м", "Этажей", "Год завершения",
    "Статус объекта", "Статус проверки",
    "Помещений в списке Росреестра", "Помещений найдено (карточки)",
    "Из них жилых", "Сверка с ГИС ЖКХ", "Комментарий",
    "Квартир (кв.) найдено", "Комнат (ком.)", "Помещений (пом.)",
    "Частных, шт", "Частных, кв.м",
    "Муниципальных, шт", "Муниципальных, кв.м",
    "Без формы собственности, шт", "Без формы собственности, кв.м",
    "Прочих форм, шт", "Прочих форм, кв.м",
    "Всего, кв.м (по найденным)", "Доля муниципальной, %", "Доля частной, %",
    "Частных меньше 50%",
    "Полнота данных", "Не найдены номера квартир",
]

HOUSE_HEADERS = INPUT_COLUMNS + _RR_HEADERS

PREMISE_HEADERS = [
    "Адрес дома (ГИС ЖКХ)", "Помещение", "Тип", "Статус объекта",
    "Форма собственности", "Вид права (общая/совместная/собственность)",
    "Площадь, кв.м",
    "Вид, номер и дата регистрации права",
    "Обременения (номер и дата)",
    "Назначение", "Этаж",
    "Адрес в Росреестре", "Адрес совпал", "Статус проверки",
    "Кадастровая стоимость", "Кадастровый номер",
]

ERROR_HEADERS = ["Адрес (ГИС ЖКХ)", "Кадастровый номер", "Помещение", "Причина"]

DIAG_HEADERS = [
    "Адрес", "Кадастровый номер", "Секунд на дом", "Секунд: поиск здания",
    "Секунд: список помещений", "Секунд: карточки", "Секунд: добор пропущенных",
    "Запросов API", "Поисков (UI)", "Карточек обработано", "Ошибок",
    "Повторных попыток", "Пустых выдач", "Лимитов частоты",
    "Квартир найдено", "Комнат найдено", "Нежилых найдено",
    "Пропущено (другой квартал)", "Ожидалось (ГИС ЖКХ)", "Полнота",
    "Самый медленный этап", "Причина завершения",
]


# ==========================================
# ТАКСОНОМИЯ ОШИБОК
# ==========================================

class SearchTimeout(Exception):
    """Тайм-аут поиска или ответ не пришёл вовремя."""


class RateLimited(Exception):
    """Сервис ограничил частоту запросов («слишком много попыток», 429)."""


class ServerBusy(Exception):
    """Временная ошибка сервера (5xx, «попробуйте позже») — повторить."""


class CaptchaRequired(Exception):
    """API требует капчу (406/401/403) — нужен UI и ручной ввод."""


class EmptyResult(Exception):
    """Пустая выдача — объект действительно отсутствует (НЕ ошибка)."""


class UnreadableAnswer(Exception):
    """Ответ не в контрактном формате — нельзя считать «не найдено»."""


class ConnectionLost(Exception):
    """Потеря сети/браузера — восстановление соединения."""


class FatalStop(Exception):
    """Сайт недоступен слишком долго — аккуратная остановка."""


class _BigHouse(Exception):
    """Внутренний сигнал: в доме больше MAX_FLATS квартир."""


ERROR_KINDS = (
    "временная ошибка сервера", "потеря соединения", "тайм-аут поиска",
    "пустая выдача", "лимит запросов", "карточка не открылась",
    "ошибка чтения карточки", "ошибка браузера", "ошибка пагинации",
    "некорректный ответ", "требуется капча", "прочее",
)


def classify_error(error):
    """Категория сбоя по исключению/тексту — для статистики и лога."""
    if isinstance(error, RateLimited):
        return "лимит запросов"
    if isinstance(error, CaptchaRequired):
        return "требуется капча"
    if isinstance(error, ServerBusy):
        return "временная ошибка сервера"
    if isinstance(error, ConnectionLost):
        return "потеря соединения"
    if isinstance(error, UnreadableAnswer):
        return "некорректный ответ"
    if isinstance(error, SearchTimeout):
        return "тайм-аут поиска"
    if isinstance(error, EmptyResult):
        return "пустая выдача"

    text = str(error).lower()

    if "has been closed" in text or "target closed" in text:
        return "ошибка браузера"
    if "слишком много" in text or "ограничил частоту" in text or "429" in text:
        return "лимит запросов"
    if "капч" in text or "406" in text or "401" in text or "403" in text:
        return "требуется капча"
    if "карточка не открылась" in text or "не удалось нажать" in text:
        return "карточка не открылась"
    if "нет результатов поиска" in text or "номер не найден в выдаче" in text:
        return "тайм-аут поиска"
    if "соединен" in text or "net::" in text or "timeout" in text:
        return "потеря соединения"
    return "прочее"


# ==========================================
# СТАТИСТИКА
# ==========================================

STATS = {
    "api_calls": 0, "searches": 0, "cards": 0, "fail": {}, "http": {},
    "rate_limited": 0, "retries": 0, "empty_results": 0,
}


def stat_fail(error):
    kind = classify_error(error)
    STATS["fail"][kind] = STATS["fail"].get(kind, 0) + 1
    return kind


def save_stats():
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as file:
            json.dump(STATS, file, ensure_ascii=False, indent=2)
    except Exception:
        pass


class HouseDiag:
    """Диагностика обработки одного дома: времена, счётчики, причина."""

    FIELDS = (
        "secs", "secs_building", "secs_list", "secs_cards", "secs_probe",
        "api_calls", "ui_searches", "cards", "errors", "retries",
        "empties", "rate_limited", "flats", "rooms", "nonres",
        "skipped_quarter", "capped", "missing_numbers", "finish_reason",
    )

    def __init__(self):
        self.t0 = time.time()
        self.stage_t0 = self.t0
        for name in self.FIELDS:
            if name != "finish_reason":
                setattr(self, name, 0)
        self.finish_reason = ""

    @contextmanager
    def stage(self, name):
        """Контекст этапа: после выхода накапливает его длительность."""
        start = time.time()
        try:
            yield
        finally:
            spent = time.time() - start
            key = {
                "building": "secs_building", "list": "secs_list",
                "cards": "secs_cards", "probe": "secs_probe",
            }.get(name)
            if key:
                setattr(self, key, getattr(self, key) + spent)
            self.stage_t0 = time.time()

    def finish(self, reason):
        self.secs = int(time.time() - self.t0)
        self.finish_reason = reason
        return self.secs

    def get(self, name, default=0):
        return getattr(self, name, default)

    def __getitem__(self, name):
        return getattr(self, name, None)

    def __setitem__(self, name, value):
        setattr(self, name, value)

    def slowest_stage(self):
        stages = {
            "поиск здания": self.secs_building,
            "список помещений": self.secs_list,
            "карточки": self.secs_cards,
            "добор пропущенных": self.secs_probe,
        }
        name, value = max(stages.items(), key=lambda kv: kv[1])
        return f"{name} ({int(value)} с)" if value > 0 else ""

    def row(self, house, rec):
        complete = rec.get("_complete_text", "")
        expected = rec.get("_expected", "")
        return [
            house["address"], house["cadastre"], self.secs,
            int(self.secs_building), int(self.secs_list),
            int(self.secs_cards), int(self.secs_probe),
            self.api_calls, self.ui_searches, self.cards, self.errors,
            self.retries, self.empties, self.rate_limited,
            self.flats, self.rooms, self.nonres, self.skipped_quarter,
            expected or None, complete, self.slowest_stage(),
            self.finish_reason or None,
        ]

    def to_dict(self):
        return {name: getattr(self, name) for name in self.FIELDS}


def diag_from_dict(data):
    diag = HouseDiag()
    if isinstance(data, dict):
        for name in HouseDiag.FIELDS:
            if name in data:
                setattr(diag, name, data[name])
    return diag


# ==========================================
# АДРЕСА: РАЗБОР И МНОГОУРОВНЕВАЯ СВЕРКА
# ==========================================

STREET_TYPES = {
    "улица": "ул", "ул": "ул",
    "проспект": "пр-кт", "пр-кт": "пр-кт", "пр-т": "пр-кт", "просп": "пр-кт", "пр": "пр-кт",
    "переулок": "пер", "пер": "пер",
    "бульвар": "б-р", "б-р": "б-р", "бул": "б-р",
    "шоссе": "ш", "ш": "ш",
    "площадь": "пл", "пл": "пл",
    "набережная": "наб", "наб": "наб",
    "тупик": "туп", "туп": "туп",
    "микрорайон": "мкр", "мкр": "мкр", "мкр-н": "мкр",
    "аллея": "ал", "ал": "ал",
    "проезд": "проезд", "тракт": "тракт", "линия": "линия", "проулок": "проулок",
    "дорога": "дор", "дор": "дор", "кольцо": "кольцо", "спуск": "спуск",
    "въезд": "въезд", "съезд": "съезд", "остров": "остров",
}

CITY_TYPES = {
    "г", "город", "с", "село", "п", "пос", "поселок", "пгт",
    "д", "дер", "деревня", "рп", "ст", "станица", "г.о", "го", "кп", "снт",
}

HOUSE_RE = re.compile(
    r"(?:^|[\s,])д(?:ом)?\.?\s*(\d+(?:/\d+)?[а-яa-z0-9]*)", re.I
)
KORPUS_RE = re.compile(
    r"^\s*,?\s*к(?:орп(?:ус)?)?\.?\s*(\d+[а-яa-z]?)", re.I
)
STROENIE_RE = re.compile(r"^\s*,?\s*стр\.?\s*(\d+[а-яa-z]?)", re.I)
ATTACHED_KORPUS_RE = re.compile(r"^(\d+(?:/\d+)?)[кk](\d+[а-яa-z]?)$")
LITERA_RE = re.compile(r"^\s*,?\s*лит(?:ера)?\.?\s*([а-яa-z])", re.I)
FLAT_RE = re.compile(
    r"(?:^|[\s,])(?:кв|квартира)\.?\s*(\d+[а-яa-z]?(?:/\d+)?)(?!\w)", re.I
)
ROOM_RE = re.compile(
    r"(?:^|[\s,])(?:ком|комн|комната)\.?\s*(\d+[а-яa-z]?)(?!\w)", re.I
)
PREM_RE = re.compile(
    r"(?:^|[\s,])(?:пом|помещение)\.?\s*(\d+[а-яa-z]?|[IVXivx]+)(?!\w)", re.I
)

GENERIC_WORDS = {
    "российская", "федерация", "область", "обл", "край", "республика",
    "респ", "г", "о", "го", "город", "городской", "муниципальный",
    "округ", "район", "р-н", "мкр", "микрорайон", "рп",
}


def normalize(text):
    return " ".join(str(text).split())


def to_tokens(text):
    text = str(text).lower().replace("ё", "е")
    tokens = re.findall(r"[a-zа-я0-9][a-zа-я0-9-]*", text)
    return [STREET_TYPES.get(token, token) for token in tokens]


def _split_house_token(token):
    """'5к2' -> ('5', '/2', ''), '5литА' -> ('5', '', 'ла'), '5аб' -> ('5', '', 'аб')."""

    match = re.fullmatch(r"(\d+(?:/\d+)?)([а-яa-z].*)?", token or "")

    if not match:
        return token or "", "", ""

    num, tail = match.group(1), match.group(2) or ""
    attached = re.fullmatch(r"([кkс])(\d+)([а-яa-z]?)", tail)

    if attached:
        letter, digits, after = attached.groups()

        if letter in ("к", "k"):
            return num, "/" + digits + after, ""

        return num, "с" + digits + after, ""

    litera = re.fullmatch(r"лит(?:ера)?([а-яa-z])", tail)

    if litera:
        return num, "", "л" + litera.group(1)

    return num, "", tail


def canon_house_key(key):
    """'5к2' (корпус без пробела) -> '5/2' — как в записи 'д 5 к2'."""

    num, suffix, letters = _split_house_token(key or "")
    return num + suffix + letters


def house_key(text):
    """Номер дома в едином виде: 86/1, 41б, 15/2 (корпус), 7с2, 7ла."""

    match = HOUSE_RE.search(text)

    if not match:
        return None

    num, suffix, letters = _split_house_token(
        match.group(1).lower().replace("ё", "е")
    )
    key = num + suffix + letters
    rest = text[match.end():]

    korpus = KORPUS_RE.match(rest)

    if korpus:
        key += "/" + korpus.group(1).lower()
        rest = rest[korpus.end():]

    stroenie = STROENIE_RE.match(rest)

    if stroenie:
        key += "с" + stroenie.group(1).lower()
        rest = rest[stroenie.end():]

    litera = LITERA_RE.match(rest)

    if litera:
        key += "л" + litera.group(1).lower()

    return key


def premise_label(text):
    """Квартира / комната / помещение из адреса, например 'кв. 5, ком. 2'."""

    parts = []

    for prefix, regex in (("кв.", FLAT_RE), ("пом.", PREM_RE), ("ком.", ROOM_RE)):
        match = regex.search(text)

        if match:
            parts.append(f"{prefix} {match.group(1)}")

    return ", ".join(parts) if parts else None


def premise_number(label):
    match = re.search(r"кв\. (\d+)", label or "")
    return int(match.group(1)) if match else None


def parse_gis_address(address):
    """Адрес из ГИС ЖКХ: '610004, обл Кировская, г Киров, ул Советская, д. 15'."""

    segments = [s.strip() for s in address.split(",") if s.strip()]
    segments = [s for s in segments if not re.fullmatch(r"\d{6}", s)]

    house_index = None

    for index, segment in enumerate(segments):
        if re.match(r"^д(?:ом)?\.?\s*\d", segment, re.I):
            house_index = index
            break

    if house_index is None:
        return None

    tail = ", ".join(segments[house_index:])
    street = to_tokens(segments[house_index - 1]) if house_index > 0 else []

    city = []

    for segment in segments[:house_index - 1]:
        if re.match(r"^(г|город|с|село|п|пгт|рп)\.?\s", segment, re.I):
            city = [t for t in to_tokens(segment) if t not in CITY_TYPES]
            break

    allowed = set()

    for segment in segments[:house_index]:
        allowed |= set(to_tokens(segment))

    return {
        "street": street,
        "city": city,
        "house": house_key(tail),
        "allowed": allowed,
    }


def parse_rr_address(address):
    house = HOUSE_RE.search(address)
    head = address[:house.start()] if house else address

    tail_clean = True

    if house:
        rest = address[house.end():]
        for regex in (KORPUS_RE, STROENIE_RE, LITERA_RE):
            m = regex.match(rest)
            if m:
                rest = rest[m.end():]
        for regex in (FLAT_RE, ROOM_RE, PREM_RE):
            rest = regex.sub(" ", rest)
        tail_clean = not re.sub(r"[\s,.;]+", "", rest)

    return {
        "tail_clean": tail_clean,
        "head": head.lower(),
        "head_tokens": set(to_tokens(head)),
        "tokens": set(to_tokens(address)),
        "house": house_key(address),
        "label": premise_label(address),
    }


ADMIN_HEAD_WORDS = {
    "обл", "область", "край", "респ", "республика", "район", "р-н",
    "мкр", "мкр-н", "микрорайон", "г.о", "го", "округ", "снт", "снп",
}


def _strip_admin_segments(extra, head):
    """Убирает из «лишних» слов те, что стоят в одном сегменте адреса с
    административным словом (регион/район/мкр) — это префикс, а не улица."""

    segments = re.split(r"[,()]|\b\d{6}\b", head or "")
    has_admin = False

    for segment in segments:
        toks = set(to_tokens(segment))

        if toks & extra and toks & ADMIN_HEAD_WORDS:
            extra = extra - toks
            has_admin = True

    return extra, has_admin


def match_level(gis, rr):
    """Многоуровневая сверка адреса. Возвращает (уровень, причина_отклонения).

    Уровни: EXACT — полное совпадение дома, улицы и города;
    STRONG — совпадают дом и улица (город может быть назван иначе);
    WEAK — совпадает только номер дома и улица без лишних слов;
    None — не наш дом, причина объясняет почему.

    rr может содержать поля адреса из API-карточки (rr_fields), тогда
    сверка идёт по полям, а не по тексту — это самый надёжный уровень.
    """

    fields = rr.get("fields") or {}

    if fields:
        return match_house_fields(gis, fields)

    if not gis or not gis["house"] or rr["house"] != gis["house"]:
        return None, "другой номер дома"

    if not rr.get("tail_clean", True):
        return None, "лишние слова после номера дома"

    needed_street = gis["street"]
    needed_city = gis["city"]

    if not needed_street:
        return None, "в адресе ГИС ЖКХ нет улицы"

    street_ok = all(t in rr["tokens"] for t in needed_street)
    city_ok = all(t in rr["tokens"] for t in needed_city) if needed_city else True

    if not street_ok:
        return None, "нет в адресе слов улицы: " + " ".join(
            t for t in needed_street if t not in rr["tokens"]
        )

    if not city_ok:
        return None, "нет в адресе слов города: " + " ".join(
            t for t in needed_city if t not in rr["tokens"]
        )

    extra = rr["head_tokens"] - gis["allowed"] - GENERIC_WORDS

    if extra:
        # «Кировская обл», «Нововятский район» — административный префикс,
        # а не другая улица: убираем слова из сегментов с админ-словом.
        extra, has_admin = _strip_admin_segments(extra, rr.get("head") or "")

        if extra:
            return None, "лишние слова в адресе: " + " ".join(sorted(extra))

        if has_admin:
            return "STRONG", "совпадает с административным префиксом"

    return "EXACT", ""


def match_house_fields(gis, fields):
    """Сверка по отдельным полям адреса из API-карточки (street/house/flat...).

    fields: {"street": "...", "house": "12", "korpus": "2", "flat": "5", ...}.
    Возвращает (уровень, причина).
    """

    fhouse = canon_house_key(str(fields.get("house") or "").strip().lower())
    fkorpus = str(fields.get("korpus") or fields.get("building") or "").strip().lower()

    if not fkorpus:
        m = ATTACHED_KORPUS_RE.match(fhouse)

        if m:
            fhouse, fkorpus = m.group(1), m.group(2)

    fstreet = to_tokens(fields.get("street") or "")

    if not fhouse:
        return None, "в карточке нет номера дома"

    gis_house = gis["house"] if gis else None

    if not gis_house:
        return None, "в адресе ГИС ЖКХ не найден номер дома"

    if "/" in gis_house:
        gnum, gkorp = gis_house.split("/", 1)
    else:
        m = re.match(r"^(\d+[а-яa-z]*)(?:с\d+)?(л[а-я])?$", gis_house)
        gnum, gkorp = (m.group(1), "") if m else (gis_house, "")

    if fhouse != gnum or (gkorp and fkorpus != gkorp.lower()):
        return None, f"другой дом: {fhouse}" + (f"/{fkorpus}" if fkorpus else "")

    if fstreet:
        needed = gis["street"] if gis else []
        needed = [t for t in needed if t not in STREET_TYPES.values()]
        if needed and not all(t in fstreet for t in needed):
            return None, "другая улица: " + (fields.get("street") or "")

    return "EXACT", ""


def match_house(gis, rr):
    """Адрес из Росреестра относится именно к этому дому (STRONG и выше)."""

    level, _ = match_level(gis, rr)
    return level in ("EXACT", "STRONG")


# ==========================================
# РАЗБОР КАРТОЧКИ ОБЪЕКТА (API + текст UI)
# ==========================================

BUILDING_PURPOSES_MKD = {"204003000000"}
BUILDING_PURPOSES_OK = {"204001000000", "204002000000", "204003000000"}
PREMISE_PURPOSES_LIVE = {"206002000000"}
PREMISE_PURPOSES_NONLIVE = {"206001000000"}

NO_OWNERSHIP = "Нет формы собственности"

CARD_LABELS = {
    "характеристики объекта", "дата присвоения кадастрового номера",
    "адрес (местоположение)", "площадь, кв.м", "назначение", "этаж",
    "сведения о кадастровой стоимости", "ранее присвоенные номера",
}


def purpose_text(code):
    code = str(code or "").strip()
    return PURPOSE_CODES.get(code, f"код {code}" if code else "")


def classify_purpose_code(code):
    """Возвращает ('кв.'|'ком.'|'пом.'|None) по коду назначения API-карточки."""

    code = str(code or "").strip()

    if code in PREMISE_PURPOSES_LIVE:
        return "кв."
    if code in PREMISE_PURPOSES_NONLIVE:
        return "пом."
    return None


def classify(purpose):
    """Строго: только «Многоквартирный дом». Остальное - не наш дом."""

    text = (purpose or "").lower()

    if not text:
        return "Не определено"

    if "нежил" in text:
        return "Не МКД"

    if "многоквартирн" in text:
        return "МКД"

    return "Не МКД"


def building_verdict(building):
    """МКД только если назначение «Многоквартирный дом» и статус «Актуально»."""

    verdict = classify(building.get("purpose"))
    state = (building.get("state") or "").strip().lower()

    if verdict == "МКД" and state != "актуально":
        return "Статус не «Актуально»"

    return verdict


def stop_reason(building):
    verdict = building.get("verdict")

    if verdict == "Не МКД":
        return (
            f"Назначение «{building.get('purpose')}» - "
            "не многоквартирный дом"
        )

    if verdict == "Не определено":
        return "Не удалось определить назначение объекта"

    return (
        f"Статус объекта «{building.get('state')}» "
        "(нужно «Актуально»)"
    )


def ownership_class(value):
    """Частная / Муниципальная / иная как в карточке / «Нет формы собственности»."""

    text = (value or "").strip()

    if not text or text.lower() in CARD_LABELS:
        return NO_OWNERSHIP

    low = text.lower()

    if "частн" in low:
        return "Частная"
    if "муницип" in low:
        return "Муниципальная"

    return text


def ownership_hint(rights):
    if not rights:
        return "Права в справке не найдены"

    kinds = " ".join(
        (r.get("kind") if isinstance(r, dict) else str(r)).lower()
        for r in rights
    )

    if "долев" in kinds or "совмест" in kinds:
        return "Вероятно частная (долевая/совместная)"

    if "собственност" in kinds:
        return "Собственность (владелец не раскрывается)"

    return "Иное право"


def ownership_from_card(card):
    """Форма собственности по карточке.

    Порядок: явный текст (UI) -> OWNERSHIP_CODE_MAP (заполняется
    пользователем по наблюдению, см. описание настройки) -> код
    ownershipType как есть -> подсказка по правам.
    """

    text = (card.get("ownership_text") or "").strip()

    if text:
        return ownership_class(text)

    code = str(card.get("ownershipType") or "").strip()

    if code:
        mapped = OWNERSHIP_CODE_MAP.get(code)

        if mapped:
            return mapped

        return f"Код {code} (не раскрыт)"

    return ownership_hint(card.get("rights") or [])


def to_float(value):
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return 0.0


def level_floor_norm(value):
    """'03' -> '3', '01' -> '1'; прочее как есть."""

    text = str(value or "").strip()

    if re.fullmatch(r"-?\d+", text):
        return str(int(text))

    return text


def epoch_ms_to_date(value):
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return ""

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%d.%m.%Y")


def _main_character(el, key_words):
    for item in (el.get("mainCharacters") or []):
        description = (item.get("description") or "").lower()

        if all(word in description for word in key_words):
            return (item.get("value") or "").strip()

    return ""


def address_fields_from_element(el):
    addr = el.get("address") or {}
    fields = {}

    for key in ("region", "district", "city", "settlement", "street",
                "house", "korpus", "building", "flat", "office", "room",
                "litera", "note"):
        value = addr.get(key)

        if value:
            fields[key] = str(value).strip()

    return fields


def card_from_api_element(el):
    """API-элемент из POST /account-back/on -> запись того же вида, что
    parse_card (UI), плюс api-поля. Никаких выдуманных значений."""

    area = _main_character(el, ("площадь",)) or el.get("area") or ""
    purpose_code = str(el.get("purpose") or "").strip()
    purpose = purpose_text(purpose_code)
    status = str(el.get("status") or "").strip()
    state = "Актуально" if status == "1" else "Погашено"

    rights = []

    for item in (el.get("rights") or []):
        rights.append({
            "kind": (item.get("rightTypeDesc") or "").strip(),
            "number": (item.get("rightNumber") or "").strip(),
            "date": epoch_ms_to_date(item.get("rightRegDate")),
        })

    restrictions = []

    for item in (el.get("encumbrances") or []):
        restrictions.append({
            "kind": (item.get("typeDesc") or item.get("encumbranceNumber") or "").strip(),
            "number": (item.get("encumbranceNumber") or "").strip(),
            "date": epoch_ms_to_date(item.get("startDate")),
        })

    address = el.get("address") or {}
    readable = normalize(
        address.get("readableAddress") or address.get("address") or ""
    )
    fields = address_fields_from_element(el)

    kind = purpose if purpose_code else (el.get("objType") or "")

    data = {
        "kind": kind,
        "state": state,
        "kn": (el.get("cadNumber") or "").strip(),
        "address": readable,
        "area": area,
        "purpose": purpose,
        "purpose_code": purpose_code,
        "floor": level_floor_norm(el.get("levelFloor") or el.get("floor")),
        "floors": str(el.get("floors") or ""),
        "year": str(el.get("oksCommisioningYear") or el.get("oksYearBuild") or ""),
        "cost": str(el.get("cadCost") or ""),
        "ownership_text": "",
        "ownershipType": str(el.get("ownershipType") or ""),
        "rights": rights,
        "restrictions": restrictions,
        "cadQuarter": str(el.get("cadQuarter") or ""),
        "parentCadNumber": str(el.get("parentCadNumber") or ""),
        "oldNumbers": el.get("oldNumbers") or [],
        "childCadNumbers": el.get("childCadNumbers") or [],
        "address_fields": fields,
        "source": "api",
        "parse_v": 3,
    }

    if not readable and fields:
        readable = ", ".join(str(v) for v in fields.values() if v)
        data["address"] = readable

    return data


def card_lines(text):
    return [line.strip() for line in text.splitlines() if line.strip()]


def value_after(lines, label):
    target = label.lower()

    for index, line in enumerate(lines):
        if line.lower() == target:
            for next_line in lines[index + 1:]:
                return next_line

    return None


def parse_card(text):
    """Разбор текста карточки из UI (fallback-режим)."""

    marker = text.find("Сведения об объекте")

    if marker >= 0:
        text = text[marker:]

    lines = card_lines(text)

    data = {
        "kind": value_after(lines, "Вид объекта недвижимости"),
        "state": value_after(lines, "Статус объекта"),
        "kn": value_after(lines, "Кадастровый номер"),
        "address": value_after(lines, "Адрес (местоположение)"),
        "area": value_after(lines, "Площадь, кв.м"),
        "purpose": value_after(lines, "Назначение"),
        "floor": value_after(lines, "Этаж"),
        "floors": value_after(lines, "Количество этажей"),
        "year": value_after(lines, "Год завершения строительства"),
        "cost": value_after(lines, "Кадастровая стоимость (руб)"),
        "ownership_text": ownership_class(value_after(lines, "Форма собственности")),
        "rights": [],
        "restrictions": [],
        "source": "ui",
        "parse_v": 2,
    }

    start = None

    for index, line in enumerate(lines):
        low = line.lower()

        if (
            low.startswith("сведения о правах")
            or low.startswith("права и ограничения")
            or "вид, номер и дата" in low
        ):
            start = index
            break

    data["rights_raw"] = ""

    if start is not None:
        section = []

        for line in lines[start:]:
            if line.upper().startswith("ВЕРНУТЬСЯ"):
                break
            section.append(line)

        data["rights_raw"] = "\n".join(section)[:1500]

        mode = "rights"
        date_re = r"\d{2}\.\d{2}\.\d{4}(?:\s*г\.?)?"

        for index, line in enumerate(section):
            low = line.lower()

            if index == 0:
                continue

            if low.startswith(("ограничение прав", "ограничения прав")):
                mode = "restrictions"
                continue

            if low.startswith(("вид, номер", "сведения о правах", "права и ограничения")):
                continue

            if line.startswith("№"):
                continue

            same = re.match(r"^(?P<kind>[^№]{3,}?)[\s,]*(?P<num>№.+)$", line)

            if same:
                data[mode].append({
                    "kind": same.group("kind").strip(" ,"),
                    "number": same.group("num").strip(),
                })
                continue

            if index + 1 < len(section) and section[index + 1].startswith("№"):
                number = section[index + 1]

                if index + 2 < len(section) and re.fullmatch(
                    date_re, section[index + 2]
                ) and section[index + 2] not in number:
                    number = f"{number} {section[index + 2]}"

                data[mode].append({"kind": line, "number": number})

    return data


def card_to_record(card, label=None):
    """Карточка -> строка листа «Квартиры»."""

    return {
        "label": label or "",
        "type": premise_type(label or "") or "",
        "state": card.get("state") or "",
        "ownership": ownership_from_card(card),
        "rights": rights_text(card),
        "area": card.get("area") or "",
        "restrictions": restrictions_text(card),
        "purpose": card.get("purpose") or "",
        "floor": card.get("floor") or "",
        "address": card.get("address") or "",
        "cost": card.get("cost") or "",
        "kn": card.get("kn") or "",
    }


def rights_text(card):
    parts = []

    for item in (card.get("rights") or []):
        if isinstance(item, dict):
            text = item.get("kind") or ""

            if item.get("number"):
                text += f" {item['number']}"

            if item.get("date"):
                text += f" от {item['date']}"
        else:
            text = str(item)

        if text.strip():
            parts.append(text.strip())

    if not parts and card.get("rights_raw"):
        parts.append(str(card["rights_raw"])[:300])

    return "; ".join(parts)


def restrictions_text(card):
    parts = []

    for item in (card.get("restrictions") or []):
        if isinstance(item, dict):
            text = item.get("kind") or ""

            if item.get("number"):
                text += f" {item['number']}"

            if item.get("date"):
                text += f" с {item['date']}"
        else:
            text = str(item)

        if text.strip():
            parts.append(text.strip())

    return "; ".join(parts)


# ==========================================
# ДВИЖОК: API (основной) + БРАУЗЕР (fallback)
# ==========================================

PACE = {"factor": 1.0}
_engine = {"api_ok": USE_API}


def human_pause(seconds):
    seconds = seconds * PACE["factor"]
    time.sleep(random.uniform(seconds, seconds * 1.4))


API_FETCH_JS = """
async ([path, options]) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs);
  try {
    const response = await fetch(path, {
      method: options.method,
      headers: options.body ? {'Content-Type': 'application/json'} : undefined,
      body: options.body ? JSON.stringify(options.body) : undefined,
      credentials: 'include',
      signal: controller.signal,
    });
    const status = response.status;
    const text = await response.text();
    return {status: status, text: text};
  } catch (error) {
    return {status: 0, text: String(error && error.name === 'AbortError'
      ? '__TIMEOUT__' : error)};
  } finally {
    clearTimeout(timer);
  }
}
"""


def api_fetch(page, method, path, params=None, body=None):
    """Один API-запрос изнутри страницы (та же сессия, что у сайта).

    Возвращает (status, parsed_json_or_None). Ошибки транспорта
    поднимаются как исключения таксономии.
    """

    url = API_BASE + path

    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)

    options = {
        "method": method,
        "timeoutMs": API_TIMEOUT * 1000,
    }

    if body is not None:
        options["body"] = body

    STATS["api_calls"] += 1

    try:
        result = page.evaluate(API_FETCH_JS, [url, options])
    except Exception as error:
        raise ConnectionLost(f"browser fetch failed: {str(error)[:120]}")

    status = int(result.get("status") or 0)
    text = result.get("text") or ""
    STATS["http"][status] = STATS["http"].get(status, 0) + 1

    if status == 0 and "__TIMEOUT__" in text:
        raise SearchTimeout(f"API тайм-аут: {path}")

    if status == 0:
        raise ConnectionLost(f"API сеть: {text[:120]}")

    if status in (401, 403):
        raise CaptchaRequired(f"API отказ {status}: {path}")

    if status == 406:
        raise CaptchaRequired("API требует капчу (406)")

    if status == 429:
        raise RateLimited("API ограничение частоты (429)")

    if status >= 500:
        raise ServerBusy(f"API сервер {status}: {path}")

    if status != 200:
        raise UnreadableAnswer(f"API статус {status}: {path}")

    try:
        data = json.loads(text)
    except ValueError:
        raise UnreadableAnswer(f"API не-JSON ответ: {path}: {text[:120]}")

    return data


def _search_rows_from_json(data, term):
    """JSON ответа /address/search -> строки. Объект вместо массива или
    не-контрактный массив = UNREADABLE (это НЕ пустая выдача)."""

    if isinstance(data, dict):
        raise UnreadableAnswer(f"поиск вернул объект вместо массива: {str(data)[:120]}")

    if not isinstance(data, list):
        raise UnreadableAnswer(f"поиск вернул не-массив: {str(data)[:120]}")

    rows = []

    for item in data:
        if not isinstance(item, dict):
            continue

        kn = str(item.get("cadnum") or item.get("cadNumber") or "").strip()

        if not re.fullmatch(KN_RE, kn):
            continue

        rows.append({
            "kn": kn,
            "address": normalize(item.get("full_name") or item.get("text") or ""),
            "actual": bool(item.get("actual")),
            "type": str(item.get("type") or "").strip(),
        })

    return rows


def search_address_api(page, term):
    """Поиск по адресу через API. Возвращает (строки, был_ли_обрезан_список)."""

    human_pause(API_PAUSE)
    data = api_fetch(page, "GET", "/address/search", params={"term": term, "objType": "all"})
    rows = _search_rows_from_json(data, term)

    if not rows:
        STATS["empty_results"] += 1

    return rows, len(rows) >= RESULTS_CAP


def fetch_cards_api(page, kns):
    """POST /account-back/on: карточки по кадастровым номерам.

    Возвращает список элементов (может быть короче запрошенного —
    отсутствующий элемент = номера нет в ЕГРН).
    """

    cards = []

    for start in range(0, len(kns), API_CARD_BATCH):
        batch = kns[start:start + API_CARD_BATCH]
        human_pause(API_PAUSE)
        data = api_fetch(
            page, "POST", "/on",
            body={"filterType": "cadastral", "cadNumbers": batch},
        )

        if not isinstance(data, dict) or "elements" not in data:
            raise UnreadableAnswer(f"карточки: нет elements: {str(data)[:120]}")

        elements = data.get("elements") or []

        if not isinstance(elements, list):
            raise UnreadableAnswer(f"карточки: elements не список: {str(data)[:120]}")

        cards.extend(el for el in elements if isinstance(el, dict))

    return cards


def fetch_card_api(page, kn):
    """Одна карточка. None = номера нет в ЕГРН (пустая выдача POST /on)."""

    cards = fetch_cards_api(page, [kn])

    for el in cards:
        if str(el.get("cadNumber") or "").strip() == kn:
            return el

    if cards:
        return cards[0]

    STATS["empty_results"] += 1
    return None


# ------------------------------------------
# UI fallback: перенос ключевых функций оригинала
# ------------------------------------------

SEARCH_STATE = {"seq": 0, "count": -1}
_first_search = {"done": False}
_site_error_re = re.compile(
    r"соединин|попробуйте (?:позже|ещ[её]|повторить)|повторите|"
    r"не удалось|сервис недоступен|временно недоступ|что-то пошло не так",
    re.I,
)


def stat_fail(error):
    if isinstance(error, BaseException):
        kind = classify_error(error)
    else:
        text = str(error).lower()

        if "слишком много" in text or "ограничил частоту" in text or "429" in text:
            kind = "лимит запросов"
        elif "карточка не открылась" in text or "не удалось нажать" in text:
            kind = "карточка не открылась"
        elif "нет результатов поиска" in text or "пустая выдача" in text:
            kind = "пустая выдача"
        elif "номер не найден в выдаче" in text:
            kind = "номер пропал из выдачи"
        elif "соединенин" in text or "ошиб" in text or "сеть" in text:
            kind = "ошибка сайта/сети"
        else:
            kind = "прочее"

    STATS["fail"][kind] = STATS["fail"].get(kind, 0) + 1
    return kind


def check_rate_limit(text):
    lowered = text.lower()

    if "слишком много" in lowered or "превышено" in lowered:
        raise RateLimited("Росреестр ограничил частоту запросов")


def page_text(page):
    return page.locator("body").inner_text()


_debug_saved = {"n": 0}


def save_debug(page, name):
    if _debug_saved["n"] >= 25:
        return

    _debug_saved["n"] += 1

    Path(DEBUG_DIR).mkdir(exist_ok=True)

    try:
        stamp = time.strftime("%H%M%S")
        Path(DEBUG_DIR, f"{stamp}_{name}.txt").write_text(
            page_text(page), encoding="utf-8"
        )
        page.screenshot(path=str(Path(DEBUG_DIR, f"{stamp}_{name}.png")))
    except Exception:
        pass


def find_search_box(page):
    box = page.get_by_label(re.compile("Адрес или кадастровый номер", re.I))

    if box.count() > 0:
        return box.first

    return page.locator(
        "input[type='text']:visible, input:not([type]):visible"
    ).first


def search_form_ready(page):
    try:
        box = find_search_box(page)
        return box.count() > 0 and box.is_visible()
    except Exception:
        return False


def reload_site(page):
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as error:
        print(f"   не удалось открыть сайт: {str(error)[:80]}")
        return

    deadline = time.time() + SITE_LOAD_WAIT

    while time.time() < deadline:
        page.wait_for_timeout(500)

        if search_form_ready(page):
            page.wait_for_timeout(500)
            return


def wait_until_site_ready(page):
    """Ждёт возврата формы поиска. Если не дождались — FatalStop."""

    deadline = time.time() + SITE_DOWN_MAX_MINUTES * 60
    first = True

    while time.time() < deadline:
        reload_site(page)

        if search_form_ready(page):
            print("   сайт снова доступен, продолжаем")
            return

        if first:
            first = False
            print(
                "\a\n!!! Форма поиска недоступна (нет интернета, сайт лежит "
                "или слетел вход/капча). Жду и проверяю раз в минуту. "
                "Если нужна капча или вход, сделайте это в окне браузера, "
                "программа продолжит сама."
            )

        for _ in range(6):
            time.sleep(10)

            if search_form_ready(page):
                print("   форма поиска снова доступна, продолжаем")
                return

    raise FatalStop(
        f"Сайт недоступен более {SITE_DOWN_MAX_MINUTES} минут. "
        "Прогресс сохранён, запустите программу позже."
    )


def go_next_page(page, target_page=None):
    """Переходит на следующую страницу списка результатов."""

    pattern = re.compile(
        r"след|next|вперед|вперёд|показать еще|загрузить еще|›|»", re.I
    )

    candidates = [
        page.get_by_role("button", name=pattern),
        page.get_by_role("link", name=pattern),
        page.locator(
            "[aria-label*='ледующ' i], [aria-label*='next' i], "
            "[title*='ледующ' i]"
        ),
        page.locator("li.pagination-base__arrow").last.locator("a"),
    ]

    if target_page:
        number = str(target_page)

        candidates = [
            page.get_by_role("button", name=number, exact=True),
            page.get_by_role("link", name=number, exact=True),
        ] + candidates + [page.get_by_text(number, exact=True)]

    for locator in candidates:
        try:
            if locator.count() == 0:
                continue

            locator.last.click(timeout=5000)
            return True

        except Exception:
            continue

    return False


def wait_text_change(page, before, timeout=20):
    deadline = time.time() + timeout

    while time.time() < deadline:
        page.wait_for_timeout(POLL_MS)
        current = page_text(page)
        check_rate_limit(current)

        if current != before:
            return True

    return False


def click_find(page):
    page.get_by_role("button", name=re.compile("найти", re.I)).first.click()


FOOTER_RE = re.compile(
    r"^(?:Страница\s+\d+\s+из\s+\d+.*|Показать.*|Назад|Вперед|Вперёд|"
    r"Предыдущая|Следующая|\d{1,3})$",
    re.I,
)


def parse_results(text):
    """Возвращает (всего_найдено, [{'kn':..., 'address':...}, ...])."""

    total_match = re.search(r"Найдено результатов:\s*(\d+)", text)
    total = int(total_match.group(1)) if total_match else 0

    if total_match:
        text = text[total_match.end():]

    rows = []
    current = None
    extra = 0

    for line in card_lines(text):
        match = re.match(rf"^({KN_RE})(?!\d)\s*(.*)$", line)

        if match:
            current = {"kn": match.group(1), "address": match.group(2)}
            rows.append(current)
            extra = 0
        elif current is not None and extra < 2:
            if FOOTER_RE.match(line):
                extra = 2
                continue

            current["address"] = (current["address"] + " " + line).strip()
            extra += 1

    for row in rows:
        row["address"] = re.sub(
            r"\s*Страница\s+\d+\s+из\s+\d+.*$", "", row["address"]
        ).strip()

    return total, rows


def wait_for_results(page, previous, limit, probe=False, seq0=None, by_address=False):
    """Возвращает 'results', 'empty' или 'timeout'."""

    deadline = time.time() + limit
    last = None
    stable_since = None

    while time.time() < deadline:
        page.wait_for_timeout(POLL_MS)
        text = page_text(page)

        if (
            by_address and seq0 is not None
            and SEARCH_STATE["seq"] > seq0 and SEARCH_STATE["count"] == 0
        ):
            return "empty"

        if probe and text != previous and "Найдено результатов" not in text:
            if text == last:
                stable_since = stable_since or time.time()

                if time.time() - stable_since >= 1.5 and not _site_error_re.search(text):
                    save_debug(page, "empty_state")
                    return "empty"
            else:
                stable_since = None

        last = text

        check_rate_limit(text)

        if text != previous and "Найдено результатов" in text:
            return "results"

        if text != previous and re.search(
            r"ничего не найдено|не найден|результатов не найдено", text, re.I
        ):
            return "empty"

        if (
            text != previous
            and "Найдено результатов" not in text
            and _site_error_re.search(text)
            and not _site_error_re.search(previous)
        ):
            raise SearchTimeout("Сайт сообщил об ошибке или потерял соединение")

    return "timeout"


def run_search(page, query, probe=False):
    """Поиск через форму. Возвращает (всего_найдено, строки_первой_страницы)."""

    previous = page_text(page)
    seq0 = SEARCH_STATE["seq"]
    by_address = not re.fullmatch(KN_RE, str(query).strip())

    STATS["searches"] += 1
    print(f"      запрос: {query}")

    human_pause(PAUSE_BEFORE_SEARCH)

    box = find_search_box(page)
    box.click()
    box.fill("")
    box.fill(query)

    human_pause(PAUSE_AFTER_TYPING)

    click_find(page)

    state = wait_for_results(
        page, previous, EMPTY_WAIT if probe else SEARCH_TIMEOUT, probe,
        seq0, by_address,
    )

    if state == "timeout" and probe:
        click_find(page)
        state = wait_for_results(
            page, previous, EMPTY_WAIT, probe, seq0, by_address
        )

        if state == "timeout" and search_form_ready(page):
            state = "empty"

    if state == "empty":
        return 0, []

    if state == "timeout":
        save_debug(page, "search_timeout")
        raise SearchTimeout(f"Нет результатов поиска: {query}")

    page.wait_for_timeout(500)

    total, rows = parse_results(page_text(page))

    if not _first_search["done"]:
        _first_search["done"] = True
        save_debug(page, "first_search")

    return total, rows


def click_cadastre(page, kn):
    """Нажимает на кадастровый номер в списке результатов."""

    pattern = re.compile(re.escape(kn) + r"(?!\d)")

    candidates = [
        page.locator("a", has_text=pattern),
        page.get_by_role("link", name=pattern),
        page.get_by_text(pattern),
    ]

    last_error = None

    for locator in candidates:
        try:
            if locator.count() == 0:
                continue

            locator.first.scroll_into_view_if_needed(timeout=5000)
            locator.first.click(timeout=8000)
            return

        except Exception as error:
            last_error = error

    save_debug(page, "click_failed")
    raise SearchTimeout(
        f"Не удалось нажать на номер {kn}: {str(last_error)[:80]}"
    )


def card_is_open(text, kn):
    return "Сведения об объекте" in text and kn in text


def wait_card_open(page, kn, seconds):
    deadline = time.time() + seconds

    while time.time() < deadline:
        page.wait_for_timeout(POLL_MS)
        text = page_text(page)

        check_rate_limit(text)

        if card_is_open(text, kn):
            return text

    return None


_card_closed = {"t": 0.0}


def wait_after_close():
    left = CARD_CLOSE_PAUSE * PACE["factor"] - (time.time() - _card_closed["t"])

    if left > 0:
        time.sleep(left)


CARD_CLICK_WAIT = 8
CARD_CLICK_TRIES = 3


def kn_in(text, kn):
    return re.search(re.escape(kn) + r"(?!\d)", text) is not None


def wait_list_stable(page, kn, seconds=6):
    deadline = time.time() + seconds
    previous = None

    while time.time() < deadline:
        page.wait_for_timeout(POLL_MS)
        text = page_text(page)

        if kn_in(text, kn) and text == previous:
            return True

        previous = text

    return False


def _back_to_results(page):
    for _ in range(3):
        if "Сведения об объекте" not in page_text(page):
            return True

        button = page.get_by_text(
            re.compile("вернуться к результатам", re.I)
        )

        if button.count() > 0:
            button.first.click()
        else:
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

            if "Сведения об объекте" in page_text(page):
                try:
                    page.locator(
                        "[aria-label*='акр' i], [aria-label*='lose' i], "
                        "[class*='close' i]"
                    ).first.click(timeout=3000)
                except Exception:
                    pass

        page.wait_for_timeout(400)

    return "Сведения об объекте" not in page_text(page)


def back_to_results(page):
    had_card = "Сведения об объекте" in page_text(page)
    result = _back_to_results(page)

    if had_card and result:
        _card_closed["t"] = time.time()

    return result


def restore_list(page, query, page_index):
    run_search(page, query)

    for step in range(2, page_index + 1):
        before = page_text(page)

        if not go_next_page(page, step):
            break

        wait_text_change(page, before)
        human_pause(PAUSE_NEXT_PAGE)


def find_on_pages(page, kn, query, max_pages=10):
    run_search(page, query)

    for index in range(1, max_pages + 1):
        if kn_in(page_text(page), kn):
            return True

        before = page_text(page)

        if not go_next_page(page, index + 1):
            return False

        wait_text_change(page, before)
        human_pause(PAUSE_NEXT_PAGE)

    return False


def ensure_on_list(page, kn, query, page_index):
    back_to_results(page)

    if kn_in(page_text(page), kn):
        return

    restore_list(page, query, page_index)

    if kn_in(page_text(page), kn):
        return

    if find_on_pages(page, kn, query):
        return

    run_search(page, kn)

    if not kn_in(page_text(page), kn):
        raise SearchTimeout(f"Номер не найден в выдаче: {kn}")


def open_card(page, kn):
    """Кликает по номеру и ждёт карточку (fallback-режим)."""

    wait_after_close()
    human_pause(PAUSE_BEFORE_CARD)

    for attempt in range(1, CARD_CLICK_TRIES + 1):
        wait_list_stable(page, kn)
        click_cadastre(page, kn)

        text = wait_card_open(page, kn, CARD_CLICK_WAIT)

        if text:
            human_pause(PAUSE_AFTER_CARD)
            return text

        if "Сведения об объекте" in page_text(page):
            back_to_results(page)

        if not kn_in(page_text(page), kn):
            break

        print(f"      клик не открыл карточку, кликаем снова "
              f"({attempt}/{CARD_CLICK_TRIES})")

    save_debug(page, "card_timeout")
    raise SearchTimeout(f"Карточка не открылась: {kn}")


def wait_for_user(message):
    print("\n" + message)
    input("Когда страница поиска снова открыта, нажмите Enter...")


def with_retry(page, action):
    for attempt in range(1, AUTO_RETRIES + 1):
        try:
            return action()

        except RateLimited:
            STATS["rate_limited"] += 1
            stat_fail("слишком много")
            PACE["factor"] = min(PACE["factor"] * 2, 8)
            print(f"   замедляемся: паузы теперь x{PACE['factor']:.1f}")
            wait = RATE_LIMIT_WAIT * attempt
            print(
                f"\nРосреестр просит подождать (слишком много попыток). "
                f"Ждём {wait} сек, попытка {attempt}/{AUTO_RETRIES}..."
            )

            for left in range(wait, 0, -10):
                print(f"   осталось {left} сек", end="\r")
                time.sleep(min(10, left))

            print(" " * 30, end="\r")
            reload_site(page)

        except (FatalStop, KeyboardInterrupt):
            raise

        except Exception as error:
            if "has been closed" in str(error).lower():
                raise

            quick = attempt == 1
            stat_fail(error)
            STATS["retries"] += 1

            wait = QUICK_RETRY_WAIT if quick else AUTO_RECOVER_WAITS[
                min(attempt - 2, len(AUTO_RECOVER_WAITS) - 1)
            ]

            print(
                f"\n   сбой ({str(error)[:70]}). "
                + (
                    f"Повторяем сразу, попытка {attempt}/{AUTO_RETRIES}"
                    if quick else
                    f"Перезагрузка через {wait} сек, "
                    f"попытка {attempt}/{AUTO_RETRIES}"
                )
            )
            time.sleep(wait)

            if not quick or not search_form_ready(page):
                reload_site(page)

                if not search_form_ready(page):
                    wait_until_site_ready(page)

    wait_until_site_ready(page)
    return action()


# ==========================================
# ЗАПРОСЫ К ПОИСКУ И ПОДПИСИ ПОМЕЩЕНИЙ
# ==========================================

def build_query(address, kind=None, number=None):
    """Шаблон запроса как в Росреестре:
    Кировская область, г.о. город Киров, г Киров, ул Ленина, д. 190, кв. 1
    """

    segments = [s.strip() for s in address.split(",") if s.strip()]
    segments = [s for s in segments if not re.fullmatch(r"\d{6}", s)]

    city_index = None

    for index, segment in enumerate(segments):
        if re.match(r"^г\.?\s", segment, re.I):
            city_index = index
            break

    if city_index is None:
        parts = segments
    else:
        head = []

        for segment in segments[:city_index]:
            region = re.match(r"^(?:обл|область)\.?\s+(.+)$", segment, re.I)
            krai = re.match(r"^край\.?\s+(.+)$", segment, re.I)
            resp = re.match(r"^респ(?:ублика)?\.?\s+(.+)$", segment, re.I)

            if region:
                head.append(f"{region.group(1)} область")
            elif krai:
                head.append(f"{krai.group(1)} край")
            elif resp:
                head.append(f"Республика {resp.group(1)}")
            else:
                head.append(segment)

        city = re.sub(r"^г\.?\s*", "", segments[city_index], flags=re.I)
        parts = head + [f"г.о. город {city}"] + segments[city_index:]

    if SPACE_BEFORE_HOUSE_COMMA:
        parts = list(parts)

        for index, part in enumerate(parts):
            if index > 0 and re.match(r"^д(?:ом)?\.?\s*\d", part, re.I):
                parts[index - 1] = parts[index - 1] + " "

    query = ", ".join(parts)

    if kind and number is not None:
        query += f", {kind} {number}"

    return query


def house_query_from_card(building):
    address = normalize(building.get("address") or "")

    return address or None


def strip_country(address):
    return re.sub(r"^Российская Федерация,?\s*", "", address or "", flags=re.I)


def query_for(house, kind=None, number=None):
    base = house.get("rr_address")

    if base:
        query = base

        if kind and number is not None:
            query += f", {kind} {number}"

        return query

    return build_query(house["address"], kind, number)


def label_has(label, kind, number):
    """Точное совпадение номера + буквенные варианты: '7' находит 'кв. 7а',
    но '1' НЕ находит 'кв. 12'."""

    number = str(number).lower()

    for ident in re.findall(
        rf"{re.escape(kind)} ([^\s,]+)", label or "", re.I
    ):
        ident = ident.lower()

        if ident == number:
            return True

        if ident.startswith(number) and len(ident) > len(number) \
                and not ident[len(number)].isdigit():
            return True

    return False


def label_numbers(label, kind):
    return {
        int(n) for n in re.findall(rf"{re.escape(kind)} (\d+)", label or "")
    }


def flat_ids(label):
    return {m.lower() for m in re.findall(r"кв\. ([^,\s]+)", label or "")}


def flat_count(labels):
    ids = set()

    for label in labels:
        ids |= flat_ids(label)

    return len(ids)


def premise_type(label):
    label = label or ""

    if "ком." in label:
        return "Комната"
    if "кв." in label:
        return "Квартира"
    if "пом." in label:
        return "Помещение"

    return None


def summarize(labels):
    flats = flat_count(labels)
    rooms = sum(1 for l in labels if "ком." in (l or ""))
    premises = sum(1 for l in labels if "пом." in (l or ""))

    return f"квартир {flats}, помещений {premises}, комнат {rooms}"


def add_comment(rec, text):
    rec["comment"] = (rec.get("comment") + "; " if rec.get("comment") else "") + text


def mark_big(rec, count):
    rec["big"] = True
    rec["comment"] = (
        f"Пропущен: больше {MAX_FLATS} квартир "
        f"(в Росреестре не меньше {count})"
    )
    print(
        f"   !!! больше {MAX_FLATS} квартир (найдено {count}+) — "
        "дом пропускаем"
    )


# ==========================================
# ПОЛНОТА ДАННЫХ
# ==========================================

def expected_units(house):
    for value in (house.get("residential"), house.get("apartments")):
        if isinstance(value, int) and value > 0:
            return value

    return 0


def found_units(rec):
    """(найдено_жилых, номера_квартир, не_открылись_карточки)."""

    flats = set()
    rooms_only = 0
    bad = 0

    for item in (rec.get("premises") or {}).values():
        if not item.get("card_ok"):
            bad += 1
            continue

        state = (item.get("state") or "").strip()

        if state and state.lower() != "актуально":
            continue

        label = item.get("label") or ""
        ids = flat_ids(label)

        if ids:
            flats |= ids
        elif "ком." in label:
            rooms_only += 1

    return len(flats) + rooms_only, flats, bad


def completeness(house, rec):
    """(полные_ли, причина_неполноты, не_найденные_номера).

    Дом НЕ объявляется полным, если: выдача была обрезана лимитом 100,
    были ошибки API/сервера, карточки не открылись, найдено меньше
    ожидаемого по ГИС ЖКХ, поиск завершился по сбою.
    """

    building = rec.get("building") or {}

    if building.get("verdict") != "МКД" or rec.get("big"):
        return True, "", []

    reasons = []
    missing = []

    diag = rec.get("diag") or {}

    if diag.get("finish_reason") in ("ошибка", "сервис недоступен", "лимит 100"):
        reasons.append(
            f"поиск завершён не полностью ({diag.get('finish_reason')})"
        )

    if diag.get("capped"):
        reasons.append("выдача обрезана лимитом 100")

    if diag.get("errors"):
        reasons.append(f"ошибок во время поиска: {diag['errors']}")

    expected = expected_units(house)
    found, flats, bad = found_units(rec)

    if expected:
        if found < expected:
            reasons.append(f"найдено {found} из {expected} по ГИС ЖКХ")

            numbers = {int(x) for x in flats if str(x).isdigit()}
            missing = [n for n in range(1, expected + 1) if n not in numbers]
    elif not rec.get("list_complete"):
        reasons.append(
            "число помещений в ГИС ЖКХ неизвестно, список Росреестра неполный"
        )

    if bad:
        reasons.append(f"не открылись карточки: {bad}")

    if rec.get("needs_retry"):
        reasons.append("есть результаты, требующие повторной проверки")

    return (not reasons), "; ".join(reasons), missing


# ==========================================
# ОРКЕСТРАЦИЯ: ЭТАПЫ А-Г
# ==========================================

def engine_mode():
    return "api" if (_engine["api_ok"] and USE_API) else "ui"


def quarter_of(kn):
    return str(kn or "").split(":")[:3]


# ---------- Этап А: поиск здания ----------

def building_from_element(el, gis, cadastre):
    building = card_from_api_element(el)
    rr = parse_rr_address(building.get("address") or "")
    building["address_match"] = match_house(gis, rr)

    if building.get("address_fields"):
        level, _ = match_house_fields(gis, building["address_fields"])
        if level == "EXACT":
            building["address_match"] = True

    if building.get("kn") and cadastre and building["kn"] != cadastre:
        building["address_match"] = False

    building["verdict"] = building_verdict(building)
    return building


def api_find_building(page, house, gis, diag):
    cadastre = house["cadastre"]

    if cadastre and re.fullmatch(KN_RE, cadastre):
        try:
            el = fetch_card_api(page, cadastre)
        except (RateLimited, CaptchaRequired):
            raise
        except (ServerBusy, ConnectionLost, SearchTimeout, UnreadableAnswer):
            el = None

        if el is not None:
            return building_from_element(el, gis, cadastre)

    print("   по кадастровому номеру ничего - ищем по адресу дома...")

    terms = [build_query(house["address"])]
    searches_ok = 0
    searches_failed = 0

    for _ in range(ADDRESS_PASSES):
        for term in list(terms):
            try:
                rows, capped = search_address_api(page, term)
            except (RateLimited, CaptchaRequired):
                raise
            except (ServerBusy, ConnectionLost, SearchTimeout,
                    UnreadableAnswer):
                searches_failed += 1
                diag["errors"] = diag.get("errors", 0) + 1
                continue

            searches_ok += 1
            diag["capped"] = diag.get("capped") or capped

            for row in rows:
                rr = parse_rr_address(row["address"])

                if rr["label"]:
                    continue

                if not match_house(gis, rr):
                    continue

                try:
                    el = fetch_card_api(page, row["kn"])
                except (RateLimited, CaptchaRequired):
                    raise
                except (ServerBusy, ConnectionLost, SearchTimeout, UnreadableAnswer):
                    continue

                if el is not None:
                    return building_from_element(el, gis, row["kn"])

        terms.append(build_query(house["address"]))

    if searches_ok == 0 and searches_failed:
        raise ServerBusy(
            f"поиск здания: сервис недоступен ({searches_failed} сбоев)"
        )

    return None


def ui_find_building(page, house, gis, diag):
    cadastre = house["cadastre"]
    query_used = cadastre
    page_index = 1

    total, rows = with_retry(page, lambda: run_search(page, cadastre))
    row = next((r for r in rows if r["kn"] == cadastre), None)

    if row is None:
        print("   по кадастровому номеру ничего - ищем по адресу дома...")
        query_used = build_query(house["address"])
        row, page_index = ui_find_building_by_address(page, gis, query_used)

    if row is None:
        return None

    kn_rr = row["kn"]

    def open_building():
        ensure_on_list(page, kn_rr, query_used, page_index)
        return open_card(page, kn_rr)

    card_text = with_retry(page, open_building)
    building = parse_card(card_text)
    building["status"] = "ok"

    rr = parse_rr_address(building.get("address") or "")
    building["address_match"] = match_house(gis, rr)

    if building.get("kn") != kn_rr:
        building["address_match"] = False

    building["verdict"] = building_verdict(building)
    back_to_results(page)
    return building


def ui_find_building_by_address(page, gis, query, max_pages=3):
    total, rows = with_retry(page, lambda: run_search(page, query))

    seen = set()
    page_index = 1

    while True:
        new_rows = [r for r in rows if r["kn"] not in seen]

        if not new_rows:
            break

        for row in new_rows:
            seen.add(row["kn"])

            rr = parse_rr_address(row["address"])

            if not rr["label"] and match_house(gis, rr):
                return row, page_index

        if len(seen) >= min(total, RESULTS_CAP) or page_index >= max_pages:
            break

        before = page_text(page)

        if not go_next_page(page, page_index + 1):
            break

        if not wait_text_change(page, before):
            break

        human_pause(PAUSE_NEXT_PAGE)
        page_index += 1
        _, rows = parse_results(page_text(page))

    return None, 1


# ---------- Этап Б: список помещений ----------

def classify_listing_rows(rows, gis, quarter, rec, probe=False):
    """Раскладывает строки выдачи на matched / other_quarter / rejected."""

    matched = {}
    other_quarter = {}
    rejected = {}
    samples = []

    for row in rows:
        rr = parse_rr_address(row["address"])

        if rr["label"] and match_house(gis, rr):
            if (
                STRICT_SAME_QUARTER and quarter and not probe
                and row["kn"].split(":")[:3] != quarter
            ):
                rec.setdefault("skipped", {})[row["kn"]] = rr["label"]
                other_quarter[row["kn"]] = rr["label"]
                continue

            matched[row["kn"]] = rr["label"]
        else:
            level, reason = match_level(gis, rr)
            reason = reason or "нет квартиры в адресе"
            rejected[reason] = rejected.get(reason, 0) + 1

            if (
                len(samples) < 4 and not probe
                and rr["house"] == (gis or {}).get("house")
            ):
                samples.append(f"{row['kn']}  {row['address']}  [{reason}]")

    return matched, other_quarter, rejected, samples


def api_collect_listing(page, house, gis, rec, diag, query, probe=False,
                        passes=None):
    """Список помещений через API: ADDRESS_PASSES проходов объединяются
    (выдача нестабильна, при обрезке на 100 разные проходы дают разные
    подмножества). Никогда не читаем один проход как полный список."""

    passes = passes or (ADDRESS_PASSES if not probe else 1)
    quarter = (house.get("rr_cadastre") or house["cadastre"] or "").split(":")[:3]

    seen = {}
    capped = False
    errors = 0

    for _ in range(passes):
        try:
            rows, pass_capped = search_address_api(page, query)
        except (RateLimited, CaptchaRequired):
            raise
        except (ServerBusy, ConnectionLost, SearchTimeout, UnreadableAnswer):
            errors += 1
            continue

        capped = capped or pass_capped

        for row in rows:
            seen.setdefault(row["kn"], row)

    diag["capped"] = diag.get("capped") or capped
    diag["errors"] = diag.get("errors", 0) + errors

    if errors and errors == passes:
        raise ServerBusy(
            f"список помещений: все {passes} прохода поиска не удались"
        )

    matched, other_quarter, rejected, samples = classify_listing_rows(
        list(seen.values()), gis, quarter, rec, probe
    )

    if rejected and not probe:
        print("      отклонено адресов:", "; ".join(
            f"{reason} — {count}" for reason, count in sorted(
                rejected.items(), key=lambda item: -item[1]
            )
        ))

        for sample in samples:
            print("        пример:", sample)

    return {
        "query": query,
        "total": len(seen),
        "seen": len(seen),
        "matched": matched,
        "other_quarter": other_quarter,
        "capped": capped,
        "errors": errors,
    }


def ui_list_pages(page, house, gis, rec, query, probe=False, max_pages=None,
                  stop_at=None, max_flats=None):
    """Список помещений через UI (перенос list_pages из оригинала)."""

    max_pages = max_pages or MAX_RESULT_PAGES

    total, rows = with_retry(page, lambda: run_search(page, query, probe))

    quarter = (house.get("rr_cadastre") or house["cadastre"] or "").split(":")[:3]

    seen = set()
    matched = {}
    pages = {}
    other_quarter = {}
    page_index = 1
    empty_pages = 0
    big = False

    while True:
        new_rows = [r for r in rows if r["kn"] not in seen]

        if not new_rows:
            break

        for row in new_rows:
            seen.add(row["kn"])

        found_here = []

        for row in new_rows:
            rr = parse_rr_address(row["address"])

            if rr["label"] and match_house(gis, rr):
                if STRICT_SAME_QUARTER and quarter and row["kn"].split(":")[:3] != quarter:
                    rec.setdefault("skipped", {})[row["kn"]] = rr["label"]
                    other_quarter[row["kn"]] = (rr["label"], page_index)
                    continue

                matched[row["kn"]] = rr["label"]
                found_here.append((row["kn"], rr["label"]))

        pages[page_index] = found_here

        if max_flats and flat_count(matched.values()) > max_flats:
            big = True
            break

        if stop_at and len(matched) >= stop_at:
            break

        empty_pages = 0 if found_here else empty_pages + 1

        if empty_pages >= 3 and total < RESULTS_CAP:
            break

        if len(seen) >= min(total, RESULTS_CAP) or page_index >= max_pages:
            break

        before = page_text(page)

        if not go_next_page(page, page_index + 1):
            break

        if not wait_text_change(page, before):
            break

        human_pause(PAUSE_NEXT_PAGE)

        page_index += 1
        _, rows = parse_results(page_text(page))

    if stop_at and not probe and len(matched) < stop_at and other_quarter:
        need = stop_at - len(matched)

        for kn, (label, idx) in list(other_quarter.items())[:need]:
            matched[kn] = label
            pages.setdefault(idx, []).append((kn, label))
            rec.get("skipped", {}).pop(kn, None)

        print(
            f"      добрано из других кадастровых кварталов: "
            f"{min(need, len(other_quarter))} (помечены в таблице)"
        )

    return {
        "query": query,
        "total": total,
        "seen": len(seen),
        "matched": matched,
        "pages": pages,
        "last_page": page_index,
        "big": big,
        "other_quarter": other_quarter,
        "capped": total >= RESULTS_CAP,
    }


# ---------- Открытие карточек ----------

def api_process_premise(page, house, gis, rec, kn, label, diag, absent_ok=False):
    """Карточка помещения через API. absent_ok: отсутствие номера в ЕГРН
    для пробы пропущенного номера — норма, а не ошибка."""

    try:
        el = fetch_card_api(page, kn)
    except (RateLimited, CaptchaRequired):
        raise
    except (ServerBusy, ConnectionLost, SearchTimeout, UnreadableAnswer) as error:
        rec["premises"][kn] = {
            "kn": kn,
            "label": label,
            "card_ok": False,
            "check": f"Ошибка: {str(error)[:120]}",
            "rights": [],
            "restrictions": [],
            "needs_retry": True,
        }
        diag["errors"] = diag.get("errors", 0) + 1
        print(f"   {label}: ошибка — {str(error)[:80]}")
        return False

    if el is None:
        if absent_ok:
            rec["premises"].pop(kn, None)
            return False

        rec["premises"][kn] = {
            "kn": kn,
            "label": label,
            "card_ok": False,
            "check": "Ошибка: номер был в выдаче, карточка не получена",
            "rights": [],
            "restrictions": [],
            "needs_retry": True,
        }
        diag["errors"] = diag.get("errors", 0) + 1
        print(f"   {label}: карточка не получена (пустой ответ ЕГРН)")
        return False

    card = card_from_api_element(el)
    data = card_to_record(card, label)
    state = (data.get("state") or "").strip()

    rr = parse_rr_address(data.get("address") or "")
    level, reason = match_level(gis, rr)

    if not level and card.get("address_fields"):
        level, reason = match_house_fields(gis, card["address_fields"])

    data["address_match"] = bool(
        data.get("kn") == kn and level in ("EXACT", "STRONG")
    )
    data["address_reason"] = reason
    data["card_ok"] = data["address_match"]
    data["check"] = "OK" if data["address_match"] else f"Адрес не совпал: {reason}"
    data["active"] = (not state) or state.lower() == "актуально"
    data["parse_v"] = 3

    if not data["active"]:
        data["rights"] = []
        data["restrictions"] = []
        print(f"   {label}: {state} — пропускаем")
    else:
        print(
            f"   {label}: {data.get('area')} кв.м, "
            f"записей о праве: {len(card['rights'])}"
        )

    STATS["cards"] += 1
    diag["cards"] = diag.get("cards", 0) + 1
    rec["premises"][kn] = data
    PACE["factor"] = max(1.0, PACE["factor"] * 0.97)
    return True


def ui_process_premise(page, house, gis, rec, kn, label, query, page_index, diag):
    try:
        def open_premise():
            ensure_on_list(page, kn, query, page_index)
            return open_card(page, kn)

        card_text = with_retry(page, open_premise)
        data = parse_card(card_text)
        data["status"] = "ok"

        state = (data.get("state") or "").strip()
        rr = parse_rr_address(data.get("address") or "")

        data["label"] = label
        data["address_match"] = bool(
            data.get("kn") == kn
            and match_house(gis, rr)
            and rr["label"] == label
        )
        data["card_ok"] = data["address_match"]
        data["check"] = "OK" if data["address_match"] else "Адрес не совпал"
        data["active"] = (not state) or state.lower() == "актуально"

        if not data["active"]:
            data["rights"] = []
            data["restrictions"] = []
            print(f"   {label}: {state} — пропускаем")
        else:
            print(
                f"   {label}: {data.get('area')} кв.м, "
                f"записей о праве: {len(data['rights'])}"
            )

        STATS["cards"] += 1
        diag["cards"] = diag.get("cards", 0) + 1
        rec["premises"][kn] = data
        PACE["factor"] = max(1.0, PACE["factor"] * 0.97)
        back_to_results(page)

    except Exception as error:
        if isinstance(error, FatalStop) or "has been closed" in str(error).lower():
            raise

        rec["premises"][kn] = {
            "kn": kn,
            "label": label,
            "card_ok": False,
            "check": f"Ошибка: {str(error)[:120]}",
            "rights": [],
            "restrictions": [],
            "needs_retry": True,
        }
        diag["errors"] = diag.get("errors", 0) + 1
        print(f"   {label}: ошибка — {str(error)[:80]}")


def open_listed(page, house, gis, rec, listing, diag):
    """Открывает карточки из уже прочитанного списка (UI-режим)."""

    query = listing["query"]
    last = listing["last_page"]

    order = [last] + [p for p in sorted(listing["pages"]) if p != last]

    for page_index in order:
        for kn, label in listing["pages"].get(page_index, []):
            existing = rec["premises"].get(kn)

            if (
                existing and existing.get("card_ok")
                and existing.get("parse_v") == 2
            ):
                continue

            ui_process_premise(
                page, house, gis, rec, kn, label, query, page_index, diag
            )


def ui_scan_list(page, house, gis, rec, query, diag):
    listing = ui_list_pages(page, house, gis, rec, query, probe=True, max_pages=1)
    open_listed(page, house, gis, rec, listing, diag)

    return listing["matched"]


def stage_needed(kind, start, labels, house):
    """Нужен ли поиск по одному для этого вида помещений."""

    if not STAGE_ONLY_IF_SEEN or kind == "кв.":
        return True

    numbers = set()

    for label in labels.values():
        numbers |= label_numbers(label, kind)

    if start >= 1000:
        return any(n >= 1000 for n in numbers)

    if numbers:
        return True

    nonres = house.get("nonresidential")

    return kind == "пом." and isinstance(nonres, int) and nonres > 0


def flag_premises(rec, house):
    """Помечает подозрительные помещения: дубли, чужой квартал, лишний номер."""

    premises = rec.get("premises") or {}
    quarter = (house.get("rr_cadastre") or house["cadastre"] or "").split(":")[:3]

    expected = 0

    for field in ("residential", "nonresidential"):
        if isinstance(house.get(field), int):
            expected += house[field]

    by_label = {}

    for kn, item in premises.items():
        if item.get("card_ok"):
            by_label.setdefault(item.get("label"), []).append(kn)

    for kn, item in premises.items():
        if not item.get("card_ok"):
            continue

        notes = []

        if len(by_label.get(item.get("label"), [])) > 1:
            notes.append(
                "Несколько объектов с одним адресом "
                "(возможно, другое здание с тем же номером дома)"
            )

        if quarter and kn.split(":")[:3] != quarter:
            notes.append("Другой кадастровый квартал")

        number = premise_number(item.get("label"))

        if expected and number and number > expected:
            notes.append("Номер больше числа помещений по ГИС ЖКХ")

        item["check"] = "; ".join(notes) if notes else "OK"


def process_house(page, house, rec, diag):
    """Полная обработка дома: этапы А-Г. Режим API или UI."""

    started = time.time()
    mode = engine_mode()
    gis = parse_gis_address(house["address"])
    cadastre = house["cadastre"]

    rec["comment"] = ""
    rec["skipped"] = {}
    rec["big"] = False
    rec["listed_count"] = None
    rec["rules"] = RULES_VERSION
    rec["diag"] = diag.to_dict()

    if gis is None:
        rec["building"] = {
            "status": "error",
            "message": "Не удалось разобрать адрес из ГИС ЖКХ",
        }
        diag.finish("ошибка разбора адреса")
        return

    if (
        SKIP_BY_GIS_COUNT
        and isinstance(house["residential"], int)
        and house["residential"] > MAX_FLATS
    ):
        rec["comment"] = (
            f"Пропущен: по ГИС ЖКХ {house['residential']} жилых помещений "
            f"(больше {MAX_FLATS})"
        )
        rec["big"] = True
        rec["building"] = rec.get("building") or {
            "status": "skipped", "message": rec["comment"],
        }
        print("   ", rec["comment"])
        diag.finish("пропущен (крупный дом)")
        return

    # ---------- Этап А. Здание ----------

    with diag.stage("building"):
        if (rec.get("building") or {}).get("status") != "ok":
            if mode == "api":
                building = api_find_building(page, house, gis, diag)
            else:
                building = ui_find_building(page, house, gis, diag)

            if building is None:
                rec["building"] = {
                    "status": "not_found",
                    "message": (
                        "Здание не найдено ни по кадастровому номеру, "
                        "ни по адресу"
                    ),
                }
                print("   здание НЕ найдено в Росреестре")
                diag.finish("здание не найдено")
                return

            rec["building"] = building

    building = rec["building"]
    building["verdict"] = building_verdict(building)
    house["rr_cadastre"] = building.get("kn") or cadastre

    if building.get("kn") and cadastre and building["kn"] != cadastre:
        add_comment(
            rec,
            f"Найден по адресу: кадастровый номер в Росреестре "
            f"{building['kn']} (в ГИС ЖКХ {cadastre})"
        )

    print(
        "   здание найдено:", building.get("purpose"),
        "| адрес совпал:", "да" if building.get("address_match") else "НЕТ",
    )

    save_cache_hook(rec)

    if building["verdict"] != "МКД":
        add_comment(rec, stop_reason(building))
        print("   ", stop_reason(building), "- дом пропускаем")
        diag.finish("не МКД")
        return

    # ---------- Этап Б. Список помещений ----------

    house["rr_address"] = house_query_from_card(building)

    if house["rr_address"]:
        print("   адрес дома из Росреестра:", house["rr_address"])

    expected = sum(
        value for value in (house["residential"], house.get("nonresidential"))
        if isinstance(value, int)
    )

    if not expected and isinstance(house.get("apartments"), int):
        expected = house["apartments"]

    rec.setdefault("premises", {})
    all_matched = {}

    with diag.stage("list"):
        if mode == "api":
            house_query = query_for(house)
            listing = api_collect_listing(page, house, gis, rec, diag, house_query)

            if listing["total"] == 0 and house.get("rr_address"):
                for variant in (strip_country(house["rr_address"]), None):
                    if listing["total"] != 0:
                        break

                    print(
                        "   по полному адресу ничего - пробуем",
                        "без «Российская Федерация»" if variant else "шаблон ГИС ЖКХ",
                    )

                    house["rr_address"] = variant
                    house_query = query_for(house)
                    listing = api_collect_listing(
                        page, house, gis, rec, diag, house_query
                    )

            matched = listing["matched"]

            # помещения в другом кадастровом квартале: адрес совпал — добираем,
            # если недобрали до ожидаемого (как в UI-движке)
            if listing.get("other_quarter") and expected \
                    and len(matched) < expected:
                need = expected - len(matched)

                for kn_o, label_o in list(
                    listing["other_quarter"].items()
                )[:need]:
                    matched[kn_o] = label_o
                    rec.get("skipped", {}).pop(kn_o, None)

            all_matched = dict(matched)

            if listing["capped"] and len(matched) < (expected or RESULTS_CAP):
                print(
                    "   !!! выдача обрезана лимитом 100 — часть квартир "
                    "будем искать по номерам"
                )
        else:
            house_query = query_for(house)
            listing = ui_list_pages(
                page, house, gis, rec, house_query,
                stop_at=expected or None, max_flats=MAX_FLATS,
            )
            matched = listing["matched"]
            all_matched = dict(matched)

    rec["listed_count"] = len(matched)

    print(
        f"   поиск вернул {listing['total']}, помещений этого дома "
        f"в списке: {len(matched)} (по ГИС ЖКХ: {expected or '?'})"
    )

    if matched:
        print("   в списке найдено:", summarize(matched.values()))

    if listing.get("big"):
        mark_big(rec, flat_count(matched.values()))
        diag.finish(f"больше {MAX_FLATS} квартир")
        return

    # проверка «кв. 31» — больше ли 30 квартир
    if max(expected, len(matched)) >= BIG_PROBE_FROM and rec["listed_count"] == len(matched):
        with diag.stage("probe"):
            number = MAX_FLATS + 1
            found_probe = probe_number(
                page, house, gis, rec, diag, "кв.", number,
                all_matched, probe_only=True,
            )

        if found_probe:
            rec["listed_count"] = max(len(matched), number)
            mark_big(rec, number)
            diag.finish(f"больше {MAX_FLATS} квартир")
            return

    # ---------- Открытие карточек из списка ----------

    with diag.stage("cards"):
        if mode == "api":
            kns = [
                kn for kn, label in matched.items()
                if not (rec["premises"].get(kn, {}).get("card_ok")
                        and rec["premises"].get(kn, {}).get("parse_v") == 3)
            ]

            for kn in kns:
                api_process_premise(page, house, gis, rec, kn, matched[kn], diag)
                save_cache_hook(rec)
        else:
            open_listed(page, house, gis, rec, listing, diag)

    # ---------- Этап В. Добор пропущенных ----------

    list_complete = (
        not listing.get("capped")
        and listing["seen"] >= listing["total"]
        and diag.get("errors", 0) == 0
    ) if mode == "api" else (
        listing["total"] < RESULTS_CAP
        and listing["seen"] >= listing["total"]
    )
    rec["list_complete"] = list_complete

    expected_res = expected_units(house)

    with diag.stage("probe"):
        try:
            if expected_res:
                probe_missing_numbers(
                    page, house, gis, rec, diag, mode, all_matched,
                    expected_res, list_complete,
                )

            # пом./ком. по одному, когда число помещений неизвестно
            sum_expected = sum(
                v for v in (house["residential"], house.get("nonresidential"))
                if isinstance(v, int)
            ) or (house["apartments"] if isinstance(house.get("apartments"), int) else 0)
            unknown = not sum_expected

            if unknown:
                incomplete = ALWAYS_PROBE_NEXT or not list_complete
            else:
                incomplete = len(all_matched) < sum_expected

            if incomplete:
                for kind, start in SEARCH_STAGES:
                    if kind == "кв." and not unknown:
                        continue

                    if not stage_needed(kind, start, all_matched, house):
                        print(f"   пропускаем «{kind}» с {start}: в списке таких нет")
                        continue

                    if not unknown and len(all_matched) >= sum_expected:
                        break

                    probe_stage_range(
                        page, house, gis, rec, diag, mode,
                        all_matched, kind, start, unknown, sum_expected,
                    )

        except _BigHouse as big:
            rec["listed_count"] = len(all_matched)
            mark_big(rec, big.args[0])
            diag.finish(f"больше {MAX_FLATS} квартир")
            return

    rec["listed_count"] = len(all_matched)

    if not all_matched and not rec.get("comment"):
        rec["comment"] = (
            f"Помещения не найдены (поиск вернул {listing['total']}, "
            f"прочитано {listing['seen']})"
        )
        diag.finish("помещения не найдены")

    if diag.get("errors"):
        diag.finish("ошибка")
    elif diag.get("capped"):
        diag.finish("лимит 100")
    elif not diag.finish_reason:
        diag.finish("все помещения собраны")


def probe_missing_numbers(page, house, gis, rec, diag, mode, all_matched,
                          expected_res, list_complete):
    """Этап В: перебор пропущенных номеров квартир.

    Когда число помещений известно, стоп по пустой серии НЕ применяется:
    перебираем все номера 1..expected + MISSING_EXTRA, пока не наберём
    expected или не исчерпаем MAX_MISSING_PROBES проб.
    """

    def short():
        return found_units(rec)[0] < expected_res

    if short():
        print(
            f"   найдено {found_units(rec)[0]} из {expected_res} - "
            f"ищем недостающие номера"
        )

    probes = {"n": 0}

    have = set()

    for label in all_matched.values():
        have |= label_numbers(label, "кв.")

    targets = [n for n in range(1, expected_res + 1) if n not in have]
    targets += list(range(expected_res + 1, expected_res + 1 + MISSING_EXTRA))

    for number in targets:
        if not short() or probes["n"] >= MAX_MISSING_PROBES:
            break

        probes["n"] += 1
        probe_number(page, house, gis, rec, diag, "кв.", number, all_matched)

    if short():
        numbers = set()

        for label in (rec.get("premises") or {}).values():
            if item_ok(label):
                numbers |= label_numbers(label.get("label"), "кв.")

        lost = [n for n in range(1, expected_res + 1) if n not in numbers]
        print(f"   !!! не нашли {len(lost)} шт.: {lost[:30]}")
        diag["missing_numbers"] = lost[:60]


def item_ok(item):
    return bool(item and item.get("card_ok"))


def probe_stage_range(page, house, gis, rec, diag, mode, all_matched, kind,
                      start, unknown, sum_expected):
    """Перебор номеров одного вида (пом./ком.), когда список неполный."""

    empty_streak = 0

    for number in range(start, start + MAX_FLAT_FALLBACK):
        if not unknown and len(all_matched) >= sum_expected:
            break

        have = set()

        for label in all_matched.values():
            have |= label_numbers(label, kind)

        if number in have:
            empty_streak = 0
            continue

        found = probe_number(
            page, house, gis, rec, diag, kind, number, all_matched
        )

        if found:
            empty_streak = 0
        else:
            empty_streak += 1

            if empty_streak >= EMPTY_STREAK_LIMIT:
                print(
                    f"   {EMPTY_STREAK_LIMIT} пустых выдач подряд "
                    f"для «{kind}» с {start}, переходим дальше"
                )
                break


def probe_number(page, house, gis, rec, diag, kind, number, all_matched=None,
                 probe_only=False):
    """Ищет одно помещение по номеру. Возвращает True, если нашли и
    карточка обработана. При probe_only достаточно увидеть строку в выдаче."""

    all_matched = all_matched if all_matched is not None else {}

    queries = [query_for(house, kind, number)]

    if ALT_QUERY_VARIANTS:
        alt = build_query(house["address"], kind, number)

        if alt not in queries:
            queries.append(alt)

    mode = engine_mode()

    for query in queries:
        if mode == "api":
            try:
                listing = api_collect_listing(
                    page, house, gis, rec, diag, query, probe=True
                )
            except (RateLimited, CaptchaRequired):
                raise
            except (ServerBusy, ConnectionLost, SearchTimeout, UnreadableAnswer):
                diag["errors"] = diag.get("errors", 0) + 1
                continue
        else:
            try:
                listing = ui_list_pages(
                    page, house, gis, rec, query, probe=True, max_pages=1
                )
            except Exception as error:
                if isinstance(error, FatalStop):
                    raise
                continue

        found = False

        for kn, label in listing["matched"].items():
            if not label_has(label, kind, number):
                continue

            found = True

            if probe_only:
                continue

            existing = rec["premises"].get(kn)

            if existing and existing.get("card_ok"):
                continue

            if mode == "api":
                api_process_premise(page, house, gis, rec, kn, label, diag)
            else:
                ui_process_premise(
                    page, house, gis, rec, kn, label, query, 1, diag
                )

            new_label = (rec["premises"].get(kn) or {}).get("label")

            if new_label:
                all_matched[kn] = new_label

        if found:
            flats_now = flat_count(all_matched.values())

            if flats_now > MAX_FLATS and not probe_only:
                raise _BigHouse(flats_now)

            return True

    return False


_cache_saver = {"fn": None}


def save_cache_hook(_rec):
    if _cache_saver["fn"]:
        _cache_saver["fn"]()


# ==========================================
# ЧТЕНИЕ ВХОДНОГО ФАЙЛА
# ==========================================

def strip_index(address):
    """Убирает почтовый индекс в начале адреса: '610000, г Киров...' -> 'г Киров...'"""

    return re.sub(r"^\s*\d{6}\s*[,;]?\s*", "", str(address)).strip()


def read_houses():
    if not Path(INPUT_FILE).exists():
        raise FileNotFoundError(f"Не найден файл {INPUT_FILE}")

    workbook = load_workbook(INPUT_FILE, read_only=True, data_only=True)

    if INPUT_SHEET not in workbook.sheetnames:
        raise ValueError(f"В файле нет листа «{INPUT_SHEET}»")

    rows = workbook[INPUT_SHEET].iter_rows(values_only=True)
    header = next(rows)
    index = {name: i for i, name in enumerate(header)}

    houses = []

    for row in rows:
        address = row[index["Адрес"]]

        if not address:
            continue

        cadastre = row[index["Кадастровый номер"]]

        raw = {
            name: (row[i] if i < len(row) else None)
            for name, i in index.items() if name
        }

        houses.append({
            "raw": raw,
            "address": strip_index(address),
            "cadastre": cadastre.strip() if isinstance(cadastre, str) else None,
            "residential": row[index["Жилых помещений"]],
            "nonresidential": row[index["Нежилых помещений"]]
            if "Нежилых помещений" in index else None,
            "apartments": row[index["Количество квартир"]]
            if "Количество квартир" in index else None,
        })

    workbook.close()
    return houses


# ==========================================
# КЭШ
# ==========================================

CACHE_VERSION = 6


def load_cache():
    """Загружает кэш. Кэш версии 5 МИГРИРУЕТ, а не сбрасывается: записи
    сохраняются, но помечены rules=5, поэтому needs_retry отправит на
    перепроверку только дома с неполными данными (полные не пересобираются)."""

    if Path(CACHE_FILE).exists():
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as file:
                cache = json.load(file)

            version = cache.get("_version")

            if version == CACHE_VERSION:
                return cache

            if version == 5:
                migrated = {"_version": CACHE_VERSION, **{
                    k: v for k, v in cache.items() if not k.startswith("_")
                }}
                print(
                    "Кэш версии 5 мигрирован: готовые дома сохранены, "
                    "неполные будут перепроверены по новым правилам."
                )
                return migrated

            print("Кэш от старой версии программы — начинаем заново.")
        except Exception:
            print("Не удалось прочитать кэш, начинаем заново.")

    return {"_version": CACHE_VERSION}


def save_cache(cache):
    temp = CACHE_FILE + ".tmp"

    try:
        with open(temp, "w", encoding="utf-8") as file:
            json.dump(cache, file, ensure_ascii=False, indent=2, default=str)

        Path(temp).replace(CACHE_FILE)
    except Exception as error:
        print("   (кэш не сохранён:", error, ")")


# ==========================================
# СОХРАНЕНИЕ EXCEL
# ==========================================

def style_sheet(sheet, widths):
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions

    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(fill_type="solid", fgColor="1F4E78")
        cell.alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True
        )

    for column, width in widths.items():
        sheet.column_dimensions[column].width = width

    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)


_save_warning = {"shown": False}


def save_excel(houses, cache):
    workbook = Workbook()

    sheet = workbook.active
    sheet.title = "Дома"
    sheet.append(HOUSE_HEADERS)

    premises_sheet = workbook.create_sheet("Квартиры")
    premises_sheet.append(PREMISE_HEADERS)

    low_sheet = workbook.create_sheet("Частных меньше 50%")
    low_sheet.append(HOUSE_HEADERS)
    low_private = []

    incomplete_sheet = workbook.create_sheet("Неполные данные")
    incomplete_sheet.append(HOUSE_HEADERS)
    incomplete_rows = []

    diag_sheet = workbook.create_sheet("Диагностика")
    diag_sheet.append(DIAG_HEADERS)

    errors = workbook.create_sheet("Ошибки и пропуски")
    errors.append(ERROR_HEADERS)

    for house in houses:
        rec = cache.get(house["cadastre"] or house["address"])

        if not rec:
            continue

        building = rec.get("building") or {}
        premises = list((rec.get("premises") or {}).values())
        found = building.get("status") == "ok"

        residential_found = sum(
            1 for p in premises
            if "жил" in (p.get("purpose") or "").lower()
            and "нежил" not in (p.get("purpose") or "").lower()
        )

        gis_count = house["residential"]
        nonres_count = house.get("nonresidential")
        gis_total = sum(
            v for v in (gis_count, nonres_count) if isinstance(v, int)
        )
        comment = rec.get("comment", "")

        if not found:
            check = "Не определено"
            comment = comment or building.get("message") or "Не найден"
        else:
            check = building.get("verdict", "Не определено")

        listed = rec.get("listed_count")
        compare = None
        complete, why_incomplete, missing_numbers = completeness(house, rec)

        rec["_complete_text"] = "Полные" if complete else "Неполные"
        rec["_expected"] = expected_units(house) or ""

        if rec.get("big"):
            check = f"Пропущен (больше {MAX_FLATS} квартир)"
            compare = (
                f"В ГИС ЖКХ {gis_total}, в Росреестре больше {MAX_FLATS} квартир"
            )
        elif found and check == "МКД" and gis_total:
            base = listed if isinstance(listed, int) else len(premises)
            compare = (
                "Совпадает" if base == gis_total
                else f"Расхождение: в ГИС ЖКХ {gis_total} "
                     f"(жилых {gis_count}, нежилых {nonres_count}), "
                     f"в Росреестре {base}"
            )

        # ---- суммы по форме собственности (только актуальные карточки)
        stats = {}
        counts = {"кв": set(), "ком": 0, "пом": 0}

        if check == "МКД" and not rec.get("big"):
            for p in premises:
                state = (p.get("state") or "").strip()

                if not p.get("card_ok") or (state and state.lower() != "актуально"):
                    continue

                kind = premise_type(p.get("label"))

                if kind == "Квартира":
                    counts["кв"] |= flat_ids(p.get("label"))
                elif kind == "Комната":
                    counts["ком"] += 1
                elif kind == "Помещение":
                    counts["пом"] += 1

                group = p.get("ownership") or NO_OWNERSHIP

                if group not in ("Частная", "Муниципальная", NO_OWNERSHIP):
                    group = "Прочая"

                n, a = stats.get(group, (0, 0.0))
                stats[group] = (n + 1, a + to_float(p.get("area")))

        def stat(group, index):
            if check != "МКД" or rec.get("big"):
                return None

            value = stats.get(group, (0, 0.0))[index]
            return round(value, 2) if index else value

        total_area = round(sum(a for _, a in stats.values()), 2)

        def share(group):
            if not stats or not total_area:
                return None

            return round(stats.get(group, (0, 0.0))[1] / total_area * 100, 1)

        private_area = stats.get("Частная", (0, 0.0))[1] if stats else 0.0
        private_less = None

        if stats and total_area:
            private_less = "ДА" if private_area / total_area * 100 < 50 else "НЕТ"

        if stats and not complete:
            # неполные данные: вывод о 50% ненадёжен
            private_less = "НЕ ОПРЕДЕЛЕНО (неполные данные)"

        house_row = [
            "Да" if found else "Нет",
            building.get("address"),
            "Да" if building.get("address_match") else "НЕТ" if found else None,
            building.get("kind"), building.get("purpose"), building.get("area"),
            building.get("floors"), building.get("year"), building.get("state"),
            check,
            listed if isinstance(listed, int) else None,
            len(premises) if check == "МКД" else None,
            residential_found if check == "МКД" else None,
            compare, comment,
            len(counts["кв"]) if stats else None,
            counts["ком"] if stats else None,
            counts["пом"] if stats else None,
            stat("Частная", 0), stat("Частная", 1),
            stat("Муниципальная", 0), stat("Муниципальная", 1),
            stat(NO_OWNERSHIP, 0), stat(NO_OWNERSHIP, 1),
            stat("Прочая", 0), stat("Прочая", 1),
            total_area if stats else None,
            share("Муниципальная"), share("Частная"),
            private_less,
            ("Полные" if complete else f"Неполные: {why_incomplete}")
            if check == "МКД" and not rec.get("big") else None,
            ", ".join(str(n) for n in missing_numbers) or None,
        ]

        # объединённая строка: все колонки «Дома в обслуживание» + Росреестр
        raw = house.get("raw") or {}
        input_part = [raw.get(name) for name in INPUT_COLUMNS]

        if not any(v is not None for v in input_part):
            input_part[0] = house["address"]
            input_part[2] = house["cadastre"]
            input_part[11] = gis_count
            input_part[12] = nonres_count

        house_row = input_part + house_row

        sheet.append(house_row)

        if check == "МКД" and not rec.get("big") and not complete:
            incomplete_rows.append(house_row)

            for cell in sheet[sheet.max_row]:
                cell.fill = PatternFill(fill_type="solid", fgColor="FFF2CC")

        elif private_less == "ДА":
            low_private.append(house_row)

            for cell in sheet[sheet.max_row]:
                cell.fill = PatternFill(fill_type="solid", fgColor="FCE4D6")

        diag = diag_from_dict(rec.get("diag") or {})
        diag_sheet.append(diag.row(house, rec))

        if not complete:
            errors.append([
                house["address"], house["cadastre"], None,
                "Неполные данные: " + why_incomplete
                + (f" (нет номеров: {', '.join(map(str, missing_numbers[:40]))})"
                   if missing_numbers else ""),
            ])

        if not found or check != "МКД" or rec.get("comment") or (
            compare and compare != "Совпадает"
        ):
            errors.append([
                house["address"], house["cadastre"], None,
                comment or compare or check,
            ])

        if diag.get("finish_reason"):
            errors.append([
                house["address"], house["cadastre"], None,
                f"Завершение: {diag['finish_reason']}"
                + (f"; самый медленный этап: {diag.slowest_stage()}"
                   if diag.slowest_stage() else ""),
            ])

        for skipped_kn, skipped_label in (rec.get("skipped") or {}).items():
            errors.append([
                house["address"], house["cadastre"], skipped_label,
                f"Пропущено: другой кадастровый квартал ({skipped_kn})",
            ])

        premises.sort(key=lambda p: (
            premise_number(p.get("label")) is None,
            premise_number(p.get("label")) or 0,
            p.get("label") or "",
        ))

        for p in premises:
            rights = p.get("rights") or []
            kinds = sorted({r["kind"] for r in rights})

            state = (p.get("state") or "").strip()
            active = (not state) or state.lower() == "актуально"
            kinds_text = "; ".join(kinds) if active and kinds else None

            if not active:
                # Погашено и т.п.: так и пишем, больше ничего не берём
                form_text = state
                rights_text = state
                area_value = None
                restrictions_text = None
                purpose_value = None
                floor_value = None
            else:
                form_text = p.get("ownership") or (
                    NO_OWNERSHIP if p.get("card_ok") else None
                )
                rights_text = "\n".join(
                    f"{r['kind']} {r['number']}" for r in rights
                ) or p.get("rights_raw") or (
                    "Права в справке не найдены" if state else None
                )
                area_value = p.get("area")
                restrictions_text = "\n".join(
                    r["number"] for r in p.get("restrictions") or []
                ) or None
                purpose_value = p.get("purpose")
                floor_value = p.get("floor")

            premises_sheet.append([
                house["address"], p.get("label"), premise_type(p.get("label")),
                state or None,
                form_text, kinds_text, area_value, rights_text,
                restrictions_text,
                purpose_value, floor_value,
                p.get("address"),
                "Да" if p.get("address_match") else "НЕТ",
                p.get("check", "OK"),
                p.get("cost") if active else None,
                p.get("kn"),
            ])

            if p.get("check") not in (None, "OK") and not p.get("variant_note"):
                errors.append([
                    house["address"], house["cadastre"], p.get("label"),
                    p.get("check"),
                ])

    for row_values in low_private:
        low_sheet.append(row_values)

    for row_values in incomplete_rows:
        incomplete_sheet.append(row_values)

    house_widths = {
        get_column_letter(i + 1): width
        for i, width in enumerate(
            [55, 16, 22, 10, 10, 12, 14, 14, 20, 40, 11, 11, 11, 10]
            + [11, 50, 10, 14, 22, 12, 9, 12, 14, 22, 14, 14, 12, 36, 40,
               11, 10, 11, 11, 12, 14, 14, 14, 14, 12, 12, 14, 12, 12, 14,
               38, 30]
        )
    }

    style_sheet(sheet, house_widths)
    style_sheet(low_sheet, house_widths)
    style_sheet(incomplete_sheet, house_widths)
    style_sheet(diag_sheet, {
        get_column_letter(i + 1): width
        for i, width in enumerate(
            [50, 22, 10, 12, 12, 12, 14, 10, 10, 12, 8, 10, 10, 10,
             10, 10, 10, 12, 12, 12, 24, 30]
        )
    })
    style_sheet(premises_sheet, {
        "A": 45, "B": 14, "C": 12, "D": 14, "E": 22, "F": 30, "G": 11,
        "H": 52, "I": 30, "J": 14, "K": 9, "L": 50, "M": 10, "N": 40,
        "O": 16, "P": 22,
    })
    style_sheet(errors, {"A": 50, "B": 22, "C": 16, "D": 60})

    temp = OUTPUT_FILE + ".tmp.xlsx"
    workbook.save(temp)

    try:
        Path(temp).replace(OUTPUT_FILE)
    except PermissionError:
        # Файл открыт в Excel. Сохраняем рядом копию и не падаем.
        alt = OUTPUT_FILE.replace(".xlsx", " (копия).xlsx")

        try:
            Path(temp).replace(alt)

            if not _save_warning["shown"]:
                _save_warning["shown"] = True
                print(
                    f"\n!!! Файл «{OUTPUT_FILE}» открыт в Excel. "
                    f"Сохраняю в «{alt}». Закройте Excel, чтобы писать "
                    "в основной файл."
                )
        except PermissionError:
            pass


# ==========================================
# ОСНОВНАЯ ПРОГРАММА
# ==========================================

def ask_limit(total):
    answer = input(
        f"\nСколько домов обработать? Enter = все ({total}), "
        "для пробы введите 2: "
    ).strip()

    if answer.isdigit() and int(answer) > 0:
        return int(answer)

    return total


def needs_retry(rec, house=None):
    """Дом обработан с ошибками и его стоит перепроверить."""

    building = rec.get("building") or {}

    if building.get("status") == "error":
        return True

    if str(rec.get("comment") or "").startswith("Ошибка"):
        return True

    diag = rec.get("diag") or {}

    if str(diag.get("finish_reason") or "") in (
        "ошибка", "сервис недоступен", "лимит 100",
    ):
        return True

    if rec.get("needs_retry"):
        return True

    # дом обработан по старым правилам - перепроверить
    if rec.get("done") and rec.get("rules") != RULES_VERSION:
        if house is None:
            return True

        # дом, обработанный по старым правилам, перепроверяем только если
        # по нему не нашлись все квартиры (остальные трогать незачем)
        return not completeness(house, rec)[0]

    # в старом кэше нет формы собственности — карточки нужно перечитать
    if rec.get("done") and any(
        item.get("card_ok") and item.get("parse_v") not in (2, 3)
        for item in (rec.get("premises") or {}).values()
    ):
        return True

    return any(
        str(item.get("check") or "").startswith("Ошибка")
        for item in (rec.get("premises") or {}).values()
    )


class Tee:
    """Дублирует вывод консоли в файл rosreestr_log.txt."""

    def __init__(self, stream, path):
        self.stream = stream
        self.file = open(path, "a", encoding="utf-8")

    def write(self, data):
        self.stream.write(data)

        try:
            self.file.write(data)
            self.file.flush()
        except Exception:
            pass

    def flush(self):
        self.stream.flush()


def keep_awake(on):
    """Не даёт Windows уснуть во время долгой работы."""

    try:
        import ctypes

        flags = 0x80000001 if on else 0x80000000
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def search_hook(response):
    """Слушатель ответов для UI-режима: моментально узнаёт пустую выдачу."""

    try:
        if "/account-back/address/search" in response.url and response.status == 200:
            body = response.json()
            SEARCH_STATE["count"] = len(body) if isinstance(body, list) else None
            SEARCH_STATE["seq"] += 1
    except Exception:
        pass


def print_summary(houses, cache, selected, t_start):
    """Итоговая статистика по требованиям п.8 ТЗ."""

    total_flats = 0
    total_rooms = 0
    total_nonres = 0
    total_errors = 0
    found_buildings = 0
    not_found = 0
    complete_houses = 0
    incomplete_houses = 0
    times = []
    slowest = ("", 0.0)

    for house in selected:
        rec = cache.get(house["cadastre"] or house["address"])

        if not rec:
            continue

        building = rec.get("building") or {}
        diag = diag_from_dict(rec.get("diag") or {})

        if building.get("status") == "ok":
            found_buildings += 1
        elif building.get("status") in ("not_found", "skipped"):
            not_found += 1

        premises = (rec.get("premises") or {}).values()
        counts = {"кв": set(), "ком": 0, "пом": 0}

        for p in premises:
            kind = premise_type(p.get("label"))

            if kind == "Квартира" and p.get("card_ok"):
                counts["кв"] |= flat_ids(p.get("label"))
            elif kind == "Комната":
                counts["ком"] += 1
            elif kind == "Помещение":
                counts["пом"] += 1

        total_flats += len(counts["кв"])
        total_rooms += counts["ком"]
        total_nonres += counts["пом"]
        total_errors += diag.get("errors", 0)

        if rec.get("big"):
            continue

        complete, why, _missing = completeness(house, rec)

        if building.get("status") == "ok":
            if complete:
                complete_houses += 1
            else:
                incomplete_houses += 1

        if rec.get("done") and diag.secs:
            times.append(diag.secs)

        stage = diag.slowest_stage()

        if stage:
            match = re.search(r"\((\d+) с\)", stage)

            if match and int(match.group(1)) > slowest[1]:
                slowest = (f"{house['address']}: {stage}", int(match.group(1)))

    total_secs = time.time() - t_start
    avg_house = sum(times) / len(times) if times else 0.0
    avg_flat = total_secs / total_flats if total_flats else 0.0

    print("\n===================================")
    print("  ИТОГОВАЯ СТАТИСТИКА")
    print("===================================")
    print("Всего домов в работе:", len(selected))
    print("Найдено зданий:", found_buildings)
    print("Не найдено зданий:", not_found)
    print("Полностью проверенных домов:", complete_houses)
    print("Домов с неполными данными:", incomplete_houses)
    print("Найдено квартир:", total_flats)
    print("Найдено комнат:", total_rooms)
    print("Найдено нежилых помещений:", total_nonres)
    print("Ошибок (по всем домам):", total_errors)
    print("Среднее время обработки дома:",
          f"{int(avg_house)} с" if times else "-")
    print("Среднее время на одну квартиру:",
          f"{avg_flat:.1f} с" if total_flats else "-")
    print("Общее время работы:", f"{int(total_secs)} с ({int(total_secs / 60)} мин)")

    if slowest[0]:
        print("Самый медленный этап:", slowest[0])

    print(
        f"\nЗапросов API: {STATS['api_calls']}, поисков (UI): {STATS['searches']}, "
        f"карточек: {STATS['cards']}, сбоев: {STATS['fail']}, "
        f"HTTP-ошибки: {STATS['http']}, "
        f"лимитов частоты: {STATS['rate_limited']}, "
        f"пустых выдач: {STATS['empty_results']}"
    )
    print("Подробности по сбоям:", STATS_FILE, "| лист «Диагностика» в Excel")


def run_check():
    from playwright.sync_api import sync_playwright

    t_start = time.time()

    houses = read_houses()
    cache = load_cache()

    print(f"\nДомов в листе «{INPUT_SHEET}»: {len(houses)}")

    limit = ask_limit(len(houses))

    original_stdout = sys.stdout
    sys.stdout = Tee(original_stdout, LOG_FILE)
    keep_awake(True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            viewport={"width": 1366, "height": 900},
        )

        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(20000)

        def on_response(response):
            search_hook(response)

            try:
                if response.status >= 400 and "rosreestr" in response.url:
                    key = str(response.status)
                    STATS["http"][key] = STATS["http"].get(key, 0) + 1
            except Exception:
                pass

        context.on("response", on_response)

        def key_of(house):
            return house["cadastre"] or house["address"]

        last_excel = {"t": 0.0}

        def persist(force=False):
            save_cache(cache)

            if force or time.time() - last_excel["t"] > 45:
                save_excel(houses, cache)
                last_excel["t"] = time.time()

            save_stats()

        _cache_saver["fn"] = lambda: save_cache(cache)

        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            page.bring_to_front()

            print("\n===================================")
            print("  ПРОВЕРКА В РОСРЕЕСТРЕ")
            print("  Движок:", "API (+ UI-фолбэк)" if engine_mode() == "api" else "UI")
            print("===================================")

            if not search_form_ready(page):
                print("1. Войдите в профиль и пройдите капчу, если просят.")
                print("2. Откройте форму поиска «Адрес или кадастровый номер».")
                input("3. Нажмите Enter, когда форма поиска готова...")
            else:
                print("Форма поиска уже открыта, начинаем.")

            print("\nДальше программа работает сама. Журнал:", LOG_FILE)

            selected = []

            for house in houses:
                rec = cache.get(key_of(house), {})

                if rec.get("done") and not needs_retry(rec, house):
                    continue

                selected.append(house)

                if len(selected) >= limit:
                    break

            print(f"Домов к обработке: {len(selected)}")

            consecutive_fails = 0

            for pass_number in range(1, MAX_HOUSE_PASSES + 1):
                pending = [
                    h for h in selected
                    if not cache.get(key_of(h), {}).get("done")
                    or needs_retry(cache.get(key_of(h), {}), h)
                ]

                if not pending:
                    break

                if pass_number > 1:
                    print(
                        f"\n=== Повторная проверка домов с ошибками "
                        f"(проход {pass_number}): {len(pending)} ==="
                    )

                for house in pending:
                    if page.is_closed():
                        alive = [
                            pg for pg in context.pages if not pg.is_closed()
                        ]

                        if not alive:
                            raise FatalStop(
                                "Окно браузера закрыто. Запустите программу "
                                "заново, она продолжит с этого места."
                            )

                        page = alive[-1]

                    key = key_of(house)
                    rec = cache.setdefault(key, {})
                    number = houses.index(house) + 1

                    print(
                        f"\n[{time.strftime('%H:%M:%S')}] "
                        f"[{number}/{len(houses)}] {house['address']}"
                    )

                    if not house["cadastre"] or not re.fullmatch(
                        KN_RE, house["cadastre"]
                    ):
                        rec["building"] = {
                            "status": "not_found",
                            "message": "Нет корректного кадастрового номера",
                        }
                        rec["done"] = True
                        persist()
                        continue

                    started = time.time()
                    searches0 = STATS["searches"]
                    api0 = STATS["api_calls"]
                    fails0 = sum(STATS["fail"].values())
                    diag = HouseDiag()

                    try:
                        process_house(page, house, rec, diag)
                        rec["done"] = True
                        consecutive_fails = 0

                        building = rec.get("building") or {}
                        complete, why, _missing = completeness(house, rec)
                        print(
                            "   итог:", building.get("status"),
                            "|", building.get("verdict"),
                            "| помещений:", len(rec.get("premises") or {}),
                            "| полнота:", "полные" if complete else why,
                            f"| {int(time.time() - started)} сек",
                        )

                    except (FatalStop, KeyboardInterrupt):
                        raise

                    except Exception as error:
                        if "has been closed" in str(error).lower():
                            raise FatalStop(
                                "Окно браузера закрыто. Запустите программу "
                                "заново, она продолжит с этого места."
                            )

                        kind = stat_fail(error)
                        diag.finish(f"ошибка ({kind})")
                        rec["diag"] = diag.to_dict()
                        rec["comment"] = f"Ошибка: {str(error)[:150]}"
                        rec["done"] = False
                        consecutive_fails += 1
                        print("   ошибка:", error)
                        save_debug(page, "error")

                        if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                            print(
                                "   несколько ошибок подряд — проверяю, "
                                "жив ли сайт..."
                            )
                            reload_site(page)

                            if not search_form_ready(page):
                                wait_until_site_ready(page)

                            consecutive_fails = 0

                    rec["secs"] = int(time.time() - started)
                    rec["searches"] = STATS["searches"] - searches0
                    rec["api_calls"] = STATS["api_calls"] - api0
                    rec["fails"] = sum(STATS["fail"].values()) - fails0
                    rec["diag"] = diag.to_dict()

                    persist()

            persist(force=True)

            failed = [
                h for h in selected
                if needs_retry(cache.get(key_of(h), {}), h)
                or not cache.get(key_of(h), {}).get("done")
            ]

            print("\n===================================")
            print("  ГОТОВО")
            print("===================================")
            if failed:
                print("С ошибками (запустите ещё раз):", len(failed))
            print("Файл Excel:", OUTPUT_FILE)

            print_summary(houses, cache, selected, t_start)

        except FatalStop as error:
            persist(force=True)
            print("\a\n!!! ОСТАНОВЛЕНО:", error)

        except KeyboardInterrupt:
            print("\nОстановлено пользователем, сохраняю...")
            persist(force=True)

        finally:
            keep_awake(False)
            sys.stdout = original_stdout

            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    run_check()

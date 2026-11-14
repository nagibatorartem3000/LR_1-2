"""
Проверка домов и помещений в сервисе Росреестра
«Справочная информация по объектам недвижимости в режиме online».

Читает «Дома в обслуживание.xlsx» (лист «Подходящие»), которые создаёт main.py,
и сохраняет результат в «Дома Росреестр.xlsx». Лист «Дома» - ПОЛНАЯ объединённая таблица:
все колонки из «Дома в обслуживание» + данные Росреестра. Остальные листы: Квартиры,
Частных меньше 50%, Неполные данные (не все квартиры найдены), Ошибки и пропуски, Диагностика.
Лимит «не больше 30» считается ТОЛЬКО по квартирам (кв.). Комнаты и помещения
(ком., пом.) в лимит не входят: общежития и дома с нежилыми помещениями проходят.
Форма собственности (частная / муниципальная / нет) берётся из карточки Росреестра.
main.py и его Excel не изменяются.

Вход в профиль и капчу вы проходите вручную в открывшемся браузере.
Сессия запоминается в папке rosreestr_profile.

Запуск:  python rosreestr_online.py
Из main.py:  from rosreestr_online import run_check; run_check()
"""

import json
import random
import re
import sys
import time
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


# ==========================================
# НАСТРОЙКИ
# ==========================================

URL = "https://lk.rosreestr.ru/eservices/real-estate-objects-online"

INPUT_FILE = "Дома в обслуживание.xlsx"
INPUT_SHEET = "Подходящие"

OUTPUT_FILE = "Дома Росреестр.xlsx"
CACHE_FILE = "rosreestr_cache.json"
PROFILE_DIR = "rosreestr_profile"
DEBUG_DIR = "rosreestr_debug"

SEARCH_TIMEOUT = 30         # секунд ждать ответа Росреестра (он медленный)
CARD_WAIT = 30                # после ОДНОГО клика по номеру ждём открытия карточки (сек)
PAUSE_AFTER_TYPING = 0.8      # после вставки адреса в строку поиска ждём, потом жмём «Найти» (сек)
CARD_CLOSE_PAUSE = 0.8       # пауза после закрытия карточки перед открытием следующей (сек)
QUICK_RETRY_WAIT = 1.5      # пауза перед первым повтором при сбое (без перезагрузки сайта)
SITE_LOAD_WAIT = 25         # сколько ждать форму поиска после перезагрузки сайта (сек)
CARD_TIMEOUT = 15           # секунд ждать открытия карточки объекта

# Паузы, чтобы не получить «слишком много попыток».
# Если всё равно блокирует, увеличьте эти числа.
PAUSE_BEFORE_SEARCH = 0      # секунд перед каждым нажатием «Найти»
PAUSE_BEFORE_CARD = 0        # секунд перед открытием каждой карточки
PAUSE_AFTER_CARD = 0.2         # секунд после открытия карточки
RATE_LIMIT_WAIT = 90        # секунд ждать, если сайт написал «слишком много»
RATE_LIMIT_RETRIES = 3       # сколько раз ждать и пробовать снова
MAX_FLAT_FALLBACK = 60       # максимум номеров для поиска по одному
MAX_RESULT_PAGES = 12        # сколько страниц списка результатов читать
PAUSE_NEXT_PAGE = 1.0          # секунд после перехода на следующую страницу списка
SPACE_BEFORE_HOUSE_COMMA = True   # как в вашем шаблоне: «ул Ленина , д. 190»
# --- автоматический режим (работа без участия человека) ---
AUTO_RETRIES = 5                          # попыток на одно действие
AUTO_RECOVER_WAITS = (5, 10, 20, 30)   # паузы перед восстановлением, сек
SITE_DOWN_MAX_MINUTES = 90                # сколько ждать, если сайт недоступен
MAX_CONSECUTIVE_FAILS = 5                 # домов подряд с ошибкой до долгого ожидания
MAX_HOUSE_PASSES = 3                      # сколько раз перепроверять дома с ошибками
# Порядок поиска по одному: (как писать в запросе, с какого номера начинать)
SEARCH_STAGES = (
    ("кв.", 1),
    ("пом.", 1),
    ("ком.", 1),
    ("пом.", 1001),
)
STAGE_ONLY_IF_SEEN = True    # True: «пом.»/«ком.»/«пом. 1001» по одному ищем, только если такие
                             # объекты уже встретились в списке (или по ГИС ЖКХ есть нежилые). Так быстрее.
ALWAYS_PROBE_NEXT = False   # True: после общего списка всё равно проверять «кв. N+1». False (быстрее): только если список неполный
ALWAYS_CHECK_1001 = False    # True: проверять «пом. 1001…» в каждом доме, даже если всё уже найдено
MAX_FLATS = 30               # дома, где больше 30 КВАРТИР (кв.), пропускаем; комнаты и помещения не считаются
SKIP_BY_GIS_COUNT = False    # True: сразу пропускать дом, если в ГИС ЖКХ «жилых» больше 30 (быстрее, но может отсечь общежития)
RULES_VERSION = 5            # версия правил; дома, пропущенные по старым правилам, перепроверяются
BIG_PROBE_FROM = 20          # с какого числа квартир/помещений проверять «кв. 31» (есть ли больше 30)
EMPTY_WAIT = 5               # секунд ждать ответа на поиск одной квартиры (пустая выдача — норма)
STRICT_SAME_QUARTER = True   # квартиры из другого кадастрового квартала не берём (кроме добора до числа по ГИС ЖКХ, с пометкой)
EMPTY_STREAK_LIMIT = 3       # сколько пустых выдач подряд до смены кв. -> пом. -> ком.
RESULTS_CAP = 100            # сервис показывает не больше 100 результатов

# --- полнота данных и поиск недостающих квартир ---
MISSING_EXTRA = 6            # сколько номеров ПОСЛЕ последнего (N+1...N+6) пробовать, если квартир всё ещё не хватает
MAX_MISSING_PROBES = 45      # максимум поисков «по одной квартире» на один дом
ALT_QUERY_VARIANTS = True    # если по основному запросу квартира не нашлась - пробуем запрос в формате ГИС ЖКХ
INCLUDE_RAW_TEXT = False     # True: переносить в итоговую таблицу «Текст из списка» / «Текст карточки» из ГИС ЖКХ
POLL_MS = 150                # как часто опрашиваем страницу (мс); было 300
STATS_FILE = "rosreestr_stats.json"

KN_RE = r"\d{1,2}:\d{1,3}:\d{1,8}:\d+"

# колонки листа «Подходящие» из «Дома в обслуживание.xlsx» (переносятся как есть)
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


# ==========================================
# АДРЕСА: РАЗБОР И СТРОГАЯ СВЕРКА
# ==========================================

STREET_TYPES = {
    "улица": "ул", "ул": "ул",
    "проспект": "пр-кт", "пр-кт": "пр-кт", "пр": "пр-кт",
    "переулок": "пер", "пер": "пер",
    "бульвар": "б-р", "б-р": "б-р",
    "шоссе": "ш", "ш": "ш",
    "площадь": "пл", "пл": "пл",
    "набережная": "наб", "наб": "наб",
    "тупик": "туп", "туп": "туп",
    "микрорайон": "мкр", "мкр": "мкр",
    "аллея": "ал", "ал": "ал",
    "проезд": "проезд", "тракт": "тракт",
}

CITY_TYPES = {
    "г", "город", "с", "село", "п", "пос", "поселок", "пгт",
    "д", "дер", "деревня", "рп", "ст", "станица", "г.о", "го",
}

HOUSE_RE = re.compile(
    r"(?:^|[\s,])д(?:ом)?\.?\s*(\d+(?:/\d+)?[а-яa-z]?)(?!\w)", re.I
)
KORPUS_RE = re.compile(
    r"^\s*,?\s*к(?:орп(?:ус)?)?\.?\s*(\d+[а-яa-z]?)(?!\w)", re.I
)
STROENIE_RE = re.compile(r"^\s*,?\s*стр\.?\s*(\d+)(?!\w)", re.I)
FLAT_RE = re.compile(
    r"(?:^|[\s,])(?:кв|квартира)\.?\s*(\d+[а-яa-z]?(?:/\d+)?)(?!\w)", re.I
)
ROOM_RE = re.compile(
    r"(?:^|[\s,])(?:ком|комн|комната)\.?\s*(\d+[а-яa-z]?)(?!\w)", re.I
)
PREM_RE = re.compile(
    r"(?:^|[\s,])(?:пом|помещение)\.?\s*(\d+[а-яa-z]?|[IVXivx]+)(?!\w)", re.I
)


def normalize(text):
    return " ".join(str(text).split())


def to_tokens(text):
    text = str(text).lower().replace("ё", "е")
    tokens = re.findall(r"[a-zа-я0-9][a-zа-я0-9-]*", text)
    return [STREET_TYPES.get(token, token) for token in tokens]


def house_key(text):
    """Номер дома в едином виде: 86/1, 41б, 15/2 (корпус) и т.п."""

    match = HOUSE_RE.search(text)

    if not match:
        return None

    key = match.group(1).lower()
    rest = text[match.end():]

    korpus = KORPUS_RE.match(rest)

    if korpus:
        key += "/" + korpus.group(1).lower()
        rest = rest[korpus.end():]

    stroenie = STROENIE_RE.match(rest)

    if stroenie:
        key += "с" + stroenie.group(1).lower()

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


# Слова, которые могут быть в адресе Росреестра и не указывают на другой район
GENERIC_WORDS = {
    "российская", "федерация", "область", "обл", "край", "республика",
    "респ", "г", "о", "го", "город", "городской", "муниципальный",
    "округ", "район", "р-н",
}


def parse_rr_address(address):
    house = HOUSE_RE.search(address)
    head = address[:house.start()] if house else address

    # «Хвост» после номера дома: корпус/строение и кв./ком./пом. допустимы,
    # любые другие слова (лит., этаж и т.п.) означают не наш объект.
    tail_clean = True

    if house:
        rest = address[house.end():]
        for regex in (KORPUS_RE, STROENIE_RE):
            m = regex.match(rest)
            if m:
                rest = rest[m.end():]
        for regex in (FLAT_RE, ROOM_RE, PREM_RE):
            rest = regex.sub(" ", rest)
        tail_clean = not re.sub(r"[\s,.;]+", "", rest)

    return {
        "tail_clean": tail_clean,
        "head_tokens": set(to_tokens(head)),
        "tokens": set(to_tokens(address)),
        "house": house_key(address),
        "label": premise_label(address),
    }


def reject_reason(gis, rr):
    """Почему адрес из выдачи не признан помещением нашего дома (для диагностики)."""

    if not rr["label"]:
        return "нет кв./ком./пом. в адресе"

    if not gis or rr["house"] != gis["house"]:
        return "другой номер дома"

    if not rr.get("tail_clean", True):
        return "лишние слова после номера дома"

    needed = gis["street"] + gis["city"]
    missing = [t for t in needed if t not in rr["tokens"]]

    if missing:
        return "нет в адресе слов: " + " ".join(missing)

    extra = rr["head_tokens"] - gis["allowed"] - GENERIC_WORDS

    if extra:
        return "лишние слова в адресе: " + " ".join(sorted(extra))

    return "другое"


def match_house(gis, rr):
    """Адрес из Росреестра относится именно к этому дому."""

    if not gis or not gis["house"] or rr["house"] != gis["house"]:
        return False

    if not rr.get("tail_clean", True):
        return False

    needed = gis["street"] + gis["city"]

    if not needed or not all(token in rr["tokens"] for token in needed):
        return False

    # Лишние слова до номера дома (например «Нововятский» в «Советская
    # (Нововятский)») означают другую улицу или район — это не наш дом.
    extra = rr["head_tokens"] - gis["allowed"] - GENERIC_WORDS

    return not extra


def build_query(address, kind=None, number=None):
    """
    Шаблон запроса как в Росреестре:
    Кировская область, г.о. город Киров, г Киров, ул Ленина, д. 190, кв. 1

    Всё, что начиная с «г Киров», берётся из адреса ГИС ЖКХ.
    kind: 'кв.', 'пом.' или 'ком.'; без него ищется весь дом.
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
    """Адрес дома целиком, один в один как в карточке Росреестра
    (с «Российская Федерация» в начале)."""

    address = normalize(building.get("address") or "")

    return address or None


def strip_country(address):
    return re.sub(r"^Российская Федерация,?\s*", "", address or "", flags=re.I)


def query_for(house, kind=None, number=None):
    """Запрос: адрес из карточки Росреестра (если есть) + «, кв. N».
    Иначе - шаблон из адреса ГИС ЖКХ."""

    base = house.get("rr_address")

    if base:
        query = base

        if kind and number is not None:
            query += f", {kind} {number}"

        return query

    return build_query(house["address"], kind, number)


def label_has(label, kind, number):
    """В подписи помещения есть, например, 'кв. 5' (а не 'кв. 55')."""

    return bool(re.search(
        rf"{re.escape(kind)} {number}(?!\w)", label or "", re.I
    ))


def label_numbers(label, kind):
    return {
        int(n) for n in re.findall(rf"{re.escape(kind)} (\d+)", label or "")
    }


def flat_ids(label):
    """Номера квартир в подписи: 'кв. 5, ком. 2' -> {'5'}."""
    return {m.lower() for m in re.findall(r"кв\. ([^,\s]+)", label or "")}


def flat_count(labels):
    """Сколько РАЗНЫХ квартир среди подписей (комнаты и помещения не считаются)."""
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


# ==========================================
# СТАТИСТИКА СБОЕВ (чтобы видеть, где сайт тормозит и блокирует)
# ==========================================

STATS = {"searches": 0, "cards": 0, "fail": {}, "http": {}, "rate_limited": 0}


def stat_fail(error):
    """Раскладывает сбой по понятным категориям и считает их."""

    text = str(error).lower()

    if "слишком много" in text or "ограничил частоту" in text:
        kind = "лимит запросов"
    elif "карточка не открылась" in text or "не удалось нажать" in text:
        kind = "карточка не открылась"
    elif "нет результатов поиска" in text:
        kind = "поиск без ответа"
    elif "номер не найден в выдаче" in text:
        kind = "номер пропал из выдачи"
    elif "соединен" in text or "ошибк" in text:
        kind = "ошибка сайта/сети"
    else:
        kind = "прочее"

    STATS["fail"][kind] = STATS["fail"].get(kind, 0) + 1
    return kind


def save_stats():
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as file:
            json.dump(STATS, file, ensure_ascii=False, indent=2)
    except Exception:
        pass


def expected_units(house):
    """Сколько ЖИЛЫХ помещений ждём по ГИС ЖКХ (квартиры/комнаты)."""

    for value in (house.get("residential"), house.get("apartments")):
        if isinstance(value, int) and value > 0:
            return value

    return 0


def found_units(rec):
    """(квартир_и_комнат_найдено, номера_найденных_квартир, есть_ошибки_карточек).
    Считаем только актуальные карточки, прошедшие проверку адреса."""

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
    """Возвращает (полные_ли_данные, текст_причины, список_не_найденных_номеров)."""

    building = rec.get("building") or {}

    if building.get("verdict") != "МКД" or rec.get("big"):
        return True, "", []

    expected = expected_units(house)
    found, flats, bad = found_units(rec)
    reasons = []
    missing = []

    if expected:
        if found < expected:
            reasons.append(f"найдено {found} из {expected} по ГИС ЖКХ")

            numbers = {int(x) for x in flats if str(x).isdigit()}
            missing = [n for n in range(1, expected + 1) if n not in numbers]
    elif not rec.get("list_complete"):
        reasons.append("число помещений в ГИС ЖКХ неизвестно, список Росреестра неполный")

    if bad:
        reasons.append(f"не открылись карточки: {bad}")

    return (not reasons), "; ".join(reasons), missing


NO_OWNERSHIP = "Нет формы собственности"

CARD_LABELS = {
    "характеристики объекта", "дата присвоения кадастрового номера",
    "адрес (местоположение)", "площадь, кв.м", "назначение", "этаж",
    "сведения о кадастровой стоимости", "ранее присвоенные номера",
}


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


def to_float(value):
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return 0.0


# ==========================================
# РАЗБОР КАРТОЧКИ ОБЪЕКТА
# ==========================================

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
    # Если карточка открыта поверх списка результатов, в тексте страницы
    # сначала идёт список (с заголовками «Кадастровый номер», «Адрес»).
    # Берём только то, что после заголовка карточки.
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
        "ownership": ownership_class(value_after(lines, "Форма собственности")),
        "rights": [],
        "restrictions": [],
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
    data["parse_v"] = 2

    if start is not None:
        section = []

        for line in lines[start:]:
            if line.upper().startswith("ВЕРНУТЬСЯ"):
                break
            section.append(line)

        # сырой текст раздела - чтобы данные о собственности не терялись,
        # даже если разметка карточки окажется необычной
        data["rights_raw"] = "\n".join(section)[:1500]

        mode = "rights"
        date_re = r"\d{2}\.\d{2}\.\d{4}(?:\s*г\.?)?"

        for index, line in enumerate(section):
            low = line.lower()

            # первая строка - заголовок раздела («Сведения о правах и
            # ограничениях (обременениях)»), её пропускаем
            if index == 0:
                continue

            if low.startswith(("ограничение прав", "ограничения прав")):
                mode = "restrictions"
                continue

            if low.startswith(("вид, номер", "сведения о правах", "права и ограничения")):
                continue

            if line.startswith("№"):
                continue

            # «Собственность № 43-43/... от 01.01.2020» в одной строке
            same = re.match(r"^(?P<kind>[^№]{3,}?)[\s,]*(?P<num>№.+)$", line)

            if same:
                data[mode].append({
                    "kind": same.group("kind").strip(" ,"),
                    "number": same.group("num").strip(),
                })
                continue

            # вид права и номер на соседних строках
            if index + 1 < len(section) and section[index + 1].startswith("№"):
                number = section[index + 1]

                if index + 2 < len(section) and re.fullmatch(
                    date_re, section[index + 2]
                ) and section[index + 2] not in number:
                    number = f"{number} {section[index + 2]}"

                data[mode].append({"kind": line, "number": number})

    return data


def ownership_hint(rights):
    if not rights:
        return "Права в справке не найдены"

    kinds = " ".join(r["kind"].lower() for r in rights)

    if "долев" in kinds or "совместн" in kinds:
        return "Вероятно частная (долевая/совместная)"

    if "собственност" in kinds:
        return "Собственность (владелец не раскрывается)"

    return "Иное право"


def classify(purpose):
    """Строго: только «Многоквартирный дом». Остальное - не наш дом."""

    text = (purpose or "").lower()

    if not text:
        return "Не определено"

    if "нежил" in text:
        return "Не МКД"

    if "многоквартир" in text:
        return "МКД"

    return "Не МКД"


def building_verdict(building):
    """МКД только если назначение «Многоквартирный дом»
    И статус объекта «Актуально»."""

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


# ==========================================
# РАЗБОР СТРАНИЦЫ ПОИСКА
# ==========================================

# Строки под последним результатом (пагинация и т.п.) - не часть адреса
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


# ==========================================
# КЭШ И ЧТЕНИЕ ИСХОДНОГО EXCEL
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


CACHE_VERSION = 5


def load_cache():
    if Path(CACHE_FILE).exists():
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as file:
                cache = json.load(file)

            if cache.get("_version") == CACHE_VERSION:
                return cache

            print("Кэш от старой версии программы — начинаем заново.")
        except Exception:
            print("Не удалось прочитать кэш, начинаем заново.")

    return {"_version": CACHE_VERSION}


def save_cache(cache):
    temp = CACHE_FILE + ".tmp"

    with open(temp, "w", encoding="utf-8") as file:
        json.dump(cache, file, ensure_ascii=False, indent=2, default=str)

    Path(temp).replace(CACHE_FILE)


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

    diag = workbook.create_sheet("Диагностика")
    diag.append([
        "Адрес", "Кадастровый номер", "Секунд на дом", "Поисков", "Сбоев",
        "Помещений найдено", "Ожидалось (ГИС)", "Полнота", "Причина",
    ])

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

        found_now = found_units(rec)[0]

        diag.append([
            house["address"], house["cadastre"], rec.get("secs"),
            rec.get("searches"), rec.get("fails"), found_now,
            expected_units(house) or None,
            "Полные" if complete else "Неполные",
            why_incomplete or rec.get("comment") or None,
        ])

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
            [55, 16, 22, 10, 10, 12, 14, 14, 20, 40, 11, 11, 11, 10]   # из «Дома в обслуживание»
            + [11, 50, 10, 14, 22, 12, 9, 12, 14, 22, 14, 14, 12, 36, 40,
               11, 10, 11, 11, 12, 14, 14, 14, 14, 12, 12, 14, 12, 12, 14,
               38, 30]
        )
    }

    style_sheet(sheet, house_widths)
    style_sheet(low_sheet, house_widths)
    style_sheet(incomplete_sheet, house_widths)
    style_sheet(diag, {"A": 50, "B": 22, "C": 12, "D": 10, "E": 10,
                       "F": 14, "G": 14, "H": 12, "I": 60})
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
# РАБОТА С БРАУЗЕРОМ
# ==========================================

# Сайт ищет через GET /account-back/address/search и сразу отвечает списком
# (пустой [] = такого объекта нет). На экране при этом ничего не меняется, поэтому
# раньше скрипт ждал тайм-аут (~10 с). Слушаем этот ответ и узнаём пустоту сразу.
SEARCH_STATE = {"seq": 0, "count": None}


def search_hook(response):
    try:
        if "/account-back/address/search" in response.url and response.status == 200:
            body = response.json()
            SEARCH_STATE["count"] = len(body) if isinstance(body, list) else None
            SEARCH_STATE["seq"] += 1
    except Exception:
        pass


class SearchTimeout(Exception):
    pass


class _BigHouse(Exception):
    pass


class RateLimited(Exception):
    pass


class FatalStop(Exception):
    """Сайт недоступен слишком долго — аккуратно останавливаемся."""


# Множитель пауз: растёт, если Росреестр жалуется на частоту запросов,
# и постепенно возвращается к 1 при спокойной работе.
PACE = {"factor": 1.0}


def human_pause(seconds):
    """Пауза с небольшим случайным разбросом, умноженная на PACE."""

    seconds = seconds * PACE["factor"]
    time.sleep(random.uniform(seconds, seconds * 1.4))


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


_first_search = {"done": False}

SITE_ERROR_RE = re.compile(
    r"соединен|попробуйте (?:позже|ещ[её]|повторить)|повторите|"
    r"не удалось|сервис недоступен|временно недоступ|что-то пошло не так",
    re.I,
)


def go_next_page(page, target_page=None):
    """Переходит на следующую страницу списка результатов."""

    pattern = re.compile(
        r"след|next|вперед|вперёд|показать ещ|загрузить ещ|›|»", re.I
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


def wait_for_results(page, previous, limit, probe=False, seq0=None, by_address=False):
    """Возвращает 'results', 'empty' или 'timeout'.

    probe=True: если страница изменилась, но результатов нет и текст
    перестал меняться (~1.5 с) - это пустая выдача (сайт пишет своё
    сообщение). Раньше такой ответ не распознавался и каждая отсутствующая
    квартира стоила ~15 секунд."""

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

                if time.time() - stable_since >= 1.5 and not SITE_ERROR_RE.search(text):
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
            and SITE_ERROR_RE.search(text)
            and not SITE_ERROR_RE.search(previous)
        ):
            raise SearchTimeout("Сайт сообщил об ошибке или потере соединения")

    return "timeout"


def run_search(page, query, probe=False):
    """
    Выполняет поиск и возвращает (всего_найдено, строки_первой_страницы).

    probe=True — поиск одного помещения: пустой ответ здесь нормален,
    поэтому ждём недолго и пустоту не считаем сбоем.
    """

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

    # адрес вставлен - ждём, потом нажимаем «Найти»
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
    """После закрытия карточки ждём 2 секунды, прежде чем открыть следующую.
    Перед новым поиском ждать не нужно."""

    left = CARD_CLOSE_PAUSE * PACE["factor"] - (time.time() - _card_closed["t"])

    if left > 0:
        time.sleep(left)


CARD_CLICK_WAIT = 8      # сек ждать карточку после ОДНОГО клика (потом кликаем снова)
CARD_CLICK_TRIES = 3     # сколько раз кликать по номеру, прежде чем считать сбоем


def kn_in(text, kn):
    return re.search(re.escape(kn) + r"(?!\d)", text) is not None


def wait_list_stable(page, kn, seconds=6):
    """Ждёт, пока в списке появится номер и текст страницы перестанет меняться."""

    deadline = time.time() + seconds
    previous = None

    while time.time() < deadline:
        page.wait_for_timeout(POLL_MS)
        text = page_text(page)

        if kn_in(text, kn) and text == previous:
            return True

        previous = text

    return False


def open_card(page, kn):
    """Кликает по номеру и ждёт карточку. Если клик «проглочен» сайтом
    (самый частый сбой) - кликает снова на той же странице, без перезагрузки."""

    wait_after_close()
    human_pause(PAUSE_BEFORE_CARD)

    for attempt in range(1, CARD_CLICK_TRIES + 1):
        wait_list_stable(page, kn)
        click_cadastre(page, kn)

        text = wait_card_open(page, kn, CARD_CLICK_WAIT)

        if text:
            human_pause(PAUSE_AFTER_CARD)
            return text

        # открылась не та карточка или полузагрузилась - закрываем
        if "Сведения об объекте" in page_text(page):
            back_to_results(page)

        if not kn_in(page_text(page), kn):
            break

        print(f"      клик не открыл карточку, кликаем снова "
              f"({attempt}/{CARD_CLICK_TRIES})")

    save_debug(page, "card_timeout")
    raise SearchTimeout(f"Карточка не открылась: {kn}")


def back_to_results(page):
    """Закрывает карточку (если открыта) и запоминает время закрытия."""

    had_card = "Сведения об объекте" in page_text(page)
    result = _back_to_results(page)

    if had_card and result:
        _card_closed["t"] = time.time()

    return result


def _back_to_results(page):
    """Закрывает карточку и возвращается к списку результатов."""

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


def restore_list(page, query, page_index):
    """Заново открывает тот же список и ту же страницу, что были раньше."""

    run_search(page, query)

    for step in range(2, page_index + 1):
        before = page_text(page)

        if not go_next_page(page, step):
            break

        wait_text_change(page, before)
        human_pause(PAUSE_NEXT_PAGE)


def find_on_pages(page, kn, query, max_pages=10):
    """Порядок выдачи Росреестра может меняться между поисками: номер,
    который был на странице 2, при повторном поиске окажется на 1 или 3.
    Поэтому листаем список с начала, пока не увидим нужный номер."""

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
    """На экране должен быть список, в котором виден нужный номер."""

    back_to_results(page)

    if kn_in(page_text(page), kn):
        return

    restore_list(page, query, page_index)

    if kn_in(page_text(page), kn):
        return

    if find_on_pages(page, kn, query):
        return

    # крайний случай: ищем номер напрямую
    run_search(page, kn)

    if not kn_in(page_text(page), kn):
        raise SearchTimeout(f"Номер не найден в выдаче: {kn}")


def wait_for_user(message):
    print("\n" + message)
    input("Когда страница поиска снова открыта, нажмите Enter...")


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
    """
    Ждёт, пока снова откроется форма поиска (интернет вернулся, сайт ожил,
    вы вошли в профиль). Если не дождались — FatalStop.
    """

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

            # первый повтор - прямо на этой странице; перезагружаем сайт
            # только если форма поиска пропала или повтор не помог
            if not quick or not search_form_ready(page):
                reload_site(page)

                if not search_form_ready(page):
                    wait_until_site_ready(page)

    wait_until_site_ready(page)
    return action()


def card_to_record(card_text):
    data = parse_card(card_text)
    data["status"] = "ok"
    return data


def find_building_by_address(page, gis, query, max_pages=3):
    """Ищет здание (без кв./ком./пом.) по адресу дома.
    Возвращает (строка_результата, номер_страницы) или (None, 1)."""

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


def add_comment(rec, text):
    rec["comment"] = (rec.get("comment") + "; " if rec.get("comment") else "") + text


def process_house(page, house, rec):
    gis = parse_gis_address(house["address"])
    cadastre = house["cadastre"]

    rec["comment"] = ""
    rec["skipped"] = {}
    rec["big"] = False
    rec["listed_count"] = None
    rec["rules"] = RULES_VERSION

    if gis is None:
        rec["building"] = {
            "status": "error",
            "message": "Не удалось разобрать адрес из ГИС ЖКХ",
        }
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
        return

    # ---------- 1. Здание: по кадастровому номеру, иначе по адресу ----------

    if (rec.get("building") or {}).get("status") != "ok":

        query_used = cadastre
        page_index = 1

        _, rows = with_retry(page, lambda: run_search(page, cadastre))
        row = next((r for r in rows if r["kn"] == cadastre), None)

        if row is None:
            print("   по кадастровому номеру ничего - ищем по адресу дома...")
            query_used = build_query(house["address"])
            row, page_index = find_building_by_address(page, gis, query_used)

        if row is None:
            rec["building"] = {
                "status": "not_found",
                "message": (
                    "Здание не найдено ни по кадастровому номеру, "
                    "ни по адресу"
                ),
            }
            print("   здание НЕ найдено в Росреестре (ни по номеру, ни по адресу)")
            save_debug(page, "building_not_found")
            return

        kn_rr = row["kn"]

        def open_building():
            ensure_on_list(page, kn_rr, query_used, page_index)
            return open_card(page, kn_rr)

        card = with_retry(page, open_building)
        building = card_to_record(card)

        rr = parse_rr_address(building.get("address") or "")
        building["address_match"] = match_house(gis, rr)
        building["verdict"] = building_verdict(building)

        if building.get("kn") != kn_rr:
            building["address_match"] = False

        rec["building"] = building
        back_to_results(page)

    building = rec["building"]

    # правила могли смениться - пересчитываем вердикт по сохранённым данным
    building["verdict"] = building_verdict(building)

    house["rr_cadastre"] = building.get("kn") or cadastre

    if building.get("kn") and building["kn"] != cadastre:
        add_comment(
            rec,
            f"Найден по адресу: кадастровый номер в Росреестре "
            f"{building['kn']} (в ГИС ЖКХ {cadastre})"
        )

    print(
        "   здание найдено:", building.get("purpose"),
        "| адрес совпал:", "да" if building.get("address_match") else "НЕТ",
    )
    print(
        f"   дом: площадь {building.get('area')} кв.м | "
        f"статус: {building.get('state')} | "
        f"этажей: {building.get('floors')} | год: {building.get('year')}"
    )

    # данные о доме сохраняем сразу, до поиска квартир
    save_cache_hook(rec)

    if building["verdict"] != "МКД":
        add_comment(rec, stop_reason(building))
        print("   ", stop_reason(building), "- дом пропускаем, идём к следующему")
        return

    if not building["address_match"]:
        add_comment(
            rec,
            "Адрес здания в Росреестре не совпал с ГИС ЖКХ "
            "(помещения всё равно проверялись по адресу дома)"
        )

    # ---------- 2. Список помещений дома (карточки пока не открываем) ----------

    house["rr_address"] = house_query_from_card(building)
    house_query = query_for(house)

    if house["rr_address"]:
        print("   адрес дома из Росреестра:", house["rr_address"])

    expected = sum(
        value for value in (house["residential"], house.get("nonresidential"))
        if isinstance(value, int)
    )

    if not expected and isinstance(house.get("apartments"), int):
        expected = house["apartments"]

    # Если число помещений неизвестно (в Excel пусто), общий поиск по адресу
    # не гарантирует полного списка - обязательно ищем квартиры по одной.
    unknown = not expected

    rec.setdefault("premises", {})

    print("   ищем помещения по адресу дома...")

    listing = list_pages(
        page, house, gis, rec, house_query,
        stop_at=expected or None, max_flats=MAX_FLATS,
    )

    if listing["total"] == 0 and house.get("rr_address"):
        full = house["rr_address"]
        variants = []

        if strip_country(full) != full:
            variants.append(strip_country(full))

        variants.append(None)   # шаблон из адреса ГИС ЖКХ

        for variant in variants:
            if listing["total"] != 0:
                break

            print(
                "   по полному адресу ничего - пробуем",
                "без «Российская Федерация»" if variant else "шаблон ГИС ЖКХ",
            )

            house["rr_address"] = variant
            house_query = query_for(house)

            listing = list_pages(
                page, house, gis, rec, house_query,
                stop_at=expected or None, max_flats=MAX_FLATS,
            )

    matched = listing["matched"]

    print(
        f"   поиск вернул {listing['total']}, прочитано {listing['seen']}, "
        f"помещений этого дома в списке: {len(matched)} "
        f"(по ГИС ЖКХ: {expected or '?'})"
    )

    rec["listed_count"] = len(matched)

    if matched:
        print("   в списке найдено:", summarize(matched.values()))
        print("   номера:", ", ".join(sorted(set(matched.values()))))

    if listing["big"]:
        mark_big(rec, flat_count(matched.values()))
        return

    # ---------- 3. Нет ли в доме больше 30 квартир (проверка «кв. 31») ----------

    if max(expected, len(matched)) >= BIG_PROBE_FROM:
        number = MAX_FLATS + 1
        probe = list_pages(
            page, house, gis, rec,
            query_for(house, "кв.", number),
            probe=True, max_pages=1,
        )

        if any(label_has(l, "кв.", number) for l in probe["matched"].values()):
            rec["listed_count"] = max(len(matched), number)
            mark_big(rec, number)
            return

    # ---------- 4. Открываем карточки из списка ----------

    open_listed(page, house, gis, rec, listing)

    all_matched = dict(matched)

    # ---------- 5. Если не хватает: добираем недостающие номера ----------

    list_complete = (
        listing["total"] < RESULTS_CAP
        and listing["seen"] >= listing["total"]
    )
    rec["list_complete"] = list_complete

    expected_res = expected_units(house)
    probes = {"n": 0}

    def probe_number(kind, number):
        """Ищет одно помещение (основной запрос, затем запрос в формате ГИС)."""

        queries = [query_for(house, kind, number)]

        if ALT_QUERY_VARIANTS:
            alt = build_query(house["address"], kind, number)

            if alt not in queries:
                queries.append(alt)

        for query in queries:
            probes["n"] += 1
            result = scan_list(page, house, gis, rec, query)
            all_matched.update(result)

            flats_now = flat_count(all_matched.values())

            if flats_now > MAX_FLATS:
                raise _BigHouse(flats_now)

            if any(label_has(lbl, kind, number) for lbl in result.values()):
                return True

        return False

    try:
        if expected_res:
            rooms = sum(
                1 for l in all_matched.values()
                if "ком." in (l or "") and "кв." not in (l or "")
            )
            primary = "ком." if rooms > flat_count(all_matched.values()) else "кв."

            def short():
                return found_units(rec)[0] < expected_res

            if short():
                print(
                    f"   найдено {found_units(rec)[0]} из {expected_res} - "
                    f"ищем недостающие номера ({primary})"
                )

            have = set()

            for label in all_matched.values():
                have |= label_numbers(label, primary)

            targets = [n for n in range(1, expected_res + 1) if n not in have]
            targets += list(range(expected_res + 1, expected_res + 1 + MISSING_EXTRA))

            for number in targets:
                if not short() or probes["n"] >= MAX_MISSING_PROBES:
                    break

                probe_number(primary, number)

            if short():
                numbers = set()

                for label in all_matched.values():
                    numbers |= label_numbers(label, primary)

                lost = [n for n in range(1, expected_res + 1) if n not in numbers]
                print(f"   !!! не нашли {len(lost)} шт.: {lost[:30]}")

        # пом./ком. по одному - как раньше (кв. при известном числе уже добрали)
        sum_expected = sum(
            v for v in (house["residential"], house.get("nonresidential"))
            if isinstance(v, int)
        ) or (house["apartments"] if isinstance(house.get("apartments"), int) else 0)
        unknown = not sum_expected

        if unknown:
            incomplete = ALWAYS_PROBE_NEXT or not list_complete
        else:
            incomplete = len(all_matched) < sum_expected

        if incomplete or ALWAYS_CHECK_1001:
            for kind, start in SEARCH_STAGES:
                empty_streak = 0
                forced = ALWAYS_CHECK_1001 and start == 1001

                if kind == "кв." and not unknown:
                    continue

                if not forced and not stage_needed(kind, start, all_matched, house):
                    print(f"   пропускаем «{kind}» с {start}: в списке таких нет")
                    continue

                if not forced and not unknown and len(all_matched) >= sum_expected:
                    continue

                for number in range(start, start + MAX_FLAT_FALLBACK):
                    if not forced and not unknown and len(all_matched) >= sum_expected:
                        break

                    have = set()

                    for label in all_matched.values():
                        have |= label_numbers(label, kind)

                    if number in have:
                        empty_streak = 0
                        continue

                    if probe_number(kind, number):
                        empty_streak = 0
                    else:
                        empty_streak += 1

                        if empty_streak >= EMPTY_STREAK_LIMIT:
                            print(
                                f"   {EMPTY_STREAK_LIMIT} пустых выдач подряд "
                                f"для «{kind}» с {start}, переходим дальше"
                            )
                            break

    except _BigHouse as big:
        rec["listed_count"] = len(all_matched)
        mark_big(rec, big.args[0])
        return

    rec["listed_count"] = len(all_matched)

    if not all_matched and not rec.get("comment"):
        rec["comment"] = (
            f"Помещения не найдены (поиск вернул {listing['total']}, "
            f"прочитано {listing['seen']})"
        )
        save_debug(page, "no_premises")


def process_premise(page, house, gis, rec, kn, label, query, page_index):
    """Открывает карточку помещения из списка и сохраняет данные."""

    try:
        def open_premise():
            ensure_on_list(page, kn, query, page_index)
            return open_card(page, kn)

        card = with_retry(page, open_premise)

        data = card_to_record(card)
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
        }
        print(f"   {label}: ошибка — {str(error)[:80]}")

    flag_premises(rec, house)
    save_cache_hook(rec)


def list_pages(page, house, gis, rec, query, probe=False, max_pages=None,
               stop_at=None, max_flats=None):
    """
    Ищет по запросу и читает список результатов (без открытия карточек).
    Возвращает словарь: total, seen, matched {кн: подпись},
    pages {страница: [(кн, подпись)]}, last_page, big.
    """

    max_pages = max_pages or MAX_RESULT_PAGES

    total, rows = with_retry(page, lambda: run_search(page, query, probe))

    quarter = (house.get("rr_cadastre") or house["cadastre"] or "").split(":")[:3]

    seen = set()
    matched = {}
    pages = {}
    other_quarter = {}
    rejected = {}
    samples = []
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
                if STRICT_SAME_QUARTER and row["kn"].split(":")[:3] != quarter:
                    rec.setdefault("skipped", {})[row["kn"]] = rr["label"]
                    other_quarter[row["kn"]] = (rr["label"], page_index)
                    continue

                matched[row["kn"]] = rr["label"]
                found_here.append((row["kn"], rr["label"]))
            else:
                reason = reject_reason(gis, rr)
                rejected[reason] = rejected.get(reason, 0) + 1

                if (
                    len(samples) < 4 and not probe
                    and rr["house"] == (gis or {}).get("house")
                ):
                    samples.append(f"{row['kn']}  {row['address']}  [{reason}]")

        pages[page_index] = found_here

        if page_index == 1 and new_rows and not matched and not probe:
            save_debug(page, "no_premises_matched")

        # лимит считаем по КВАРТИРАМ: как только их больше 30 — сразу выходим
        if max_flats and flat_count(matched.values()) > max_flats:
            big = True
            break

        # --- когда дальше листать нет смысла ---
        if stop_at and len(matched) >= stop_at:
            break

        empty_pages = 0 if found_here else empty_pages + 1

        # Если выдача упёрлась в потолок (100), нужные квартиры могут быть
        # на любой странице — тогда листаем до конца, а не бросаем после 3 пустых.
        if empty_pages >= 3 and total < RESULTS_CAP:
            break

        if len(seen) >= min(total, RESULTS_CAP) or page_index >= max_pages:
            break

        before = page_text(page)

        if not go_next_page(page, page_index + 1):
            print("      (кнопка следующей страницы списка не найдена)")
            save_debug(page, "no_next_page")
            break

        if not wait_text_change(page, before):
            break

        human_pause(PAUSE_NEXT_PAGE)

        page_index += 1
        _, rows = parse_results(page_text(page))

    # Не хватает до числа по ГИС ЖКХ — добираем из других кадастровых кварталов
    # (квартиры нового дома нередко числятся в соседнем квартале). Такие строки
    # помечаются в «Статус проверки» как «Другой кадастровый квартал».
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

    if not probe and rejected and (not stop_at or len(matched) < stop_at):
        print("      отклонено адресов:", "; ".join(
            f"{reason} — {count}" for reason, count in sorted(
                rejected.items(), key=lambda item: -item[1]
            )
        ))

        for sample in samples:
            print("        пример:", sample)

    return {
        "query": query,
        "total": total,
        "seen": len(seen),
        "matched": matched,
        "pages": pages,
        "last_page": page_index,
        "big": big,
    }


def open_listed(page, house, gis, rec, listing):
    """Открывает карточки помещений из уже прочитанного списка."""

    query = listing["query"]
    last = listing["last_page"]

    # сначала страница, на которой мы сейчас стоим, потом остальные
    order = [last] + [p for p in sorted(listing["pages"]) if p != last]

    for page_index in order:
        for kn, label in listing["pages"].get(page_index, []):
            existing = rec["premises"].get(kn)

            if (
                existing and existing.get("card_ok")
                and "ownership" in existing and existing.get("parse_v") == 2
            ):
                continue

            process_premise(
                page, house, gis, rec, kn, label, query, page_index
            )


def scan_list(page, house, gis, rec, query):
    """Поиск одного помещения: читаем первую страницу и открываем карточку."""

    listing = list_pages(page, house, gis, rec, query, probe=True, max_pages=1)
    open_listed(page, house, gis, rec, listing)

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


def summarize(labels):
    """'квартир 2, помещений 1, комнат 0' по подписям из списка."""

    flats = flat_count(labels)
    rooms = sum(1 for l in labels if "ком." in (l or ""))
    premises = sum(1 for l in labels if "пом." in (l or ""))

    return f"квартир {flats}, помещений {premises}, комнат {rooms}"


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


_cache_saver = {"fn": None}


def save_cache_hook(_rec):
    if _cache_saver["fn"]:
        _cache_saver["fn"]()


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

    # дом обработан по старым правилам - перепроверить
    if rec.get("done") and rec.get("rules") != RULES_VERSION:
        if house is None:
            return True

        # дом, обработанный по старым правилам, перепроверяем только если
        # по нему не нашлись все квартиры (остальные трогать незачем)
        return not completeness(house, rec)[0]

    # в старом кэше нет формы собственности — карточки нужно перечитать
    if rec.get("done") and any(
        item.get("card_ok")
        and ("ownership" not in item or item.get("parse_v") != 2)
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


def run_check():
    from playwright.sync_api import sync_playwright

    t_start = time.time()

    houses = read_houses()
    cache = load_cache()

    print(f"\nДомов в листе «{INPUT_SHEET}»: {len(houses)}")

    limit = ask_limit(len(houses))

    original_stdout = sys.stdout
    sys.stdout = Tee(original_stdout, "rosreestr_log.txt")
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

        _cache_saver["fn"] = lambda: save_cache(cache)   # быстро; Excel - после каждого дома

        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            page.bring_to_front()

            print("\n===================================")
            print("  ПРОВЕРКА В РОСРЕЕСТРЕ (автоматический режим)")
            print("===================================")

            if not search_form_ready(page):
                print("1. Войдите в профиль и пройдите капчу, если просят.")
                print("2. Откройте форму поиска «Адрес или кадастровый номер».")
                input("3. Нажмите Enter, когда форма поиска готова...")
            else:
                print("Форма поиска уже открыта, начинаем.")

            print("\nДальше программа работает сама. Журнал: rosreestr_log.txt")

            # какие дома берём в работу
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
                    fails0 = sum(STATS["fail"].values())

                    try:
                        process_house(page, house, rec)
                        rec["done"] = True
                        consecutive_fails = 0

                        building = rec.get("building") or {}
                        print(
                            "   итог:", building.get("status"),
                            "|", building.get("verdict"),
                            "| помещений:", len(rec.get("premises") or {}),
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
                    rec["fails"] = sum(STATS["fail"].values()) - fails0

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
            print("Время работы:", int((time.time() - t_start) / 60), "мин")
            print("Обработано домов:", len(selected) - len(failed))
            print("С ошибками (запустите ещё раз):", len(failed))
            print("Файл Excel:", OUTPUT_FILE)
            print(
                f"Поисков: {STATS['searches']}, карточек: {STATS['cards']}, "
                f"сбоев: {STATS['fail']}, ошибки HTTP: {STATS['http']}, "
                f"лимитов запросов: {STATS['rate_limited']}"
            )
            print("Подробности по сбоям:", STATS_FILE, "| лист «Диагностика» в Excel")

        except FatalStop as error:
            persist(force=True)
            print("\a\n!!! ОСТАНОВЛЕНО:", error)

        except KeyboardInterrupt:
            print("\nОстановлено пользователем, сохраняем...")
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

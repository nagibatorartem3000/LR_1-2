import pytest

import rosreestr_optimized as r
from fakes import FakePage, FakeRosreestr


HAR_ELEMENT = {
    "cadNumber": "43:40:000298:96",
    "address": {
        "readableAddress": "Кировская обл, г Киров, ул Гагарина, д 32, кв 12",
        "street": "ул Гагарина",
        "house": "32",
        "flat": "12",
        "korpus": "",
        "litera": "",
        "region": "Кировская",
    },
    "objType": "206",
    "purpose": "206002000000",
    "status": "1",
    "cadCost": "3250000.0",
    "cadQuarter": "43:40:000298",
    "levelFloor": "03",
    "ownershipType": "01",
    "parentCadNumber": "43:40:000298:90",
    "oldNumbers": [],
    "childCadNumbers": [],
    "mainCharacters": [
        {"description": "Площадь", "value": "59.90", "unitDescription": "кв.м"},
    ],
    "rights": [{
        "rightTypeDesc": "Собственность",
        "rightNumber": "43:40:000298:96-43/079/2026-2",
        "rightRegDate": 1767225600000,
        "ownershipType": "01",
        "sharedOwnershipType": False,
    }],
    "encumbrances": [],
}


def test_card_from_har_element():
    card = r.card_from_api_element(HAR_ELEMENT)

    assert card["kn"] == "43:40:000298:96"
    assert card["state"] == "Актуально"
    assert card["purpose"] == "Жилое помещение"
    assert card["purpose_code"] == "206002000000"
    assert card["floor"] == "3"
    assert card["area"] == "59.90"
    assert card["cost"] == "3250000.0"
    assert card["cadQuarter"] == "43:40:000298"
    assert card["parentCadNumber"] == "43:40:000298:90"
    assert card["parse_v"] == 3
    assert card["rights"][0]["kind"] == "Собственность"
    assert card["rights"][0]["date"]
    assert card["address_fields"]["house"] == "32"
    assert card["address_fields"]["flat"] == "12"


def test_ownership_from_card_unconfirmed_code():
    card = r.card_from_api_element(HAR_ELEMENT)

    form = r.ownership_from_card(card)
    assert form == "Код 01 (не раскрыт)"

    r.OWNERSHIP_CODE_MAP["01"] = "Частная"
    try:
        assert r.ownership_from_card(card) == "Частная"
    finally:
        r.OWNERSHIP_CODE_MAP.clear()

    no_code = dict(card, ownershipType="", ownership_text="")
    assert "Собственность" in r.ownership_from_card(no_code)


def test_status_not_1_is_cancelled():
    el = dict(HAR_ELEMENT, status="2")
    card = r.card_from_api_element(el)
    assert card["state"] == "Погашено"


def test_search_rows_dict_body_is_unreadable():
    with pytest.raises(r.UnreadableAnswer):
        r._search_rows_from_json({"error": "temporarily unavailable"}, "term")

    with pytest.raises(r.UnreadableAnswer):
        r._search_rows_from_json("oops", "term")

    assert r._search_rows_from_json([], "term") == []

    rows = r._search_rows_from_json([
        {"cadnum": "43:40:000001:5", "full_name": "ул Ленина, д 5, кв 5",
         "actual": True, "type": "FLAT"},
        {"cadnum": "мусор", "full_name": "x", "actual": True, "type": "FLAT"},
    ], "term")
    assert len(rows) == 1 and rows[0]["kn"] == "43:40:000001:5"


class BreakPage:
    def evaluate(self, _js, _args):
        return {"status": 0, "text": "__TIMEOUT__"}


def test_api_fetch_classification():
    service = FakeRosreestr()
    page = FakePage(service)

    data = r.api_fetch(page, "GET", "/address/search",
                       params={"term": "x", "objType": "all"})
    assert isinstance(data, list)

    with pytest.raises(r.SearchTimeout):
        r.api_fetch(BreakPage(), "GET", "/address/search")

    for status, expected in (
        (406, r.CaptchaRequired), (401, r.CaptchaRequired),
        (403, r.CaptchaRequired), (429, r.RateLimited),
        (500, r.ServerBusy), (502, r.ServerBusy), (418, r.UnreadableAnswer),
    ):
        class One:
            def evaluate(self, _js, _args):
                return {"status": status, "text": "boom"}

        with pytest.raises(expected):
            r.api_fetch(One(), "GET", "/address/search")

    class NotJson:
        def evaluate(self, _js, _args):
            return {"status": 200, "text": "<html>service unavailable</html>"}

    with pytest.raises(r.UnreadableAnswer):
        r.api_fetch(NotJson(), "GET", "/address/search")


def test_fetch_card_api_empty_means_absent():
    service = FakeRosreestr()
    page = FakePage(service)

    assert r.fetch_card_api(page, "43:40:000001:999") is None
    assert r.STATS["empty_results"] >= 1


def test_fetch_card_api_error_not_absent():
    service = FakeRosreestr()
    service.fail_kns.add("43:40:000001:5")
    page = FakePage(service)

    with pytest.raises(r.ServerBusy):
        r.fetch_card_api(page, "43:40:000001:5")


def test_card_with_numeric_json_values():
    """Живой прогон: JSON отдаёт числа (площадь, стоимость) не строками."""

    el = dict(HAR_ELEMENT)
    el["cadCost"] = 3250000.5
    el["area"] = 59.9
    el["mainCharacters"] = [
        {"description": "Площадь", "value": 1200.5, "unitDescription": "кв.м"},
    ]

    card = r.card_from_api_element(el)

    assert card["kn"] == "43:40:000298:96"
    assert card["area"] == "1200.5"
    assert card["cost"] == "3250000.5"
    assert card["rights"][0]["number"] == "43:40:000298:96-43/079/2026-2"
    assert isinstance(card["area"], str) and isinstance(card["cost"], str)


def test_kn_server_error_not_treated_as_not_found():
    """Сбой карточки по КН не превращает дом в «не найден» — должен быть ServerBusy."""

    service = FakeRosreestr()
    service.add_building("43:40:000001:1", "Кировская обл, г Киров, ул Ленина, д 5")
    service.fail_kns.add("43:40:000001:1")
    page = FakePage(service)

    house = {
        "address": "обл Кировская, г Киров, ул Ленина, д. 5",
        "cadastre": "43:40:000001:1",
    }
    gis = r.parse_gis_address(house["address"])

    with pytest.raises(r.ServerBusy):
        r.api_find_building(page, house, gis, r.HouseDiag())

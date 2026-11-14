import pytest

import rosreestr_optimized as r
from fakes import FakePage, FakeRosreestr

Q = "43:40:000001"
ADDR = "Кировская обл, г Киров, ул Ленина, д 5"


def make_service(n_flats, rooms=0, nonres=0, **kw):
    service = FakeRosreestr()
    service.add_building(f"{Q}:1", ADDR, quarter=Q)

    flat_numbers = kw.pop("flat_numbers", None) or list(
        range(1, n_flats + 1)
    )
    for i, num in enumerate(flat_numbers, start=1):
        service.add_premise(
            f"{Q}:{100 + i}", f"{ADDR}, кв {num}", quarter=Q
        )
    for i in range(rooms):
        service.add_premise(
            f"{Q}:{200 + i}", f"{ADDR}, ком {i + 1}",
            purpose="206002000000", quarter=Q,
        )
    for i in range(nonres):
        service.add_premise(
            f"{Q}:{300 + i}", f"{ADDR}, пом {i + 1}",
            purpose="206001000000", quarter=Q,
        )
    return service


def make_house(residential, nonresidential=None):
    return {
        "raw": {},
        "address": "г Киров, ул Ленина, д 5",
        "cadastre": f"{Q}:1",
        "residential": residential,
        "nonresidential": nonresidential,
        "apartments": None,
    }


@pytest.fixture(autouse=True)
def fast_quiet(monkeypatch):
    monkeypatch.setattr(r, "human_pause", lambda seconds: None)
    monkeypatch.setattr(r, "PACE", {"factor": 1.0})
    for key in ("api_calls", "searches", "cards", "rate_limited",
                "retries", "empty_results"):
        r.STATS[key] = 0
    r.STATS["fail"] = {}
    r.STATS["http"] = {}
    r._cache_saver["fn"] = None
    yield


def run_house(service, residential, nonresidential=None):
    page = FakePage(service)
    house = make_house(residential, nonresidential)
    rec = {}
    diag = r.HouseDiag()
    r.process_house(page, house, rec, diag)
    return house, rec, diag


def count_kinds(rec):
    counts = {"кв": set(), "ком": 0, "пом": 0}
    for item in (rec.get("premises") or {}).values():
        if not item.get("card_ok"):
            continue
        label = item.get("label") or ""
        if "кв." in label:
            counts["кв"] |= r.flat_ids(label)
        elif "ком." in label:
            counts["ком"] += 1
        elif "пом." in label:
            counts["пом"] += 1
    return counts


def test_scenario_1_five_flats():
    house, rec, diag = run_house(make_service(5), 5)
    counts = count_kinds(rec)
    assert len(counts["кв"]) == 5
    assert rec["building"]["verdict"] == "МКД"
    complete, why, _ = r.completeness(house, rec)
    assert complete, why


def test_scenario_2_fifteen_flats():
    house, rec, diag = run_house(make_service(15), 15)
    assert len(count_kinds(rec)["кв"]) == 15
    complete, why, _ = r.completeness(house, rec)
    assert complete, why


def test_scenario_3_thirty_flats():
    house, rec, diag = run_house(make_service(30), 30)
    assert len(count_kinds(rec)["кв"]) == 30
    assert not rec.get("big")
    complete, why, _ = r.completeness(house, rec)
    assert complete, why


def test_scenario_4_over_thirty_flats_is_skipped():
    house, rec, diag = run_house(make_service(40), 40)
    assert rec.get("big")
    assert "больше" in rec.get("comment", "")
    counts = count_kinds(rec)
    assert len(counts["кв"]) <= 40


def test_scenario_5_rooms_do_not_count_into_limit():
    service = make_service(25, rooms=10)
    house, rec, diag = run_house(service, 25)
    counts = count_kinds(rec)
    assert len(counts["кв"]) == 25
    assert counts["ком"] == 10
    assert not rec.get("big")
    complete, why, _ = r.completeness(house, rec)
    assert complete, why


def test_scenario_6_nonresidential_do_not_count_into_limit():
    service = make_service(10, nonres=25)
    house, rec, diag = run_house(service, 10, nonresidential=25)
    counts = count_kinds(rec)
    assert len(counts["кв"]) == 10
    assert counts["пом"] == 25
    assert not rec.get("big")
    complete, why, _ = r.completeness(house, rec)
    assert complete, why


def test_scenario_7_hidden_flats_found_by_probes():
    service = make_service(15)
    service.sample_size = 10
    service.sample_step = 7
    house, rec, diag = run_house(service, 15)
    assert len(count_kinds(rec)["кв"]) == 15
    complete, why, _ = r.completeness(house, rec)
    assert complete, why


def test_scenario_8_nonstandard_numbering():
    service = make_service(0)
    for i, num in enumerate(["1", "2", "3", "5", "7А"], start=1):
        service.add_premise(f"{Q}:{100 + i}", f"{ADDR}, кв {num}", quarter=Q)

    service.sample_size = 3
    service.sample_step = 1

    house = make_house(5)
    page = FakePage(service)
    rec = {}
    diag = r.HouseDiag()
    r.process_house(page, house, rec, diag)

    ids = count_kinds(rec)["кв"]
    assert {"1", "2", "3", "5", "7а"} <= ids
    complete, why, _ = r.completeness(house, rec)
    assert complete, why


def test_scenario_9_unopenable_card_is_not_complete():
    service = make_service(5)
    bad_kn = f"{Q}:103"
    service.fail_kns.add(bad_kn)

    house, rec, diag = run_house(service, 5)
    counts = count_kinds(rec)
    assert len(counts["кв"]) == 4

    failed = [
        p for p in rec["premises"].values()
        if str(p.get("check") or "").startswith("Ошибка")
    ]
    assert failed and failed[0]["needs_retry"]

    complete, why, _ = r.completeness(house, rec)
    assert not complete
    assert "карточки" in why or "ошиб" in why.lower()
    assert r.needs_retry(rec)


def test_scenario_10_service_down_is_not_missing():
    service = make_service(5)
    service.fail_search = True

    house = make_house(5)
    page = FakePage(service)
    rec = {}
    diag = r.HouseDiag()

    with pytest.raises((r.ServerBusy, r.ConnectionLost)):
        r.process_house(page, house, rec, diag)

    rec["comment"] = f"Ошибка: сервис недоступен"
    rec["done"] = False
    assert r.needs_retry(rec)
    complete, why, _ = r.completeness(house, rec)
    assert not complete


def test_scenario_10b_transient_outage_recovers():
    service = make_service(5)
    service.search_fails_first = 2

    house, rec, diag = run_house(service, 5)
    assert len(count_kinds(rec)["кв"]) == 5
    complete, why, _ = r.completeness(house, rec)
    assert complete, why


def test_scenario_11_similar_addresses_rejected():
    service = make_service(5)
    service.add_building(f"{Q}:50", "Кировская обл, г Киров, ул Ленина, д 5к2",
                         quarter=Q)
    for i in range(1, 4):
        service.add_premise(f"{Q}:{400 + i}",
                            f"Кировская обл, г Киров, ул Ленина, д 5к2, кв {i}",
                            quarter=Q)

    house, rec, diag = run_house(service, 5)
    counts = count_kinds(rec)
    assert len(counts["кв"]) == 5

    for item in rec["premises"].values():
        assert "5к2" not in (item.get("address") or "") or not item["card_ok"] \
            or item["label"].startswith("кв.")

    complete, why, _ = r.completeness(house, rec)
    assert complete, why


def test_scenario_12_other_cadastral_quarter():
    service = make_service(0)
    service.add_building(f"{Q}:1", ADDR, quarter=Q)

    other_q = "43:40:000777"
    for i in range(1, 6):
        service.add_premise(
            f"{other_q}:{500 + i}", f"{ADDR}, кв {i}", quarter=other_q
        )

    house, rec, diag = run_house(service, 5)
    counts = count_kinds(rec)
    assert len(counts["кв"]) == 5
    complete, why, _ = r.completeness(house, rec)
    assert complete, why


def test_no_false_complete_anywhere():
    for n in (5, 15, 30):
        house, rec, diag = run_house(make_service(n), n)
        complete, _, _ = r.completeness(house, rec)
        assert complete

    house, rec, diag = run_house(make_service(4), 5)
    complete, _, _ = r.completeness(house, rec)
    assert not complete


def test_float_residential_from_excel():
    """Excel отдаёт числа как float (4.0) — дом не должен считаться «неизвестным»."""

    house, rec, diag = run_house(make_service(4), 4.0)
    counts = count_kinds(rec)

    assert len(counts["кв"]) == 4
    assert not rec.get("needs_retry")

    complete, why, _ = r.completeness(house, rec)
    assert complete, why


SOVETSK_BLD = (
    "Российская Федерация, Кировская область, Советский муниципальный район, "
    "г. Советск, ул. Гагарина, д. 23"
)


def test_gis_city_wrong_flats_still_found():
    """Живой прогон [29/397]: ГИС ЖКХ указывает г Киров, дом реально в Советске."""

    service = FakeRosreestr()
    service.add_building("43:31:010153:446", SOVETSK_BLD, quarter="43:31:010153")

    for i in range(1, 5):
        service.add_premise(
            f"43:31:010153:{200 + i}",
            SOVETSK_BLD + f", кв. {i}",
            quarter="43:31:010153",
        )

    page = FakePage(service)
    house = {
        "raw": {},
        "address": "обл Кировская, г Киров, ул Гагарина, д. 23",
        "cadastre": "43:31:010153:446",
        "residential": 4,
        "nonresidential": None,
        "apartments": None,
    }
    rec = {}
    diag = r.HouseDiag()
    r.process_house(page, house, rec, diag)

    counts = count_kinds(rec)
    assert len(counts["кв"]) == 4, sorted(counts["кв"])

    complete, why, _ = r.completeness(house, rec)
    assert complete, why


def test_save_excel_with_string_rights(tmp_path, monkeypatch):
    """Живой прогон: права строкой (формат UI/старого кэша) не роняют Excel."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(r, "OUTPUT_FILE", str(tmp_path / "out.xlsx"))

    house = make_house(2)
    rec = {
        "building": {
            "status": "ok", "verdict": "МКД", "kn": "43:40:000001:1",
            "address": ADDR, "address_match": True,
        },
        "premises": {
            "43:40:000001:101": {
                "kn": "43:40:000001:101", "label": "кв. 1", "card_ok": True,
                "state": "Актуально", "area": "50",
                "rights": "Собственность 43:40:000001:101-1 от 2026-01-01",
                "restrictions": "", "parse_v": 3,
            },
            "43:40:000001:102": {
                "kn": "43:40:000001:102", "label": "кв. 2", "card_ok": True,
                "state": "Актуально", "area": "51",
                "rights": [{"kind": "Собственность", "number": "N-2",
                            "date": "2026-01-02"}],
                "restrictions": [], "parse_v": 3,
            },
        },
        "listed_count": 2,
        "list_complete": True,
        "rules": r.RULES_VERSION,
        "done": True,
        "diag": {},
    }

    r.save_excel([house], {house["cadastre"]: rec})

    assert (tmp_path / "out.xlsx").exists()


def test_building_children_collected_even_if_listing_empty():
    """Живой прогон [2/397]: адресный поиск видит только здание, но в карточке
    здания перечислены помещения (childCadNumbers) — они должны попасть в итог."""

    service = FakeRosreestr()
    service.add_building(f"{Q}:1", ADDR, quarter=Q)
    service.objects[f"{Q}:1"]["childCadNumbers"] = [f"{Q}:101", f"{Q}:102"]

    for i in (1, 2):
        service.add_premise(f"{Q}:{100 + i}", f"{ADDR}, кв. {i}", quarter=Q)

    page = FakePage(service)
    house = make_house(None)
    rec = {}
    diag = r.HouseDiag()
    r.process_house(page, house, rec, diag)

    counts = count_kinds(rec)
    assert len(counts["кв"]) == 2, sorted(counts["кв"])


def test_zero_found_probes_deeper_than_three():
    """Живой прогон [2/397]: 0 найденных, ожидаемое неизвестно — пробы не
    останавливаются после 3 пустых, а ищут глубже (кв. 7 найдена)."""

    service = FakeRosreestr()
    service.add_building(f"{Q}:1", ADDR, quarter=Q)
    service.add_premise(f"{Q}:107", f"{ADDR}, кв. 7", quarter=Q)

    page = FakePage(service)
    house = make_house(None)
    rec = {}
    diag = r.HouseDiag()
    r.process_house(page, house, rec, diag)

    counts = count_kinds(rec)
    assert "7" in counts["кв"], sorted(counts["кв"])

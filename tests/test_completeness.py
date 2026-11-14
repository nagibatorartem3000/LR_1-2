import rosreestr_optimized as r


def make_house(residential=5, nonresidential=None):
    return {
        "raw": {},
        "address": "г Киров, ул Ленина, д 5",
        "cadastre": "43:40:000001:1",
        "residential": residential,
        "nonresidential": nonresidential,
        "apartments": None,
    }


def make_rec(flats=(), bad=0, **extra):
    premises = {}
    for label in flats:
        kn = f"43:40:000001:{label}"
        premises[kn] = {
            "kn": kn, "label": f"кв. {label}", "card_ok": True,
            "state": "Актуально", "area": "50", "purpose": "Жилое помещение",
            "rights": [], "restrictions": [], "parse_v": 3,
        }
    for i in range(bad):
        kn = f"43:40:000001:9{i}"
        premises[kn] = {
            "kn": kn, "label": f"кв. 9{i}", "card_ok": False,
            "check": "Ошибка: карточка не открылась", "needs_retry": True,
        }
    rec = {
        "building": {
            "status": "ok", "verdict": "МКД", "kn": "43:40:000001:1",
            "address": "Кировская обл, г Киров, ул Ленина, д 5",
            "address_match": True,
        },
        "premises": premises,
        "listed_count": len(premises),
        "list_complete": True,
        "rules": r.RULES_VERSION,
        "done": True,
        "diag": {},
    }
    rec.update(extra)
    return rec


def test_complete_house_is_complete():
    house = make_house(5)
    complete, why, missing = r.completeness(house, make_rec([1, 2, 3, 4, 5]))
    assert complete, why
    assert missing == []


def test_error_finish_is_never_complete():
    house = make_house(5)
    rec = make_rec([1, 2, 3, 4, 5], diag={"finish_reason": "ошибка",
                                          "errors": 2})
    complete, why, _ = r.completeness(house, rec)
    assert not complete
    assert "поиск завершён не полностью" in why


def test_capped_is_never_complete():
    house = make_house(5)
    rec = make_rec([1, 2, 3, 4, 5], diag={"finish_reason": "лимит 100",
                                          "capped": 1})
    complete, why, _ = r.completeness(house, rec)
    assert not complete
    assert "лимитом 100" in why


def test_missing_flats_is_never_complete():
    house = make_house(5)
    rec = make_rec([1, 2, 3, 4])
    complete, why, missing = r.completeness(house, rec)
    assert not complete
    assert missing == [5]
    assert "найдено 4 из 5" in why


def test_bad_cards_are_never_complete():
    house = make_house(5)
    rec = make_rec([1, 2, 3, 4, 5], bad=1)
    complete, why, _ = r.completeness(house, rec)
    assert not complete
    assert "карточки" in why


def test_incomplete_list_without_gis_count():
    house = make_house(0)
    rec = make_rec([1, 2], list_complete=False)
    complete, why, _ = r.completeness(house, rec)
    assert not complete
    assert "неизвестно" in why


def test_not_mkd_or_big_is_out_of_scope():
    house = make_house(5)
    rec = make_rec([])
    rec["building"]["verdict"] = "Не МКД"
    complete, _, _ = r.completeness(house, rec)
    assert complete

    rec2 = make_rec([])
    rec2["big"] = True
    complete2, _, _ = r.completeness(house, rec2)
    assert complete2


def test_needs_retry_on_error_finish():
    rec = make_rec([1, 2, 3, 4, 5], diag={"finish_reason": "ошибка"})
    assert r.needs_retry(rec)

    rec_ok = make_rec([1, 2, 3, 4, 5])
    assert not r.needs_retry(rec_ok)

    rec_old = make_rec([1, 2, 3, 4, 5])
    rec_old["rules"] = 5
    assert not r.needs_retry(rec_old, make_house(5))
    assert r.needs_retry(rec_old)

    rec_old_bad = make_rec([1, 2, 3, 4])
    rec_old_bad["rules"] = 5
    assert r.needs_retry(rec_old_bad, make_house(5))


def test_needs_retry_on_premise_failure():
    rec = make_rec([1, 2, 3, 4, 5], bad=1)
    assert r.needs_retry(rec)

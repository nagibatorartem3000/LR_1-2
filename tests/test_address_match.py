import rosreestr_optimized as r


GIS = r.parse_gis_address("г Киров, ул Ленина, д 5")


def rr(address):
    return r.parse_rr_address(r.normalize(address))


def test_street_abbreviations():
    for variant in (
        "г Киров, ул Ленина, д 5, кв 1",
        "Кировская обл, г Киров, улица Ленина, дом 5, квартира 1",
        "обл Кировская, г. Киров, ул. Ленина, д. 5, кв. 1",
    ):
        assert r.match_house(GIS, rr(variant)), variant


def test_province_prefix_is_not_extra_word():
    level, reason = r.match_level(GIS, rr(
        "Кировская обл, г Киров, ул Ленина, д 5, кв 12"
    ))
    assert level in ("EXACT", "STRONG")
    assert r.match_house(GIS, rr(
        "Кировская обл, г Киров, ул Ленина, д 5, кв 12"
    ))


def test_korpus_stroenie_litera():
    assert not r.match_house(GIS, rr("г Киров, ул Ленина, д 5к2, кв 1"))
    assert not r.match_house(GIS, rr("г Киров, ул Ленина, д 5 с2, кв 1"))

    gis_k = r.parse_gis_address("г Киров, ул Ленина, д 5к2")
    assert r.match_house(gis_k, rr("г Киров, ул Ленина, д 5 к2, кв 1"))
    assert not r.match_house(gis_k, rr("г Киров, ул Ленина, д 5, кв 1"))

    gis_s = r.parse_gis_address("г Киров, ул Ленина, д 5с1")
    assert r.match_house(gis_s, rr("г Киров, ул Ленина, д 5 стр 1, кв 1"))

    gis_l = r.parse_gis_address("г Киров, ул Ленина, д 5лА")
    assert r.match_house(gis_l, rr("г Киров, ул Ленина, д 5 лит А, кв 1"))


def test_fractional_and_letter_houses():
    gis_frac = r.parse_gis_address("г Киров, ул Ленина, д 5/2")
    assert r.match_house(gis_frac, rr("г Киров, ул Ленина, д 5/2, кв 3"))
    assert not r.match_house(gis_frac, rr("г Киров, ул Ленина, д 5, кв 3"))

    gis_let = r.parse_gis_address("г Киров, ул Ленина, д 5а")
    assert r.match_house(gis_let, rr("г Киров, ул Ленина, д 5а, кв 3"))
    assert not r.match_house(gis_let, rr("г Киров, ул Ленина, д 5, кв 3"))
    assert not r.match_house(gis_let, rr("г Киров, ул Ленина, д 5б, кв 3"))


def test_reject_neighbor_and_other_street():
    assert not r.match_house(GIS, rr("г Киров, ул Ленина, д 7, кв 1"))
    assert not r.match_house(GIS, rr(
        "Кировская обл, г Киров, ул Гагарина, д 5, кв 1"
    ))
    level, reason = r.match_level(GIS, rr("г Киров, ул Ленина, д 7, кв 1"))
    assert reason == "другой номер дома"


def test_city_words_required():
    level, reason = r.match_level(GIS, rr(
        "Нововятский район, ул Ленина, д 5, кв 2"
    ))
    assert level is None
    assert "города" in reason


def test_flat_label_variants():
    assert r.premise_label("ул Ленина, д 5, кв 7А") == "кв. 7А"
    assert r.premise_label("ул Ленина, д 5, ком 3") == "ком. 3"
    assert r.premise_label("ул Ленина, д 5, пом 12") == "пом. 12"
    assert r.label_has("кв. 7а", "кв.", "7")
    assert not r.label_has("кв. 12", "кв.", "1")
    assert r.label_has("кв. 12", "кв.", "12")


def test_match_house_fields_api():
    fields = {
        "street": "ул Ленина",
        "house": "5",
        "flat": "59",
        "korpus": "",
    }
    level, _ = r.match_house_fields(GIS, fields)
    assert level == "EXACT"

    level, reason = r.match_house_fields(GIS, {**fields, "house": "7"})
    assert level is None and "дом" in reason

    level, _ = r.match_house_fields(
        r.parse_gis_address("г Киров, ул Ленина, д 5к1"),
        {"street": "Ленина", "house": "5", "korpus": "1"},
    )
    assert level == "EXACT"

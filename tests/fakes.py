"""Мок сервиса Росреестра: тот же контракт, что у lk.rosreestr.ru/account-back."""

import json
import re
from urllib.parse import urlparse, parse_qs


FLAT_RE = re.compile(r"кв\.?\s*([0-9]+[а-яa-z]?(?:/[0-9]+)?)(?!\w)", re.I)
ROOM_RE = re.compile(r"ком\.?\s*([0-9]+[а-яa-z]?)(?!\w)", re.I)
PREM_RE = re.compile(r"пом\.?\s*([0-9]+[а-яa-z]?|[ivx]+)(?!\w)", re.I)
KIND_RES = {"кв": FLAT_RE, "ком": ROOM_RE, "пом": PREM_RE}


def norm(text):
    return re.sub(r"\s+", " ", str(text or "").lower())


def flat_id_of(name):
    m = FLAT_RE.search(name)
    return m.group(1).lower() if m else None


class FakePage:
    """page.evaluate(API_FETCH_JS, [url, options]) -> {"status", "text"}."""

    def __init__(self, service):
        self.service = service

    def evaluate(self, _js, args):
        url, options = args
        return self.service.fetch(url, options or {})


class FakeRosreestr:
    def __init__(self):
        self.objects = {}
        self.fail_search = False
        self.fail_kns = set()
        self.search_fails_first = 0
        self.sample_size = None
        self.sample_step = 7
        self.search_counter = 0
        self.on_calls = 0
        self.search_calls = 0

    def add_building(self, kn, address, purpose="204003000000",
                     quarter="43:40:000001", floors="5"):
        self.objects[kn] = {
            "cadNumber": kn,
            "address": {"readableAddress": address},
            "objType": "204",
            "purpose": purpose,
            "status": "1",
            "cadCost": "12000000.0",
            "cadQuarter": quarter,
            "floors": floors,
            "mainCharacters": [
                {"description": "Площадь", "value": "1200.5",
                 "unitDescription": "кв.м"},
            ],
        }

    def add_premise(self, kn, address, purpose="206002000000",
                    quarter="43:40:000001", area="50.5", floor=3,
                    ownership="01", rights=None, status="1"):
        self.objects[kn] = {
            "cadNumber": kn,
            "address": {"readableAddress": address},
            "objType": "206",
            "purpose": purpose,
            "status": status,
            "cadCost": "1500000.5",
            "cadQuarter": quarter,
            "levelFloor": f"{floor:02d}",
            "ownershipType": ownership,
            "parentCadNumber": "",
            "oldNumbers": [],
            "childCadNumbers": [],
            "mainCharacters": [
                {"description": "Площадь", "value": area,
                 "unitDescription": "кв.м"},
            ],
            "rights": rights if rights is not None else [{
                "rightTypeDesc": "Собственность",
                "rightNumber": f"{kn}-43/079/2026-2",
                "rightRegDate": 1767225600000,
                "ownershipType": ownership,
                "sharedOwnershipType": False,
            }],
            "encumbrances": [],
        }

    def _matches(self, term, name):
        term = norm(term)
        flat_part = None
        for kind, regex in KIND_RES.items():
            m = regex.search(term)
            if m:
                flat_part = (kind, m.group(1).lower())
                term = (term[:m.start()] + " " + term[m.end():]).strip()
                break

        tokens = [t for t in re.split(r"[^0-9a-zа-яё/]+", term) if t]
        name_low = norm(name)

        if any(t not in name_low for t in tokens):
            return False

        if flat_part is None:
            return True

        kind, number = flat_part
        name_id = flat_id_of(name_low)

        if kind == "кв":
            if name_id is None:
                return False
            if name_id == number:
                return True
            return name_id.startswith(number) and not name_id[
                len(number):
            ].isdigit()

        other = re.search(
            rf"{kind}\.?\s*([0-9]+[а-яa-z]?)(?!\w)", name_low
        )
        return bool(other) and other.group(1).lower() == number

    def search(self, term):
        rows = [
            {
                "cadnum": kn,
                "full_name": obj["address"]["readableAddress"],
                "actual": obj["status"] == "1",
                "type": "PARCEL" if obj["objType"] == "204" else "FLAT",
            }
            for kn, obj in self.objects.items()
            if self._matches(term, obj["address"]["readableAddress"])
        ]
        return rows

    def fetch(self, url, options):
        parsed = urlparse(url)
        path = parsed.path

        if path.endswith("/address/search"):
            self.search_calls += 1
            term = parse_qs(parsed.query).get("term", [""])[0]

            if self.fail_search:
                return {"status": 500, "text": "Internal Server Error"}

            if self.search_fails_first > 0:
                self.search_fails_first -= 1
                return {"status": 502, "text": "Bad Gateway"}

            rows = self.search(term)

            if self.sample_size and len(rows) > self.sample_size:
                offset = (
                    (self.search_counter * self.sample_step) % len(rows)
                )
                rows = rows[offset:] + rows[:offset]
                rows = rows[:self.sample_size]

            self.search_counter += 1
            return {"status": 200, "text": json.dumps(rows, ensure_ascii=False)}

        if path.endswith("/on"):
            self.on_calls += 1
            raw_body = options.get("body") or "{}"

            if not isinstance(raw_body, str):
                raw_body = json.dumps(raw_body, ensure_ascii=False)

            body = json.loads(raw_body)
            kns = body.get("cadNumbers") or []

            if any(kn in self.fail_kns for kn in kns):
                return {"status": 500, "text": "Internal Server Error"}

            elements = [self.objects[kn] for kn in kns if kn in self.objects]
            return {
                "status": 200,
                "text": json.dumps(
                    {"elements": elements, "count": len(elements)},
                    ensure_ascii=False,
                ),
            }

        return {"status": 404, "text": "not found"}

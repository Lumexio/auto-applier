"""A small gazetteer: which countries does a posting's location text say the job can be done from?

Deliberately modest. It knows countries, a few regions ("Americas", "EMEA"), US / Canadian / Mexican states and the cities that
tech employers list. Anything it does not recognise yields no countries at all, and the callers treat "no idea" as "keep the
posting" -- a wrong guess here hides a job, a missing one only costs a model call later.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

WORLD = "*"      # a "country" that stands for "anywhere"


def fold(text: Any) -> str:
    """Lower-case, accent-free, punctuation-free: 'Querétaro, Qro.' -> 'queretaro qro'."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()


def _table(text: str) -> list[tuple[str, list[str]]]:
    rows = []
    for line in text.strip().splitlines():
        iso, _, names = line.partition(":")
        rows.append((iso.strip(), [n.strip() for n in names.split(",") if n.strip()]))
    return rows


_COUNTRIES = _table("""
AR: argentina
AU: australia
AT: austria
BD: bangladesh
BE: belgium
BO: bolivia
BR: brazil, brasil
BG: bulgaria
CA: canada
CL: chile
CN: china
CO: colombia
CR: costa rica
HR: croatia
CY: cyprus
CZ: czech republic, czechia
DK: denmark
DO: dominican republic
EC: ecuador
EG: egypt
SV: el salvador
EE: estonia
FI: finland
FR: france
DE: germany, deutschland
GH: ghana
GR: greece
GT: guatemala
HN: honduras
HK: hong kong
HU: hungary
IS: iceland
IN: india
ID: indonesia
IE: ireland, republic of ireland
IL: israel
IT: italy
JP: japan
KE: kenya
KR: south korea, korea
KW: kuwait
LV: latvia
LT: lithuania
LU: luxembourg
MY: malaysia
MT: malta
MX: mexico
MA: morocco
NL: netherlands, the netherlands, holland
NZ: new zealand
NI: nicaragua
NG: nigeria
NO: norway
PK: pakistan
PA: panama
PY: paraguay
PE: peru
PH: philippines
PL: poland
PT: portugal
PR: puerto rico
QA: qatar
RO: romania
RS: serbia
SA: saudi arabia, ksa
SG: singapore
SK: slovakia
SI: slovenia
ZA: south africa
ES: spain
LK: sri lanka
SE: sweden
CH: switzerland
TW: taiwan
TH: thailand
TR: turkey, turkiye
UA: ukraine
AE: united arab emirates, uae
GB: united kingdom, great britain, britain, england, scotland, wales, northern ireland, uk
US: united states of america, united states, usa, u s a, u s
UY: uruguay
VE: venezuela
VN: vietnam
CU: cuba
JM: jamaica
""")

_NORTH_AMERICA = {"US", "CA", "MX"}
_CENTRAL_AMERICA = {"GT", "BZ", "HN", "SV", "NI", "CR", "PA"}
_SOUTH_AMERICA = {"BR", "AR", "CL", "CO", "PE", "UY", "PY", "BO", "EC", "VE"}
_CARIBBEAN = {"DO", "CU", "PR", "JM", "BS", "TT", "HT"}
_EUROPE = {"GB", "IE", "FR", "DE", "ES", "PT", "IT", "NL", "BE", "LU", "CH", "AT", "PL", "CZ", "SK", "HU", "RO", "BG", "GR", "HR", "RS",
           "SI", "UA", "LT", "LV", "EE", "FI", "SE", "NO", "DK", "IS", "TR", "CY", "MT"}
_EU = _EUROPE - {"GB", "CH", "NO", "IS", "UA", "RS", "TR"}
_AFRICA = {"NG", "KE", "GH", "EG", "MA", "ZA", "TN"}
_MIDDLE_EAST = {"IL", "AE", "SA", "QA", "KW"}
_ASIA = {"IN", "CN", "JP", "KR", "TW", "HK", "SG", "ID", "PH", "VN", "TH", "MY", "PK", "BD", "LK"}

_REGIONS: list[tuple[set[str], list[str]]] = [
    ({WORLD}, ["worldwide", "world wide", "global", "globally", "anywhere", "anywhere in the world"]),
    (_NORTH_AMERICA, ["north america"]),
    (_NORTH_AMERICA | _CENTRAL_AMERICA | _SOUTH_AMERICA | _CARIBBEAN, ["americas", "the americas", "north and south america", "amer"]),
    ({"MX"} | _CENTRAL_AMERICA | _SOUTH_AMERICA | {"DO", "CU", "PR"}, ["latam", "latin america", "latin american"]),
    ({"US"}, ["east coast", "west coast", "midwest", "pacific northwest"]),
    (_CENTRAL_AMERICA, ["central america"]),
    (_SOUTH_AMERICA, ["south america"]),
    (_EUROPE | _AFRICA | _MIDDLE_EAST, ["emea"]),
    (_EUROPE, ["europe", "european"]),
    (_EU, ["european union", "eu"]),
    (_ASIA | {"AU", "NZ"}, ["apac", "asia pacific"]),
    (_ASIA, ["asia"]),
    (_AFRICA, ["africa"]),
    (_MIDDLE_EAST, ["middle east", "mena"]),
]

_US_STATES = ("alabama, alaska, arizona, arkansas, california, colorado, connecticut, delaware, florida, georgia, hawaii, idaho, illinois, "
              "indiana, iowa, kansas, kentucky, louisiana, maine, maryland, massachusetts, michigan, minnesota, mississippi, missouri, montana, "
              "nebraska, nevada, new hampshire, new jersey, new mexico, new york, north carolina, north dakota, ohio, oklahoma, oregon, "
              "pennsylvania, rhode island, south carolina, south dakota, tennessee, texas, utah, vermont, virginia, washington, west virginia, "
              "wisconsin, wyoming, district of columbia")
_CA_PROVINCES = ("ontario, quebec, british columbia, alberta, manitoba, saskatchewan, nova scotia, new brunswick, newfoundland, "
                 "prince edward island")
_MX_STATES = ("aguascalientes, baja california, baja california sur, campeche, chiapas, chihuahua, coahuila, colima, durango, guanajuato, "
              "guerrero, hidalgo, jalisco, estado de mexico, michoacan, morelos, nayarit, nuevo leon, oaxaca, puebla, queretaro, quintana roo, "
              "san luis potosi, sinaloa, sonora, tabasco, tamaulipas, tlaxcala, veracruz, yucatan, zacatecas, cdmx, jal, qro, chih, edomex")

_STATES: list[tuple[str, list[str]]] = [(iso, [n.strip() for n in names.split(",")]) for iso, names in
                                        (("US", _US_STATES), ("CA", _CA_PROVINCES), ("MX", _MX_STATES))]

_CITIES = _table("""
US: new york, nyc, brooklyn, manhattan, san francisco, bay area, silicon valley, los angeles, san diego, san jose, sunnyvale, mountain view, palo alto, menlo park, redwood city, santa clara, oakland, sacramento, seattle, bellevue, redmond, portland, austin, dallas, houston, san antonio, boston, chicago, denver, boulder, atlanta, miami, orlando, tampa, philadelphia, pittsburgh, arlington, reston, mclean, baltimore, raleigh, durham, charlotte, nashville, salt lake city, lehi, phoenix, scottsdale, las vegas, minneapolis, detroit, ann arbor, columbus, cleveland, cincinnati, indianapolis, st louis, kansas city, omaha, madison, milwaukee, new orleans, honolulu, jersey city, stamford, hartford, providence, fort worth, princeton, tysons, conshohocken, sf
CA: toronto, vancouver, montreal, ottawa, calgary, edmonton, waterloo, kitchener, winnipeg, halifax, quebec city, mississauga
MX: mexico city, ciudad de mexico, guadalajara, monterrey, tijuana, ciudad juarez, juarez, puebla, queretaro, merida, cancun, toluca, aguascalientes, saltillo, hermosillo, mexicali, culiacan, morelia, zapopan
GB: london, manchester, edinburgh, glasgow, bristol, leeds, belfast, cardiff, oxford
IE: dublin, cork, galway
DE: berlin, munich, hamburg, frankfurt, cologne, stuttgart, dusseldorf
FR: paris, lyon, marseille, toulouse
ES: madrid, barcelona, valencia, seville, malaga
PT: lisbon, porto, aveiro, braga, coimbra
NL: amsterdam, rotterdam, the hague, eindhoven, utrecht
BE: brussels, antwerp
CH: zurich, geneva, lausanne
AT: vienna
IT: milan, rome, turin
PL: warsaw, krakow, wroclaw, gdansk, poznan
CZ: prague, brno
HU: budapest
RO: bucharest, cluj napoca, cluj
BG: sofia
GR: athens
SE: stockholm, gothenburg
DK: copenhagen
NO: oslo
FI: helsinki
IL: tel aviv, jerusalem, haifa
TR: istanbul, ankara
UA: kyiv, kiev
IS: reykjavik
SA: riyadh, jeddah
MT: valletta
VN: ho chi minh
AE: dubai, abu dhabi
IN: bengaluru, bangalore, hyderabad, pune, mumbai, delhi, new delhi, gurgaon, gurugram, chennai, noida, kolkata, ahmedabad
SG: singapore
JP: tokyo, osaka
CN: beijing, shanghai, shenzhen, guangzhou
TW: taipei
KR: seoul
HK: hong kong
AU: sydney, melbourne, brisbane, perth, canberra
NZ: auckland, wellington
BR: sao paulo, rio de janeiro, brasilia, curitiba, belo horizonte, porto alegre
AR: buenos aires, cordoba, rosario
CO: bogota, medellin, cali, barranquilla
CL: santiago, valparaiso
PE: lima
UY: montevideo
CR: heredia
NG: lagos, abuja
KE: nairobi
ZA: cape town, johannesburg, pretoria
EG: cairo
PH: manila, cebu
VN: ho chi minh city, hanoi
TH: bangkok
MY: kuala lumpur
ID: jakarta
PK: karachi, lahore, islamabad
BD: dhaka
""")

_US_ABBREVIATIONS = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC "
                     "SD TN TX UT VT VA WA WV WI WY DC").split()
_CA_ABBREVIATIONS = "AB BC MB NB NS ON QC SK".split()           # NL and PE are left out: Amsterdam, NL / Lima, PE
_ABBREVIATION = re.compile(r"(?:,|\(|-|\u2013)\s*(" + "|".join(_US_ABBREVIATIONS + _CA_ABBREVIATIONS) + r")(?![A-Za-z])")
_US_WORD = re.compile(r"\bU\.?S\.?(?:A\.?)?(?![A-Za-z])")        # capitals only: the pronoun "us" is not a country

_KIND_ORDER = ("country", "region", "state", "city")


def _build() -> tuple[dict[str, tuple[str, frozenset[str]]], re.Pattern[str]]:
    table: dict[str, tuple[str, frozenset[str]]] = {}

    def add(kind: str, codes: set[str], names: list[str]) -> None:
        for n in names:
            table.setdefault(fold(n), (kind, frozenset(codes)))      # the first (more specific) reading of a name wins

    for iso, names in _COUNTRIES:
        add("country", {iso}, names)
    for codes, names in _REGIONS:
        add("region", codes, names)
    for iso, names in _STATES:
        add("state", {iso}, names)
    for iso, names in _CITIES:
        add("city", {iso}, names)
    aliases = sorted(table, key=len, reverse=True)
    return table, re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(a) for a in aliases) + r")(?![a-z0-9])")


_TABLE, _ALIAS = _build()
_BY_NAME = {name: next(iter(codes)) for name, (kind, codes) in _TABLE.items() if kind == "country"}


def country_code(name: Any) -> str | None:
    """'MX' -> 'MX', 'Mexico' -> 'MX', 'méxico' -> 'MX', 'Atlantis' -> None."""
    raw = str(name or "").strip()
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    return _BY_NAME.get(fold(raw))


@dataclass(frozen=True)
class Place:
    codes: frozenset[str]            # countries the text says the job can be done from (WORLD = anywhere)
    countries: frozenset[str]        # only those a country was NAMED for (not a region, state or city)
    seen: frozenset[str]             # every country any country, state or city named in the text belongs to
    granular: bool                   # a state or city was named, not only a country or region
    cities: tuple[str, ...]          # folded names of the cities and states named


NOWHERE = Place(frozenset(), frozenset(), frozenset(), False, ())


def places_in(text: Any) -> Place:
    raw = str(text or "")
    folded = fold(raw)
    countries: set[str] = set()
    region_codes: set[str] = set()
    state_codes: set[str] = set()
    city_codes: set[str] = set()
    names: list[str] = []
    for m in _ALIAS.finditer(folded):
        kind, codes = _TABLE[m.group(0)]
        if kind == "country":
            countries |= codes
        elif kind == "region":
            region_codes |= codes
        elif kind == "state":
            state_codes |= codes
            names.append(m.group(0))
        else:
            city_codes |= codes
            names.append(m.group(0))
    if _US_WORD.search(raw):
        countries.add("US")
    named = countries | state_codes
    if named:
        region_codes.discard(WORLD)          # "Anywhere in the United States" is narrower than anywhere
    codes = named | region_codes
    if not codes:
        codes = set(city_codes)
    if not codes:                            # last resort, because ", IN" may be Indiana or India: "Springfield, TX"
        for m in _ABBREVIATION.finditer(raw):
            codes.add("CA" if m.group(1) in _CA_ABBREVIATIONS else "US")
            names.append(m.group(1).lower())
    if not codes:
        return NOWHERE
    return Place(frozenset(codes), frozenset(countries), frozenset(countries | state_codes | city_codes), bool(state_codes or city_codes or names), tuple(names))

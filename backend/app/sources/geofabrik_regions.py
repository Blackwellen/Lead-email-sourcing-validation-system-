"""Country -> Geofabrik extract path table.

Paths follow the Geofabrik index naming scheme: ``https://download.geofabrik.de/<path>-latest.osm.pbf``.
The table was written from knowledge of that scheme and has NOT been verified entry by entry against the live
index (the sandbox could not reach download.geofabrik.de).  Operators should check
https://download.geofabrik.de/index-v1-nogeom.json (machine-readable index) before relying on a path; a wrong
path simply yields HTTP 404 at download time.  Several extracts cover multiple countries (e.g. GCC states,
Ireland+Northern Ireland, Senegal+Gambia): records outside the wanted country must be filtered by the adapter
(it does so via addr:country / postcode heuristics).
"""
from __future__ import annotations

GEOFABRIK_BASE = "https://download.geofabrik.de/"

# alpha-2 -> list of extract paths (first = primary)
COUNTRY_EXTRACTS: dict[str, list[str]] = {
    # Europe
    "GB": ["europe/great-britain", "europe/ireland-and-northern-ireland"],  # NI lives in the Ireland extract (filter BT)
    "IE": ["europe/ireland-and-northern-ireland"],
    "AL": ["europe/albania"], "AD": ["europe/andorra"], "AT": ["europe/austria"], "BY": ["europe/belarus"],
    "BE": ["europe/belgium"], "BA": ["europe/bosnia-herzegovina"], "BG": ["europe/bulgaria"],
    "HR": ["europe/croatia"], "CY": ["europe/cyprus"], "CZ": ["europe/czech-republic"], "DK": ["europe/denmark"],
    "EE": ["europe/estonia"], "FO": ["europe/faroe-islands"], "FI": ["europe/finland"], "FR": ["europe/france"],
    "GE": ["europe/georgia"], "DE": ["europe/germany"], "GR": ["europe/greece"], "GG": ["europe/guernsey-jersey"],
    "JE": ["europe/guernsey-jersey"], "HU": ["europe/hungary"], "IS": ["europe/iceland"], "IM": ["europe/isle-of-man"],
    "IT": ["europe/italy"], "XK": ["europe/kosovo"], "LV": ["europe/latvia"], "LI": ["europe/liechtenstein"],
    "LT": ["europe/lithuania"], "LU": ["europe/luxembourg"], "MK": ["europe/macedonia"], "MT": ["europe/malta"],
    "MD": ["europe/moldova"], "MC": ["europe/monaco"], "ME": ["europe/montenegro"], "NL": ["europe/netherlands"],
    "NO": ["europe/norway"], "PL": ["europe/poland"], "PT": ["europe/portugal"], "RO": ["europe/romania"],
    "RU": ["russia"], "RS": ["europe/serbia"], "SK": ["europe/slovakia"], "SI": ["europe/slovenia"],
    "ES": ["europe/spain"], "SE": ["europe/sweden"], "CH": ["europe/switzerland"], "TR": ["europe/turkey"],
    "UA": ["europe/ukraine"],
    # North America
    "US": ["north-america/us"], "CA": ["north-america/canada"], "MX": ["north-america/mexico"],
    "GL": ["north-america/greenland"],
    # Central America / Caribbean
    "BZ": ["central-america/belize"], "CU": ["central-america/cuba"], "GT": ["central-america/guatemala"],
    "HT": ["central-america/haiti-and-domrep"], "DO": ["central-america/haiti-and-domrep"],
    "JM": ["central-america/jamaica"], "NI": ["central-america/nicaragua"],
    "CR": ["central-america/costa-rica"], "SV": ["central-america/el-salvador"], "HN": ["central-america/honduras"],
    "PA": ["central-america/panama"],
    # South America
    "AR": ["south-america/argentina"], "BO": ["south-america/bolivia"], "BR": ["south-america/brazil"],
    "CL": ["south-america/chile"], "CO": ["south-america/colombia"], "EC": ["south-america/ecuador"],
    "PY": ["south-america/paraguay"], "PE": ["south-america/peru"], "SR": ["south-america/suriname"],
    "UY": ["south-america/uruguay"],
    # Australia / Oceania
    "AU": ["australia-oceania/australia"], "NZ": ["australia-oceania/new-zealand"], "FJ": ["australia-oceania/fiji"],
    "PG": ["australia-oceania/papua-new-guinea"], "NC": ["australia-oceania/new-caledonia"],
    "CK": ["australia-oceania/cook-islands"], "KI": ["australia-oceania/kiribati"],
    "MH": ["australia-oceania/marshall-islands"], "NR": ["australia-oceania/nauru"], "NU": ["australia-oceania/niue"],
    "PW": ["australia-oceania/palau"], "WS": ["australia-oceania/samoa"], "SB": ["australia-oceania/solomon-islands"],
    "TO": ["australia-oceania/tonga"], "TV": ["australia-oceania/tuvalu"], "VU": ["australia-oceania/vanuatu"],
    # Asia
    "AF": ["asia/afghanistan"], "AM": ["asia/armenia"], "AZ": ["asia/azerbaijan"], "BD": ["asia/bangladesh"],
    "BT": ["asia/bhutan"], "KH": ["asia/cambodia"], "CN": ["asia/china"], "IN": ["asia/india"],
    "ID": ["asia/indonesia"], "IR": ["asia/iran"], "IQ": ["asia/iraq"], "IL": ["asia/israel-and-palestine"],
    "PS": ["asia/israel-and-palestine"], "JP": ["asia/japan"], "JO": ["asia/jordan"], "KZ": ["asia/kazakhstan"],
    "KG": ["asia/kyrgyzstan"], "LA": ["asia/laos"], "LB": ["asia/lebanon"],
    "MY": ["asia/malaysia-singapore-brunei"], "SG": ["asia/malaysia-singapore-brunei"],
    "BN": ["asia/malaysia-singapore-brunei"], "MV": ["asia/maldives"], "MN": ["asia/mongolia"],
    "MM": ["asia/myanmar"], "NP": ["asia/nepal"], "KP": ["asia/north-korea"], "PK": ["asia/pakistan"],
    "PH": ["asia/philippines"], "KR": ["asia/south-korea"], "LK": ["asia/sri-lanka"], "SY": ["asia/syria"],
    "TW": ["asia/taiwan"], "TJ": ["asia/tajikistan"], "TH": ["asia/thailand"], "TM": ["asia/turkmenistan"],
    "UZ": ["asia/uzbekistan"], "VN": ["asia/vietnam"], "YE": ["asia/yemen"],
    "SA": ["asia/gcc-states"], "AE": ["asia/gcc-states"], "QA": ["asia/gcc-states"], "KW": ["asia/gcc-states"],
    "BH": ["asia/gcc-states"], "OM": ["asia/gcc-states"],
    # Africa
    "DZ": ["africa/algeria"], "AO": ["africa/angola"], "BJ": ["africa/benin"], "BW": ["africa/botswana"],
    "BF": ["africa/burkina-faso"], "BI": ["africa/burundi"], "CM": ["africa/cameroon"], "CV": ["africa/cape-verde"],
    "CF": ["africa/central-african-republic"], "TD": ["africa/chad"], "KM": ["africa/comores"],
    "CG": ["africa/congo-brazzaville"], "CD": ["africa/congo-democratic-republic"], "DJ": ["africa/djibouti"],
    "EG": ["africa/egypt"], "GQ": ["africa/equatorial-guinea"], "ER": ["africa/eritrea"], "ET": ["africa/ethiopia"],
    "GA": ["africa/gabon"], "GH": ["africa/ghana"], "GN": ["africa/guinea"], "GW": ["africa/guinea-bissau"],
    "CI": ["africa/ivory-coast"], "KE": ["africa/kenya"], "LS": ["africa/lesotho"], "LR": ["africa/liberia"],
    "LY": ["africa/libya"], "MG": ["africa/madagascar"], "MW": ["africa/malawi"], "ML": ["africa/mali"],
    "MR": ["africa/mauritania"], "MU": ["africa/mauritius"], "MA": ["africa/morocco"], "MZ": ["africa/mozambique"],
    "NA": ["africa/namibia"], "NE": ["africa/niger"], "NG": ["africa/nigeria"], "RW": ["africa/rwanda"],
    "ST": ["africa/sao-tome-and-principe"], "SN": ["africa/senegal-and-gambia"], "GM": ["africa/senegal-and-gambia"],
    "SC": ["africa/seychelles"], "SL": ["africa/sierra-leone"], "SO": ["africa/somalia"], "ZA": ["africa/south-africa"],
    "SS": ["africa/south-sudan"], "SD": ["africa/sudan"], "SZ": ["africa/swaziland"], "TZ": ["africa/tanzania"],
    "TG": ["africa/togo"], "TN": ["africa/tunisia"], "UG": ["africa/uganda"], "ZM": ["africa/zambia"],
    "ZW": ["africa/zimbabwe"],
}

# Sub-regional UK extracts (same naming scheme; also unverified).
UK_SUBREGIONS: dict[str, str] = {
    "England": "europe/great-britain/england", "Scotland": "europe/great-britain/scotland",
    "Wales": "europe/great-britain/wales",
    "Greater London": "europe/great-britain/england/greater-london",
    "Greater Manchester": "europe/great-britain/england/greater-manchester",
    "West Midlands": "europe/great-britain/england/west-midlands",
    "West Yorkshire": "europe/great-britain/england/west-yorkshire",
    "Merseyside": "europe/great-britain/england/merseyside",
    "Kent": "europe/great-britain/england/kent", "Essex": "europe/great-britain/england/essex",
    "Surrey": "europe/great-britain/england/surrey", "Hampshire": "europe/great-britain/england/hampshire",
}

# Northern Ireland is not in the Great Britain extract.
NI_POSTCODE_AREA = "BT"


def extract_paths(country_code: str) -> list[str]:
    return list(COUNTRY_EXTRACTS.get(country_code.upper(), []))


def extract_url(path: str, *, fmt: str = "osm.pbf") -> str:
    return f"{GEOFABRIK_BASE}{path}-latest.{fmt}"


def index_url() -> str:
    return GEOFABRIK_BASE + "index-v1-nogeom.json"


def supported_countries() -> list[str]:
    return sorted(COUNTRY_EXTRACTS)

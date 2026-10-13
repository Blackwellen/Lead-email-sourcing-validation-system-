"""CPV (Common Procurement Vocabulary) -> industry tag mapping, shared by the OCDS / TED adapters.

A transparent static table: longest matching CPV prefix wins (so a 4-digit group can override its 2-digit
division).  Industry slugs are generic tags written to ``BusinessRecord.raw['industries']`` and used as
catalogue relevance tags; they are deliberately coarse and are NOT a claim about what the supplier does beyond
"it won a contract in this CPV area".
"""
from __future__ import annotations

from collections.abc import Iterable

# prefix -> (industry slug, label)
CPV_INDUSTRY: dict[str, tuple[str, str]] = {
    "03": ("agriculture", "Agricultural, farming, fishing, forestry products"),
    "09": ("energy", "Petroleum products, fuel, electricity and other sources of energy"),
    "14": ("mining", "Mining, basic metals and related products"),
    "15": ("food_drink", "Food, beverages, tobacco and related products"),
    "16": ("agriculture", "Agricultural machinery"),
    "18": ("retail_goods", "Clothing, footwear, luggage articles and accessories"),
    "19": ("retail_goods", "Leather and textile fabrics, plastic and rubber materials"),
    "22": ("printing_media", "Printed matter and related products"),
    "24": ("chemicals", "Chemical products"),
    "30": ("it_hardware", "Office and computing machinery, equipment and supplies"),
    "31": ("electrical", "Electrical machinery, apparatus, equipment and consumables; lighting"),
    "32": ("telecoms", "Radio, television, communication, telecommunication and related equipment"),
    "33": ("healthcare", "Medical equipments, pharmaceuticals and personal care products"),
    "34": ("transport", "Transport equipment and auxiliary products to transportation"),
    "35": ("security", "Security, fire-fighting, police and defence equipment"),
    "37": ("leisure", "Musical instruments, sport goods, games, toys, handicraft"),
    "38": ("science_lab", "Laboratory, optical and precision equipments"),
    "39": ("furniture", "Furniture, furnishings, domestic appliances and cleaning products"),
    "41": ("utilities", "Collected and purified water"),
    "42": ("industrial_machinery", "Industrial machinery"),
    "43": ("industrial_machinery", "Machinery for mining, quarrying, construction equipment"),
    "44": ("construction_materials", "Construction structures and materials; auxiliary products to construction"),
    "45": ("construction", "Construction work"),
    "48": ("software", "Software package and information systems"),
    "50": ("repair_maintenance", "Repair and maintenance services"),
    "51": ("installation", "Installation services (except software)"),
    "55": ("hospitality", "Hotel, restaurant and retail trade services"),
    "60": ("transport", "Transport services (excl. waste transport)"),
    "63": ("logistics", "Supporting and auxiliary transport services; travel agencies"),
    "64": ("telecoms", "Postal and telecommunications services"),
    "65": ("utilities", "Public utilities"),
    "66": ("finance", "Financial and insurance services"),
    "70": ("property", "Real estate services"),
    "71": ("engineering_architecture", "Architectural, construction, engineering and inspection services"),
    "72": ("it_services", "IT services: consulting, software development, Internet and support"),
    "73": ("research", "Research and development services and related consultancy services"),
    "75": ("public_admin", "Administration, defence and social security services"),
    "76": ("energy", "Services related to the oil and gas industry"),
    "77": ("agriculture", "Agricultural, forestry, horticultural, aquacultural and apicultural services"),
    "79": ("business_services", "Business services: law, marketing, consulting, recruitment, printing and security"),
    "80": ("education", "Education and training services"),
    "85": ("healthcare", "Health and social work services"),
    "90": ("environmental_cleaning", "Sewage, refuse, cleaning and environmental services"),
    "92": ("leisure", "Recreational, cultural and sporting services"),
    "98": ("personal_services", "Other community, social and personal services"),
    # 4+ digit overrides
    "7931": ("recruitment", "Staff recruitment services"),
    "7934": ("marketing", "Advertising and marketing services"),
    "7941": ("business_services", "Business and management consultancy"),
    "7995": ("events", "Exhibition, fair and congress organisation services"),
    "7996": ("events", "Event and venue-related organisation services"),
    "7997": ("events", "Event organisation services"),
    "7971": ("security", "Security services"),
    "7970": ("printing_media", "Printing and related services"),
    "9091": ("environmental_cleaning", "Cleaning services"),
    "5530": ("hospitality", "Restaurant services"),
    "5520": ("hospitality", "Camping sites and other non-hotel accommodation"),
    "5510": ("hospitality", "Hotel services"),
    "7000": ("property", "Real estate services"),
    "7021": ("property", "Residential property services"),
    "7022": ("property", "Non-residential property services"),
    "7031": ("property", "Building rental or sale services"),
    "7032": ("property", "Building management services"),
    "7033": ("property", "Land rental or sale services"),
    "9810": ("property", "Accommodation, rental and property services"),
}


def cpv_to_industry(cpv: str | None) -> tuple[str, str] | None:
    """Longest-prefix lookup for one CPV code (digits only; dashes / check digit tolerated)."""
    if not cpv:
        return None
    digits = "".join(ch for ch in str(cpv).split("-")[0] if ch.isdigit())
    for n in range(min(len(digits), 8), 1, -1):
        hit = CPV_INDUSTRY.get(digits[:n])
        if hit:
            return hit
    return None


def cpv_to_industries(codes: Iterable[str]) -> list[str]:
    """Ordered, de-duplicated industry slugs for a set of CPV codes."""
    out: list[str] = []
    for c in codes:
        hit = cpv_to_industry(c)
        if hit and hit[0] not in out:
            out.append(hit[0])
    return out


__all__ = ["CPV_INDUSTRY", "cpv_to_industry", "cpv_to_industries"]

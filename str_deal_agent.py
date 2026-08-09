"""
STR Deal Agent - Celebration, FL area (Disney vacation-home corridor)
ICP-match version

Trigger: the moment a NEW for-sale listing appears that matches your Ideal
property Profile (location + bedroom band + bathrooms + price/type), you get a
Pushover alert. Profitability (cash-on-cash) rides along inside the alert.

Runs hourly on GitHub Actions. One radius query around Celebration keeps the
API call count low.

Amenity reality: beds, baths, location, and Disney distance are matched exactly.
Pool, hot tub, and game room usually live only in the listing's marketing text,
which is not reliably in the data feed, so they show as "yes / verify" flags in
the alert rather than hard filters. That way you never miss a great home just
because its game room was not tagged.
"""

import os
import json
import time
import math
import datetime
import requests

# =====================================================================
# CONFIG  -  the only part you normally edit
# =====================================================================

# --- Search area: one radius query centered on WALT DISNEY WORLD ---
# Centered on Disney and focused on the closer-in communities on your
# (Orlando-facing) side. The community whitelist below is what actually
# decides which communities alert, so edit that to widen or narrow the area.
CENTER_LAT = 28.3852
CENTER_LNG = -81.5639
RADIUS_MILES = 8

# --- Your ICP ---
# Recommendation from market data: 6 bedrooms is the profit sweet spot.
# Band is set to 4-7 so you also see strong 4-5BR balance plays and can compare
# returns. To hunt only the sweet spot, set MIN_BEDS = 6.
MIN_BEDS = 4
MAX_BEDS = 7
MIN_BATHS = 3
MAX_PRICE = 1100000
ALLOWED_TYPES = ["Single Family", "Townhouse", "Condo"]

# --- STR-legal community whitelist (lowercase; substring match on address) ---
# Active = the closer-in communities on your side of Disney.
# The farther southwest / Davenport communities are intentionally left OUT.
# If alerts get too sparse, move any name up from the "optional" block.
STR_COMMUNITIES = [
    "storey lake", "windsor hills", "margaritaville", "sunset walk",
    "emerald island", "paradise palms", "terra verde", "bella vida",
    "seven dwarfs", "reunion", "encore",
    # ---- optional: farther out / Davenport (add back if you want them) ----
    # "championsgate", "champions gate", "windsor at westside",
    # "windsor island", "solara", "solterra", "veranda palms", "sonoma resort",
]
WHITELIST_ONLY = True   # only alert inside these communities (recommended)

# --- Should a match still clear your return threshold to alert you? ---
REQUIRE_PROFIT_GATE = True    # True = only alert if it clears the floor below
MIN_CASH_ON_CASH = 0.0        # 0.0 = block money-losers; raise to 0.08 for target-only

# --- Disney proximity (shown in every alert; optional hard filter) ---
DISNEY_LAT = 28.3852
DISNEY_LNG = -81.5639
FILTER_BY_DISNEY_DISTANCE = False   # True = require within the miles below
MAX_DISNEY_MILES = 5

# --- Cost / financing assumptions (edit to your situation) ---
DOWN_PAYMENT_PCT   = 0.25
INTEREST_RATE      = 0.07     # check today's investment-loan rate
LOAN_TERM_YEARS    = 30
CLOSING_COST_PCT   = 0.03
FURNISHING_COST    = 35000    # 6BR themed home furnishing runs higher
PROPERTY_TAX_RATE  = 0.011
INSURANCE_ANNUAL   = 3800
DEFAULT_HOA_MONTHLY = 500
UTILITIES_ANNUAL   = 5400
MGMT_PCT           = 0.22
MAINTENANCE_PCT    = 0.05

# --- STR revenue SEED values by bedroom (annual gross). AirROI replaces these. ---
REVENUE_SEEDS = {4: 48000, 5: 58000, 6: 72000, 7: 84000}

STATE_FILE = "seen.json"

RENTCAST_API_KEY = os.environ.get("RENTCAST_API_KEY", "")
AIRROI_API_KEY   = os.environ.get("AIRROI_API_KEY", "")
PUSHOVER_TOKEN   = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER    = os.environ.get("PUSHOVER_USER", "")


# =====================================================================
# Helpers
# =====================================================================

def log(msg):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("[{}] {}".format(stamp, msg), flush=True)


def load_seen():
    try:
        with open(STATE_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


def haversine_miles(lat1, lng1, lat2, lng2):
    if None in (lat1, lng1, lat2, lng2):
        return None
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def community_match(address_text):
    text = (address_text or "").lower()
    for name in STR_COMMUNITIES:
        if name in text:
            return name
    return None


def money(x):
    return "${:,.0f}".format(x)


# =====================================================================
# Data sources
# =====================================================================

def rentcast_radius_listings():
    """One radius query for active for-sale listings around Celebration."""
    url = "https://api.rentcast.io/v1/listings/sale"
    params = {
        "latitude": CENTER_LAT,
        "longitude": CENTER_LNG,
        "radius": RADIUS_MILES,
        "status": "Active",
        "limit": 500,
    }
    headers = {"X-Api-Key": RENTCAST_API_KEY, "Accept": "application/json"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=40)
        if r.status_code != 200:
            log("RentCast returned HTTP {}".format(r.status_code))
            return []
        data = r.json()
        return data if isinstance(data, list) else data.get("listings", [])
    except Exception as e:
        log("RentCast error: {}".format(e))
        return []


def airroi_str_revenue(lat, lng, beds):
    """Annual Airbnb gross revenue near a point. Falls back to seeds on failure."""
    try:
        url = "https://api.airroi.com/v1/revenue"
        params = {"latitude": lat, "longitude": lng,
                  "bedrooms": beds, "api_key": AIRROI_API_KEY}
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 200:
            data = r.json()
            for key in ("annual_revenue", "annualRevenue", "revenue", "ltm_revenue"):
                if data.get(key):
                    return float(data[key])
            adr = data.get("adr") or data.get("average_daily_rate")
            occ = data.get("occupancy") or data.get("occupancy_rate")
            if adr and occ:
                occ = occ / 100.0 if occ > 1 else occ
                return float(adr) * float(occ) * 365
    except Exception as e:
        log("AirROI error: {}; using seed.".format(e))
    b = min(max(int(beds or 4), 4), 7)
    return float(REVENUE_SEEDS.get(b, REVENUE_SEEDS[4]))


def amenity_flags(listing):
    """Return dict of amenity status: 'yes' if found in data, else 'verify'."""
    text = json.dumps(listing).lower()
    flags = {}
    flags["pool"] = "yes" if ('"pool": true' in text or "pool" in text) else "verify"
    flags["hot tub"] = "yes" if ("hot tub" in text or "spa" in text) else "verify"
    flags["game room"] = "yes" if "game room" in text else "verify"
    return flags


# =====================================================================
# Economics
# =====================================================================

def annual_debt_service(price):
    loan = price * (1 - DOWN_PAYMENT_PCT)
    mr = INTEREST_RATE / 12.0
    n = LOAN_TERM_YEARS * 12
    if mr == 0:
        return (loan / n) * 12
    pmt = loan * mr / (1 - math.pow(1 + mr, -n))
    return pmt * 12


def compute_economics(listing, annual_revenue):
    price = float(listing.get("price") or 0)
    if price <= 0:
        return None
    hoa = listing.get("hoa")
    hoa_fee = float(hoa.get("fee")) if isinstance(hoa, dict) and hoa.get("fee") else 0
    hoa_annual = (hoa_fee if hoa_fee else DEFAULT_HOA_MONTHLY) * 12

    operating = (annual_revenue * MGMT_PCT + annual_revenue * MAINTENANCE_PCT
                 + price * PROPERTY_TAX_RATE + INSURANCE_ANNUAL
                 + hoa_annual + UTILITIES_ANNUAL)
    noi = annual_revenue - operating
    cash_flow = noi - annual_debt_service(price)
    cash_invested = price * DOWN_PAYMENT_PCT + price * CLOSING_COST_PCT + FURNISHING_COST

    return {
        "annual_revenue": annual_revenue,
        "noi": noi,
        "monthly_cash_flow": cash_flow / 12.0,
        "cash_invested": cash_invested,
        "cash_on_cash": cash_flow / cash_invested if cash_invested else 0,
        "cap_rate": noi / price if price else 0,
    }


# =====================================================================
# Alerts
# =====================================================================

def send_pushover(title, message, url=None):
    if not (PUSHOVER_TOKEN and PUSHOVER_USER):
        log("Pushover not configured; skipping alert.")
        return
    payload = {"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
               "title": title, "message": message, "priority": 0}
    if url:
        payload["url"] = url
        payload["url_title"] = "View listing"
    try:
        requests.post("https://api.pushover.net/1/messages.json", data=payload, timeout=30)
    except Exception as e:
        log("Pushover error: {}".format(e))


# =====================================================================
# Main
# =====================================================================

def matches_icp(lst):
    """Return (True, reason) if the listing fits the ICP, else (False, reason)."""
    price = float(lst.get("price") or 0)
    beds = int(lst.get("bedrooms") or 0)
    baths = float(lst.get("bathrooms") or 0)
    ptype = lst.get("propertyType") or ""
    address = lst.get("formattedAddress") or lst.get("addressLine1") or ""

    if price <= 0 or price > MAX_PRICE:
        return False, "price"
    if beds < MIN_BEDS or beds > MAX_BEDS:
        return False, "beds"
    if baths < MIN_BATHS:
        return False, "baths"
    if ALLOWED_TYPES and ptype not in ALLOWED_TYPES:
        return False, "type"
    if WHITELIST_ONLY and not community_match(address):
        return False, "community"
    if FILTER_BY_DISNEY_DISTANCE:
        d = haversine_miles(lst.get("latitude"), lst.get("longitude"), DISNEY_LAT, DISNEY_LNG)
        if d is None or d > MAX_DISNEY_MILES:
            return False, "disney"
    return True, "match"


def main():
    if not RENTCAST_API_KEY:
        log("Missing RENTCAST_API_KEY. Exiting.")
        return

    seen = load_seen()
    new_seen = set(seen)
    alerts = 0

    listings = rentcast_radius_listings()
    log("Fetched {} active listings in radius.".format(len(listings)))

    for lst in listings:
        listing_id = str(lst.get("id") or lst.get("formattedAddress") or "")
        if not listing_id or listing_id in seen:
            continue

        ok, reason = matches_icp(lst)
        new_seen.add(listing_id)   # mark seen either way, so we do not re-check
        if not ok:
            continue

        address = lst.get("formattedAddress") or lst.get("addressLine1") or ""
        beds = int(lst.get("bedrooms") or 0)
        baths = lst.get("bathrooms") or "?"
        price = float(lst.get("price") or 0)
        community = community_match(address) or "unverified"

        revenue = airroi_str_revenue(lst.get("latitude"), lst.get("longitude"), beds)
        econ = compute_economics(lst, revenue)
        if not econ:
            continue

        coc = econ["cash_on_cash"]
        if REQUIRE_PROFIT_GATE and coc < MIN_CASH_ON_CASH:
            log("ICP match below return gate: {} ({:.1%})".format(address, coc))
            continue

        d = haversine_miles(lst.get("latitude"), lst.get("longitude"), DISNEY_LAT, DISNEY_LNG)
        am = amenity_flags(lst)

        title = "ICP match: {}BR in {}".format(beds, community.title())
        msg = (
            "{addr}\n"
            "{price} | {beds}BR/{baths}BA\n"
            "Disney: {dist} mi\n"
            "Pool: {pool} | Hot tub: {tub} | Game room: {game}\n"
            "Est. Airbnb: {rev}/yr\n"
            "Cash-on-cash: {coc:.1%} | Cap: {cap:.1%}\n"
            "Monthly cash flow: {mcf}\n"
            "Cash to close: {cash}"
        ).format(
            addr=address, price=money(price), beds=beds, baths=baths,
            dist=("{:.1f}".format(d) if d is not None else "?"),
            pool=am["pool"], tub=am["hot tub"], game=am["game room"],
            rev=money(econ["annual_revenue"]), coc=coc, cap=econ["cap_rate"],
            mcf=money(econ["monthly_cash_flow"]), cash=money(econ["cash_invested"]),
        )
        zurl = "https://www.zillow.com/homes/{}_rb/".format(
            address.replace(" ", "-").replace(",", ""))
        send_pushover(title, msg, zurl)
        alerts += 1
        log("ALERT: {} | {}BR | CoC {:.1%}".format(address, beds, coc))

    save_seen(new_seen)
    log("Run complete. {} new alerts.".format(alerts))


if __name__ == "__main__":
    main()

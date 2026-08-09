"""
STR Deal Agent - Four Corners / Windsor Hills area (Disney vacation-home corridor)
Live-list version

Each hourly run it:
  1. Pulls active for-sale listings around the Four Corners belt from RentCast.
  2. Keeps the ones in your target ZIP codes that fit your ICP and clear your
     return floor.
  3. Estimates Airbnb revenue and models the economics for each.
  4. Writes the FULL current list to listings.csv (best deals on top). Homes
     that sell drop off automatically, because they stop showing as active.
  5. Sends a Pushover alert for any home that is brand-new since the last run.

listings.csv is what feeds your live Google Sheet. Edit the CONFIG block below.
"""

import os
import csv
import math
import datetime
import requests

# =====================================================================
# CONFIG  -  the only part you normally edit
# =====================================================================

# --- Search area: one radius query that covers the Four Corners belt ---
CENTER_LAT = 28.30
CENTER_LNG = -81.61
RADIUS_MILES = 10

# --- Target ZIP codes (this is the real area filter) ---
# 34747 = Kissimmee / Four Corners: Windsor Hills, Acadia Estates, Reunion, Formosa Gardens (closest)
# 33896 = Davenport / ChampionsGate
# 34714 = Clermont / Four Corners (Lake County)
# 33897 = Davenport West / Four Corners
# 33837 = Davenport / Citrus Ridge (farthest; delete this line if too far)
TARGET_ZIPS = ["34747", "33896", "34714", "33897", "33837"]

# --- Your ICP ---
MIN_BEDS = 4
MAX_BEDS = 7
MIN_BATHS = 4          # you asked for 4+; lower to 3 if the list is too thin
MAX_PRICE = 1100000
ALLOWED_TYPES = ["Single Family", "Townhouse", "Condo"]

# --- Known STR-legal communities (used to flag each home, not to filter) ---
# A home in a target ZIP that matches one of these is flagged "Confirmed".
# Anything else is flagged "Verify HOA" so you check before making an offer.
STR_COMMUNITIES = [
    "windsor hills", "acadia", "reunion", "encore", "storey lake",
    "margaritaville", "sunset walk", "emerald island", "paradise palms",
    "formosa gardens", "terra verde", "seven dwarfs", "bella vida",
    "championsgate", "champions gate", "windsor at westside", "windsor island",
    "solara", "solterra", "west haven", "legacy park", "highlands reserve",
    "calabay parc", "sonoma resort", "veranda palms",
]

# --- Your floor: minimum MONTHLY cash flow in dollars ---
# A home must clear at least this much profit per month to turn green on the map,
# show "Yes" in the sheet, and ping your phone. This does NOT limit the live list:
# every home that fits your size / baths / ZIP / price still shows (in gray).
MIN_MONTHLY_CASHFLOW = 500    # your minimum; set to 1000 for your preferred target

# --- Target used for the "price you'd need to pay" figure in each alert ---
TARGET_MONTHLY = 1000    # the buy price shown is what hits THIS much cash flow

# --- Disney reference (shown for each home) ---
DISNEY_LAT = 28.3852
DISNEY_LNG = -81.5639

# --- Cost / financing assumptions (edit to your situation) ---
DOWN_PAYMENT_PCT   = 0.25
INTEREST_RATE      = 0.07
LOAN_TERM_YEARS    = 30
CLOSING_COST_PCT   = 0.03
FURNISHING_COST    = 35000
PROPERTY_TAX_RATE  = 0.011
INSURANCE_ANNUAL   = 3800
DEFAULT_HOA_MONTHLY = 500
UTILITIES_ANNUAL   = 5400
MGMT_PCT           = 0.22
MAINTENANCE_PCT    = 0.05

REVENUE_SEEDS = {4: 48000, 5: 58000, 6: 72000, 7: 84000}

STATE_FILE = "seen.json"
CSV_FILE = "listings.csv"

RENTCAST_API_KEY = os.environ.get("RENTCAST_API_KEY", "")
AIRROI_API_KEY   = os.environ.get("AIRROI_API_KEY", "")
PUSHOVER_TOKEN   = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER    = os.environ.get("PUSHOVER_USER", "")


# =====================================================================
# Helpers
# =====================================================================

import json

def log(msg):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("[{}] {}".format(stamp, msg), flush=True)


def load_seen():
    """Return a dict of {listing_id: first_seen_date}."""
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            return {str(x): "" for x in data}   # migrate old list format
    except Exception:
        return {}


def save_seen(seen):
    with open(STATE_FILE, "w") as f:
        json.dump(seen, f, indent=2)


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
    url = "https://api.rentcast.io/v1/listings/sale"
    params = {"latitude": CENTER_LAT, "longitude": CENTER_LNG,
              "radius": RADIUS_MILES, "status": "Active", "limit": 500}
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


def airroi_str_revenue(lat, lng, beds, baths=0):
    """Projected annual Airbnb revenue from AirROI's calculator/estimate endpoint.
    Docs: airroi.com/api . Auth is the X-API-KEY header. Falls back to a
    conservative seed on any failure so the agent keeps running."""
    try:
        if lat is None or lng is None:
            raise ValueError("missing coordinates")
        guests = int(beds or 4) * 2 + 2            # group vacation homes sleep large
        params = {"lat": lat, "lng": lng, "bedrooms": int(beds or 0),
                  "guests": guests, "currency": "usd"}
        if baths:
            params["baths"] = baths
        headers = {"X-API-KEY": AIRROI_API_KEY}
        r = requests.get("https://api.airroi.com/calculator/estimate",
                         params=params, headers=headers, timeout=30)
        if r.status_code == 200:
            data = r.json()
            if data.get("revenue"):
                return float(data["revenue"])
            adr, occ = data.get("average_daily_rate"), data.get("occupancy")
            if adr and occ:
                occ = occ / 100.0 if occ > 1 else occ
                return float(adr) * float(occ) * 365
        else:
            log("AirROI HTTP {}: {}".format(r.status_code, str(r.text)[:180]))
    except Exception as e:
        log("AirROI error: {}".format(e))
    b = min(max(int(beds or 4), 4), 7)
    log("AirROI unavailable; using seed for {}BR.".format(b))
    return float(REVENUE_SEEDS.get(b, REVENUE_SEEDS[4]))


def pool_flag(listing):
    text = json.dumps(listing).lower()
    return "Yes" if ('"pool": true' in text or "pool" in text) else "Verify"


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


def hoa_annual_of(listing):
    hoa = listing.get("hoa")
    fee = float(hoa.get("fee")) if isinstance(hoa, dict) and hoa.get("fee") else 0
    return (fee if fee else DEFAULT_HOA_MONTHLY) * 12


def target_buy_price(annual_revenue, hoa_annual, target_monthly):
    """Highest purchase price that still yields target_monthly cash flow.
    Price drives both property tax and the financed mortgage, so both are in the
    denominator. Returns None if the revenue is too low to hit the target at any
    price."""
    mr = INTEREST_RATE / 12.0
    n = LOAN_TERM_YEARS * 12
    annual_loan_constant = (mr / (1 - math.pow(1 + mr, -n))) * 12  # per $ of loan
    per_dollar_cost = PROPERTY_TAX_RATE + (1 - DOWN_PAYMENT_PCT) * annual_loan_constant
    variable = annual_revenue * (MGMT_PCT + MAINTENANCE_PCT)
    fixed = INSURANCE_ANNUAL + hoa_annual + UTILITIES_ANNUAL
    numerator = annual_revenue - variable - fixed - target_monthly * 12
    if numerator <= 0:
        return None
    return numerator / per_dollar_cost


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
        "monthly_cash_flow": cash_flow / 12.0,
        "cash_invested": cash_invested,
        "cash_on_cash": cash_flow / cash_invested if cash_invested else 0,
        "cap_rate": noi / price if price else 0,
    }


# =====================================================================
# Alerts + CSV
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


CSV_HEADER = ["Address", "Community", "STR-legal", "Beds", "Baths", "Price",
              "Est Annual Airbnb", "Monthly Cash Flow", "Cash-on-Cash",
              "Cap Rate", "Clears Floor", "Miles to Disney", "Pool",
              "Listing Link", "First Seen", "Latitude", "Longitude",
              "Target Buy Price"]


def write_csv(rows):
    with open(CSV_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for r in rows:
            w.writerow(r)


# =====================================================================
# ICP
# =====================================================================

def matches_icp(lst):
    price = float(lst.get("price") or 0)
    beds = int(lst.get("bedrooms") or 0)
    baths = float(lst.get("bathrooms") or 0)
    ptype = lst.get("propertyType") or ""
    zipc = str(lst.get("zipCode") or "")
    if TARGET_ZIPS and zipc not in TARGET_ZIPS:
        return False
    if price <= 0 or price > MAX_PRICE:
        return False
    if beds < MIN_BEDS or beds > MAX_BEDS:
        return False
    if baths < MIN_BATHS:
        return False
    if ALLOWED_TYPES and ptype not in ALLOWED_TYPES:
        return False
    return True


# =====================================================================
# Main
# =====================================================================

def main():
    if not RENTCAST_API_KEY:
        log("Missing RENTCAST_API_KEY. Exiting.")
        return

    seen = load_seen()
    today = datetime.date.today().isoformat()
    listings = rentcast_radius_listings()
    log("Fetched {} active listings in radius.".format(len(listings)))

    current = []     # rows for the CSV
    new_alerts = 0

    for lst in listings:
        if not matches_icp(lst):
            continue
        revenue = airroi_str_revenue(lst.get("latitude"), lst.get("longitude"),
                                     int(lst.get("bedrooms") or 0),
                                     lst.get("bathrooms") or 0)
        econ = compute_economics(lst, revenue)
        if not econ:
            continue

        listing_id = str(lst.get("id") or lst.get("formattedAddress") or "")
        address = lst.get("formattedAddress") or lst.get("addressLine1") or ""
        matched = community_match(address)
        community = (matched.title() if matched else (lst.get("city") or "Unknown"))
        legal = "Confirmed" if matched else "Verify HOA"
        beds = int(lst.get("bedrooms") or 0)
        baths = lst.get("bathrooms") or ""
        price = float(lst.get("price") or 0)
        coc = econ["cash_on_cash"]
        mcf = econ["monthly_cash_flow"]
        d = haversine_miles(lst.get("latitude"), lst.get("longitude"), DISNEY_LAT, DISNEY_LNG)
        link = "https://www.zillow.com/homes/{}_rb/".format(
            address.replace(" ", "-").replace(",", ""))

        is_new = listing_id and listing_id not in seen
        first_seen = today if is_new else seen.get(listing_id, today)
        if listing_id:
            seen[listing_id] = first_seen

        clears = "Yes" if mcf >= MIN_MONTHLY_CASHFLOW else "No"
        tp = target_buy_price(revenue, hoa_annual_of(lst), TARGET_MONTHLY)
        tp_str = money(tp) if tp else "revenue too low"
        current.append([
            address, community, legal, beds, baths, int(price),
            int(econ["annual_revenue"]), int(mcf),
            "{:.1%}".format(coc), "{:.1%}".format(econ["cap_rate"]), clears,
            ("{:.1f}".format(d) if d is not None else ""),
            pool_flag(lst), link, first_seen,
            lst.get("latitude") or "", lst.get("longitude") or "", tp_str,
        ])

        if is_new and mcf >= MIN_MONTHLY_CASHFLOW:
            if tp and price <= tp:
                tline = "Already clears ${:,}/mo".format(TARGET_MONTHLY)
            elif tp:
                tline = "For ${:,}/mo, buy at/below {}".format(TARGET_MONTHLY, money(tp))
            else:
                tline = "Cannot reach ${:,}/mo at this revenue".format(TARGET_MONTHLY)
            title = "New: {}BR {} ({}/mo)".format(beds, community, money(mcf))
            msg = ("{addr}\n{price} | {beds}BR/{baths}BA | {legal}\n"
                   "Airbnb ~{rev}/yr | {mcf}/mo | CoC {coc:.1%}\n"
                   "{tline}").format(
                addr=address, price=money(price), beds=beds, baths=baths,
                legal=legal, rev=money(econ["annual_revenue"]), coc=coc,
                mcf=money(mcf), tline=tline)
            send_pushover(title, msg, link)
            new_alerts += 1

    # best deals on top (sort by the Cash-on-Cash column)
    current.sort(key=lambda r: float(r[8].rstrip("%")), reverse=True)
    write_csv(current)
    save_seen(seen)
    log("Wrote {} current listings to {}. {} new alerts.".format(
        len(current), CSV_FILE, new_alerts))


if __name__ == "__main__":
    main()

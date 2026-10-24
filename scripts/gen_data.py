#!/usr/bin/env python3
"""Generate SupplyLine's demo dataset (deterministic).

IMPORTANT: every store, address, inventory level and closure produced here is
FICTIONAL. Store names are invented; neighborhood names and coordinates are
approximate and only place the stores in the San Jose area. The app is
built so this file can be replaced by a real inventory provider (see
README "Going beyond the demo").

Run:  python3 scripts/gen_data.py
Writes app/data/{catalog,stores,places}.json
"""
import json
import math
import pathlib
import random

OUT = pathlib.Path(__file__).resolve().parent.parent / "app" / "data"
random.seed(2026)

EPICENTER = (37.3382, -121.8863)  # downtown San Jose: where panic buying is worst
SHORTAGE_KM = 9.0                 # radius of the demo shortage zone
GUARANTEED_OUT_KM = 9.0           # key items are always out inside this radius


# ----------------------------------------------------------------- catalog
def I(id, name, cat, tags, aliases, policy="free", note="", escalate="", safe_subs=None):
    """policy: free    = any substitute the AI judges safe
               caution = substitutes ONLY from the curated safe_subs allowlist (may be empty)
               none    = never substituted (prescriptions, medical oxygen)"""
    return dict(id=id, name=name, category=cat, tags=tags, aliases=aliases,
                policy=policy, note=note, escalate=escalate, safe_subs=safe_subs)


CATALOG = [
    # Water
    I("water_bottled", "Bottled drinking water", "Water", ["drinking-water", "hydration"],
      ["water", "bottled water", "drinking water", "case of water", "water bottles", "gallons of water"],
      note="Plan on about 1 gallon per person per day."),
    I("water_jug", "Water jug or container (1-5 gal)", "Water", ["water-storage", "drinking-water"],
      ["water jug", "water container", "water storage", "jerry can for water"],
      note="Fill only from a source you trust; treat the water if unsure."),
    I("water_filter", "Portable water filter", "Water", ["water-treatment", "drinking-water"],
      ["water filter", "filter", "lifestraw", "water purifier"],
      note="Check what the filter removes; many do not remove viruses."),
    I("water_tabs", "Water purification tablets", "Water", ["water-treatment"],
      ["purification tablets", "water tablets", "iodine tablets", "chlorine dioxide"],
      note="Follow the package directions and wait time."),
    I("bleach", "Unscented household bleach", "Water", ["water-treatment", "sanitation"],
      ["bleach"], policy="caution",
      note="Use for disinfection only as the label or CDC directs. Never mix with other cleaners.", safe_subs=['water_tabs', 'water_filter']),
    I("electrolyte", "Electrolyte or rehydration drink", "Water", ["hydration", "electrolytes"],
      ["electrolyte", "gatorade", "pedialyte", "rehydration", "sports drink"]),
    # Food
    I("food_canned", "Canned food (ready to eat)", "Food", ["shelf-food", "no-cook"],
      ["canned food", "cans", "canned beans", "canned soup", "canned tuna", "food"]),
    I("food_meals", "Shelf-stable meals / MREs", "Food", ["shelf-food", "no-cook"],
      ["mre", "mres", "freeze dried", "shelf stable meals", "emergency meals"]),
    I("food_bars", "Energy or protein bars", "Food", ["shelf-food", "no-cook"],
      ["bars", "protein bars", "granola bars", "energy bars", "snacks"]),
    I("peanut_butter", "Peanut or nut butter", "Food", ["shelf-food", "no-cook"],
      ["peanut butter", "nut butter"], note="Check for allergies first."),
    I("dried_fruit_nuts", "Dried fruit and nuts", "Food", ["shelf-food", "no-cook"],
      ["dried fruit", "nuts", "trail mix"]),
    I("can_opener", "Manual can opener", "Food", ["food-prep"],
      ["can opener", "manual can opener", "tin opener"]),
    I("pet_food", "Pet food (dry)", "Pets", ["pet-food", "pets"],
      ["pet food", "dog food", "cat food", "kibble"]),
    # Baby
    I("baby_formula_powder", "Infant formula (powder)", "Baby", ["baby-feeding", "infant"],
      ["formula", "baby formula", "infant formula", "powdered formula"], policy="caution",
      note="Never dilute formula or make your own. Ask the pediatrician before switching types.",
      escalate="Call the baby's pediatrician or a WIC office. Dialing 211 can point you to emergency formula supplies.", safe_subs=['baby_formula_rtf']),
    I("baby_formula_rtf", "Infant formula (ready to feed)", "Baby", ["baby-feeding", "infant"],
      ["ready to feed formula", "rtf formula", "liquid formula", "ready-to-feed"], policy="caution",
      note="Needs no water, which helps when tap water is unsafe. Check with the pediatrician before switching types.", safe_subs=['baby_formula_powder']),
    I("baby_food", "Baby food pouches", "Baby", ["baby-feeding", "infant", "no-cook"],
      ["baby food", "pouches", "purees"]),
    I("diapers", "Diapers", "Baby", ["infant-care"], ["diapers", "nappies", "pull-ups"]),
    I("wipes", "Wet wipes", "Baby", ["hygiene", "infant-care"], ["wipes", "wet wipes", "baby wipes"]),
    # Power and light
    I("batt_aa", "AA batteries", "Power and light", ["batteries", "portable-power"],
      ["aa batteries", "aa", "double a"]),
    I("batt_aaa", "AAA batteries", "Power and light", ["batteries", "portable-power"],
      ["aaa batteries", "aaa"]),
    I("batt_d", "D batteries", "Power and light", ["batteries", "portable-power"],
      ["d batteries", "d-cell", "d cell", "d size batteries"]),
    I("flashlight_led", "LED flashlight", "Power and light", ["light"], ["flashlight", "torch"]),
    I("headlamp", "Headlamp", "Power and light", ["light", "hands-free"],
      ["headlamp", "head lamp", "headlight"]),
    I("lantern_led_battery", "LED lantern (battery)", "Power and light", ["light"],
      ["lantern", "led lantern", "battery lantern"]),
    I("lantern_solar_usb", "Solar or hand-crank lantern with USB charging", "Power and light",
      ["light", "phone-charging", "rechargeable"],
      ["solar lantern", "crank lantern", "solar light", "hand crank light"]),
    I("glow_sticks", "Glow sticks", "Power and light", ["light"],
      ["glow sticks", "light sticks", "chem lights"],
      note="A flame-free light source that is safe around children. One use only."),
    I("candles", "Candles", "Power and light", ["light"], ["candles"], policy="caution",
      note="Fire risk: never leave a flame unattended and never use one if you smell gas. LED light is safer.", safe_subs=['glow_sticks', 'flashlight_led', 'headlamp', 'lantern_led_battery', 'lantern_solar_usb']),
    I("power_bank", "USB power bank", "Power and light", ["phone-charging", "portable-power"],
      ["power bank", "battery pack", "portable charger", "phone charger", "charge phones",
       "charge my phone", "phone battery"]),
    I("car_charger", "Car phone charger", "Power and light", ["phone-charging"],
      ["car charger", "car phone charger"],
      note="Charge from a parked car with the engine running outdoors only, never in a closed garage."),
    I("radio_crank", "Hand-crank or solar emergency radio", "Power and light",
      ["info-radio", "phone-charging", "rechargeable"],
      ["radio", "emergency radio", "crank radio", "weather radio"]),
    I("radio_battery", "Battery-powered radio", "Power and light", ["info-radio"],
      ["battery radio", "am fm radio", "transistor radio"]),
    I("generator_portable", "Portable generator", "Power and light", ["backup-power"],
      ["generator", "portable generator"], policy="caution",
      note="Run outdoors only, well away from doors and windows. Generator exhaust has carbon monoxide that can kill.", safe_subs=['power_station', 'power_bank']),
    I("power_station", "Portable power station", "Power and light",
      ["backup-power", "phone-charging", "rechargeable"],
      ["power station", "battery generator", "jackery", "portable power", "solar generator"],
      note="Can run medical equipment only if its wattage and runtime fit. Check the device label."),
    I("extension_cord", "Heavy-duty extension cord", "Power and light", ["power-accessory"],
      ["extension cord", "power cord"]),
    I("gas_can", "Gasoline can", "Power and light", ["fuel"],
      ["gas can", "gasoline can", "fuel can"], policy="caution",
      note="Store fuel outdoors, away from flame and living space.", safe_subs=[]),
    # Cooking, heat, cooling
    I("propane_1lb", "Propane canister (1 lb)", "Cooking and heat", ["fuel-propane", "cooking-fuel"],
      ["propane", "propane canister", "propane tank", "camping propane", "1 lb propane"], policy="caution",
      note="Outdoor use only.", safe_subs=['butane_canister', 'canned_fuel', 'food_meals']),
    I("propane_20lb", "Propane tank (20 lb, grill)", "Cooking and heat", ["fuel-propane", "cooking-fuel"],
      ["20 lb propane", "bbq propane", "grill propane"], policy="caution", note="Outdoor use only.", escalate='A grill tank does not connect to a camp stove or work as a swap for a small canister without the right adapter. Ask store staff about tank exchange or refill, and use it outdoors only.', safe_subs=[]),
    I("camp_stove_propane", "Camp stove (propane)", "Cooking and heat", ["cooking", "stove"],
      ["camp stove", "camping stove", "portable stove"], note="Outdoor use only."),
    I("butane_canister", "Butane canister", "Cooking and heat", ["fuel-butane", "cooking-fuel"],
      ["butane", "butane canister"], policy="caution",
      note="Fits butane stoves only. It will not work in a propane stove.", safe_subs=['propane_1lb', 'canned_fuel']),
    I("canned_fuel", "Canned heat fuel (chafing fuel)", "Cooking and heat", ["cooking-fuel", "heat"],
      ["sterno", "canned heat", "chafing fuel"], policy="caution",
      note="Burns hot. Ventilate and keep away from children.", safe_subs=['butane_canister', 'propane_1lb']),
    I("charcoal", "Charcoal", "Cooking and heat", ["cooking-fuel"],
      ["charcoal", "briquettes"], policy="caution",
      note="Outdoors only. Burning charcoal indoors produces deadly carbon monoxide.", safe_subs=['propane_1lb', 'propane_20lb', 'canned_fuel']),
    I("lighter_matches", "Lighters or waterproof matches", "Cooking and heat", ["fire-starting"],
      ["lighter", "matches", "lighters"]),
    I("cooler", "Insulated cooler", "Cooking and heat", ["keep-cold"],
      ["cooler", "ice chest", "insulated bag"]),
    I("ice_bag", "Bagged ice", "Cooking and heat", ["keep-cold"],
      ["ice", "bag of ice", "bagged ice"]),
    I("ice_packs", "Reusable gel ice packs", "Cooking and heat", ["keep-cold"],
      ["ice packs", "gel packs", "cold packs"]),
    # Shelter and repair
    I("mylar_blanket", "Emergency (mylar) blankets", "Shelter and repair", ["warmth", "shelter"],
      ["mylar blanket", "space blanket", "emergency blanket", "blanket", "blankets"]),
    I("sleeping_bag", "Sleeping bag", "Shelter and repair", ["warmth", "shelter"],
      ["sleeping bag", "bedding"]),
    I("hand_warmers", "Hand warmers", "Shelter and repair", ["warmth"], ["hand warmers"]),
    I("tent", "Tent", "Shelter and repair", ["shelter"], ["tent"]),
    I("tarp", "Heavy-duty tarp", "Shelter and repair", ["shelter", "repair"], ["tarp", "tarpaulin"]),
    I("plastic_sheeting", "Plastic sheeting", "Shelter and repair", ["shelter", "repair"],
      ["plastic sheeting", "visqueen"]),
    I("duct_tape", "Duct tape", "Shelter and repair", ["repair"], ["duct tape"]),
    I("sandbags", "Sandbags", "Shelter and repair", ["flood-defense"], ["sandbags", "sand bags"]),
    I("work_gloves", "Heavy work gloves", "Shelter and repair", ["hand-protection"],
      ["work gloves", "gloves"]),
    # Health and safety
    I("n95_mask", "N95 respirator masks", "Health and safety", ["smoke-protection"],
      ["n95", "n95 mask", "respirator mask", "smoke mask", "mask", "masks"]),
    I("respirator_p100", "P100 half-face respirator", "Health and safety", ["smoke-protection"],
      ["p100", "respirator"]),
    I("goggles", "Safety goggles", "Health and safety", ["eye-protection"],
      ["goggles", "safety glasses"]),
    I("first_aid_kit", "First aid kit", "Health and safety", ["first-aid"],
      ["first aid kit", "first aid"]),
    I("bandages", "Bandages and gauze", "Health and safety", ["first-aid"],
      ["bandages", "gauze", "band-aids"]),
    I("antiseptic", "Antiseptic wipes or ointment", "Health and safety", ["first-aid"],
      ["antiseptic", "antibiotic ointment", "antiseptic wipes"]),
    I("pain_reliever", "Over-the-counter pain reliever", "Health and safety", ["otc-medicine"],
      ["pain reliever", "ibuprofen", "acetaminophen", "tylenol", "advil", "painkillers"],
      policy="caution",
      note="Follow the label. Children's doses depend on weight, so ask a pharmacist.", escalate='Ask a pharmacist which over-the-counter option is safe for you or your child.', safe_subs=[]),
    I("hand_sanitizer", "Hand sanitizer", "Health and safety", ["hygiene", "sanitation"],
      ["hand sanitizer", "sanitizer"]),
    I("toilet_paper", "Toilet paper", "Health and safety", ["sanitation"], ["toilet paper", "tp"]),
    I("trash_bags", "Heavy trash bags", "Health and safety", ["sanitation"],
      ["trash bags", "garbage bags"]),
    I("feminine_hygiene", "Feminine hygiene products", "Health and safety", ["personal-care"],
      ["pads", "tampons", "feminine hygiene", "period products"]),
    # Medical
    I("insulin", "Insulin (prescription)", "Medical", ["rx", "diabetes-care"],
      ["insulin"], policy="none",
      note="Prescription only. There is no safe substitute you can buy yourself.",
      escalate=("Call your pharmacy or prescriber now and ask about an emergency supply. "
                "Keep insulin cool but never frozen. Check the label or ask a pharmacist how long it "
                "stays good at room temperature.")),
    I("rx_medication", "Prescription medication (refill)", "Medical", ["rx"],
      ["prescription", "medication", "medicine", "meds", "refill", "rx", "pills", "inhaler"],
      policy="none",
      note="Prescription only. There is no safe substitute you can buy yourself.",
      escalate=("Call your pharmacy or doctor and ask about an emergency supply. "
                "If you cannot reach them, dial 211 or contact the Red Cross.")),
    I("glucose_tabs", "Glucose tablets (fast sugar)", "Medical", ["diabetes-care"],
      ["glucose tablets", "glucose tabs", "glucose gel"]),
    I("glucose_strips", "Blood glucose test strips", "Medical", ["diabetes-care"],
      ["test strips", "glucose strips", "glucose meter strips"], policy="caution",
      note="Strips must match the meter model.", escalate='Ask a pharmacist for test strips that match your meter model.', safe_subs=[]),
    I("oxygen_cylinder", "Medical oxygen (prescription)", "Medical", ["rx", "oxygen"],
      ["oxygen", "oxygen tank", "oxygen cylinder", "oxygen concentrator"], policy="none",
      note="Prescription only. There is no safe substitute you can buy yourself.",
      escalate=("Call your oxygen supplier and your electric utility's medical-needs line now. "
                "If anyone is struggling to breathe, call 911.")),
]
CAT = {c["id"]: c for c in CATALOG}


# ------------------------------------------------------------------ stores
HOODS = [
    ("Downtown San José", 37.3382, -121.8863, ["supermarket", "pharmacy", "outdoor", "hardware"]),
    ("SJSU and Naglee Park", 37.3330, -121.8780, ["convenience", "pharmacy", "petstore"]),
    ("Japantown", 37.3490, -121.8940, ["supermarket", "hardware", "convenience"]),
    ("Willow Glen", 37.3010, -121.9010, ["supermarket", "pharmacy", "bigbox"]),
    ("Rose Garden", 37.3290, -121.9230, ["supermarket", "convenience", "medical"]),
    ("Santa Clara", 37.3541, -121.9552, ["supermarket", "pharmacy", "hardware", "outdoor"]),
    ("Campbell", 37.2872, -121.9500, ["supermarket", "pharmacy", "hardware"]),
    ("Cambrian", 37.2530, -121.9230, ["supermarket", "pharmacy", "convenience"]),
    ("Almaden Valley", 37.2260, -121.8640, ["supermarket", "pharmacy", "bigbox", "hardware"]),
    ("Blossom Valley", 37.2750, -121.8450, ["supermarket", "convenience", "pharmacy"]),
    ("Evergreen", 37.3170, -121.7800, ["supermarket", "pharmacy", "hardware", "bigbox"]),
    ("Alum Rock", 37.3660, -121.8180, ["supermarket", "convenience", "pharmacy"]),
    ("Berryessa", 37.3800, -121.8650, ["supermarket", "pharmacy", "hardware", "medical"]),
    ("North San José", 37.4000, -121.9400, ["bigbox", "supermarket", "outdoor"]),
    ("Milpitas", 37.4323, -121.8996, ["supermarket", "pharmacy", "hardware"]),
    ("Saratoga", 37.2638, -122.0230, ["supermarket", "pharmacy", "petstore"]),
]

BRANDS = {
    "supermarket": ["Fresh Basket", "Valley Foods", "Orchard Market", "Sunrise Grocers"],
    "pharmacy": ["CareFirst Pharmacy", "Alder Pharmacy", "MedStop Pharmacy"],
    "hardware": ["Bay Hardware & Supply", "Ironwood Hardware", "Tru-Fix Hardware"],
    "bigbox": ["MegaMart Supercenter", "Bulk Depot", "ValueWorld"],
    "outdoor": ["Summit Outfitters", "Trailhead Gear"],
    "convenience": ["QuickStop", "Corner Mart", "Night Owl Mart"],
    "medical": ["Valley Medical Supply", "HomeCare Equipment"],
    "petstore": ["Paws & Co."],
}
HOURS = {  # (open, close) in minutes after midnight
    "supermarket": (6 * 60, 23 * 60), "pharmacy": (8 * 60, 21 * 60), "hardware": (7 * 60, 20 * 60),
    "bigbox": (6 * 60, 22 * 60), "outdoor": (9 * 60, 20 * 60), "convenience": (6 * 60, 24 * 60),
    "medical": (9 * 60, 18 * 60), "petstore": (9 * 60, 20 * 60),
}
ALWAYS_OPEN_CHANCE = {"convenience": .5, "pharmacy": .15, "supermarket": .15}


def P(p, ids):
    return {i: p for i in ids.split()}


CARRY = {
    "supermarket": {
        **P(.95, "water_bottled food_canned food_bars peanut_butter toilet_paper trash_bags hand_sanitizer "
                 "wipes diapers baby_formula_powder baby_food feminine_hygiene"),
        **P(.8, "dried_fruit_nuts electrolyte pet_food lighter_matches ice_bag pain_reliever bandages bleach "
                "batt_aa candles"),
        **P(.55, "water_jug baby_formula_rtf can_opener batt_aaa antiseptic first_aid_kit"),
        **P(.3, "food_meals batt_d flashlight_led cooler ice_packs glucose_tabs charcoal glow_sticks n95_mask "
                "propane_1lb"),
    },
    "pharmacy": {
        **P(1.0, "rx_medication bandages antiseptic hand_sanitizer pain_reliever feminine_hygiene"),
        **P(.9, "insulin glucose_tabs glucose_strips first_aid_kit electrolyte"),
        **P(.7, "n95_mask batt_aa batt_aaa water_bottled wipes"),
        **P(.5, "baby_formula_powder baby_formula_rtf diapers flashlight_led power_bank ice_packs food_bars"),
        **P(.25, "batt_d toilet_paper"),
    },
    "hardware": {
        **P(1.0, "flashlight_led tarp duct_tape work_gloves"),
        **P(.9, "batt_aa batt_aaa batt_d extension_cord gas_can propane_1lb propane_20lb plastic_sheeting"),
        **P(.8, "lantern_led_battery goggles n95_mask trash_bags"),
        **P(.7, "generator_portable headlamp lighter_matches charcoal"),
        **P(.6, "respirator_p100 bleach glow_sticks sandbags"),
        **P(.4, "camp_stove_propane cooler lantern_solar_usb radio_battery can_opener candles"),
        **P(.3, "power_bank water_jug power_station"),
    },
    "bigbox": {
        **P(.9, "water_bottled food_canned food_bars peanut_butter batt_aa batt_aaa batt_d flashlight_led "
                "toilet_paper trash_bags hand_sanitizer first_aid_kit bandages diapers wipes pain_reliever "
                "baby_formula_powder pet_food lighter_matches"),
        **P(.7, "water_jug cooler ice_bag ice_packs lantern_led_battery headlamp power_bank n95_mask "
                "extension_cord tarp duct_tape work_gloves can_opener baby_formula_rtf baby_food food_meals "
                "dried_fruit_nuts electrolyte candles propane_1lb camp_stove_propane sleeping_bag"),
        **P(.5, "generator_portable power_station radio_battery radio_crank lantern_solar_usb water_filter "
                "tent mylar_blanket gas_can charcoal antiseptic feminine_hygiene"),
        **P(.3, "water_tabs respirator_p100 goggles glow_sticks sandbags"),
    },
    "outdoor": {
        **P(1.0, "sleeping_bag mylar_blanket water_filter water_tabs headlamp lighter_matches first_aid_kit "
                 "cooler tarp food_meals food_bars"),
        **P(.9, "tent hand_warmers camp_stove_propane propane_1lb butane_canister lantern_solar_usb "
                "power_bank batt_aa batt_aaa water_jug"),
        **P(.7, "lantern_led_battery radio_crank power_station canned_fuel ice_packs dried_fruit_nuts "
                "can_opener glow_sticks"),
        **P(.4, "batt_d n95_mask work_gloves"),
    },
    "convenience": {
        **P(1.0, "water_bottled"),
        **P(.85, "food_bars lighter_matches ice_bag batt_aa electrolyte"),
        **P(.6, "pain_reliever power_bank batt_aaa car_charger hand_sanitizer"),
        **P(.3, "candles toilet_paper diapers charcoal batt_d propane_1lb flashlight_led baby_formula_powder"),
    },
    "medical": {
        **P(.95, "oxygen_cylinder glucose_strips first_aid_kit bandages hand_sanitizer n95_mask"),
        **P(.8, "glucose_tabs antiseptic power_station"),
        **P(.4, "respirator_p100 extension_cord ice_packs cooler"),
    },
    "petstore": {
        **P(1.0, "pet_food"),
        **P(.3, "water_jug"),
    },
}

# probability an item is sold out INSIDE the demo shortage zone (panic buying)
SCARCE = {
    "water_bottled": .9, "batt_d": .9, "propane_1lb": .85, "baby_formula_powder": .8,
    "generator_portable": .9, "ice_bag": .5, "batt_aa": .4, "n95_mask": .5, "power_bank": .4,
    "flashlight_led": .35, "gas_can": .5, "water_jug": .4, "lantern_led_battery": .5,
}
GUARANTEED_OUT = ["water_bottled", "batt_d", "propane_1lb", "baby_formula_powder"]

CLOSED_NOTES = ["Closed: power outage", "Closed: shelving damage, inspection pending",
                "Closed: evacuation order nearby"]
LIMITED_NOTES = ["Open: cash only", "Open: reduced hours", "Open: generator power, one register"]


def haversine_km(a, b, c, d):
    r = 6371.0088
    p1, p2 = math.radians(a), math.radians(c)
    dphi, dl = p2 - p1, math.radians(d - b)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def verified_age():
    r = random.random()
    if r < .6:
        return random.randint(4, 55)
    if r < .85:
        return random.randint(60, 230)
    return random.randint(250, 900)


def make_inventory(stype, dist_epi):
    inv = {}
    for item, p in CARRY[stype].items():
        if random.random() > p:
            continue
        inv[item] = {"q": random.choice([1, 2, 3, 4, 6, 8, 12, 18, 24, 36]), "age": verified_age()}
    for item, rec in inv.items():
        sp = SCARCE.get(item)
        if sp is not None:
            out_prob = sp if dist_epi <= SHORTAGE_KM else sp * .22
            if random.random() < out_prob:
                rec["q"] = 0
        elif random.random() < .05:  # ordinary random sell-outs
            rec["q"] = 0
    if dist_epi <= GUARANTEED_OUT_KM:
        for item in GUARANTEED_OUT:
            if item in inv:
                inv[item]["q"] = 0
    return inv


def build_stores():
    stores, counters = [], {}
    n = 0
    for hood, lat, lng, types in HOODS:
        for stype in types:
            n += 1
            k = counters.get(stype, 0)
            counters[stype] = k + 1
            brand = BRANDS[stype][k % len(BRANDS[stype])]
            slat = round(lat + random.uniform(-.008, .008), 5)
            slng = round(lng + random.uniform(-.008, .008), 5)
            dist = haversine_km(*EPICENTER, slat, slng)
            if random.random() < ALWAYS_OPEN_CHANCE.get(stype, 0):
                hours = {"always": True}
            else:
                o, c = HOURS[stype]
                hours = {"open": o, "close": c}
            status, note = "open", ""
            if n % 9 == 4:
                status, note = "closed", CLOSED_NOTES[n % len(CLOSED_NOTES)]
            elif n % 7 == 2:
                status, note = "limited", LIMITED_NOTES[n % len(LIMITED_NOTES)]
            stores.append({
                "id": f"s{n:02d}", "name": f"{brand}, {hood}", "type": stype, "hood": hood,
                "lat": slat, "lng": slng, "hours": hours, "status": status, "note": note,
                "inv": make_inventory(stype, dist),
            })
    return stores


def ensure_supply_outside_shortage(stores):
    """Key scarce items must still exist somewhere beyond the shortage zone,
    so 'nearest in stock' is a real, far-away answer."""
    for item in GUARANTEED_OUT:
        far = [s for s in stores
               if haversine_km(*EPICENTER, s["lat"], s["lng"]) > SHORTAGE_KM + 1 and s["status"] != "closed"]
        have = [s for s in far if s["inv"].get(item, {}).get("q", 0) > 0]
        need = 3 - len(have)
        for s in far:
            if need <= 0:
                break
            if item in s["inv"] and s["inv"][item]["q"] == 0 and s["type"] in ("supermarket", "bigbox", "hardware", "pharmacy", "convenience"):
                s["inv"][item]["q"] = random.choice([6, 12, 24])
                need -= 1


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    stores = build_stores()
    ensure_supply_outside_shortage(stores)
    places = [{"id": "p%02d" % i, "label": h, "lat": la, "lng": ln}
              for i, (h, la, ln, _) in enumerate(HOODS)]
    (OUT / "catalog.json").write_text(json.dumps(CATALOG, indent=1, ensure_ascii=False))
    (OUT / "stores.json").write_text(json.dumps(stores, separators=(",", ":"), ensure_ascii=False))
    (OUT / "places.json").write_text(json.dumps(places, indent=1, ensure_ascii=False))
    closed = sum(s["status"] == "closed" for s in stores)
    limited = sum(s["status"] == "limited" for s in stores)
    print(f"{len(CATALOG)} catalog items, {len(stores)} stores ({closed} closed, {limited} limited)")
    for item in GUARANTEED_OUT:
        total = sum(s["inv"].get(item, {}).get("q", 0) > 0 for s in stores)
        print(f"  {item}: in stock at {total} stores")


if __name__ == "__main__":
    main()

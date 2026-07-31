import os
import json
import time
import requests

# Координаты аэропорта им. Шопена (EPWA), Варшава
AIRPORT_ICAO = "EPWA"
AIRPORT_LAT = 52.1657
AIRPORT_LON = 20.9671
BOX_LAT_PAD = 0.14   # ~16 км по широте
BOX_LON_PAD = 0.22   # ~16 км по долготе на этой широте

STATE_FILE = "state.json"
MAX_TRACKED = 500          # сколько бортов помним между запусками
STALE_SECONDS = 60 * 60    # если борт не виден дольше часа - забываем про него

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
# Можно указать несколько ID через запятую, например: 111111111,222222222
CHAT_IDS = [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]
OPENSKY_CLIENT_ID = os.environ["OPENSKY_CLIENT_ID"]
OPENSKY_CLIENT_SECRET = os.environ["OPENSKY_CLIENT_SECRET"]

# Какие категории слать: military, bizjet, cargo, passenger (через запятую)
# Задаётся в GitHub -> Settings -> Secrets and variables -> Actions -> Variables -> FILTER_CATEGORIES
WATCH_CATEGORIES = {
    c.strip().lower()
    for c in os.environ.get("FILTER_CATEGORIES", "military,bizjet,cargo,passenger").split(",")
    if c.strip()
}

BIZJET_TYPES = {
    "GLF4", "GLF5", "GLF6", "GALX", "G150", "G200", "G280",
    "CL30", "CL35", "CL60", "C25A", "C25B", "C25C", "C500", "C510",
    "C525", "C550", "C560", "C56X", "C650", "C680", "C700", "C750",
    "F2TH", "F900", "FA7X", "FA8X", "LJ31", "LJ35", "LJ40", "LJ45",
    "LJ60", "LJ75", "E50P", "E55P", "PC12", "PC24", "TBM7", "TBM8",
    "TBM9", "H25B", "BE40", "PRM1",
}

CARGO_HINTS = {
    "fedex", "ups", "cargo", "lufthansa cargo", "cargolux", "aerologic",
    "kalitta", "atlas air", "dhl", "silk way", "cargolines", "cargoitalia",
    "volga-dnepr", "antonov airlines", "martinair cargo", "qatar cargo",
    "china cargo", "korean air cargo", "asl airlines",
}

MILITARY_HINTS = {
    "air force", "navy", "army", "ministry of defence", "ministry of defense",
    "government", "nato", "wojsko", "marynarka", "sily powietrzne",
}

CATEGORY_EMOJI = {
    "military": "⚫️",
    "bizjet": "🟡",
    "cargo": "🟤",
    "passenger": "⚪",
}


def classify_flight(aircraft_info):
    owner = (aircraft_info.get("RegisteredOwners") or "").lower()
    typ = (aircraft_info.get("ICAOTypeCode") or "").upper()

    if any(h in owner for h in MILITARY_HINTS):
        return "military"
    if any(h in owner for h in CARGO_HINTS):
        return "cargo"
    if typ in BIZJET_TYPES:
        return "bizjet"
    return "passenger"


def get_opensky_token():
    url = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": OPENSKY_CLIENT_ID,
        "client_secret": OPENSKY_CLIENT_SECRET,
    }
    r = requests.post(url, data=data, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"tracked": {}}


def save_state(state):
    now = time.time()
    tracked = state.get("tracked", {})
    # чистим борта, которых давно не видно
    tracked = {
        icao: info for icao, info in tracked.items()
        if now - info.get("last_seen", 0) < STALE_SECONDS
    }
    # ограничение на размер на всякий случай
    if len(tracked) > MAX_TRACKED:
        # оставляем самых недавно виденных
        items = sorted(tracked.items(), key=lambda kv: kv[1].get("last_seen", 0), reverse=True)
        tracked = dict(items[:MAX_TRACKED])
    state["tracked"] = tracked
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def get_aircraft_info(icao24):
    """Возвращает (model_str, category) по icao24. category одна из:
    military / bizjet / cargo / passenger"""
    try:
        r = requests.get(f"https://hexdb.io/api/v1/aircraft/{icao24}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            manufacturer = data.get("Manufacturer", "") or ""
            typ = data.get("Type", "") or data.get("ICAOTypeCode", "") or ""
            model = f"{manufacturer} {typ}".strip() or None
            category = classify_flight(data)
            return model, category
    except Exception:
        pass
    return None, "passenger"


def degrees_to_compass(deg):
    directions = [
        "север", "северо-восток", "восток", "юго-восток",
        "юг", "юго-запад", "запад", "северо-запад",
    ]
    idx = round(deg / 45) % 8
    return directions[idx]


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chat_id in CHAT_IDS:
        r = requests.post(
            url,
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"Telegram error for chat {chat_id}:", r.text)


def fetch_states_near_airport(token):
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "lamin": AIRPORT_LAT - BOX_LAT_PAD,
        "lamax": AIRPORT_LAT + BOX_LAT_PAD,
        "lomin": AIRPORT_LON - BOX_LON_PAD,
        "lomax": AIRPORT_LON + BOX_LON_PAD,
    }
    r = requests.get(
        "https://opensky-network.org/api/states/all",
        headers=headers, params=params, timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("states") or []


def main():
    state = load_state()
    tracked = state.get("tracked", {})
    token = get_opensky_token()
    now = time.time()

    states = fetch_states_near_airport(token)
    new_events = []
    seen_this_run = set()

    for s in states:
        icao24 = s[0]
        callsign = (s[1] or "").strip() or "без позывного"
        on_ground = s[8]
        true_track = s[10]
        if icao24 is None or on_ground is None:
            continue

        seen_this_run.add(icao24)
        prev = tracked.get(icao24)
        was_on_ground = prev.get("on_ground") if prev else None

        event_kind = None
        if was_on_ground is True and on_ground is False:
            event_kind = "departure"
        elif was_on_ground is False and on_ground is True:
            event_kind = "arrival"

        tracked[icao24] = {"on_ground": on_ground, "last_seen": now}

        if event_kind:
            model, category = get_aircraft_info(icao24)
            if category not in WATCH_CATEGORIES:
                continue
            model = model or "модель неизвестна"
            heading = degrees_to_compass(true_track) if true_track is not None else None
            emoji = CATEGORY_EMOJI.get(category, "⚪")

            if event_kind == "departure":
                direction = f"курс {heading}" if heading else "нет данных"
                text = (
                    f"{emoji} 🛫 <b>Вылет из Варшавы (EPWA)</b>\n"
                    f"Категория: {category}\n"
                    f"Рейс: {callsign}\n"
                    f"Самолёт: {model}\n"
                    f"Куда: {direction}"
                )
            else:
                direction = f"курс {heading}" if heading else "нет данных"
                text = (
                    f"{emoji} 🛬 <b>Посадка в Варшаве (EPWA)</b>\n"
                    f"Категория: {category}\n"
                    f"Рейс: {callsign}\n"
                    f"Самолёт: {model}\n"
                    f"Откуда: {direction}"
                )
            new_events.append(text)

    for text in new_events:
        send_telegram(text)
        time.sleep(1)

    state["tracked"] = tracked
    save_state(state)
    print(f"Бортов рядом с аэропортом: {len(states)}, новых событий: {len(new_events)}")


if __name__ == "__main__":
    main()

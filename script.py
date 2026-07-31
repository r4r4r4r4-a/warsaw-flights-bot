import os
import json
import time
import requests

AIRPORT = "EPWA"  # Варшава, Шопена
STATE_FILE = "state.json"
WINDOW_MINUTES = 20  # окно проверки назад (с запасом, чтобы не пропустить рейсы)
MAX_STATE_IDS = 1000  # сколько ID держим в памяти, чтобы файл не рос бесконечно

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
    return {"sent_ids": []}


def save_state(state):
    state["sent_ids"] = state["sent_ids"][-MAX_STATE_IDS:]
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


def get_airport_name(icao):
    if not icao:
        return None
    try:
        r = requests.get(f"https://hexdb.io/api/v1/airport/icao/{icao}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            name = data.get("airport")
            if name:
                return f"{name} ({icao})"
    except Exception:
        pass
    return icao


def degrees_to_compass(deg):
    directions = [
        "север", "северо-восток", "восток", "юго-восток",
        "юг", "юго-запад", "запад", "северо-запад",
    ]
    idx = round(deg / 45) % 8
    return directions[idx]


def get_last_heading(token, icao24):
    """Возвращает последний известный курс полёта (компас), если есть."""
    try:
        headers = {"Authorization": f"Bearer {token}"}
        params = {"icao24": icao24, "time": 0}
        r = requests.get(
            "https://opensky-network.org/api/tracks/all",
            headers=headers, params=params, timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            path = data.get("path") or []
            for point in reversed(path):
                # point: [time, lat, lon, baro_altitude, true_track, on_ground]
                if len(point) > 4 and point[4] is not None:
                    return degrees_to_compass(point[4])
    except Exception:
        pass
    return None


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


def fetch_flights(token, kind):
    now = int(time.time())
    begin = now - WINDOW_MINUTES * 60
    url = f"https://opensky-network.org/api/flights/{kind}"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"airport": AIRPORT, "begin": begin, "end": now}
    r = requests.get(url, headers=headers, params=params, timeout=20)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return r.json()


def main():
    state = load_state()
    sent = set(state["sent_ids"])
    token = get_opensky_token()

    new_events = []

    for flight in fetch_flights(token, "arrival"):
        fid = f"arr-{flight['icao24']}-{flight.get('lastSeen')}"
        if fid in sent:
            continue
        sent.add(fid)
        model, category = get_aircraft_info(flight["icao24"])
        if category not in WATCH_CATEGORIES:
            continue
        model = model or "модель неизвестна"
        dep = get_airport_name(flight.get("estDepartureAirport"))
        if not dep:
            heading = get_last_heading(token, flight["icao24"])
            dep = f"курс {heading}" if heading else "нет данных"
        callsign = (flight.get("callsign") or "").strip() or "без позывного"
        emoji = CATEGORY_EMOJI.get(category, "⚪")
        text = (
            f"{emoji} 🛬 <b>Посадка в Варшаве (EPWA)</b>\n"
            f"Категория: {category}\n"
            f"Рейс: {callsign}\n"
            f"Самолёт: {model}\n"
            f"Откуда: {dep}"
        )
        new_events.append(text)

    for flight in fetch_flights(token, "departure"):
        fid = f"dep-{flight['icao24']}-{flight.get('firstSeen')}"
        if fid in sent:
            continue
        sent.add(fid)
        model, category = get_aircraft_info(flight["icao24"])
        if category not in WATCH_CATEGORIES:
            continue
        model = model or "модель неизвестна"
        arr = get_airport_name(flight.get("estArrivalAirport"))
        if not arr:
            heading = get_last_heading(token, flight["icao24"])
            arr = f"курс {heading}" if heading else "нет данных"
        callsign = (flight.get("callsign") or "").strip() or "без позывного"
        emoji = CATEGORY_EMOJI.get(category, "⚪")
        text = (
            f"{emoji} 🛫 <b>Вылет из Варшавы (EPWA)</b>\n"
            f"Категория: {category}\n"
            f"Рейс: {callsign}\n"
            f"Самолёт: {model}\n"
            f"Куда: {arr}"
        )
        new_events.append(text)

    for text in new_events:
        send_telegram(text)
        time.sleep(1)

    state["sent_ids"] = list(sent)
    save_state(state)
    print(f"Отправлено новых событий: {len(new_events)}")


if __name__ == "__main__":
    main()

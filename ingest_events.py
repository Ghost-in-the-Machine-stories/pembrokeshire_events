import sqlite3
from bs4 import BeautifulSoup
import requests
from requests.exceptions import ConnectionError, Timeout
from datetime import datetime
import json
import re
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut
import time
from urllib.parse import urljoin
import os

DB_NAME = "pembrokeshire_events.db"
OUTPUT_JSON = "events.json"
SOURCES_JSON = "sources.json"

geolocator = Nominatim(user_agent="wales_community_mapper_v16")

def load_target_sources():
    if not os.path.exists(SOURCES_JSON):
        print(f"Error: {SOURCES_JSON} not found!")
        return []
    with open(SOURCES_JSON, "r", encoding="utf-8") as f:
        return json.load(f)

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT UNIQUE,
            town TEXT,
            address TEXT,
            lat REAL,
            lng REAL,
            date_from TEXT,
            date_to TEXT,
            time TEXT,
            originator_url TEXT,
            category TEXT,
            price_range TEXT,
            restrictions TEXT
        )
    """)
    conn.commit()
    conn.close()

def is_valid_event_candidate(title):
    if not title or len(title.strip()) < 4:
        return False
        
    title_clean = title.strip().lower()

    # Block calendar UI junk like "1 event,18", "2 events,28"
    if re.search(r'^\d+\s*events?(,\d+)?$', title_clean):
        return False

    ignore_exact = {
        "home", "contact us", "menu", "tickets", "read more", "find out more",
        "view details", "search", "events", "views navigation", "event views navigation",
        "skip to content", "privacy policy", "cookie policy", "terms & conditions",
        "book now", "book tickets", "register", "login", "basket", "checkout",
        "our partners", "about us", "quick links", "councillors", "financial matters",
        "get in touch", "how to find us", "castle opening times", "image use & credits",
        "businesses", "sign up to our newsletter", "october 2026", "november 2026", "december 2026",
        "local parking", "we're dementia friendly!", "book a room", "where to find us",
        "whitestone media group", "latest news and events", "support arts for social change",
        "newsletter sign-up", "other welsh water adventures", "events search and views navigation"
    }
    if title_clean in ignore_exact:
        return False

    if "defibrillator" in title_clean or "parking" in title_clean:
        return False

    return True

def query_postcode_api(postcode_str):
    if not postcode_str:
        return None
    clean_pc = postcode_str.replace(" ", "").upper()
    url = f"https://api.postcodes.io/postcodes/{clean_pc}"
    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json().get('result', {})
            town = data.get('parish') or data.get('admin_district') or "Pembrokeshire"
            return {
                "town": town,
                "lat": data.get('latitude'),
                "lng": data.get('longitude'),
                "postcode": data.get('postcode')
            }
    except Exception:
        pass
    return None

def geocode_with_nominatim(location_query):
    if not location_query:
        return "Pembrokeshire", None, None

    clean_query = location_query.strip()
    if "wales" not in clean_query.lower() and "uk" not in clean_query.lower():
        query_string = f"{clean_query}, Pembrokeshire, Wales, UK"
    else:
        query_string = f"{clean_query}, UK"

    try:
        time.sleep(1)
        location = geolocator.geocode(query_string, timeout=10)
        if location:
            address_details = location.raw.get('address', {})
            town = (
                address_details.get('town') or 
                address_details.get('city') or 
                address_details.get('village') or 
                address_details.get('suburb') or 
                address_details.get('municipality') or
                "Pembrokeshire"
            )
            return town, location.latitude, location.longitude
    except (GeocoderTimedOut, Exception):
        pass

    return "Pembrokeshire", None, None

def extract_precise_location(soup):
    if not soup:
        return None, None, None, None

    for script in soup.find_all('script', type='application/ld+json'):
        try:
            if not script.string:
                continue
            data = json.loads(script.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get('@type') in ['Event', 'MusicEvent', 'SocialEvent', 'TheaterEvent', 'EducationEvent']:
                    loc = item.get('location')
                    if isinstance(loc, dict):
                        venue_name = loc.get('name', '')
                        addr = loc.get('address', {})
                        pc = addr.get('postalCode') if isinstance(addr, dict) else None
                        if pc:
                            ons_res = query_postcode_api(pc)
                            if ons_res:
                                full_venue = f"{venue_name or 'Venue'}, {ons_res['town']} ({ons_res['postcode']})"
                                return full_venue, ons_res['town'], ons_res['lat'], ons_res['lng']
                        
                        locality = addr.get('addressLocality') if isinstance(addr, dict) else ''
                        if venue_name or locality:
                            query_str = f"{venue_name}, {locality}".strip(', ')
                            town, lat, lng = geocode_with_nominatim(query_str)
                            return query_str, town, lat, lng
        except (json.JSONDecodeError, TypeError):
            continue

    body_text = soup.get_text(separator=' ')
    postcode_match = re.search(r'\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b', body_text, re.IGNORECASE)
    if postcode_match:
        pc_str = postcode_match.group(1)
        ons_res = query_postcode_api(pc_str)
        if ons_res:
            return f"Event Location ({ons_res['postcode']})", ons_res['town'], ons_res['lat'], ons_res['lng']

    for label in ['Venue:', 'Location:', 'Where:', 'Held at:', 'Address:']:
        match = re.search(rf'{label}\s*([^\n\.]{3,100})', body_text, re.IGNORECASE)
        if match:
            found_loc = match.group(1).strip()
            if len(found_loc) > 3 and not re.search(r'\d{5,}', found_loc):
                town, lat, lng = geocode_with_nominatim(found_loc)
                return found_loc, town, lat, lng

    return None, None, None, None

def extract_price_and_restrictions(text):
    t_lower = text.lower()
    price = "Free" if "free" in t_lower else "Check Website"
    price_match = re.search(r'(?:£|\bfrom\s*£)(\d+(?:\.\d{2})?)', text)
    if price_match:
        price = f"£{price_match.group(1)}"

    restrictions = "All Ages / None"
    if "18+" in t_lower or "adults only" in t_lower:
        restrictions = "18+ Only"
    elif "under 3" in t_lower or "toddler" in t_lower:
        restrictions = "Toddlers (Under 3)"
    elif "under 5" in t_lower or "pre-school" in t_lower:
        restrictions = "Under 5s"
    elif "family" in t_lower or "all ages" in t_lower:
        restrictions = "Family Friendly"
    
    return price, restrictions

def extract_date_from_text(text):
    current_year = datetime.now().year
    months = {
        'january': '01', 'february': '02', 'march': '03', 'april': '04',
        'may': '05', 'june': '06', 'july': '07', 'august': '08',
        'september': '09', 'october': '10', 'november': '11', 'december': '12'
    }

    text_lower = text.lower()
    for month_name, month_num in months.items():
        if month_name in text_lower:
            day_match = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\b', text_lower)
            if day_match:
                day_num = day_match.group(1).zfill(2)
                parsed_date = f"{current_year}-{month_num}-{day_num}"
                if parsed_date < datetime.now().strftime("%Y-%m-%d"):
                    parsed_date = f"{current_year + 1}-{month_num}-{day_num}"
                return parsed_date
    return datetime.now().strftime("%Y-%m-%d")

def classify_category(text, source_category="Community"):
    t = text.lower()
    if any(w in t for w in ["halloween", "spook", "witch", "ghost", "pumpkin"]):
        return "Halloween"
    elif any(w in t for w in ["christmas", "santa", "festive", "winter"]):
        return "Christmas"
    elif any(w in t for w in ["rugby", "football", "match", "fc", "sport", "race", "cycling"]):
        return "Sports"
    elif any(w in t for w in ["holiday", "school", "family", "kids", "children", "workshop", "trail", "panto"]):
        return "School Hol/Family"
    elif any(w in t for w in ["music", "concert", "band", "show", "theater", "theatre", "quiz"]):
        return "Entertainment"
    return source_category

def parse_and_store():
    init_db()
    target_sources = load_target_sources()
    if not target_sources:
        return

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("DELETE FROM events WHERE date_to < ?", (today_str,))
    conn.commit()

    scraped_entries = []

    for source in target_sources:
        print(f"\nScanning live source: {source['name']} ({source['url']})...")
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            resp = requests.get(source["url"], headers=headers, timeout=15)
            if resp.status_code != 200:
                print(f" -> Skipped: HTTP status {resp.status_code}")
                continue

            soup = BeautifulSoup(resp.text, 'html.parser')
            event_cards = soup.find_all(['div', 'article', 'li'], class_=re.compile('event|grid|box|tile|item', re.I))

            for card in event_cards:
                title_el = card.find(['h2', 'h3', 'h4', 'h5', 'strong'])
                if not title_el:
                    continue

                title = title_el.get_text(strip=True)
                if not is_valid_event_candidate(title):
                    continue

                snippet = card.get_text(separator=' ', strip=True)

                link = source["url"]
                a_tag = card.find('a', href=True)
                if a_tag:
                    link = urljoin(source["url"], a_tag['href'])

                if title not in [e["title"] for e in scraped_entries]:
                    scraped_entries.append({
                        "title": title,
                        "snippet": snippet,
                        "link": link,
                        "card_soup": card,
                        "source_name": source["name"],
                        "source_category": source["category"],
                        "default_town": source.get("default_town", "Pembrokeshire"),
                        "default_lat": source.get("default_lat"),
                        "default_lng": source.get("default_lng")
                    })
        except (ConnectionError, Timeout) as e:
            print(f" -> Connection/Timeout warning: Skipped source {source['name']} ({e})")
            continue
        except Exception as e:
            print(f" -> Unexpected error with source {source['name']}: {e}")
            continue

    print(f"\nFound {len(scraped_entries)} valid candidates. Inspecting individual event pages on the fly...")

    for entry in scraped_entries:
        title = entry["title"]
        snippet = entry["snippet"]
        link = entry["link"]
        source_name = entry["source_name"]

        address_final, town, lat, lng = None, None, None, None
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            detail_resp = requests.get(link, headers=headers, timeout=12)
            if detail_resp.status_code == 200:
                detail_soup = BeautifulSoup(detail_resp.text, 'html.parser')
                address_final, town, lat, lng = extract_precise_location(detail_soup)
        except Exception:
            pass

        if not address_final or not lat:
            address_final, town, lat, lng = extract_precise_location(entry["card_soup"])

        if not lat or not lng:
            lat = entry["default_lat"]
            lng = entry["default_lng"]
            town = entry["default_town"]
            address_final = f"{source_name}, {town}"

        if not town:
            town = entry["default_town"]

        address_str = address_final if address_final else f"{source_name}, {town}"
        event_date = extract_date_from_text(snippet)
        category = classify_category(title + " " + snippet, entry["source_category"])
        price_range, restrictions = extract_price_and_restrictions(snippet)

        try:
            cursor.execute("""
                INSERT OR IGNORE INTO events (title, town, address, lat, lng, date_from, date_to, time, originator_url, category, price_range, restrictions)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (title, town, address_str, lat, lng, event_date, event_date, "10:00 BST", link, category, price_range, restrictions))
            
            if cursor.rowcount > 0:
                loc_status = f"{lat:.4f}, {lng:.4f}" if lat and lng else "Ungeocoded"
                print(f" -> ADDED: '{title}' [{loc_status}] | Venue: {address_str} | Town: {town}")
        except Exception as e:
            print(f"DB Error for '{title}': {e}")

    conn.commit()

    cursor.execute("SELECT title, town, address, lat, lng, date_from, date_to, time, originator_url, category, price_range, restrictions FROM events")
    rows = cursor.fetchall()
    conn.close()

    events_list = [{
        "title": r[0], "town": r[1], "address": r[2],
        "lat": r[3], "lng": r[4], "date": r[5], "dateFrom": r[5], "dateTo": r[6],
        "time": r[7], "originatorUrl": r[8], "category": r[9],
        "priceRange": r[10], "restrictions": r[11]
    } for r in rows]

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(events_list, f, indent=2)

    print(f"\n[Ingestion Complete]: Processed and exported {len(events_list)} precise events to {OUTPUT_JSON}.")

if __name__ == "__main__":
    parse_and_store()
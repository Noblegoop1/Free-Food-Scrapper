import json
import time
import re
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import os
import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
# Supabase config — set these as GitHub Secrets
# SUPABASE_URL  e.g. https://xxxx.supabase.co
# SUPABASE_KEY  your anon/public key
# ─────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=ignore-duplicates",  # skip dupes by unique link
}

folder_path = os.path.join(os.path.dirname(__file__), "event_data")
os.makedirs(folder_path, exist_ok=True)

options = webdriver.ChromeOptions()
options.add_argument("--headless")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

driver = webdriver.Chrome(
    service=Service(ChromeDriverManager().install()), options=options
)

FOOD_KEYWORDS = [
    "food", "drinks", "snacks", "meal", "pizza", "catering",
    "refreshments", "lunch", "dinner", "breakfast", "coffee", "tea",
    "free food", "free lunch", "free dinner", "appetizers", "dessert",
    "boba", "wings", "cookies", "donuts", "bagels",
]

STRONG_KEYWORDS = [
    "free food", "free lunch", "free dinner", "free breakfast",
    "catering", "refreshments provided", "food provided",
    "light refreshments", "complimentary food",
]

FREE_PRICE_PATTERNS = ["free", "0", "$0", "no cost", ""]

def compute_likelihood(event: dict) -> int:
    """
    Returns a 0-100 likelihood score that free food will actually be there.
    Scoring rubric:
      - Price field is free/empty           → +40
      - Strong food keyword in description  → +35
      - Soft food keyword in description    → +20
      - Event is today                      → +5  (bonus confidence)
    Capped at 100.
    """
    score = 0
    price = (event.get("Price") or "").strip().lower()
    desc  = (event.get("description") or "").lower()
    day   = (event.get("Day") or "").lower()

    if any(p == price for p in FREE_PRICE_PATTERNS):
        score += 40

    if any(kw in desc for kw in STRONG_KEYWORDS):
        score += 35
    elif any(kw in desc for kw in FOOD_KEYWORDS):
        score += 20

    today_str = datetime.now().strftime("%A, %B %-d").lower()  # e.g. "monday, april 7"
    if today_str in day.lower():
        score += 5

    return min(score, 100)


def save_event_to_supabase(event_data: dict):
    payload = {
        "event_name":   event_data["EventName"],
        "event_day":    event_data["Day"],
        "start_time":   event_data["Start_time"],
        "end_time":     event_data["End_time"],
        "link":         event_data["Link"],
        "price":        event_data["Price"],
        "description":  event_data.get("description", ""),
        "likelihood":   compute_likelihood(event_data),
        "date_added":   datetime.utcnow().isoformat(),
    }
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/free_food_events",
        headers=SUPABASE_HEADERS,
        json=payload,
        timeout=10,
    )
    if resp.status_code not in (200, 201):
        print(f"  Supabase error {resp.status_code}: {resp.text[:200]}")
    else:
        print(f"  Saved: {event_data['EventName']} (likelihood {payload['likelihood']}%)")


def web_scanning(scan_folder: str):
    if not os.path.exists(scan_folder):
        print(f"{scan_folder} not found"); return

    files = [f for f in os.listdir(scan_folder) if f.endswith(".json")]
    if not files:
        print("No event JSON files found"); return

    for filename in files:
        file_path = os.path.join(scan_folder, filename)
        print(f"\nScanning: {filename}")
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                events = json.load(f)

            for i in events:
                url = i.get("Link")
                if not url:
                    continue
                try:
                    resp = requests.get(url, timeout=10)
                    soup = BeautifulSoup(resp.text, "html.parser")
                    event_div = soup.find("div", id="event_details")

                    if event_div:
                        description = event_div.get_text(separator="\n", strip=True).lower()
                        if any(word in description for word in FOOD_KEYWORDS):
                            event_to_save = {
                                "EventName":  i.get("EventName"),
                                "Day":        i.get("Day"),
                                "Start_time": i.get("Start_time"),
                                "End_time":   i.get("End_time"),
                                "Link":       url,
                                "Price":      i.get("Price"),
                                "description": description,
                            }
                            save_event_to_supabase(event_to_save)
                except Exception as e:
                    print(f"  Error fetching {url}: {e}")

        except Exception as e:
            print(f"Error processing {filename}: {e}")
        finally:
            os.remove(file_path)


def process_json(scan_folder: str):
    if not os.path.exists(scan_folder):
        print(f"{scan_folder} not found"); return

    files = [f for f in os.listdir(scan_folder) if f.endswith(".json")]
    if not files:
        print("No raw JSON files found"); return

    print(f"Processing {len(files)} raw file(s)…")
    pattern = r">([^<]+)</p><p[^>]*>(\d{1,2}(?::\d{2})?\s*[AP]M)\s*(?:&ndash;|-)\s*(\d{1,2}(?::\d{2})?\s*[AP]M)</p>"

    for filename in files:
        file_path = os.path.join(scan_folder, filename)
        data_list = []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                events = json.load(f)

            for i in events:
                if i.get("listingSeparator") == "true":
                    continue
                name        = i.get("p3", "No name")
                url_1       = i.get("p8", "").lower()
                url_2       = i.get("p18", "").lower()
                price       = i.get("p12", "").lower()
                r_date_time = i.get("p4", "")

                if r_date_time and isinstance(r_date_time, str):
                    match = re.search(pattern, r_date_time)
                    if match:
                        data_list.append({
                            "EventName":  name,
                            "Day":        match.group(1),
                            "Start_time": match.group(2),
                            "End_time":   match.group(3),
                            "Link":       f"https://mason360.gmu.edu/{url_1}{url_2}",
                            "Price":      price,
                        })

            out_file = os.path.join(scan_folder, f"event_list_({filename}).json")
            with open(out_file, "w", encoding="utf-8") as jf:
                json.dump(data_list, jf, indent=4, ensure_ascii=False)
            print(f"  Processed → {out_file}")

        except Exception as e:
            print(f"Error processing {filename}: {e}")
        finally:
            os.remove(file_path)


def scrape_mobile_events(url: str):
    driver.get(url)
    processed_ids = set()
    current_range = 0
    max_range = 200

    print("Monitoring network for event XHR responses…")

    while current_range < max_range:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1)

        for entry in driver.get_log("performance"):
            message = json.loads(entry["message"])["message"]
            if message["method"] != "Network.responseReceived":
                continue

            resp_url   = message["params"]["response"]["url"]
            request_id = message["params"]["requestId"]

            if "mobile_events_list" not in resp_url or request_id in processed_ids:
                continue

            range_match   = re.search(r"range=(\d+)", resp_url)
            file_range    = range_match.group(1) if range_match else "unknown"
            if range_match:
                current_range = int(range_match.group(1))
                print(f"  XHR range={current_range}")

            try:
                response = driver.execute_cdp_cmd(
                    "Network.getResponseBody", {"requestId": request_id}
                )
                data     = json.loads(response["body"])
                filename = os.path.join(folder_path, f"events_range_{file_range}.json")
                with open(filename, "w", encoding="utf-8") as jf:
                    json.dump(data, jf, indent=4, ensure_ascii=False)
                print(f"  Saved {filename}")
            except Exception:
                pass

            processed_ids.add(request_id)

            if current_range >= max_range:
                print(f"Reached max range ({current_range}). Done scraping.")
                return

    driver.quit()


# ── Entry point ───────────────────────────────
scrape_mobile_events("https://mason360.gmu.edu/events")
process_json(folder_path)
web_scanning(folder_path)
driver.quit()

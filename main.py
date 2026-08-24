import os
import time
import hashlib
from playwright.sync_api import sync_playwright
import requests

# ====== CONFIG ======
URL = "https://zealy.io/cw/minebit/questboard/sprints"
CHECK_INTERVAL = 45          # seconds (change to 30 if you want)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SCREENSHOT_PATH = "current.png"
PREV_HASH = None
# ====================

def send_telegram(photo_path, caption="Change detected on Zealy!"):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    with open(photo_path, "rb") as photo:
        requests.post(url, data={"chat_id": CHAT_ID, "caption": caption}, files={"photo": photo})

def get_image_hash(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()

def main():
    global PREV_HASH

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        print("Starting watcher...")
        while True:
            try:
                page.goto(URL, wait_until="networkidle", timeout=60000)
                time.sleep(3)  # let JS fully load
                page.screenshot(path=SCREENSHOT_PATH, full_page=True)

                current_hash = get_image_hash(SCREENSHOT_PATH)

                if PREV_HASH is None:
                    PREV_HASH = current_hash
                    print("First screenshot taken.")
                elif current_hash != PREV_HASH:
                    print("Change detected! Sending Telegram...")
                    send_telegram(SCREENSHOT_PATH, "🚨 Change detected on Minebit Zealy questboard!")
                    PREV_HASH = current_hash
                else:
                    print("No change.")

            except Exception as e:
                print(f"Error: {e}")

            time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()

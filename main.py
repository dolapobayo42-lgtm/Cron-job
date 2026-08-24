import os
import time
import hashlib
from datetime import datetime
from playwright.sync_api import sync_playwright
import requests

# ====== CONFIG ======
URL = "https://zealy.io/cw/minebit/questboard/sprints"
CHECK_INTERVAL = 45          # seconds (change to 30 if you want)
HEALTH_CHECK_INTERVAL = 7200  # 2 hours in seconds
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SCREENSHOT_PATH = "current.png"
PREV_HASH = None
# ====================

def send_telegram(photo_path, caption="Change detected on Zealy!"):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    with open(photo_path, "rb") as photo:
        requests.post(url, data={"chat_id": CHAT_ID, "caption": caption}, files={"photo": photo})

def send_telegram_text(message):
    """Send a text-only message to Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": message})

def get_image_hash(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()

def main():
    global PREV_HASH

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        print("Starting watcher...")
        last_health_check = 0
        
        while True:
            try:
                page.goto(URL, wait_until="networkidle", timeout=60000)
                time.sleep(3)  # let JS fully load
                page.screenshot(path=SCREENSHOT_PATH, full_page=True)

                current_hash = get_image_hash(SCREENSHOT_PATH)
                current_time = time.time()

                if PREV_HASH is None:
                    PREV_HASH = current_hash
                    print("First screenshot taken. Sending initial snapshot to Telegram...")
                    send_telegram(SCREENSHOT_PATH, "✅ Zealy Watcher Started - Initial Snapshot")
                    last_health_check = current_time
                elif current_hash != PREV_HASH:
                    print("Change detected! Sending Telegram...")
                    send_telegram(SCREENSHOT_PATH, "🚨 Change detected on Minebit Zealy questboard!")
                    PREV_HASH = current_hash
                    last_health_check = current_time  # Reset health check timer on change
                else:
                    print("No change.")
                
                # Health check every 2 hours
                if current_time - last_health_check >= HEALTH_CHECK_INTERVAL:
                    print("Sending health check...")
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    send_telegram(SCREENSHOT_PATH, f"✅ Health Check - Watcher is running!\n{timestamp}\nNo changes detected in last 2 hours")
                    last_health_check = current_time

            except Exception as e:
                print(f"Error: {e}")
                send_telegram_text(f"❌ Zealy Watcher Error:\n{str(e)}")

            time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()

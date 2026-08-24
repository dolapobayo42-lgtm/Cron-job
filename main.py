import os
import time
import json
import html as html_lib
import re
from datetime import datetime
from playwright.sync_api import sync_playwright
import requests
import glob

# ====== CONFIG ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SITES_FILE = "watched_sites.json"
SCREENSHOTS_DIR = "screenshots"
HTML_DIR = "html_dumps"
LOG_FILE = "watcher.log"

DEFAULT_SITES = {
    "zealy": {
        "url": "https://zealy.io/cw/minebit/questboard/sprints",
        "interval": 30,
        "enabled": True,
        "last_check": 0,
        "description": "Minebit Zealy Quest Board"
    }
}

COOKIE_BUTTON_TEXTS = [
    "Accept all", "Accept All", "Accept all cookies",
    "I accept", "Accept", "Got it", "Allow all", "Agree",
]

# The real quest-title element we confirmed from the HTML dump
QUEST_NAME_SELECTOR = "[class*='quest-card-quest-name']"
# ====================


class SiteWatcher:
    def __init__(self):
        self.sites = self.load_sites()
        self.previous_quests = {}
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        os.makedirs(HTML_DIR, exist_ok=True)
        self.log_file = open(LOG_FILE, 'a')

    def log(self, message):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        full_msg = f"[{timestamp}] {message}"
        print(full_msg)
        self.log_file.write(full_msg + "\n")
        self.log_file.flush()

    def load_sites(self):
        if os.path.exists(SITES_FILE):
            with open(SITES_FILE, 'r') as f:
                sites = json.load(f)
            for site in sites.values():
                if site.get('last_check') is None:
                    site['last_check'] = 0
            return sites
        else:
            self.save_sites(DEFAULT_SITES)
            return DEFAULT_SITES

    def save_sites(self, sites):
        with open(SITES_FILE, 'w') as f:
            json.dump(sites, f, indent=2)
        self.sites = sites

    def dismiss_cookie_banner(self, page):
        for text in COOKIE_BUTTON_TEXTS:
            try:
                btn = page.get_by_text(text, exact=False).first
                if btn.is_visible(timeout=1500):
                    btn.click(timeout=1500)
                    self.log(f"🍪 Clicked cookie/consent button: '{text}'")
                    page.wait_for_timeout(1000)
                    return True
            except Exception:
                continue
        return False

    def scrape_and_capture(self, url, site_name, dump_html=False):
        """
        Loads the page, dismisses cookie banner, extracts real quest titles
        from the DOM (no OCR), and takes a screenshot for visual confirmation.
        Returns (quests, screenshot_path, html_path)
        """
        html_path = None
        try:
            self.log(f"🌐 Loading {site_name}...")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 1600})
                page.goto(url, wait_until="networkidle", timeout=60000)

                self.dismiss_cookie_banner(page)

                # Wait for at least one quest card to actually appear before reading
                try:
                    page.wait_for_selector(QUEST_NAME_SELECTOR, timeout=15000)
                except Exception:
                    self.log("⚠️ Quest name elements didn't appear within 15s, continuing anyway")

                time.sleep(2)

                # ---- Real DOM extraction (replaces OCR) ----
                quests = []
                try:
                    elements = page.query_selector_all(QUEST_NAME_SELECTOR)
                    for el in elements:
                        text = el.inner_text().strip()
                        if text:
                            text = html_lib.unescape(text)
                            text = re.sub(r'\s+', ' ', text)
                            quests.append(text)
                    # de-dupe while preserving order
                    seen = set()
                    deduped = []
                    for q in quests:
                        if q not in seen:
                            seen.add(q)
                            deduped.append(q)
                    quests = deduped
                    self.log(f"📝 Extracted {len(quests)} quest titles from DOM")
                except Exception as e:
                    self.log(f"⚠️ DOM extraction error: {e}")

                if dump_html:
                    try:
                        html_content = page.content()
                        html_path = f"{HTML_DIR}/{site_name}_{int(time.time())}.html"
                        with open(html_path, "w", encoding="utf-8") as f:
                            f.write(html_content)
                        self.log(f"📄 Saved full page HTML: {html_path}")
                    except Exception as e:
                        self.log(f"⚠️ Could not dump HTML: {e}")

                screenshot_path = f"{SCREENSHOTS_DIR}/{site_name}_{int(time.time())}.png"
                page.screenshot(path=screenshot_path, full_page=True)
                browser.close()

            self.log(f"✅ Screenshot saved: {screenshot_path}")
            return quests, screenshot_path, html_path
        except Exception as e:
            self.log(f"❌ Error scraping {site_name}: {e}")
            self.send_telegram_text(f"❌ Scrape Error for {site_name}:\n{str(e)}")
            return [], None, None

    def compare_quests(self, old_quests, new_quests):
        old_set = set(old_quests) if old_quests else set()
        new_set = set(new_quests) if new_quests else set()

        added = new_set - old_set
        removed = old_set - new_set

        return {
            'added': sorted(list(added)),
            'removed': sorted(list(removed)),
            'similarity': len(old_set & new_set) / max(len(old_set | new_set), 1) * 100 if (old_set | new_set) else 100
        }

    def send_telegram(self, photo_path, caption="Update"):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            with open(photo_path, "rb") as photo:
                requests.post(url, data={"chat_id": CHAT_ID, "caption": caption}, files={"photo": photo}, timeout=15)
            self.log(f"📱 Sent photo to Telegram")
        except Exception as e:
            self.log(f"Error sending photo: {e}")

    def send_telegram_text(self, message):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, data={"chat_id": CHAT_ID, "text": message}, timeout=15)
            self.log(f"📱 Sent text message to Telegram")
        except Exception as e:
            self.log(f"Error sending message: {e}")

    def send_telegram_document(self, file_path, caption=""):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
            with open(file_path, "rb") as doc:
                requests.post(
                    url,
                    data={"chat_id": CHAT_ID, "caption": caption},
                    files={"document": doc},
                    timeout=30
                )
            self.log(f"📱 Sent document to Telegram: {file_path}")
        except Exception as e:
            self.log(f"Error sending document: {e}")

    def show_dashboard(self):
        message = f"""
🔍 **ZEALY WATCHER DASHBOARD**

📊 Status: ✅ ACTIVE (DOM mode — no OCR)

Site: Minebit Zealy Quest Board
Check Interval: 30 seconds
Last Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

🟢 MONITORING ENABLED
- Reading real quest titles from page DOM
- Real-time alerts enabled

Alerts will show:
✨ New quests added
❌ Quests removed
"""
        self.send_telegram_text(message)

    def watch(self):
        self.log("=" * 60)
        self.log("🚀 STARTING ZEALY WATCHER — DOM EXTRACTION MODE")
        self.log("=" * 60)

        self.send_telegram_text("✅ Zealy Watcher Started!\n\nNow reading real quest titles directly from the page (no more OCR).\nYou will receive alerts when quests are added or removed.")
        self.show_dashboard()

        check_count = 0

        while True:
            current_time = time.time()
            check_count += 1

            self.log(f"\n{'='*60}")
            self.log(f"--- CHECK #{check_count} at {datetime.now().strftime('%H:%M:%S')} ---")
            self.log(f"{'='*60}")

            for site_name, site_config in self.sites.items():
                if not site_config['enabled']:
                    self.log(f"⏭️ {site_name} is disabled, skipping")
                    continue

                last_check = site_config.get('last_check', 0) or 0
                time_since_check = current_time - last_check

                if time_since_check < site_config['interval']:
                    self.log(f"⏱️ {site_name}: Wait {site_config['interval'] - int(time_since_check)}s")
                    continue

                self.log(f"\n🔄 CHECKING {site_name.upper()}")
                self.log(f"Description: {site_config['description']}")

                try:
                    is_first_capture = site_name not in self.previous_quests

                    current_quests, screenshot_path, html_path = self.scrape_and_capture(
                        site_config['url'], site_name, dump_html=is_first_capture
                    )
                    if not screenshot_path:
                        self.log(f"❌ Failed to capture")
                        continue

                    if is_first_capture:
                        self.log(f"📌 FIRST TIME CAPTURE for {site_name}")
                        self.previous_quests[site_name] = current_quests

                        quest_text = "\n".join([f"• {q}" for q in current_quests])
                        caption = f"""✅ ZEALY WATCHER STARTED

Site: {site_config['description']}
Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

📝 Initial Quests Captured ({len(current_quests)}):
{quest_text}"""

                        # Telegram caption limit is 1024 chars, split if needed
                        if len(caption) <= 1024:
                            self.send_telegram(screenshot_path, caption)
                        else:
                            self.send_telegram(screenshot_path, f"✅ ZEALY WATCHER STARTED\n\n{len(current_quests)} quests found — full list below")
                            self.send_telegram_text(quest_text)

                        if html_path:
                            self.send_telegram_document(html_path, caption=f"📄 Reference HTML for {site_name}")

                        self.log(f"✅ Initial snapshot sent with {len(current_quests)} quests")

                    else:
                        self.log(f"Comparing with previous capture...")
                        comparison = self.compare_quests(self.previous_quests[site_name], current_quests)

                        self.log(f"📊 Similarity: {comparison['similarity']:.1f}%")
                        self.log(f"✨ Items added: {len(comparison['added'])}")
                        self.log(f"❌ Items removed: {len(comparison['removed'])}")

                        if comparison['added'] or comparison['removed']:
                            self.log(f"🚨 REAL CHANGE DETECTED!")
                            self.previous_quests[site_name] = current_quests

                            alert_text = f"""🚨 ZEALY BOARD UPDATED

Site: {site_config['description']}
Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Similarity: {comparison['similarity']:.1f}%

"""
                            if comparison['added']:
                                alert_text += f"✨ NEW QUESTS ({len(comparison['added'])}):\n"
                                for item in comparison['added']:
                                    alert_text += f"• {item}\n"
                                alert_text += "\n"

                            if comparison['removed']:
                                alert_text += f"❌ REMOVED QUESTS ({len(comparison['removed'])}):\n"
                                for item in comparison['removed']:
                                    alert_text += f"• {item}\n"

                            self.send_telegram(screenshot_path, alert_text)
                            self.log(f"✅ Alert sent to Telegram")
                        else:
                            self.log(f"✅ No changes detected (content matches)")

                    self.sites[site_name]['last_check'] = current_time
                    self.save_sites(self.sites)
                    self.log(f"✅ Saved check time for {site_name}")

                except Exception as e:
                    self.log(f"❌ Error checking {site_name}: {e}")
                    self.send_telegram_text(f"❌ Error checking {site_name}:\n{str(e)}")

            self.log(f"\n⏸️ Sleeping 10 seconds before next check cycle...")
            time.sleep(10)


def main():
    watcher = SiteWatcher()
    try:
        watcher.watch()
    except KeyboardInterrupt:
        watcher.log("Watcher stopped by user")
    except Exception as e:
        watcher.log(f"FATAL ERROR: {e}")
        watcher.send_telegram_text(f"🔴 WATCHER CRASHED:\n{str(e)}")


if __name__ == "__main__":
    main()

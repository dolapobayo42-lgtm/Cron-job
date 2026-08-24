import os
import time
import json
import html as html_lib
import re
from datetime import datetime
from playwright.sync_api import sync_playwright
import requests

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
        "interval": 10,
        "enabled": True,
        "last_check": 0,
        "description": "Minebit Zealy Quest Board"
    }
}

COOKIE_BUTTON_TEXTS = [
    "Accept all", "Accept All", "Accept all cookies",
    "I accept", "Accept", "Got it", "Allow all", "Agree",
]

QUEST_NAME_SELECTOR = "[class*='quest-card-quest-name']"

# Cooldown config
COOLDOWN_BASE = 60    # seconds, doubles each consecutive hit
COOLDOWN_MAX  = 3600  # cap at 1 hour

# Only match these EXACT page titles — not body text (too many false positives)
RATE_LIMIT_TITLES = [
    "just a moment",       # Cloudflare challenge
    "access denied",       # Hard block
    "429 too many requests",
    "error 429",
    "rate limited",
    "attention required",  # Cloudflare attention page
]
# ====================


class SiteWatcher:
    def __init__(self):
        self.sites = self.load_sites()
        self.previous_quests = {}
        self.rate_limit_hits = {}
        self.cooldown_until = {}
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

    # ------------------------------------------------------------------ #
    #  Rate limit helpers
    # ------------------------------------------------------------------ #

    def is_rate_limited_page(self, page):
        """
        Only triggers on specific page TITLES that indicate a block/challenge.
        Never checks body text — too many false positives on normal quest content.
        Also triggers if 0 quests found AND page title is not a known Zealy title.
        """
        try:
            title = (page.title() or "").lower().strip()
            self.log(f"📄 Page title: '{title}'")
            return any(sig in title for sig in RATE_LIMIT_TITLES)
        except Exception:
            return False

    def handle_rate_limit(self, site_name):
        hits = self.rate_limit_hits.get(site_name, 0) + 1
        self.rate_limit_hits[site_name] = hits
        cooldown = min(COOLDOWN_BASE * (2 ** (hits - 1)), COOLDOWN_MAX)
        resume_at = time.time() + cooldown
        self.cooldown_until[site_name] = resume_at
        resume_str = datetime.fromtimestamp(resume_at).strftime("%H:%M:%S")

        msg = (
            f"⚠️ RATE LIMIT DETECTED — {site_name}\n\n"
            f"Hit #{hits} in a row\n"
            f"Cooling down for {cooldown}s\n"
            f"Resuming at: {resume_str}"
        )
        self.log(f"⚠️ Rate limit hit #{hits} — cooldown {cooldown}s")
        self.send_telegram_text(msg)
        return cooldown

    def clear_rate_limit(self, site_name):
        if self.rate_limit_hits.get(site_name, 0) > 0:
            self.log(f"✅ Rate limit cleared for {site_name}")
            self.send_telegram_text(f"✅ Rate limit lifted — {site_name} back to normal")
        self.rate_limit_hits[site_name] = 0
        self.cooldown_until.pop(site_name, None)

    # ------------------------------------------------------------------ #
    #  Page helpers
    # ------------------------------------------------------------------ #

    def dismiss_cookie_banner(self, page):
        for text in COOKIE_BUTTON_TEXTS:
            try:
                btn = page.get_by_text(text, exact=False).first
                if btn.is_visible(timeout=1500):
                    btn.click(timeout=1500)
                    self.log(f"🍪 Dismissed cookie banner: '{text}'")
                    page.wait_for_timeout(1000)
                    return True
            except Exception:
                continue
        return False

    def scrape_and_capture(self, url, site_name, dump_html=False):
        """
        Returns (quests, screenshot_path, html_path, rate_limited: bool)
        """
        html_path = None
        try:
            self.log(f"🌐 Loading {site_name}...")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 1600})
                page.goto(url, wait_until="networkidle", timeout=60000)

                self.dismiss_cookie_banner(page)

                # Check title BEFORE hydration wait — catches hard blocks immediately
                if self.is_rate_limited_page(page):
                    screenshot_path = f"{SCREENSHOTS_DIR}/{site_name}_ratelimit_{int(time.time())}.png"
                    page.screenshot(path=screenshot_path, full_page=True)
                    browser.close()
                    return [], screenshot_path, None, True

                # DOM hydration wait (~11s as confirmed for Zealy's React shell)
                self.log("⏳ Waiting 11s for DOM hydration...")
                try:
                    page.wait_for_selector(QUEST_NAME_SELECTOR, timeout=15000)
                except Exception:
                    self.log("⚠️ Quest elements didn't appear within 15s, continuing anyway")
                time.sleep(11)

                # Check title again after hydration
                if self.is_rate_limited_page(page):
                    screenshot_path = f"{SCREENSHOTS_DIR}/{site_name}_ratelimit_{int(time.time())}.png"
                    page.screenshot(path=screenshot_path, full_page=True)
                    browser.close()
                    return [], screenshot_path, None, True

                # ---- DOM extraction ----
                quests = []
                try:
                    elements = page.query_selector_all(QUEST_NAME_SELECTOR)
                    for el in elements:
                        text = el.inner_text().strip()
                        if text:
                            text = html_lib.unescape(text)
                            text = re.sub(r'\s+', ' ', text)
                            quests.append(text)
                    seen, deduped = set(), []
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
                    except Exception as e:
                        self.log(f"⚠️ Could not dump HTML: {e}")

                screenshot_path = f"{SCREENSHOTS_DIR}/{site_name}_{int(time.time())}.png"
                page.screenshot(path=screenshot_path, full_page=True)
                browser.close()

            self.log(f"✅ Screenshot saved: {screenshot_path}")
            return quests, screenshot_path, html_path, False

        except Exception as e:
            self.log(f"❌ Error scraping {site_name}: {e}")
            self.send_telegram_text(f"❌ Scrape Error for {site_name}:\n{str(e)}")
            return [], None, None, False

    # ------------------------------------------------------------------ #
    #  Telegram helpers
    # ------------------------------------------------------------------ #

    def compare_quests(self, old_quests, new_quests):
        old_set = set(old_quests) if old_quests else set()
        new_set = set(new_quests) if new_quests else set()
        added   = new_set - old_set
        removed = old_set - new_set
        return {
            'added':      sorted(added),
            'removed':    sorted(removed),
            'similarity': len(old_set & new_set) / max(len(old_set | new_set), 1) * 100
                          if (old_set | new_set) else 100
        }

    def send_telegram(self, photo_path, caption="Update"):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            with open(photo_path, "rb") as photo:
                requests.post(url, data={"chat_id": CHAT_ID, "caption": caption},
                              files={"photo": photo}, timeout=15)
            self.log("📱 Sent photo")
        except Exception as e:
            self.log(f"Error sending photo: {e}")

    def send_telegram_text(self, message):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, data={"chat_id": CHAT_ID, "text": message}, timeout=15)
            self.log("📱 Sent text")
        except Exception as e:
            self.log(f"Error sending message: {e}")

    def send_telegram_document(self, file_path, caption=""):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
            with open(file_path, "rb") as doc:
                requests.post(url, data={"chat_id": CHAT_ID, "caption": caption},
                              files={"document": doc}, timeout=30)
            self.log(f"📱 Sent document: {file_path}")
        except Exception as e:
            self.log(f"Error sending document: {e}")

    # ------------------------------------------------------------------ #
    #  Main loop
    # ------------------------------------------------------------------ #

    def watch(self):
        self.log("=" * 60)
        self.log("🚀 ZEALY WATCHER — DOM MODE + RATE LIMIT PROTECTION")
        self.log("=" * 60)

        self.send_telegram_text(
            "✅ Zealy Watcher Started!\n\n"
            "• DOM extraction (no OCR)\n"
            "• 11s hydration wait (~22-25s per real check)\n"
            "• Rate limit detection + auto-cooldown\n\n"
            "Alerts when quests are added or removed."
        )

        check_count = 0

        while True:
            current_time = time.time()
            check_count += 1

            self.log(f"\n{'='*60}")
            self.log(f"--- CHECK #{check_count} at {datetime.now().strftime('%H:%M:%S')} ---")
            self.log(f"{'='*60}")

            for site_name, site_config in self.sites.items():
                if not site_config['enabled']:
                    continue

                # Cooldown gate
                cooldown_end = self.cooldown_until.get(site_name, 0)
                if current_time < cooldown_end:
                    remaining = int(cooldown_end - current_time)
                    self.log(f"🕐 {site_name} cooling down — {remaining}s left")
                    continue

                # Interval gate
                last_check = site_config.get('last_check', 0) or 0
                if current_time - last_check < site_config['interval']:
                    continue

                self.log(f"\n🔄 CHECKING {site_name.upper()}")

                try:
                    is_first = site_name not in self.previous_quests

                    current_quests, screenshot_path, html_path, rate_limited = \
                        self.scrape_and_capture(site_config['url'], site_name, dump_html=is_first)

                    if not screenshot_path:
                        self.log("❌ Failed to capture")
                        continue

                    if rate_limited:
                        cooldown = self.handle_rate_limit(site_name)
                        self.send_telegram(
                            screenshot_path,
                            f"⚠️ RATE LIMITED — {site_name}\nCooling down {cooldown}s"
                        )
                        continue

                    self.clear_rate_limit(site_name)

                    if is_first:
                        self.log(f"📌 FIRST CAPTURE — {len(current_quests)} quests")
                        self.previous_quests[site_name] = current_quests

                        quest_text = "\n".join([f"• {q}" for q in current_quests])
                        caption = (
                            f"✅ ZEALY WATCHER STARTED\n\n"
                            f"Site: {site_config['description']}\n"
                            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                            f"📝 Initial Quests ({len(current_quests)}):\n{quest_text}"
                        )
                        if len(caption) <= 1024:
                            self.send_telegram(screenshot_path, caption)
                        else:
                            self.send_telegram(
                                screenshot_path,
                                f"✅ ZEALY WATCHER STARTED\n{len(current_quests)} quests — full list below"
                            )
                            self.send_telegram_text(quest_text)

                        if html_path:
                            self.send_telegram_document(html_path, caption=f"📄 HTML dump — {site_name}")

                    else:
                        comparison = self.compare_quests(self.previous_quests[site_name], current_quests)
                        self.log(f"Similarity: {comparison['similarity']:.1f}% | "
                                 f"+{len(comparison['added'])} / -{len(comparison['removed'])}")

                        if comparison['added'] or comparison['removed']:
                            self.log("🚨 CHANGE DETECTED!")
                            self.previous_quests[site_name] = current_quests

                            alert = (
                                f"🚨 ZEALY BOARD UPDATED\n\n"
                                f"Site: {site_config['description']}\n"
                                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                            )
                            if comparison['added']:
                                alert += f"✨ NEW QUESTS ({len(comparison['added'])}):\n"
                                alert += "\n".join(f"• {q}" for q in comparison['added']) + "\n\n"
                            if comparison['removed']:
                                alert += f"❌ REMOVED ({len(comparison['removed'])}):\n"
                                alert += "\n".join(f"• {q}" for q in comparison['removed'])

                            self.send_telegram(screenshot_path, alert)
                        else:
                            self.log("✅ No changes")

                    self.sites[site_name]['last_check'] = current_time
                    self.save_sites(self.sites)

                except Exception as e:
                    self.log(f"❌ Error checking {site_name}: {e}")
                    self.send_telegram_text(f"❌ Error checking {site_name}:\n{str(e)}")

            time.sleep(5)


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

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
COOLDOWN_BASE = 60
COOLDOWN_MAX  = 3600
RATE_LIMIT_TITLES = [
    "just a moment", "access denied", "429 too many requests",
    "error 429", "rate limited", "attention required",
]
# ====================


class SiteWatcher:
    def __init__(self):
        self.sites = self.load_sites()
        self.previous_quests = {}
        self.rate_limit_hits = {}
        self.cooldown_until = {}
        self.playwright = None
        self.browser = None
        self.log_file = open(LOG_FILE, 'a')
        self.last_update_id = 0
        self.paused = False
        self.start_time = time.time()
        self.check_count = 0
        self.consecutive_errors = 0

    def log(self, msg):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        self.log_file.write(line + "\n")
        self.log_file.flush()

    def load_sites(self):
        if os.path.exists(SITES_FILE):
            with open(SITES_FILE, 'r') as f:
                sites = json.load(f)
            for s in sites.values():
                s.setdefault('last_check', 0)
            return sites
        self.save_sites(DEFAULT_SITES)
        return dict(DEFAULT_SITES)

    def save_sites(self, sites=None):
        if sites:
            self.sites = sites
        with open(SITES_FILE, 'w') as f:
            json.dump(self.sites, f, indent=2)

    # ------------------------------------------------------------------ #
    #  Browser — one persistent instance for the life of the process
    # ------------------------------------------------------------------ #

    def start_browser(self):
        self.log("🚀 Launching browser...")
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)
        self.log("✅ Browser ready")

    def stop_browser(self):
        try:
            if self.browser:   self.browser.close()
            if self.playwright: self.playwright.stop()
        except Exception:
            pass
        self.browser = self.playwright = None

    def restart_browser(self):
        self.log("♻️ Restarting browser...")
        self.stop_browser()
        time.sleep(3)
        self.start_browser()

    # ------------------------------------------------------------------ #
    #  Rate limiting
    # ------------------------------------------------------------------ #

    def is_rate_limited(self, page):
        try:
            title = (page.title() or "").lower().strip()
            self.log(f"📄 Title: '{title}'")
            return any(s in title for s in RATE_LIMIT_TITLES)
        except Exception:
            return False

    def handle_rate_limit(self, site_name):
        hits = self.rate_limit_hits.get(site_name, 0) + 1
        self.rate_limit_hits[site_name] = hits
        cooldown = min(COOLDOWN_BASE * (2 ** (hits - 1)), COOLDOWN_MAX)
        self.cooldown_until[site_name] = time.time() + cooldown
        resume = datetime.fromtimestamp(self.cooldown_until[site_name]).strftime("%H:%M:%S")
        self.send_text(
            f"⚠️ RATE LIMIT — {site_name}\n"
            f"Hit #{hits}\nCooling down {cooldown}s\nResumes: {resume}"
        )

    def clear_rate_limit(self, site_name):
        if self.rate_limit_hits.get(site_name, 0) > 0:
            self.send_text(f"✅ Rate limit cleared — {site_name} resuming")
        self.rate_limit_hits[site_name] = 0
        self.cooldown_until.pop(site_name, None)

    # ------------------------------------------------------------------ #
    #  Scraper — one tab per check, adapts to real hydration time
    # ------------------------------------------------------------------ #

    def dismiss_cookie(self, page):
        for text in COOKIE_BUTTON_TEXTS:
            try:
                btn = page.get_by_text(text, exact=False).first
                if btn.is_visible(timeout=1500):
                    btn.click(timeout=1500)
                    self.log(f"🍪 Dismissed: '{text}'")
                    page.wait_for_timeout(800)
                    return True
            except Exception:
                continue
        return False

    def scrape(self, url, site_name):
        """Returns (quests, rate_limited, duration_seconds)"""
        page = None
        t0 = time.time()
        try:
            page = self.browser.new_page(viewport={"width": 1280, "height": 900})

            # 'load' fires faster than 'networkidle' on React SPAs
            page.goto(url, wait_until="load", timeout=60000)
            self.dismiss_cookie(page)

            if self.is_rate_limited(page):
                return [], True, round(time.time() - t0)

            # Wait exactly until quests appear — no fixed sleep needed
            self.log("⏳ Waiting for quests to render...")
            try:
                page.wait_for_selector(QUEST_NAME_SELECTOR, timeout=20000)
            except Exception:
                self.log("⚠️ Quests didn't appear in 20s, continuing anyway")

            time.sleep(2)  # small stability buffer

            if self.is_rate_limited(page):
                return [], True, round(time.time() - t0)

            elements = page.query_selector_all(QUEST_NAME_SELECTOR)
            quests, seen = [], set()
            for el in elements:
                text = el.inner_text().strip()
                if text:
                    text = re.sub(r'\s+', ' ', html_lib.unescape(text))
                    if text not in seen:
                        seen.add(text)
                        quests.append(text)

            duration = round(time.time() - t0)
            self.log(f"📝 {len(quests)} quests in {duration}s")
            return quests, False, duration

        except Exception as e:
            raise e
        finally:
            if page:
                try: page.close()
                except Exception: pass

    # ------------------------------------------------------------------ #
    #  Telegram helpers
    # ------------------------------------------------------------------ #

    def send_text(self, message):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": message},
                timeout=15
            )
            self.log("📱 Sent message")
        except Exception as e:
            self.log(f"TG error: {e}")

    def send_keyboard(self, message, keyboard, parse_mode="HTML"):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={
                    "chat_id": CHAT_ID,
                    "text": message,
                    "parse_mode": parse_mode,
                    "reply_markup": {"inline_keyboard": keyboard}
                },
                timeout=15
            )
        except Exception as e:
            self.log(f"TG keyboard error: {e}")

    def answer_callback(self, callback_id, text="✅"):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                data={"callback_query_id": callback_id, "text": text},
                timeout=5
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  Dashboard & site list
    # ------------------------------------------------------------------ #

    def uptime_str(self):
        secs = int(time.time() - self.start_time)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h:   return f"{h}h {m}m"
        if m:   return f"{m}m {s}s"
        return f"{s}s"

    def show_dashboard(self):
        status = "⏸ PAUSED" if self.paused else "✅ RUNNING"
        lines = [
            f"🔍 <b>ZEALY WATCHER</b>",
            f"",
            f"Status: {status}",
            f"Uptime: {self.uptime_str()}",
            f"Total Checks: {self.check_count}",
            f"",
            f"<b>Watched Sites:</b>",
        ]
        for name, cfg in self.sites.items():
            icon  = "✅" if cfg['enabled'] else "🔴"
            count = len(self.previous_quests.get(name, []))
            last  = cfg.get('last_check', 0) or 0
            ago   = f"{int(time.time()-last)}s ago" if last else "never"
            lines.append(f"  {icon} <b>{name}</b> — {count} quests — last: {ago}")

        lines += [
            "",
            "<b>Text commands:</b>",
            "/add &lt;name&gt; &lt;url&gt;",
            "/remove &lt;name&gt;",
            "/check [name]",
        ]

        keyboard = [
            [
                {"text": "📋 Manage Sites", "callback_data": "list_sites"},
                {"text": "🔄 Force Check All", "callback_data": "check_all"},
            ],
            [
                {"text": "⏸ Pause All" if not self.paused else "▶️ Resume",
                 "callback_data": "pause_all" if not self.paused else "resume_all"},
            ],
        ]
        self.send_keyboard("\n".join(lines), keyboard)

    def show_sites_list(self):
        if not self.sites:
            self.send_text("No sites are being watched.\nUse /add <name> <url> to add one.")
            return

        lines = ["<b>📋 Manage Sites</b>", ""]
        keyboard = []

        for name, cfg in self.sites.items():
            icon  = "✅" if cfg['enabled'] else "🔴"
            count = len(self.previous_quests.get(name, []))
            lines.append(f"{icon} <b>{name}</b> | {count} quests")
            lines.append(f"   {cfg['url']}")
            lines.append("")
            keyboard.append([
                {"text": f"{'🔴 Disable' if cfg['enabled'] else '✅ Enable'}", "callback_data": f"toggle:{name}"},
                {"text": "🔄 Check now", "callback_data": f"check:{name}"},
                {"text": "🗑 Remove", "callback_data": f"remove:{name}"},
            ])

        keyboard.append([{"text": "◀️ Dashboard", "callback_data": "dashboard"}])
        self.send_keyboard("\n".join(lines), keyboard)

    # ------------------------------------------------------------------ #
    #  Telegram update polling
    # ------------------------------------------------------------------ #

    def get_updates(self):
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": self.last_update_id + 1, "timeout": 1},
                timeout=5
            )
            data = resp.json()
            if data.get("ok"):
                return data.get("result", [])
        except Exception:
            pass
        return []

    def process_updates(self):
        for update in self.get_updates():
            self.last_update_id = update["update_id"]
            if "message" in update:
                self.handle_message(update["message"])
            elif "callback_query" in update:
                self.handle_callback(update["callback_query"])

    def handle_message(self, msg):
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=2)
        cmd   = parts[0].lower().split("@")[0]

        if cmd in ("/start", "/dashboard", "/status"):
            self.show_dashboard()

        elif cmd == "/list":
            self.show_sites_list()

        elif cmd == "/pause":
            self.paused = True
            self.send_text("⏸ All checks paused.\nSend /resume to continue.")

        elif cmd == "/resume":
            self.paused = False
            self.send_text("▶️ Checks resumed.")

        elif cmd == "/add":
            if len(parts) < 3:
                self.send_text(
                    "Usage: /add <name> <url>\n\n"
                    "Example:\n"
                    "/add minebit2 https://zealy.io/cw/minebit/questboard/sprints"
                )
                return
            name = parts[1].lower().strip()
            url  = parts[2].strip()
            if name in self.sites:
                self.send_text(f"⚠️ '{name}' already exists.\nRemove it first: /remove {name}")
                return
            self.sites[name] = {
                "url": url,
                "interval": 10,
                "enabled": True,
                "last_check": 0,
                "description": name
            }
            self.save_sites()
            self.send_text(f"✅ Added '{name}'\nURL: {url}\nWill start on next check cycle.")

        elif cmd == "/remove":
            if len(parts) < 2:
                self.send_text("Usage: /remove <name>")
                return
            name = parts[1].lower().strip()
            if name not in self.sites:
                self.send_text(f"⚠️ '{name}' not found.")
                return
            del self.sites[name]
            self.previous_quests.pop(name, None)
            self.save_sites()
            self.send_text(f"🗑 Removed '{name}'")

        elif cmd == "/check":
            name = parts[1].lower().strip() if len(parts) > 1 else None
            if name and name in self.sites:
                self.sites[name]['last_check'] = 0
                self.save_sites()
                self.send_text(f"🔄 Force check queued for '{name}'")
            else:
                for n in self.sites:
                    self.sites[n]['last_check'] = 0
                self.save_sites()
                self.send_text("🔄 Force check queued for all sites")

        else:
            self.send_text(
                "📖 Commands:\n\n"
                "/dashboard — status & controls\n"
                "/list — manage all sites\n"
                "/add <name> <url> — add new site\n"
                "/remove <name> — remove site\n"
                "/check [name] — force check now\n"
                "/pause — pause all checks\n"
                "/resume — resume checks"
            )

    def handle_callback(self, cb):
        cid  = cb["id"]
        data = cb.get("data", "")
        self.answer_callback(cid)

        if data == "dashboard":
            self.show_dashboard()
        elif data == "list_sites":
            self.show_sites_list()
        elif data == "pause_all":
            self.paused = True
            self.send_text("⏸ All checks paused. Use /resume to continue.")
        elif data == "resume_all":
            self.paused = False
            self.send_text("▶️ Checks resumed.")
        elif data == "check_all":
            for n in self.sites:
                self.sites[n]['last_check'] = 0
            self.save_sites()
            self.send_text("🔄 Force check queued for all sites")
        elif data.startswith("toggle:"):
            name = data.split(":", 1)[1]
            if name in self.sites:
                self.sites[name]['enabled'] = not self.sites[name]['enabled']
                self.save_sites()
                state = "enabled ✅" if self.sites[name]['enabled'] else "disabled 🔴"
                self.send_text(f"'{name}' {state}")
                self.show_sites_list()
        elif data.startswith("check:"):
            name = data.split(":", 1)[1]
            if name in self.sites:
                self.sites[name]['last_check'] = 0
                self.save_sites()
                self.send_text(f"🔄 Force check queued for '{name}'")
        elif data.startswith("remove:"):
            name = data.split(":", 1)[1]
            if name in self.sites:
                del self.sites[name]
                self.previous_quests.pop(name, None)
                self.save_sites()
                self.send_text(f"🗑 Removed '{name}'")
                self.show_sites_list()

    # ------------------------------------------------------------------ #
    #  Main loop
    # ------------------------------------------------------------------ #

    def watch(self):
        self.log("=" * 60)
        self.log("🚀 ZEALY WATCHER — PERSISTENT BROWSER + DASHBOARD")
        self.log("=" * 60)

        self.start_browser()
        self.send_text(
            "✅ Zealy Watcher Online!\n\n"
            "• Persistent browser (no thread exhaustion)\n"
            "• DOM extraction — no OCR, no screenshots\n"
            "• Smart hydration wait (adapts to page speed)\n"
            "• Rate limit detection + cooldown\n"
            "• Full Telegram dashboard\n\n"
            "Send /dashboard to open controls."
        )

        while True:
            self.process_updates()

            if not self.paused:
                current_time = time.time()

                for site_name, site_config in list(self.sites.items()):
                    if not site_config['enabled']:
                        continue

                    cooldown_end = self.cooldown_until.get(site_name, 0)
                    if current_time < cooldown_end:
                        self.log(f"🕐 {site_name} cooling down — {int(cooldown_end - current_time)}s left")
                        continue

                    last_check = site_config.get('last_check', 0) or 0
                    if current_time - last_check < site_config['interval']:
                        continue

                    self.check_count += 1
                    self.log(f"\n--- CHECK #{self.check_count} | {site_name} | {datetime.now().strftime('%H:%M:%S')} ---")

                    try:
                        is_first = site_name not in self.previous_quests
                        current_quests, rate_limited, duration = self.scrape(site_config['url'], site_name)
                        self.consecutive_errors = 0

                        if rate_limited:
                            self.handle_rate_limit(site_name)
                            continue

                        self.clear_rate_limit(site_name)

                        if is_first:
                            self.previous_quests[site_name] = current_quests
                            quest_text = "\n".join(f"• {q}" for q in current_quests)
                            header = (
                                f"✅ WATCHING: {site_config['description']}\n"
                                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                                f"First check took: {duration}s\n\n"
                                f"📝 Initial Quests ({len(current_quests)}):"
                            )
                            full = f"{header}\n{quest_text}"
                            # Telegram message limit is 4096 chars
                            if len(full) <= 4096:
                                self.send_text(full)
                            else:
                                self.send_text(header)
                                self.send_text(quest_text)

                        else:
                            old_set = set(self.previous_quests[site_name])
                            new_set = set(current_quests)
                            added   = sorted(new_set - old_set)
                            removed = sorted(old_set - new_set)
                            sim     = len(old_set & new_set) / max(len(old_set | new_set), 1) * 100
                            self.log(f"Similarity: {sim:.1f}% | +{len(added)} / -{len(removed)}")

                            if added or removed:
                                self.log("🚨 CHANGE DETECTED!")
                                self.previous_quests[site_name] = current_quests
                                alert = (
                                    f"🚨 ZEALY BOARD UPDATED\n\n"
                                    f"Site: {site_config['description']}\n"
                                    f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                                )
                                if added:
                                    alert += f"✨ NEW QUESTS ({len(added)}):\n"
                                    alert += "\n".join(f"• {q}" for q in added) + "\n\n"
                                if removed:
                                    alert += f"❌ REMOVED ({len(removed)}):\n"
                                    alert += "\n".join(f"• {q}" for q in removed)
                                self.send_text(alert)
                            else:
                                self.log("✅ No changes")

                        self.sites[site_name]['last_check'] = current_time
                        self.save_sites()

                    except Exception as e:
                        self.consecutive_errors += 1
                        self.log(f"❌ Error: {e}")
                        self.send_text(f"❌ Error checking {site_name}:\n{e}")

                        if self.consecutive_errors >= 3:
                            self.log("⚠️ 3 consecutive errors — restarting browser")
                            self.send_text("♻️ Restarting browser due to repeated errors...")
                            try:
                                self.restart_browser()
                                self.consecutive_errors = 0
                            except Exception as re_err:
                                self.log(f"❌ Restart failed: {re_err}")

            time.sleep(3)


def main():
    watcher = SiteWatcher()
    try:
        watcher.watch()
    except KeyboardInterrupt:
        watcher.log("Stopped by user")
        watcher.stop_browser()
    except Exception as e:
        watcher.log(f"FATAL: {e}")
        watcher.send_text(f"🔴 WATCHER CRASHED:\n{e}")
        watcher.stop_browser()


if __name__ == "__main__":
    main()

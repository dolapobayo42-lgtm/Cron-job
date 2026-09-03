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

# IMPORTANT: Railway's local container disk is wiped on every restart/redeploy.
# If you attach a Railway Volume (Settings → Volumes) and mount it at, say,
# /data, set the env var DATA_DIR=/data so sites/state survive redeploys.
# Without a volume, sites added via /add will be lost on the next restart.
DATA_DIR = os.getenv("DATA_DIR", ".")
SITES_FILE = os.path.join(DATA_DIR, "watched_sites.json")
LOG_FILE = os.path.join(DATA_DIR, "watcher.log")
HAS_PERSISTENT_STORAGE = os.getenv("DATA_DIR") is not None

# Headless Chromium slowly leaks memory across thousands of tab reloads on a
# long-running process. Fully relaunching the browser periodically bounds
# that growth so it can't OOM-kill the container. Default: every hour.
BROWSER_RESTART_SECONDS = int(os.getenv("BROWSER_RESTART_SECONDS", "3600"))

# If true: each check closes its tab immediately after reading, instead of
# keeping tabs open. Bounds peak memory to ~1 tab regardless of site count —
# trades a bit of extra time per check (fresh load vs fast reload) for much
# lower steady-state RAM. Worth trying if you're memory-constrained.
SEQUENTIAL_TABS = os.getenv("SEQUENTIAL_TABS", "false").lower() == "true"

# Start with NO sites — add them manually via /add once the bot is running.
DEFAULT_SITES = {}

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
        self.pages = {}          # persistent tab per site: {site_name: page}
        self.log_file = open(LOG_FILE, 'a')
        self.last_update_id = 0
        self.paused = False
        self.start_time = time.time()
        self.check_count = 0
        self.consecutive_errors = 0
        self.site_errors = {}
        self.browser_started_at = 0

    def log(self, msg):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        self.log_file.write(line + "\n")
        self.log_file.flush()

    def load_sites(self):
        if os.path.exists(SITES_FILE):
            try:
                with open(SITES_FILE, 'r') as f:
                    sites = json.load(f)
                for s in sites.values():
                    s.setdefault('last_check', 0)
                return sites
            except (json.JSONDecodeError, ValueError) as e:
                self.log(f"⚠️ {SITES_FILE} is empty/corrupt ({e}) — resetting to empty site list")
                # fall through to recreate a clean file below
        self.save_sites(dict(DEFAULT_SITES))
        return dict(DEFAULT_SITES)

    def save_sites(self, sites=None):
        if sites is not None:
            self.sites = sites
        with open(SITES_FILE, 'w') as f:
            json.dump(self.sites, f, indent=2)

    # ------------------------------------------------------------------ #
    #  Browser — one persistent instance, one persistent tab per site
    # ------------------------------------------------------------------ #

    def start_browser(self):
        self.log("🚀 Launching browser...")
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)
        self.pages = {}
        self.browser_started_at = time.time()
        self.log("✅ Browser ready")

    def stop_browser(self):
        try:
            if self.browser:    self.browser.close()
            if self.playwright: self.playwright.stop()
        except Exception:
            pass
        self.browser = self.playwright = None
        self.pages = {}

    def restart_browser(self):
        self.log("♻️ Restarting browser...")
        self.stop_browser()
        time.sleep(3)
        self.start_browser()

    def close_page(self, site_name):
        page = self.pages.pop(site_name, None)
        if page:
            try: page.close()
            except Exception: pass

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
        # Close the tab so it does a fresh cold load after cooldown
        self.close_page(site_name)

    def clear_rate_limit(self, site_name):
        if self.rate_limit_hits.get(site_name, 0) > 0:
            self.send_text(f"✅ Rate limit cleared — {site_name} resuming")
        self.rate_limit_hits[site_name] = 0
        self.cooldown_until.pop(site_name, None)

    # ------------------------------------------------------------------ #
    #  Scraper — persistent tabs, reload instead of new page
    # ------------------------------------------------------------------ #

    def dismiss_cookie(self, page):
        for text in COOKIE_BUTTON_TEXTS:
            try:
                btn = page.get_by_text(text, exact=False).first
                if btn.is_visible(timeout=1000):
                    btn.click(timeout=1000)
                    self.log(f"🍪 Dismissed: '{text}'")
                    page.wait_for_timeout(600)
                    return True
            except Exception:
                continue
        return False

    # Common third-party trackers/analytics that add network weight but
    # contribute nothing to the quest content we extract.
    _BLOCKED_DOMAINS = (
        "google-analytics.com", "googletagmanager.com", "doubleclick.net",
        "segment.com", "segment.io", "amplitude.com", "mixpanel.com",
        "intercom.io", "sentry.io", "hotjar.com", "fullstory.com",
        "facebook.net", "connect.facebook.net", "clarity.ms",
    )

    def _route_filter(self, route):
        req = route.request
        if req.resource_type in ("image", "media", "font"):
            return route.abort()
        if any(d in req.url for d in self._BLOCKED_DOMAINS):
            return route.abort()
        return route.continue_()

    def wait_for_quests(self, page):
        try:
            page.wait_for_selector(QUEST_NAME_SELECTOR, timeout=15000)
            return True
        except Exception:
            self.log("⚠️ Quests didn't appear in 15s")
            return False

    def extract_quests(self, page):
        elements = page.query_selector_all(QUEST_NAME_SELECTOR)
        quests, seen = [], set()
        for el in elements:
            text = el.inner_text().strip()
            if text:
                text = re.sub(r'\s+', ' ', html_lib.unescape(text))
                if text not in seen:
                    seen.add(text)
                    quests.append(text)
        return quests

    def scrape(self, url, site_name):
        """
        Returns (quests, rate_limited, duration_seconds, ready).
        `ready` is False when the quest elements never showed up in time —
        callers must treat that as a FAILED read, not "0 quests now".

        SEQUENTIAL_TABS=true (env var): closes the tab after every check
        instead of keeping it open, so peak memory stays ~1 tab's worth no
        matter how many sites are being watched. Costs a bit of extra time
        per check (fresh page load vs a fast reload).
        """
        t0 = time.time()
        is_first_load = site_name not in self.pages

        try:
            if is_first_load:
                self.log("🆕 Cold load (first time)...")
                page = self.browser.new_page(viewport={"width": 1280, "height": 900})
                # We only ever read text — block images/fonts/media plus
                # common third-party trackers/analytics domains. Chromium
                # never has to fetch/render them, and they were also making
                # the "load" event (below) wait longer than necessary.
                page.route("**/*", self._route_filter)
                # domcontentloaded, not "load": wait_for_quests() below does
                # a real selector-based readiness check anyway, so we don't
                # need to also wait for every last background request
                # (trackers, websockets, etc.) to settle first — that was
                # pure added latency on a JS-heavy SPA.
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                self.dismiss_cookie(page)

                if self.is_rate_limited(page):
                    try: page.close()
                    except Exception: pass
                    return [], True, round(time.time() - t0), True

                ready = self.wait_for_quests(page)
                time.sleep(0.5)
                self.pages[site_name] = page
            else:
                # Reload the existing warm tab — much faster, cookie already accepted
                page = self.pages[site_name]
                self.log("🔄 Reloading warm tab...")
                page.reload(wait_until="domcontentloaded", timeout=60000)

                if self.is_rate_limited(page):
                    return [], True, round(time.time() - t0), True

                ready = self.wait_for_quests(page)
                time.sleep(0.5)

            if self.is_rate_limited(page):
                return [], True, round(time.time() - t0), True

            if not ready:
                # One retry: give the SPA a bit more time before giving up.
                self.log("⏳ Retrying wait once before treating as failed read...")
                ready = self.wait_for_quests(page)
                time.sleep(0.5)

            quests = self.extract_quests(page)
            duration = round(time.time() - t0)
            self.log(f"📝 {len(quests)} quests in {duration}s (ready={ready})")

            if SEQUENTIAL_TABS:
                # Free this tab's memory immediately instead of keeping it
                # warm — next check for this site will do a fresh cold load.
                self.close_page(site_name)

            return quests, False, duration, ready

        except Exception as e:
            # Tab is probably dead — close it so next check does a cold load
            self.close_page(site_name)
            raise e

    # ------------------------------------------------------------------ #
    #  Telegram
    # ------------------------------------------------------------------ #

    def send_text(self, message):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": message},
                timeout=15
            )
            self.log("📱 Sent")
        except Exception as e:
            self.log(f"TG error: {e}")

    def send_keyboard(self, message, keyboard):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={
                    "chat_id": CHAT_ID,
                    "text": message,
                    "parse_mode": "HTML",
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
    #  Dashboard
    # ------------------------------------------------------------------ #

    def uptime_str(self):
        secs = int(time.time() - self.start_time)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h:  return f"{h}h {m}m"
        if m:  return f"{m}m {s}s"
        return f"{s}s"

    def show_dashboard(self):
        status = "⏸ PAUSED" if self.paused else "✅ RUNNING"
        lines = [
            "🔍 <b>ZEALY WATCHER</b>", "",
            f"Status: {status}",
            f"Uptime: {self.uptime_str()}",
            f"Total Checks: {self.check_count}",
            f"Active Tabs: {len(self.pages)}/{len(self.sites)}",
            "", "<b>Watched Sites:</b>",
        ]
        for name, cfg in self.sites.items():
            icon  = "✅" if cfg['enabled'] else "🔴"
            count = len(self.previous_quests.get(name, []))
            last  = cfg.get('last_check', 0) or 0
            ago   = f"{int(time.time()-last)}s ago" if last else "never"
            warm  = "🟢 warm" if name in self.pages else "🔵 cold"
            lines.append(f"  {icon} <b>{name}</b> — {count} quests — {ago} — {warm}")

        lines += [
            "", "<b>Commands:</b>",
            "/add &lt;name&gt; &lt;url&gt; — add site",
            "/remove &lt;name&gt; — remove site",
            "/tasks [name] — show current quest list",
            "/check [name] — force check",
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
            self.send_text("No sites watched.\nUse /add <name> <url> to add one.")
            return

        lines = ["<b>📋 Manage Sites</b>", ""]
        keyboard = []
        for name, cfg in self.sites.items():
            icon  = "✅" if cfg['enabled'] else "🔴"
            count = len(self.previous_quests.get(name, []))
            warm  = "🟢" if name in self.pages else "🔵"
            lines.append(f"{icon} <b>{name}</b> {warm} | {count} quests")
            lines.append(f"   {cfg['url']}")
            lines.append("")
            keyboard.append([
                {"text": "🔴 Disable" if cfg['enabled'] else "✅ Enable",
                 "callback_data": f"toggle:{name}"},
                {"text": "🔄 Check now", "callback_data": f"check:{name}"},
                {"text": "🗑 Remove",    "callback_data": f"remove:{name}"},
            ])
        keyboard.append([{"text": "◀️ Dashboard", "callback_data": "dashboard"}])
        self.send_keyboard("\n".join(lines), keyboard)

    # ------------------------------------------------------------------ #
    #  Command & callback handling
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
            self.send_text("⏸ All checks paused. Send /resume to continue.")

        elif cmd == "/resume":
            self.paused = False
            self.send_text("▶️ Checks resumed.")

        elif cmd == "/add":
            if len(parts) < 3:
                self.send_text(
                    "Usage: /add <name> <url>\n\n"
                    "Example:\n/add exolix https://zealy.io/cw/exolix/questboard/sprints"
                )
                return
            name = parts[1].lower().strip()
            url  = parts[2].strip()
            # Catch accidental extra words before the URL
            if not url.startswith("http://") and not url.startswith("https://"):
                self.send_text(
                    f"⚠️ Invalid URL: '{url}'\n\n"
                    f"URL must start with https://\n\n"
                    f"Usage: /add <name> <url>\n"
                    f"Example:\n/add {name} https://zealy.io/cw/{name}/questboard/sprints"
                )
                return
            if name in self.sites:
                self.send_text(f"⚠️ '{name}' already exists. Use /remove {name} first.")
                return
            self.sites[name] = {
                "url": url, "interval": 10, "enabled": True,
                "last_check": 0, "description": name
            }
            self.save_sites()
            self.send_text(f"✅ Added '{name}'\nURL: {url}\nStarts on next cycle.")

        elif cmd == "/tasks":
            name = parts[1].lower().strip() if len(parts) > 1 else None
            targets = [name] if name else list(self.sites.keys())
            if name and name not in self.sites:
                self.send_text(f"⚠️ '{name}' not found. Use /list to see sites.")
                return
            if not targets:
                self.send_text("No sites yet. Use /add <name> <url> first.")
                return
            for n in targets:
                quests = self.previous_quests.get(n)
                if quests is None:
                    self.send_text(f"📝 {n}: no data yet (hasn't completed its first check).")
                    continue
                header = f"📝 <b>{self.sites[n]['description']}</b> — {len(quests)} quests"
                body = "\n".join(f"• {q}" for q in quests) if quests else "(none found)"
                full = f"{header}\n{body}"
                if len(full) <= 4096:
                    self.send_keyboard(full, [])
                else:
                    self.send_keyboard(header, [])
                    self.send_text(body)

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
            self.close_page(name)
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
                "/list — manage sites\n"
                "/add <name> <url> — add site\n"
                "/remove <name> — remove site\n"
                "/tasks [name] — show current quest list\n"
                "/check [name] — force check\n"
                "/pause — pause all\n"
                "/resume — resume"
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
            self.send_text("⏸ Paused. Use /resume to continue.")
        elif data == "resume_all":
            self.paused = False
            self.send_text("▶️ Resumed.")
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
                if not self.sites[name]['enabled']:
                    self.close_page(name)
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
                self.close_page(name)
                self.save_sites()
                self.send_text(f"🗑 Removed '{name}'")
                self.show_sites_list()

    # ------------------------------------------------------------------ #
    #  Main loop
    # ------------------------------------------------------------------ #

    def watch(self):
        self.log("=" * 60)
        self.log("🚀 ZEALY WATCHER — PERSISTENT TABS + DASHBOARD")
        self.log("=" * 60)

        self.start_browser()
        storage_note = (
            "💾 Persistent storage: ON (DATA_DIR set)\n"
            if HAS_PERSISTENT_STORAGE else
            "⚠️ No persistent storage — sites added via /add WILL BE LOST on the next "
            "restart/redeploy. Attach a Railway Volume and set env var DATA_DIR to fix this.\n"
        )
        self.send_text(
            "✅ Zealy Watcher Online!\n\n"
            "• Persistent tabs (reload, not new page each time)\n"
            "• DOM extraction — no OCR, no screenshots\n"
            "• Rate limit detection + cooldown\n"
            "• Full Telegram dashboard\n\n"
            f"{storage_note}\n"
            "No sites yet — use /add <name> <url> to start tracking.\n"
            "Send /dashboard to open controls."
        )

        while True:
            self.process_updates()

            if self.browser and time.time() - self.browser_started_at > BROWSER_RESTART_SECONDS:
                self.log(f"🔁 Scheduled browser restart (running {BROWSER_RESTART_SECONDS//60}min) — bounding memory growth")
                self.restart_browser()

            if not self.paused:
                current_time = time.time()

                for site_name, site_config in list(self.sites.items()):
                    if not site_config['enabled']:
                        continue

                    cooldown_end = self.cooldown_until.get(site_name, 0)
                    if current_time < cooldown_end:
                        self.log(f"🕐 {site_name} cooling — {int(cooldown_end - current_time)}s left")
                        continue

                    last_check = site_config.get('last_check', 0) or 0
                    if current_time - last_check < site_config['interval']:
                        continue

                    self.check_count += 1
                    self.log(f"\n--- CHECK #{self.check_count} | {site_name} | {datetime.now().strftime('%H:%M:%S')} ---")

                    try:
                        is_first = site_name not in self.previous_quests
                        current_quests, rate_limited, duration, ready = self.scrape(site_config['url'], site_name)
                        self.consecutive_errors = 0
                        self.site_errors[site_name] = 0

                        if rate_limited:
                            self.handle_rate_limit(site_name)
                            continue

                        self.clear_rate_limit(site_name)

                        # Guard against false "everything removed" alerts caused by a
                        # page that hadn't finished rendering when we read it.
                        prev_count = len(self.previous_quests.get(site_name, []))
                        suspicious_drop = (
                            not is_first
                            and prev_count > 0
                            and len(current_quests) < prev_count / 2
                        )
                        if not ready or suspicious_drop:
                            reason = "page not ready" if not ready else f"count dropped {prev_count}→{len(current_quests)}"
                            self.log(f"⚠️ Skipping this read as unreliable ({reason}) — not comparing/saving")
                            # Retry sooner instead of waiting the full interval
                            self.sites[site_name]['last_check'] = current_time - site_config['interval'] + 15
                            self.save_sites()
                            continue

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
                            self.log(f"Sim: {sim:.1f}% | +{len(added)} / -{len(removed)}")

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
                        self.site_errors[site_name] = self.site_errors.get(site_name, 0) + 1
                        fails = self.site_errors[site_name]

                        # Crashed/dead tabs already self-heal: scrape() closes the
                        # tab on any exception, so the next check for this site
                        # does a fresh cold load automatically. Only page Telegram
                        # once a site keeps failing even after fresh reloads —
                        # that's a real problem, not routine crash-and-recover.
                        self.log(f"❌ Error ({fails}x in a row): {e}")
                        if fails >= 3:
                            self.send_text(f"❌ {site_name} failed {fails} checks in a row (even after fresh reloads):\n{e}")

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
        watcher.log("Stopped")
        watcher.stop_browser()
    except Exception as e:
        watcher.log(f"FATAL: {e}")
        watcher.send_text(f"🔴 WATCHER CRASHED:\n{e}")
        watcher.stop_browser()


if __name__ == "__main__":
    main()


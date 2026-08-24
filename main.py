import os
import time
import json
from datetime import datetime
from playwright.sync_api import sync_playwright
from PIL import Image
import pytesseract
import requests
import glob
import re

# ====== CONFIG ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SITES_FILE = "watched_sites.json"
SCREENSHOTS_DIR = "screenshots"
LOG_FILE = "watcher.log"

# Only Zealy to watch
DEFAULT_SITES = {
    "zealy": {
        "url": "https://zealy.io/cw/minebit/questboard/sprints","https://zealy.io/cw/exolix/questboard/sprints",
        "interval": 30,
        "enabled": True,
        "last_check": 0,
        "description": "Minebit Zealy Quest Board"
    }
}

# ====================

class SiteWatcher:
    def __init__(self):
        self.sites = self.load_sites()
        self.previous_quests = {}
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        self.log_file = open(LOG_FILE, 'a')
        
    def log(self, message):
        """Log message with timestamp"""
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
    
    def get_screenshot(self, url, site_name):
        """Capture screenshot and return path"""
        try:
            self.log(f"📸 Capturing screenshot for {site_name}...")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 1600})
                page.goto(url, wait_until="networkidle", timeout=60000)
                time.sleep(3)  # Let JS load
                
                screenshot_path = f"{SCREENSHOTS_DIR}/{site_name}_{int(time.time())}.png"
                page.screenshot(path=screenshot_path, full_page=True)
                browser.close()
                self.log(f"✅ Screenshot saved: {screenshot_path}")
                return screenshot_path
        except Exception as e:
            self.log(f"❌ Error capturing screenshot for {site_name}: {e}")
            self.send_telegram_text(f"❌ Screenshot Error for {site_name}:\n{str(e)}")
            return None
    
    def extract_quests(self, image_path):
        """Extract quest text from screenshot using OCR"""
        try:
            self.log(f"🔍 Extracting text from screenshot using OCR...")
            img = Image.open(image_path)
            
            # Convert to RGB if needed
            if img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Use Tesseract to extract text
            text = pytesseract.image_to_string(img)
            
            # Extract quest titles (lines with specific patterns)
            lines = text.split('\n')
            quests = []
            for line in lines:
                line = line.strip()
                # Filter for quest-like content (non-empty, reasonable length)
                if line and len(line) > 5 and len(line) < 200:
                    quests.append(line)
            
            quests = list(set(quests))  # Remove duplicates
            quests.sort()
            
            self.log(f"📝 Extracted {len(quests)} text blocks from screenshot")
            return quests
        except Exception as e:
            self.log(f"⚠️  OCR Error: {e}")
            return []
    
    def compare_quests(self, old_quests, new_quests):
        """Compare quest lists and return differences"""
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
        """Send photo to Telegram"""
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            with open(photo_path, "rb") as photo:
                requests.post(url, data={"chat_id": CHAT_ID, "caption": caption}, files={"photo": photo}, timeout=15)
            self.log(f"📱 Sent photo to Telegram")
        except Exception as e:
            self.log(f"Error sending photo: {e}")
    
    def send_telegram_text(self, message):
        """Send text message to Telegram"""
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, data={"chat_id": CHAT_ID, "text": message}, timeout=15)
            self.log(f"📱 Sent text message to Telegram")
        except Exception as e:
            self.log(f"Error sending message: {e}")
    
    def show_dashboard(self):
        """Display dashboard with inline buttons"""
        message = f"""
🔍 **ZEALY WATCHER DASHBOARD**

📊 Status: ✅ ACTIVE

Site: Minebit Zealy Quest Board
Check Interval: 30 seconds
Last Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

🟢 MONITORING ENABLED
- Tracking quest changes
- Text extraction active
- Real-time alerts enabled

Alerts will show:
✨ New quests added
❌ Quests removed
📝 Full text of changes
        """
        self.send_telegram_text(message)
    
    def get_latest_screenshot(self, site_name):
        """Get the most recent screenshot for a site"""
        screenshots = glob.glob(f"{SCREENSHOTS_DIR}/{site_name}_*.png")
        if screenshots:
            latest = max(screenshots, key=os.path.getctime)
            self.log(f"Found previous screenshot: {latest}")
            return latest
        self.log(f"No previous screenshot for {site_name}")
        return None
    
    def watch(self):
        """Main watch loop"""
        self.log("=" * 60)
        self.log("🚀 STARTING ZEALY WATCHER WITH OCR TEXT DETECTION")
        self.log("=" * 60)
        
        self.send_telegram_text("✅ Zealy Watcher Started!\n\nMonitoring for quest changes with text extraction.\nYou will receive alerts when quests are added or removed.")
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
                    self.log(f"⏭️  {site_name} is disabled, skipping")
                    continue
                
                last_check = site_config.get('last_check', 0) or 0
                time_since_check = current_time - last_check
                
                if time_since_check < site_config['interval']:
                    self.log(f"⏱️  {site_name}: Wait {site_config['interval'] - int(time_since_check)}s (interval: {site_config['interval']}s)")
                    continue
                
                self.log(f"\n🔄 CHECKING {site_name.upper()}")
                self.log(f"Description: {site_config['description']}")
                
                try:
                    screenshot_path = self.get_screenshot(site_config['url'], site_name)
                    if not screenshot_path:
                        self.log(f"❌ Failed to capture screenshot")
                        continue
                    
                    # Extract quest text using OCR
                    current_quests = self.extract_quests(screenshot_path)
                    
                    if not current_quests:
                        self.log(f"⚠️  No text extracted from screenshot")
                    
                    # First check - store and send
                    if site_name not in self.previous_quests:
                        self.log(f"📌 FIRST TIME CAPTURE for {site_name}")
                        self.previous_quests[site_name] = current_quests
                        
                        quest_text = "\n".join([f"• {q}" for q in current_quests[:10]])
                        if len(current_quests) > 10:
                            quest_text += f"\n... and {len(current_quests) - 10} more"
                        
                        caption = f"""✅ ZEALY WATCHER STARTED

Site: {site_config['description']}
Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

📝 Initial Content Captured:
{quest_text}

Total items found: {len(current_quests)}"""
                        
                        self.send_telegram(screenshot_path, caption)
                        self.log(f"✅ Initial snapshot sent with {len(current_quests)} items")
                    
                    # Compare with previous
                    else:
                        self.log(f"Comparing with previous capture...")
                        comparison = self.compare_quests(self.previous_quests[site_name], current_quests)
                        
                        self.log(f"📊 Similarity: {comparison['similarity']:.1f}%")
                        self.log(f"✨ Items added: {len(comparison['added'])}")
                        self.log(f"❌ Items removed: {len(comparison['removed'])}")
                        
                        # If changes detected
                        if comparison['added'] or comparison['removed']:
                            self.log(f"🚨 REAL CHANGE DETECTED!")
                            self.previous_quests[site_name] = current_quests
                            
                            alert_text = f"""🚨 ZEALY BOARD UPDATED

Site: {site_config['description']}
Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Similarity: {comparison['similarity']:.1f}%

"""
                            
                            if comparison['added']:
                                alert_text += f"✨ NEW ITEMS ADDED ({len(comparison['added'])}):\n"
                                for item in comparison['added'][:5]:
                                    alert_text += f"• {item}\n"
                                if len(comparison['added']) > 5:
                                    alert_text += f"... and {len(comparison['added']) - 5} more\n"
                                alert_text += "\n"
                            
                            if comparison['removed']:
                                alert_text += f"❌ ITEMS REMOVED ({len(comparison['removed'])}):\n"
                                for item in comparison['removed'][:5]:
                                    alert_text += f"• {item}\n"
                                if len(comparison['removed']) > 5:
                                    alert_text += f"... and {len(comparison['removed']) - 5} more\n"
                            
                            self.send_telegram(screenshot_path, alert_text)
                            self.log(f"✅ Alert sent to Telegram")
                        else:
                            self.log(f"✅ No changes detected (content matches)")
                    
                    # Update last check time
                    self.sites[site_name]['last_check'] = current_time
                    self.save_sites(self.sites)
                    self.log(f"✅ Saved check time for {site_name}")
                    
                except Exception as e:
                    self.log(f"❌ Error checking {site_name}: {e}")
                    self.send_telegram_text(f"❌ Error checking {site_name}:\n{str(e)}")
            
            self.log(f"\n⏸️  Sleeping 10 seconds before next check cycle...")
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

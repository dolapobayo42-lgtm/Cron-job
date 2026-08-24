import os
import time
import json
from datetime import datetime
from playwright.sync_api import sync_playwright
from PIL import Image
import requests
import glob

# ====== CONFIG ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SITES_FILE = "watched_sites.json"
SCREENSHOTS_DIR = "screenshots"
LOG_FILE = "watcher.log"

# Default sites to watch
DEFAULT_SITES = {
    "zealy": {
        "url": "https://zealy.io/cw/minebit/questboard/sprints",
        "interval": 45,
        "enabled": True,
        "last_check": 0,
        "description": "Minebit Zealy Quest Board"
    },
    "espn": {
        "url": "https://www.espn.com",
        "interval": 300,
        "enabled": True,
        "last_check": 0,
        "description": "ESPN Sports News"
    }
}

# ====================

class SiteWatcher:
    def __init__(self):
        self.sites = self.load_sites()
        self.hashes = {}
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
                # Ensure all sites have last_check as a number
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
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(url, wait_until="networkidle", timeout=60000)
                time.sleep(2)  # Let JS load
                
                screenshot_path = f"{SCREENSHOTS_DIR}/{site_name}_{int(time.time())}.png"
                page.screenshot(path=screenshot_path, full_page=True)
                browser.close()
                self.log(f"✅ Screenshot saved: {screenshot_path}")
                return screenshot_path
        except Exception as e:
            self.log(f"❌ Error capturing screenshot for {site_name}: {e}")
            self.send_telegram_text(f"❌ Screenshot Error for {site_name}:\n{str(e)}")
            return None
    
    def get_image_hash(self, image_path):
        """Get perceptual hash of image (ignores minor changes)"""
        try:
            img = Image.open(image_path)
            img = img.resize((8, 8))
            img = img.convert('L')
            # Use get_flattened_data instead of deprecated getdata()
            pixels = list(img.tobytes())
            
            avg = sum(pixels) / len(pixels)
            hash_bits = ''.join(['1' if p > avg else '0' for p in pixels])
            return hash_bits
        except Exception as e:
            self.log(f"Error hashing image: {e}")
            return None
    
    def compare_images(self, old_path, new_path):
        """Compare two images and return similarity percentage"""
        try:
            old_img = Image.open(old_path).convert('RGB')
            new_img = Image.open(new_path).convert('RGB')
            
            # Resize to same dimensions
            new_img = new_img.resize(old_img.size)
            
            # Use tobytes() instead of deprecated getdata()
            old_bytes = old_img.tobytes()
            new_bytes = new_img.tobytes()
            
            differences = sum(1 for a, b in zip(old_bytes, new_bytes) if a != b)
            total = len(old_bytes)
            similarity = 100 - (differences / total * 100)
            
            self.log(f"📊 Image comparison: {similarity:.1f}% similar")
            return similarity
        except Exception as e:
            self.log(f"Error comparing images: {e}")
            return 100
    
    def send_telegram(self, photo_path, caption="Update"):
        """Send photo to Telegram"""
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            with open(photo_path, "rb") as photo:
                requests.post(url, data={"chat_id": CHAT_ID, "caption": caption}, files={"photo": photo}, timeout=10)
            self.log(f"📱 Sent photo to Telegram: {caption[:30]}...")
        except Exception as e:
            self.log(f"Error sending photo: {e}")
    
    def send_telegram_text(self, message):
        """Send text message to Telegram"""
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, data={"chat_id": CHAT_ID, "text": message}, timeout=10)
            self.log(f"📱 Sent message to Telegram: {message[:50]}...")
        except Exception as e:
            self.log(f"Error sending message: {e}")
    
    def show_dashboard(self):
        """Display dashboard"""
        status_lines = []
        for name, config in self.sites.items():
            status = "✅ ENABLED" if config['enabled'] else "❌ DISABLED"
            status_lines.append(f"{name.upper()}: {status}\nInterval: {config['interval']}s\nDesc: {config['description']}")
        
        message = f"""
🔍 **WATCHER DASHBOARD**

{chr(10).join(status_lines)}

CONTROLS:
- Site must update to change
- Similarity < 85% triggers alert
- Real changes only, no spam!
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
        self.log("🚀 STARTING MULTI-SITE WATCHER")
        self.log("=" * 60)
        
        self.send_telegram_text("✅ Zealy Watcher Started!\n\nMonitoring sites for real changes only. Dashboard sent above.")
        self.show_dashboard()
        
        check_count = 0
        
        while True:
            current_time = time.time()
            check_count += 1
            
            self.log(f"\n--- CHECK #{check_count} at {datetime.now().strftime('%H:%M:%S')} ---")
            
            for site_name, site_config in self.sites.items():
                if not site_config['enabled']:
                    self.log(f"⏭️  {site_name} is disabled, skipping")
                    continue
                
                last_check = site_config.get('last_check', 0) or 0
                time_since_check = current_time - last_check
                
                if time_since_check < site_config['interval']:
                    self.log(f"⏱️  {site_name}: Wait {site_config['interval'] - int(time_since_check)}s (interval: {site_config['interval']}s)")
                    continue
                
                self.log(f"\n🔄 CHECKING {site_name.upper()} ({site_config['description']})")
                
                try:
                    screenshot_path = self.get_screenshot(site_config['url'], site_name)
                    if not screenshot_path:
                        self.log(f"❌ Failed to capture screenshot")
                        continue
                    
                    # Get hash
                    current_hash = self.get_image_hash(screenshot_path)
                    if not current_hash:
                        self.log(f"❌ Failed to hash image")
                        continue
                    
                    # First check - store and send
                    if site_name not in self.hashes:
                        self.log(f"📌 FIRST TIME CAPTURE for {site_name}")
                        self.hashes[site_name] = current_hash
                        self.send_telegram(screenshot_path, f"✅ {site_name.upper()}\n{site_config['description']}\n\nInitial Snapshot - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                        self.log(f"✅ Initial snapshot sent")
                    
                    # Compare with previous
                    else:
                        self.log(f"Comparing with previous snapshot...")
                        if current_hash != self.hashes[site_name]:
                            self.log(f"⚠️  Hash changed! Verifying with image comparison...")
                            
                            prev_screenshot = self.get_latest_screenshot(site_name)
                            if prev_screenshot:
                                similarity = self.compare_images(prev_screenshot, screenshot_path)
                                self.log(f"Similarity score: {similarity:.1f}%")
                                
                                # If similarity < 85%, it's a real change
                                if similarity < 85:
                                    self.hashes[site_name] = current_hash
                                    caption = f"🚨 {site_name.upper()} - CHANGE DETECTED!\n{site_config['description']}\n\nSimilarity: {similarity:.1f}%\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                                    self.send_telegram(screenshot_path, caption)
                                    self.log(f"✅ REAL CHANGE DETECTED! Sent alert.")
                                else:
                                    self.log(f"✅ Minor change detected (similarity: {similarity:.1f}%) - IGNORED (< 85% threshold)")
                            else:
                                self.log(f"No previous screenshot, sending as update")
                                self.send_telegram(screenshot_path, f"📊 {site_name.upper()} - Update\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                        else:
                            self.log(f"✅ No change detected (hash matches)")
                    
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

import os
import time
import hashlib
import json
from datetime import datetime
from playwright.sync_api import sync_playwright
from PIL import Image
import requests
from io import BytesIO

# ====== CONFIG ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SITES_FILE = "watched_sites.json"
SCREENSHOTS_DIR = "screenshots"

# Default sites to watch
DEFAULT_SITES = {
    "zealy": {
        "url": "https://zealy.io/cw/minebit/questboard/sprints",
        "interval": 45,
        "enabled": True,
        "last_check": 0
    },
    "espn": {
        "url": "https://www.espn.com",
        "interval": 300,
        "enabled": True,
        "last_check": 0
    },
    "bbc": {
        "url": "https://www.bbc.com/news",
        "interval": 600,
        "enabled": True,
        "last_check": 0
    }
}

# ====================

class SiteWatcher:
    def __init__(self):
        self.sites = self.load_sites()
        self.hashes = {}
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        
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
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(url, wait_until="networkidle", timeout=60000)
                time.sleep(2)  # Let JS load
                
                screenshot_path = f"{SCREENSHOTS_DIR}/{site_name}_{int(time.time())}.png"
                page.screenshot(path=screenshot_path, full_page=True)
                browser.close()
                return screenshot_path
        except Exception as e:
            print(f"Error capturing screenshot for {site_name}: {e}")
            self.send_telegram_text(f"❌ Screenshot Error for {site_name}:\n{str(e)}")
            return None
    
    def get_image_hash(self, image_path):
        """Get perceptual hash of image (ignores minor changes)"""
        try:
            img = Image.open(image_path)
            # Resize to small size for comparison
            img = img.resize((8, 8))
            img = img.convert('L')
            pixels = list(img.getdata())
            
            # Simple perceptual hash
            avg = sum(pixels) / len(pixels)
            hash_bits = ''.join(['1' if p > avg else '0' for p in pixels])
            return hash_bits
        except:
            return hashlib.md5(open(image_path, 'rb').read()).hexdigest()
    
    def compare_images(self, old_path, new_path):
        """Compare two images and return similarity percentage"""
        try:
            old_img = Image.open(old_path).convert('RGB')
            new_img = Image.open(new_path).convert('RGB')
            
            # Resize to same dimensions
            new_img = new_img.resize(old_img.size)
            
            old_pixels = list(old_img.getdata())
            new_pixels = list(new_img.getdata())
            
            differences = sum(1 for a, b in zip(old_pixels, new_pixels) if a != b)
            similarity = 100 - (differences / len(old_pixels) * 100)
            
            return similarity
        except:
            return 100
    
    def send_telegram(self, photo_path, caption="Update"):
        """Send photo to Telegram"""
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            with open(photo_path, "rb") as photo:
                requests.post(url, data={"chat_id": CHAT_ID, "caption": caption}, files={"photo": photo}, timeout=10)
        except Exception as e:
            print(f"Error sending photo: {e}")
    
    def send_telegram_text(self, message):
        """Send text message to Telegram"""
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, data={"chat_id": CHAT_ID, "text": message}, timeout=10)
        except Exception as e:
            print(f"Error sending message: {e}")
    
    def show_dashboard(self):
        """Display dashboard with keyboard"""
        sites_list = "\n".join([f"{'✅' if v['enabled'] else '❌'} {k.upper()} - {v['interval']}s" for k, v in self.sites.items()])
        
        message = f"""
📊 **ZEALY WATCHER DASHBOARD**

{sites_list}

/add - Add new site
/remove - Remove site
/interval - Change check interval
/enable - Enable site
/disable - Disable site
/status - Show detailed status
        """
        self.send_telegram_text(message)
    
    def watch(self):
        """Main watch loop"""
        print("Starting multi-site watcher...")
        self.send_telegram_text("✅ Zealy Watcher Started! Use /dashboard for controls")
        self.show_dashboard()
        
        while True:
            current_time = time.time()
            
            for site_name, site_config in self.sites.items():
                if not site_config['enabled']:
                    continue
                
                last_check = site_config.get('last_check', 0) or 0
                if current_time - last_check < site_config['interval']:
                    continue
                
                print(f"Checking {site_name}...")
                
                try:
                    screenshot_path = self.get_screenshot(site_config['url'], site_name)
                    if not screenshot_path:
                        continue
                    
                    # Get hash
                    current_hash = self.get_image_hash(screenshot_path)
                    
                    # First check - store and send
                    if site_name not in self.hashes:
                        self.hashes[site_name] = current_hash
                        self.send_telegram(screenshot_path, f"✅ {site_name.upper()} - Initial Snapshot Captured")
                        print(f"First screenshot for {site_name} taken")
                    
                    # Compare with previous
                    elif current_hash != self.hashes[site_name]:
                        # Verify it's not just noise - get more detailed comparison
                        prev_screenshot = self.get_latest_screenshot(site_name)
                        if prev_screenshot:
                            similarity = self.compare_images(prev_screenshot, screenshot_path)
                            
                            # If similarity < 85%, it's a real change
                            if similarity < 85:
                                self.hashes[site_name] = current_hash
                                caption = f"🚨 {site_name.upper()} - CHANGE DETECTED!\nSimilarity: {similarity:.1f}%\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                                self.send_telegram(screenshot_path, caption)
                                print(f"Change detected on {site_name}!")
                            else:
                                print(f"Minor change on {site_name} (similarity: {similarity:.1f}%) - ignoring")
                        else:
                            self.send_telegram(screenshot_path, f"📊 {site_name.upper()} - Update Detected!")
                    else:
                        print(f"No change on {site_name}")
                    
                    # Update last check time
                    self.sites[site_name]['last_check'] = current_time
                    self.save_sites(self.sites)
                    
                except Exception as e:
                    print(f"Error checking {site_name}: {e}")
                    self.send_telegram_text(f"❌ Error checking {site_name}:\n{str(e)}")
            
            time.sleep(10)  # Check every 10 seconds if any site needs monitoring
    
    def get_latest_screenshot(self, site_name):
        """Get the most recent screenshot for a site"""
        import glob
        screenshots = glob.glob(f"{SCREENSHOTS_DIR}/{site_name}_*.png")
        if screenshots:
            return max(screenshots, key=os.path.getctime)
        return None

def main():
    watcher = SiteWatcher()
    watcher.watch()

if __name__ == "__main__":
    main()

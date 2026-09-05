    def send_text(self, message):
        print(f"[telegram-debug] Attempting to send message...")
        print(f"[telegram-debug] Token present: {bool(TELEGRAM_TOKEN)}")
        print(f"[telegram-debug] Chat ID present: {bool(CHAT_ID)}")
        print(f"[telegram-debug] Token value (first 10 chars): {TELEGRAM_TOKEN[:10] if TELEGRAM_TOKEN else 'EMPTY'}")
        print(f"[telegram-debug] Chat ID value: {CHAT_ID if CHAT_ID else 'EMPTY'}")
        
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": message}
            print(f"[telegram-debug] POST to: {url}")
            print(f"[telegram-debug] Message length: {len(message)} chars")
            
            resp = requests.post(url, json=payload, timeout=15)
            
            print(f"[telegram-debug] Response status: {resp.status_code}")
            print(f"[telegram-debug] Response text: {resp.text}")
            
            if resp.status_code == 200:
                self.log("✅ 📱 Sent")
            else:
                self.log(f"❌ TG error: status {resp.status_code} - {resp.text}")
        except Exception as e:
            self.log(f"❌ TG error: {e}")
            import traceback
            traceback.print_exc()

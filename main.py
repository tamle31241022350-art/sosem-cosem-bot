import os
import json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from playwright.sync_api import sync_playwright

# === CONFIG ===
KEYWORDS = ["SOSEM", "COSEM"]
DRS_URL = "https://drs.ueh.edu.vn/activity"
UEH_EMAIL = os.environ["UEH_EMAIL"]
UEH_PASSWORD = os.environ["UEH_PASSWORD"]

def get_gmail_service():
    token_data = json.loads(os.environ["GMAIL_TOKEN"])
    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
    )
    return build("gmail", "v1", credentials=creds)

def check_email_has_keyword():
    service = get_gmail_service()
    query = " OR ".join([f'subject:{kw}' for kw in KEYWORDS])
    results = service.users().messages().list(
        userId="me", q=query, maxResults=5
    ).execute()
    messages = results.get("messages", [])
    return len(messages) > 0

def register_on_drs():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(DRS_URL)
        page.click("a[href='/Account/LoginStudentUehCallback']")
        page.wait_for_load_state("networkidle")

        page.fill('input[type="email"]', UEH_EMAIL)
        page.click('button:has-text("Tiếp tục")')
        page.wait_for_timeout(1000)
        page.fill('input[type="password"]', UEH_PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        page.goto(DRS_URL)
        page.wait_for_load_state("networkidle")

        for keyword in KEYWORDS:
            activities = page.locator(f"text={keyword}").all()
            for activity in activities:
                try:
                    card = activity.locator("xpath=ancestor::div[contains(@class,'card')]")
                    detail_btn = card.locator("text=Chi tiết")
                    detail_btn.click()
                    page.wait_for_load_state("networkidle")

                    if page.locator("text=Đã đủ số lượng đăng ký").count() == 0:
                        register_btn = page.locator("button:has-text('Đăng ký')")
                        if register_btn.count() > 0:
                            register_btn.click()
                            page.wait_for_timeout(2000)
                            print(f"✅ Đã đăng ký: {keyword}")
                    else:
                        print(f"❌ Full slot: {keyword}")

                    page.go_back()
                    page.wait_for_load_state("networkidle")
                except Exception as e:
                    print(f"Lỗi: {e}")
                    continue

        browser.close()

if __name__ == "__main__":
    if check_email_has_keyword():
        print("📧 Tìm thấy email SOSEM/COSEM → Bắt đầu đăng ký...")
        register_on_drs()
    else:
        print("Không có email mới.")

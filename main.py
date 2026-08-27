"""
SOSEM / COSEM auto-register bot.

Kiến trúc:
  1. Process start SỚM (6:30). Scheduler trễ bao nhiêu cũng không sao.
  2. Ngủ tới T - LEAD_SECONDS  (mặc định 7:28:00).
  3. Login DRS lúc đó -> session còn mới tinh, không bị hết hạn.
  4. Từ T (7:30) poll lại mỗi POLL_SECONDS giây, trong WINDOW_MINUTES phút.
  5. Thấy hoạt động khớp keyword -> đăng ký -> gửi mail báo -> thoát.
"""

import os
import sys
import json
import time
import base64
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- CONFIG

VN = timezone(timedelta(hours=7))

DRS_URL = os.environ.get("DRS_URL", "https://drs.ueh.edu.vn/activity")
LOGIN_URL = os.environ.get(
    "DRS_LOGIN_URL", "https://drs.ueh.edu.vn/Account/LoginStudentUehCallback"
)

KEYWORDS = [k.strip().upper() for k in os.environ.get("KEYWORDS", "SOSEM,COSEM").split(",") if k.strip()]

TARGET_TIME = os.environ.get("TARGET_TIME", "07:30")
LEAD_SECONDS = int(os.environ.get("LEAD_SECONDS", "120"))
WINDOW_MINUTES = int(os.environ.get("WINDOW_MINUTES", "60"))

# Poll thích ứng: dồn sức lúc đầu, giãn ra về sau.
POLL_FAST_SECONDS = float(os.environ.get("POLL_FAST_SECONDS", "2"))
POLL_SLOW_SECONDS = float(os.environ.get("POLL_SLOW_SECONDS", "10"))
FAST_MINUTES = int(os.environ.get("FAST_MINUTES", "10"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

UEH_EMAIL = os.environ["UEH_EMAIL"]
UEH_PASSWORD = os.environ["UEH_PASSWORD"]

# Selector -- ĐỂ NGOÀI CODE để sửa được qua env var, khỏi phải commit lại.
# ⚠️ Mấy giá trị mặc định này lấy từ code cũ, CHƯA AI KIỂM CHỨNG.
SEL_USER = os.environ.get("SEL_USER", "#taikhoan")
SEL_PASS = os.environ.get("SEL_PASS", 'input[type="password"]')
SEL_SUBMIT = os.environ.get("SEL_SUBMIT", "#btnLogin")
SEL_REGISTER = os.environ.get("SEL_REGISTER", "button:has-text('Đăng ký')")
TXT_FULL = os.environ.get("TXT_FULL", "Đã đủ số lượng đăng ký")


def log(msg):
    """In kèm giờ VN. flush=True để log hiện ngay trên tab Actions."""
    print(f"[{datetime.now(VN):%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- TIMING

def compute_target():
    h, m = map(int, TARGET_TIME.split(":"))
    return datetime.now(VN).replace(hour=h, minute=m, second=0, microsecond=0)


def sleep_until(when, label):
    """Ngủ tới mốc `when`. Log 5 phút/lần để biết job còn sống."""
    last_report = 0
    while True:
        remaining = (when - datetime.now(VN)).total_seconds()
        if remaining <= 0:
            return
        if time.time() - last_report > 300:
            log(f"⏳ Còn {remaining/60:.1f} phút tới {label} ({when:%H:%M:%S})")
            last_report = time.time()
        time.sleep(min(20, remaining))


# ---------------------------------------------------------------- GMAIL

def get_gmail_service():
    d = json.loads(os.environ["GMAIL_TOKEN"])
    creds = Credentials(
        token=d["token"],
        refresh_token=d["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=d["client_id"],
        client_secret=d["client_secret"],
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
    return build("gmail", "v1", credentials=creds)


def send_notification(activity_name):
    """Gửi mail báo. Bọc try/except để lỗi mail không làm hỏng phần đăng ký."""
    try:
        msg = MIMEText(
            f"Bot đã đăng ký hoạt động: {activity_name}\n\n"
            f"Kiểm tra lại tại: {DRS_URL}"
        )
        msg["to"] = UEH_EMAIL
        msg["subject"] = f"✅ Đã đăng ký: {activity_name}"
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        get_gmail_service().users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
        log(f"📧 Đã gửi mail báo cho {UEH_EMAIL}")
    except Exception as e:
        log(f"⚠️ Gửi mail thất bại (đăng ký vẫn ok): {e}")


# ---------------------------------------------------------------- DRS

def login(page):
    log("🔑 Đang login DRS...")
    page.goto(LOGIN_URL, wait_until="networkidle", timeout=60_000)

    if page.locator(SEL_USER).count() == 0:
        log(f"ℹ️ Không thấy ô login ({SEL_USER}) — có thể đã có session sẵn.")
        return

    page.fill(SEL_USER, UEH_EMAIL)
    page.fill(SEL_PASS, UEH_PASSWORD)
    page.click(SEL_SUBMIT)
    page.wait_for_load_state("networkidle", timeout=60_000)
    log("✅ Login xong.")


def find_activities(page):
    """
    PHA 1: chỉ THU THẬP href, KHÔNG click.

    Đây là chỗ fix bug cũ: code cũ giữ element handle rồi go_back(),
    làm handle bị detach -> vòng lặp thứ 2 crash. Giờ mình chỉ giữ
    string URL, string thì không bao giờ stale.
    """
    page.goto(DRS_URL, wait_until="networkidle", timeout=60_000)

    # Poll suốt 1 tiếng -> session có thể hết hạn giữa chừng.
    # Nếu bị đá về trang login thì login lại rồi vào lại, đừng để chết âm thầm.
    if page.locator(SEL_USER).count() > 0:
        log("🔄 Session hết hạn, đang login lại...")
        login(page)
        page.goto(DRS_URL, wait_until="networkidle", timeout=60_000)

    found = {}
    for kw in KEYWORDS:
        for link in page.locator(f"a:has-text('{kw}')").all():
            try:
                href = link.get_attribute("href")
                name = link.inner_text().strip()
            except Exception:
                continue
            if href:
                found[page.url.split("/activity")[0] + href if href.startswith("/") else href] = name
    return found


def try_register(page, url, name):
    """PHA 2: mở từng URL đã thu thập được, bằng goto chứ không click."""
    page.goto(url, wait_until="networkidle", timeout=60_000)

    if page.locator(f"text={TXT_FULL}").count() > 0:
        log(f"❌ Full slot: {name}")
        return False

    btn = page.locator(SEL_REGISTER)
    if btn.count() == 0:
        log(f"⏸️ Chưa có nút đăng ký: {name}")
        return False

    if DRY_RUN:
        log(f"🧪 DRY_RUN — thấy nút đăng ký cho '{name}' nhưng KHÔNG bấm.")
        return False

    btn.first.click()
    page.wait_for_timeout(1500)
    log(f"🎉 ĐÃ ĐĂNG KÝ: {name}")
    send_notification(name)
    return True


# ---------------------------------------------------------------- MAIN

def main():
    target = compute_target()
    deadline = target + timedelta(minutes=WINDOW_MINUTES)
    login_at = target - timedelta(seconds=LEAD_SECONDS)

    log(f"🚀 Bot start. Bây giờ là {datetime.now(VN):%H:%M:%S} (giờ VN)")
    log(f"   Giờ mở đăng ký : {target:%H:%M:%S}")
    log(f"   Login lúc      : {login_at:%H:%M:%S}")
    log(f"   Poll tới       : {deadline:%H:%M:%S}  (mỗi {POLL_SECONDS}s)")
    log(f"   DRY_RUN        : {DRY_RUN}")
    log(f"   Keywords       : {', '.join(KEYWORDS)}")

    if datetime.now(VN) >= deadline:
        log("🛑 Job start quá trễ, đã qua cửa sổ đăng ký. Thoát.")
        return 1

    sleep_until(login_at, "giờ login")

    registered = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context().new_page()

        login(page)
        sleep_until(target, "GIỜ MỞ ĐĂNG KÝ")

        fast_until = target + timedelta(minutes=FAST_MINUTES)
        log(f"🔥 Bắt đầu poll. Nhanh ({POLL_FAST_SECONDS}s) tới {fast_until:%H:%M}, "
            f"sau đó chậm ({POLL_SLOW_SECONDS}s) tới {deadline:%H:%M}.")

        rounds = 0
        last_beat = 0
        while datetime.now(VN) < deadline:
            rounds += 1
            try:
                acts = find_activities(page)
                if rounds == 1 and not acts:
                    log("⚠️ Vòng đầu không thấy link nào khớp keyword.")
                    log("   -> Có thể selector sai. Xem phần hướng dẫn DevTools.")
                for url, name in acts.items():
                    if url in registered:
                        continue
                    if try_register(page, url, name):
                        registered.add(url)
            except Exception as e:
                log(f"⚠️ Lỗi vòng {rounds}: {type(e).__name__}: {e}")

            # Nhịp tim 5 phút/lần: biết job còn sống mà không spam log
            if time.time() - last_beat > 300:
                log(f"💓 Vòng {rounds} | đã đăng ký {len(registered)} | "
                    f"còn {(deadline - datetime.now(VN)).total_seconds()/60:.0f} phút")
                last_beat = time.time()

            fast = datetime.now(VN) < fast_until
            time.sleep(POLL_FAST_SECONDS if fast else POLL_SLOW_SECONDS)

        browser.close()

    log(f"🏁 Kết thúc sau {rounds} vòng. Đăng ký được: {len(registered)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

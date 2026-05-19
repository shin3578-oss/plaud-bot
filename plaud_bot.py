# -*- coding: utf-8 -*-
"""
軸MTG Bot - 毎週水曜 21:00 JST (GitHub Actions / Playwright)
1. PLAUDから最新の「軸MTG」ファイルの要約を取得
2. Google Docsに追記
3. PlaywrightでLINE WORKSの「軸」グループに投稿
"""
import json, gzip, requests, os, time
from datetime import datetime

PLAUD_API      = "https://api-apne1.plaud.ai"
PLAUD_TOKEN    = os.environ["PLAUD_TOKEN"]
GOOGLE_DOCS_ID = os.environ["GOOGLE_DOCS_ID"]
GOOGLE_CREDS   = os.environ["GOOGLE_CREDENTIALS_JSON"]
LW_ID          = os.environ["LW_LOGIN_ID"]
LW_PASS        = os.environ["LW_PASSWORD"]
LW_GROUP       = "軸"


# ========================
# PLAUD API
# ========================

def find_latest_jiku_mtg():
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    r = requests.get(
        f"{PLAUD_API}/file/simple/web?skip=0&limit=50&is_trash=0&sort_by=start_time&is_desc=true",
        headers=headers, timeout=30
    )
    r.raise_for_status()
    for f in r.json().get("data_file_list", []):
        title = f.get("filename", "") or f.get("title", "")
        if "軸MTG" in title:
            return f.get("id", ""), title
    return None, None


def get_file_summary(file_id):
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    r = requests.get(f"{PLAUD_API}/file/detail/{file_id}", headers=headers, timeout=30)
    r.raise_for_status()
    for item in r.json().get("data", {}).get("content_list", []):
        if item.get("data_type") == "auto_sum_note":
            r_s3 = requests.get(item["data_link"], timeout=30)
            try:
                return json.loads(gzip.decompress(r_s3.content)).get("ai_content", "")
            except Exception:
                return r_s3.json().get("ai_content", "")
    return ""


def get_share_url(file_id):
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    r = requests.post(
        f"{PLAUD_API}/share/public/get", headers=headers,
        json={"object_id": file_id, "object_type": "file"}, timeout=30
    )
    r.raise_for_status()
    return r.json().get("data", {}).get("share_url", "")


# ========================
# Google Docs
# ========================

def append_to_google_docs(title, summary, share_url):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS),
        scopes=["https://www.googleapis.com/auth/documents"]
    )
    service = build("docs", "v1", credentials=creds)
    today = datetime.now().strftime("%Y-%m-%d")
    sep = "=" * 50
    content = f"\n\n{sep}\n{today}  {title}\n{sep}\n\n{summary}\n\n共有リンク: {share_url}\n"
    doc = service.documents().get(documentId=GOOGLE_DOCS_ID).execute()
    end_index = doc["body"]["content"][-1]["endIndex"] - 1
    service.documents().batchUpdate(
        documentId=GOOGLE_DOCS_ID,
        body={"requests": [{"insertText": {"location": {"index": end_index}, "text": content}}]}
    ).execute()
    print(f"Google Docs更新完了: {len(content)}文字")


# ========================
# LINE WORKS (Playwright)
# ========================

def send_to_lineworks(message):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()
        page.goto("https://talk.worksmobile.com/")
        time.sleep(3)

        # ログイン
        page.wait_for_selector('input[type="text"]', timeout=15000)
        page.fill('input[type="text"]', LW_ID)
        page.click('button:has-text("ログイン")')
        time.sleep(2)
        page.wait_for_selector('input[type="password"]', timeout=15000)
        page.fill('input[type="password"]', LW_PASS)
        page.click('button:has-text("ログイン")')
        page.wait_for_function(
            "!window.location.href.startsWith('https://auth.worksmobile.com')",
            timeout=60000
        )
        time.sleep(3)

        # グループ検索・選択
        search = page.locator('input[placeholder*="検索"]').first
        search.click(timeout=10000)
        search.fill(LW_GROUP)
        time.sleep(2)
        page.locator(f'text="{LW_GROUP}"').first.click(timeout=10000)
        time.sleep(2)

        # メッセージ送信
        editor = page.locator('div[contenteditable="true"]').last
        editor.click(timeout=10000)
        editor.type(message)
        time.sleep(1)
        page.keyboard.press("Enter")
        time.sleep(2)
        browser.close()
    print("LINE WORKS送信完了")


# ========================
# Main
# ========================

def main():
    print(f"軸MTG Bot 開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    file_id, title = find_latest_jiku_mtg()
    if not file_id:
        print("ERROR: 軸MTGファイルが見つかりません")
        return
    print(f"対象: {title}")

    summary = get_file_summary(file_id)
    if not summary:
        print("ERROR: 要約取得失敗")
        return

    share_url = get_share_url(file_id)
    if not share_url:
        print("ERROR: 共有URL取得失敗")
        return

    append_to_google_docs(title, summary, share_url)

    lw_message = f"【軸MTG議事録】\n{title}\n\nPLAUD要約リンク: {share_url}"
    send_to_lineworks(lw_message)

    print(f"完了: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
朝練Bot - 毎日10:00 JST (GitHub Actions / LINE WORKS Bot API)
1. PLAUDから最新の「朝練」ファイルの要約を取得
2. Google Docsに追記
3. LINE WORKS Bot APIで「歯科医師」チャンネルに投稿
"""
import json, gzip, requests, os, time
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

PLAUD_API       = "https://api-apne1.plaud.ai"
PLAUD_TOKEN     = os.environ["PLAUD_TOKEN"]
GOOGLE_DOCS_ID  = os.environ["GOOGLE_DOCS_ID_ASAREN"]
GOOGLE_CREDS    = os.environ["GOOGLE_CREDENTIALS_JSON"]

LW_CLIENT_ID      = "0cAEPO2Yzau80tSsEhxV"
LW_CLIENT_SECRET  = "d7WfxxO2t1"
LW_SERVICE_ACCOUNT = "3w266.serviceaccount@ovalcourtdental"
LW_BOT_ID         = "12266491"
LW_ASAREN_CH      = "6854ad46-6be5-bc50-f6ea-5efa1831062f"
LW_PRIVATE_KEY    = os.environ["LW_PRIVATE_KEY"]


# ========================
# PLAUD API
# ========================

def find_latest_asaren():
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    r = requests.get(
        f"{PLAUD_API}/file/simple/web?skip=0&limit=50&is_trash=0&sort_by=start_time&is_desc=true",
        headers=headers, timeout=30
    )
    r.raise_for_status()
    today = datetime.now(JST).strftime("%Y-%m-%d")
    for f in r.json().get("data_file_list", []):
        title = f.get("filename", "") or f.get("title", "")
        start_time = f.get("start_time", 0)
        file_date = datetime.fromtimestamp(start_time / 1000, tz=JST).strftime("%Y-%m-%d")
        if "朝練" in title and file_date == today:
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


SHARE_CONTENT = {"overview": True, "transcript": True, "notes": True, "audio": False}

def get_share_url(file_id):
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    r = requests.post(
        f"{PLAUD_API}/share/public/get", headers=headers,
        json={"object_id": file_id, "object_type": "file"}, timeout=30
    )
    r.raise_for_status()
    data = r.json().get("data", {})
    share_url = data.get("share_url", "")

    if share_url:
        # コンテンツ設定が無効なら更新する
        cfg = data.get("content_config", {})
        if not cfg.get("overview") or not cfg.get("notes"):
            r_upd = requests.post(
                f"{PLAUD_API}/share/public/update", headers=headers,
                json={"object_id": file_id, "object_type": "file", "content_config": SHARE_CONTENT},
                timeout=30
            )
            print(f"共有設定更新: {r_upd.status_code} {r_upd.text[:200]}")
        return share_url

    # 未発行の場合はAI要約を有効にして作成
    r2 = requests.post(
        f"{PLAUD_API}/share/public/create", headers=headers,
        json={"object_id": file_id, "object_type": "file", "content_config": SHARE_CONTENT},
        timeout=30
    )
    r2.raise_for_status()
    return r2.json().get("data", {}).get("share_url", "")


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
    today = datetime.now(JST).strftime("%Y-%m-%d")
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
# LINE WORKS Bot API
# ========================

def get_lw_access_token():
    import jwt as pyjwt
    now = int(time.time())
    token = pyjwt.encode(
        {"iss": LW_CLIENT_ID, "sub": LW_SERVICE_ACCOUNT, "iat": now, "exp": now + 3600},
        LW_PRIVATE_KEY, algorithm="RS256"
    )
    r = requests.post(
        "https://auth.worksmobile.com/oauth2/v2.0/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": token,
            "client_id": LW_CLIENT_ID,
            "client_secret": LW_CLIENT_SECRET,
            "scope": "bot",
        },
        timeout=30
    )
    r.raise_for_status()
    return r.json()["access_token"]


def send_to_lineworks(message):
    access_token = get_lw_access_token()
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    r = requests.post(
        f"https://www.worksapis.com/v1.0/bots/{LW_BOT_ID}/channels/{LW_ASAREN_CH}/messages",
        headers=headers,
        json={"content": {"type": "text", "text": message}},
        timeout=30
    )
    r.raise_for_status()
    print("LINE WORKS送信完了")


# ========================
# Main
# ========================

def main():
    print(f"朝練Bot 開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")

    file_id, title = find_latest_asaren()
    if not file_id:
        print("ERROR: 朝練ファイルが見つかりません")
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

    lw_message = f"【朝練議事録】\n{title}\n\nPLAUD要約リンク: {share_url}"
    send_to_lineworks(lw_message)

    print(f"完了: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()

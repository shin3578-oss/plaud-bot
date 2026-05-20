# -*- coding: utf-8 -*-
"""最新の軸MTGファイルを日付関係なく投稿するテスト用スクリプト"""
import json, gzip, requests, os, time
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

PLAUD_API      = "https://api-apne1.plaud.ai"
PLAUD_TOKEN    = os.environ["PLAUD_TOKEN"]
GOOGLE_DOCS_ID = os.environ["GOOGLE_DOCS_ID"]
GOOGLE_CREDS   = os.environ["GOOGLE_CREDENTIALS_JSON"]

LW_CLIENT_ID       = "0cAEPO2Yzau80tSsEhxV"
LW_CLIENT_SECRET   = "d7WfxxO2t1"
LW_SERVICE_ACCOUNT = "3w266.serviceaccount@ovalcourtdental"
LW_BOT_ID          = "12266491"
LW_JIKU_CH         = os.environ["LW_JIKU_CH"]
LW_PRIVATE_KEY     = os.environ["LW_PRIVATE_KEY"]

SHARE_CONTENT_BASE = {"overview": True, "transcript": True, "audio": False}


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


def get_file_detail(file_id):
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    r = requests.get(f"{PLAUD_API}/file/detail/{file_id}", headers=headers, timeout=30)
    r.raise_for_status()
    return r.json().get("data", {})


def get_file_summary(detail):
    for item in detail.get("content_list", []):
        if item.get("data_type") == "auto_sum_note":
            r_s3 = requests.get(item["data_link"], timeout=30)
            try:
                return json.loads(gzip.decompress(r_s3.content)).get("ai_content", "")
            except Exception:
                return r_s3.json().get("ai_content", "")
    return ""


def get_note_ids(detail):
    return [str(item["data_id"]) for item in detail.get("content_list", [])
            if item.get("data_type") in ("auto_sum_note", "sum_multi_note")
            and item.get("data_id")]


def get_share_url(file_id, note_ids):
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    content_config = {**SHARE_CONTENT_BASE, "notes": note_ids}

    r = requests.post(
        f"{PLAUD_API}/share/public/get", headers=headers,
        json={"object_id": file_id, "object_type": "file"}, timeout=30
    )
    r.raise_for_status()
    data = r.json().get("data", {})
    share_url = data.get("share_url", "")

    if share_url:
        cfg = data.get("content_config", {})
        if not cfg.get("overview") or not cfg.get("notes"):
            requests.post(
                f"{PLAUD_API}/share/public/update", headers=headers,
                json={"object_id": file_id, "object_type": "file", "content_config": content_config},
                timeout=30
            )
        return share_url

    r2 = requests.post(
        f"{PLAUD_API}/share/public/create", headers=headers,
        json={"object_id": file_id, "object_type": "file", "content_config": content_config},
        timeout=30
    )
    r2.raise_for_status()
    return r2.json().get("data", {}).get("share_url", "")


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
        f"https://www.worksapis.com/v1.0/bots/{LW_BOT_ID}/channels/{LW_JIKU_CH}/messages",
        headers=headers,
        json={"content": {"type": "text", "text": message}},
        timeout=30
    )
    r.raise_for_status()
    print("LINE WORKS送信完了")


def main():
    print(f"軸MTG テスト投稿 開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")

    file_id, title = find_latest_jiku_mtg()
    if not file_id:
        print("ERROR: 軸MTGファイルが見つかりません")
        return
    print(f"対象: {title}")

    detail = get_file_detail(file_id)
    summary = get_file_summary(detail)
    if not summary:
        print("ERROR: 要約取得失敗")
        return

    note_ids = get_note_ids(detail)
    share_url = get_share_url(file_id, note_ids)
    if not share_url:
        print("ERROR: 共有URL取得失敗")
        return

    lw_message = f"【軸MTG議事録】\n{title}\n\nPLAUD要約リンク: {share_url}"
    send_to_lineworks(lw_message)

    print(f"完了: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()

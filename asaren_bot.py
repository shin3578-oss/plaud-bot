# -*- coding: utf-8 -*-
"""
朝練Bot - 毎日10:00 JST (GitHub Actions / LINE WORKS Bot API)
1. PLAUDから最新の「朝練」ファイルの要約を取得
2. Google Docsに追記
3. LINE WORKS Bot APIで「歯科医師」チャンネルに投稿
"""
import json, gzip, requests, os, sys, time, re
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
LW_SHINCHO_ID     = "shin@ovalcourtdental"
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
    today = os.environ.get("TARGET_DATE", "") or datetime.now(JST).strftime("%Y-%m-%d")
    print(f"対象日付: {today}")
    for f in r.json().get("data_file_list", []):
        title = f.get("filename", "") or f.get("title", "")
        start_time = f.get("start_time", 0)
        file_date = datetime.fromtimestamp(start_time / 1000, tz=JST).strftime("%Y-%m-%d")
        if "朝練" in title and file_date == today:
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
            print(f"S3レスポンス: status={r_s3.status_code}, size={len(r_s3.content)}bytes")
            # ① gzip + JSON 形式（旧形式）
            try:
                return json.loads(gzip.decompress(r_s3.content)).get("ai_content", "")
            except Exception:
                pass
            # ② JSON 形式（非圧縮）
            try:
                return r_s3.json().get("ai_content", "")
            except Exception:
                pass
            # ③ プレーンMarkdown形式（新形式）
            try:
                text = r_s3.content.decode('utf-8').strip()
                if text:
                    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
                    text = re.sub(r'##\s*コア[・･]シノプシス.*?(?=##|\Z)', '', text, flags=re.DOTALL)
                    text = re.sub(r'\n{3,}', '\n\n', text).strip()
                    if text:
                        print(f"プレーンMarkdown形式で取得: {len(text)}文字")
                        return text
            except Exception as e:
                print(f"プレーンテキスト取得エラー: {e}")
            print(f"S3内容プレビュー: {r_s3.content[:200]}")
    return ""


def get_note_ids(detail):
    return [str(item["data_id"]) for item in detail.get("content_list", [])
            if item.get("data_type") in ("auto_sum_note", "sum_multi_note")
            and item.get("data_id")]


def get_share_url(file_id, note_ids):
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    content_config = {"overview": True, "transcript": False, "audio": False, "notes": note_ids}

    r = requests.post(
        f"{PLAUD_API}/share/public/get", headers=headers,
        json={"object_id": file_id, "object_type": "file"}, timeout=30
    )
    r.raise_for_status()
    data = r.json().get("data", {})
    share_url = data.get("share_url", "")

    if share_url:
        cfg = data.get("content_config", {})
        if not cfg.get("overview") or not cfg.get("notes") or cfg.get("transcript"):
            r_upd = requests.post(
                f"{PLAUD_API}/share/public/update", headers=headers,
                json={"object_id": file_id, "object_type": "file", "content_config": content_config},
                timeout=30
            )
            print(f"共有設定更新: {r_upd.status_code} {r_upd.text[:200]}")
        return share_url

    r2 = requests.post(
        f"{PLAUD_API}/share/public/create", headers=headers,
        json={"object_id": file_id, "object_type": "file", "content_config": content_config},
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


def send_alert_to_shincho(message):
    """院長へDMでアラートを送る"""
    try:
        access_token = get_lw_access_token()
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        r = requests.post(
            f"https://www.worksapis.com/v1.0/bots/{LW_BOT_ID}/users/{LW_SHINCHO_ID}/messages",
            headers=headers,
            json={"content": {"type": "text", "text": message}},
            timeout=30
        )
        r.raise_for_status()
        print(f"院長へアラート送信完了")
    except Exception as e:
        print(f"アラート送信失敗: {e}")


# ========================
# Main
# ========================

def main():
    print(f"朝練Bot 開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST")

    MAX_ATTEMPTS = 6
    RETRY_INTERVAL = 3600      # 1時間
    ALERT_AFTER_ATTEMPTS = 2   # 2回失敗したら1度だけアラート送信
    today = os.environ.get("TARGET_DATE", "") or datetime.now(JST).strftime("%Y-%m-%d")
    alert_sent = False
    found_but_no_summary = False

    for attempt in range(1, MAX_ATTEMPTS + 1):
        now_jst = datetime.now(JST)
        print(f"\n--- 試行 {attempt}/{MAX_ATTEMPTS}: {now_jst.strftime('%H:%M')} JST ---")

        file_id, title = find_latest_asaren()
        if not file_id:
            print("朝練ファイルが見つかりません")

            # 2回失敗したら1度だけアラートを送る
            if attempt >= ALERT_AFTER_ATTEMPTS and not alert_sent:
                alert = (
                    f"【朝練Bot】{today} の朝練ファイルがPLAUDで見つかりません。\n\n"
                    "朝練がある場合はPLAUDをアップロードしてください。\n"
                    "アップロードされ次第、自動で歯科医師チャンネルに投稿します。\n\n"
                    "本日朝練がない場合はこのメッセージは無視してください。"
                )
                send_alert_to_shincho(alert)
                alert_sent = True
                print("アラート送信済み。引き続き検索を続けます...")

            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_INTERVAL)
            continue

        print(f"対象: {title}")
        detail = get_file_detail(file_id)
        summary = get_file_summary(detail)

        if not summary:
            print("要約がまだ生成されていません")
            found_but_no_summary = True
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_INTERVAL)
            continue

        note_ids = get_note_ids(detail)
        share_url = get_share_url(file_id, note_ids)
        if not share_url:
            # 未投稿のまま「成功」で終わらせない（院長に知らせて失敗扱いにする）
            print("ERROR: 共有URL取得失敗")
            send_alert_to_shincho(f"⚠️【朝練Bot】共有URLの取得に失敗し、投稿できませんでした。\n{title}")
            sys.exit(1)

        append_to_google_docs(title, summary, share_url)
        lw_message = f"【朝練議事録】\n{title}\n\nPLAUD要約リンク: {share_url}"
        send_to_lineworks(lw_message)

        if alert_sent:
            send_alert_to_shincho(f"✅ 朝練議事録を歯科医師チャンネルに投稿しました。\n{title}")

        print(f"完了: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")
        return

    print(f"{MAX_ATTEMPTS}回試みましたが朝練ファイルが見つかりませんでした（本日朝練なしの可能性）")
    if found_but_no_summary:
        # ファイルはあるのに要約が生成されないまま終わった＝投稿漏れ。黙って成功にしない
        send_alert_to_shincho(
            f"⚠️【朝練Bot】{today} の朝練ファイルは見つかりましたが、"
            "要約が生成されず投稿できませんでした。PLAUD側を確認してください。"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
# -*- coding: utf-8 -*-
"""
朝練Bot - 水〜土 9:00頃 JST (GitHub Actions / LINE WORKS Bot API)
  2026-11-27 再開（院長「寺澤先生の朝練が水曜日から土曜日までつづくから」）。
  起動は apotool の sassatopay.yml（月〜土9:00）が水〜土だけ連鎖dispatch（Cron-job.org 12件上限のため）。
0. 休診日なら何もしないで終わる（休診日は朝練をしないため）
1. PLAUDから対象日の「朝練」ファイルの要約を取得
2. Google Docsに追記（見出しの日付は対象日。同じ録音がもう書かれていれば書かない＝二重記録の防止）
3. LINE WORKS Bot APIで「歯科医師」チャンネルに投稿（DOC_ONLY=true のときは投稿しない＝過去分の書き足し用）
NO_NOTIFY=true のときは院長DM・チャンネルへ何も送らない（文面はログへ）
"""
import json, gzip, requests, os, sys, time, re
from sheets_retry import requests_session  # 一時エラー(503/タイムアウト)を自動で再試行（2026-09-12）
HTTP = requests_session()
from datetime import datetime, timezone, timedelta

from clinic_calendar import closed_reason

JST = timezone(timedelta(hours=9))

PLAUD_API       = "https://api-apne1.plaud.ai"
PLAUD_TOKEN     = os.environ["PLAUD_TOKEN"]
GOOGLE_DOCS_ID  = os.environ["GOOGLE_DOCS_ID_ASAREN"]
GOOGLE_CREDS    = os.environ["GOOGLE_CREDENTIALS_JSON"]

LW_CLIENT_ID      = "0cAEPO2Yzau80tSsEhxV"
LW_CLIENT_SECRET  = os.environ["LW_CLIENT_SECRET"]   # 2026-10-05 直書きをやめてSecretから
LW_SERVICE_ACCOUNT = "3w266.serviceaccount@ovalcourtdental"
LW_BOT_ID         = "12266491"
# 休診日スキップの1行だけは完了通知Bot（要対応の既存Botに混ぜない）
LW_SKIP_BOT_ID    = "12786833"
LW_FAIL_BOT_ID    = "12789558"  # 失敗通知Bot（2026-09-23: 失敗DMをここへ分けた）
LW_ASAREN_CH      = "6854ad46-6be5-bc50-f6ea-5efa1831062f"
LW_SHINCHO_ID     = "shin@ovalcourtdental"
LW_PRIVATE_KEY    = os.environ["LW_PRIVATE_KEY"]

NO_NOTIFY = os.environ.get("NO_NOTIFY", "").lower() == "true"   # 試し実行：誰にも送らない
DOC_ONLY  = os.environ.get("DOC_ONLY", "").lower() == "true"    # 過去分の書き足し：ドキュメントだけ


# ========================
# PLAUD API
# ========================

def find_latest_asaren():
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    today = os.environ.get("TARGET_DATE", "") or datetime.now(JST).strftime("%Y-%m-%d")
    print(f"対象日付: {today}")
    # 新しい順に50件ずつ。対象日より古い録音まで来たら打ち切る（過去分の書き足しで50件より奥を探すため・2026-11-27）
    for skip in range(0, 1000, 50):
        r = HTTP.get(
            f"{PLAUD_API}/file/simple/web?skip={skip}&limit=50&is_trash=0&sort_by=start_time&is_desc=true",
            headers=headers, timeout=30
        )
        r.raise_for_status()
        files = r.json().get("data_file_list", [])
        if not files:
            break
        for f in files:
            title = f.get("filename", "") or f.get("title", "")
            start_time = f.get("start_time", 0)
            file_date = datetime.fromtimestamp(start_time / 1000, tz=JST).strftime("%Y-%m-%d")
            if "朝練" in title and file_date == today:
                return f.get("id", ""), title
        stamps = [f.get("start_time") for f in files if f.get("start_time")]   # 時刻が欠けた録音は判定に使わない
        if stamps:
            last_date = datetime.fromtimestamp(min(stamps) / 1000, tz=JST).strftime("%Y-%m-%d")
            if last_date < today:
                break
    else:
        print("WARN: 1000件まで探しても対象日に届きませんでした")
    return None, None


def get_file_detail(file_id):
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    r = HTTP.get(f"{PLAUD_API}/file/detail/{file_id}", headers=headers, timeout=30)
    r.raise_for_status()
    return r.json().get("data", {})


def get_file_summary(detail):
    for item in detail.get("content_list", []):
        if item.get("data_type") == "auto_sum_note":
            r_s3 = HTTP.get(item["data_link"], timeout=30)
            # 2026-09-23: 非200（署名URL期限切れの403 XML など）でも3段フォールバックを素通りし、
            # エラー本文が「要約」として返っていた。ここで止める。
            r_s3.raise_for_status()
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

    r = HTTP.post(
        f"{PLAUD_API}/share/public/get", headers=headers,
        json={"object_id": file_id, "object_type": "file"}, timeout=30
    )
    r.raise_for_status()
    data = r.json().get("data", {})
    share_url = data.get("share_url", "")

    if share_url:
        cfg = data.get("content_config", {})
        if not cfg.get("overview") or not cfg.get("notes") or cfg.get("transcript"):
            r_upd = HTTP.post(
                f"{PLAUD_API}/share/public/update", headers=headers,
                json={"object_id": file_id, "object_type": "file", "content_config": content_config},
                timeout=30
            )
            print(f"共有設定更新: {r_upd.status_code}")  # 本文はリンクを含むので公開ログに出さない
        return share_url

    r2 = HTTP.post(
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
    # 見出しの日付は「実行した日」ではなく「朝練の日（対象日）」（過去分を書き足しても日付がずれないように・2026-11-27）
    target = os.environ.get("TARGET_DATE", "") or datetime.now(JST).strftime("%Y-%m-%d")
    sep = "=" * 50
    content = f"\n\n{sep}\n{target}  {title}\n{sep}\n\n{summary}\n\n共有リンク: {share_url}\n"
    doc = service.documents().get(documentId=GOOGLE_DOCS_ID).execute(num_retries=3)
    # 同じ録音がもう書かれていれば書かない＝再実行・書き足しで二重に記録しない
    body_text = "".join(
        r.get("textRun", {}).get("content", "")
        for el in doc["body"]["content"] for r in el.get("paragraph", {}).get("elements", [])
    )
    if f"{target}  {title}" in body_text or (share_url and share_url in body_text):
        print(f"Google Docsにはもう記録済み → 書かない: {target}  {title}")
        return False
    if NO_NOTIFY:
        # 試し実行ではドキュメントにも書かない（先に書くと本番の回が「記録済み」で投稿しなくなるため）
        print(f"[NO_NOTIFY] ドキュメントには書かない: {target}  {title}（{len(content)}文字）")
        return True
    end_index = doc["body"]["content"][-1]["endIndex"] - 1
    service.documents().batchUpdate(
        documentId=GOOGLE_DOCS_ID,
        body={"requests": [{"insertText": {"location": {"index": end_index}, "text": content}}]}
    ).execute(num_retries=3)
    print(f"Google Docs更新完了: {len(content)}文字")
    return True


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
    r = HTTP.post(
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
    if NO_NOTIFY or DOC_ONLY:
        print(f"[送信しない NO_NOTIFY={NO_NOTIFY} DOC_ONLY={DOC_ONLY}] 歯科医師CHへ: {message.splitlines()[0]}…（リンクは公開ログに出さない）")
        return
    access_token = get_lw_access_token()
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    r = HTTP.post(
        f"https://www.worksapis.com/v1.0/bots/{LW_BOT_ID}/channels/{LW_ASAREN_CH}/messages",
        headers=headers,
        json={"content": {"type": "text", "text": message}},
        timeout=30
    )
    r.raise_for_status()
    print("LINE WORKS送信完了")


def send_skip_notice(message):
    """休診日スキップの1行を院長DMへ送る（完了通知Bot＝要対応の既存Botと分ける）。

    黙って終わると「止まったのか休診なのか」が区別できないため、1行だけ知らせる
    （院長指示 2026-08-12）。送信に失敗しても本体の判断は変えない。
    """
    if NO_NOTIFY:
        print(f"[NO_NOTIFY] 院長DMへ: {message.splitlines()[0]}…")
        return
    try:
        access_token = get_lw_access_token()
        r = HTTP.post(
            f"https://www.worksapis.com/v1.0/bots/{LW_SKIP_BOT_ID}/users/{LW_SHINCHO_ID}/messages",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"content": {"type": "text", "text": message}},
            timeout=30
        )
        r.raise_for_status()
        print(f"院長DMへ送信しました: {message.splitlines()[0]}")
    except Exception as e:
        print(f"院長DMへの送信に失敗（本体の判断は変えずに続行）: {e}")


def send_alert_to_shincho(message, bot_id=None):
    """院長へDMでアラートを送る"""
    if NO_NOTIFY:
        print(f"[NO_NOTIFY] 院長DMへ: {message.splitlines()[0]}…")
        return
    try:
        access_token = get_lw_access_token()
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        r = HTTP.post(
            f"https://www.worksapis.com/v1.0/bots/{bot_id or LW_BOT_ID}/users/{LW_SHINCHO_ID}/messages",
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

    # ── 休診日は朝練をしないので、探しにいかない ──
    # 2026-08-12（お盆の水曜・休診）に「朝練ファイルが見つかりません」を院長DMへ
    # 誤送信した。祝日判定では お盆・年末年始・臨時休診 を拾えないため、
    # アポツールの rest-all を書き出した公開カレンダーで判定する。
    # TARGET_DATE を手で指定したとき（過去分の取り直し）は院長の意図なのでガードしない。
    if not os.environ.get("TARGET_DATE"):
        reason = closed_reason()
        if reason:
            print(f"本日は{reason} → 朝練はないため何もしません")
            send_skip_notice(f"【朝練Bot】本日は{reason}のためスキップしました")
            return

    # 2026-11-27 起動が10:00→9:00頃に早まったので回数を1つ増やした。
    # GitHub のクラウド実行は1ジョブ6時間（360分）で打ち切られるので、待ちの合計を 55分×6＝330分に収める
    # （9:05頃・10:00・10:55・11:50・12:45・13:40・14:35頃に探す）
    MAX_ATTEMPTS = 7
    RETRY_INTERVAL = 3300      # 55分
    ALERT_AFTER_ATTEMPTS = 3   # 3回目（10:55頃）まで見つからなければ1度だけアラート送信
    if DOC_ONLY or NO_NOTIFY:  # 過去分の書き足し・試し実行は待たない
        MAX_ATTEMPTS = 1
    # 対象日は最初に決めて固定する（再試行中に日付をまたぐと翌日を探し始めてしまうため）
    today = os.environ.get("TARGET_DATE", "") or datetime.now(JST).strftime("%Y-%m-%d")
    os.environ["TARGET_DATE"] = today
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
            send_alert_to_shincho(f"⚠️【朝練Bot】共有URLの取得に失敗し、投稿できませんでした。\n{title}",
                                  bot_id=LW_FAIL_BOT_ID)
            sys.exit(1)

        written = append_to_google_docs(title, summary, share_url)
        if not written:
            # もう記録済み＝同じ日の2回目の起動。チャンネルへも二重に流さない
            print("記録済みのため歯科医師CHへの投稿もしません")
            return
        lw_message = f"【朝練議事録】\n{title}\n\nPLAUD要約リンク: {share_url}"
        try:
            send_to_lineworks(lw_message)
        except Exception as e:
            # ドキュメントには書けたのでもう一度動かしても「記録済み」で投稿しない。院長へリンクごと知らせる
            print(f"ERROR: 歯科医師CHへの投稿に失敗: {type(e).__name__}")
            send_alert_to_shincho(
                "⚠️【朝練Bot】ドキュメントには記録しましたが、歯科医師チャンネルへの投稿に失敗しました。"
                "もう一度動かしても投稿されないので、下の文をチャンネルに貼ってください。\n\n" + lw_message,
                bot_id=LW_FAIL_BOT_ID)
            sys.exit(1)

        if alert_sent:
            send_alert_to_shincho(f"✅ 朝練議事録を歯科医師チャンネルに投稿しました。\n{title}")

        print(f"完了: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")
        return

    print(f"{MAX_ATTEMPTS}回試みましたが朝練ファイルが見つかりませんでした（本日朝練なしの可能性）")
    if found_but_no_summary:
        # ファイルはあるのに要約が生成されないまま終わった＝投稿漏れ。黙って成功にしない
        send_alert_to_shincho(
            f"⚠️【朝練Bot】{today} の朝練ファイルは見つかりましたが、"
            "要約が生成されず投稿できませんでした。PLAUD側を確認してください。",
            bot_id=LW_FAIL_BOT_ID
        )
        sys.exit(1)
    if DOC_ONLY:
        # 手で日付を指定した書き足しで見つからない＝指定の誤りか取りこぼし。緑で終わらせない
        print(f"ERROR: {today} の朝練録音が見つかりませんでした（書き足しは行っていません）")
        sys.exit(1)


if __name__ == "__main__":
    main()
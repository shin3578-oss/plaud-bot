# -*- coding: utf-8 -*-
"""
軸MTG Bot - 毎週 水・木・金・土 21:00 JST (GitHub Actions / LINE WORKS Bot API)
1. PLAUDから今週（月曜〜当日）の「軸MTG」録音のうち、まだ投稿していないものの要約を取得
2. Google Docsに追記（録音IDを一緒に書く＝次の夜の重複防止に使う）
3. LINE WORKS Bot APIで「軸」チャンネルに投稿

【水〜土の4晩にした理由（院長指示 2026-09-24）】
PLAUDへのアップロードが水曜のうちに済まないことが多い。以前は水曜の当日分しか探さず、
木曜以降に上がった録音は永久に拾われなかった。今は土曜の夜まで毎晩探し、
上がっていれば投稿する。投稿済みかどうかは Google Docs に残した録音ID（旧分は共有URL）で判定する。
"""
import json, gzip, requests, os, sys, time, re
from sheets_retry import requests_session  # 一時エラー(503/タイムアウト)を自動で再試行（2026-09-12）
HTTP = requests_session()
from datetime import datetime, timezone, timedelta

from clinic_calendar import closed_reason

JST = timezone(timedelta(hours=9))

PLAUD_API      = "https://api-apne1.plaud.ai"
PLAUD_TOKEN    = os.environ["PLAUD_TOKEN"]
GOOGLE_DOCS_ID = os.environ["GOOGLE_DOCS_ID"]
GOOGLE_CREDS   = os.environ["GOOGLE_CREDENTIALS_JSON"]

LW_CLIENT_ID       = "0cAEPO2Yzau80tSsEhxV"
LW_CLIENT_SECRET   = "d7WfxxO2t1"
LW_SERVICE_ACCOUNT = "3w266.serviceaccount@ovalcourtdental"
LW_BOT_ID          = "12266491"
# 休診日スキップの1行だけは完了通知Bot（要対応の既存Botに混ぜない）
LW_SKIP_BOT_ID     = "12786833"
LW_FAIL_BOT_ID    = "12789558"  # 失敗通知Bot（2026-09-23: 失敗DMをここへ分けた）
LW_JIKU_CH         = os.environ["LW_JIKU_CH"]
LW_PRIVATE_KEY     = os.environ["LW_PRIVATE_KEY"]
LW_SHINCHO_ID      = "shin@ovalcourtdental"
# 検証用：Docs追記・チャンネル投稿・院長DMの成功報告をしない（探す・判定するところまで）
DRY_RUN            = os.environ.get("JIKU_DRY_RUN", "") == "1"


# ========================
# PLAUD API
# ========================

def find_jiku_mtgs(start, end):
    """録音日が start〜end（date・両端含む）の「軸MTG」録音を古い順に返す。[(id, title, 録音日)]"""
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    print(f"対象期間: {start} 〜 {end}")
    found, in_range = [], []
    skip, limit = 0, 50
    while True:
        r = HTTP.get(
            f"{PLAUD_API}/file/simple/web?skip={skip}&limit={limit}&is_trash=0&sort_by=start_time&is_desc=true",
            headers=headers, timeout=30
        )
        r.raise_for_status()
        files = r.json().get("data_file_list", [])
        reached_older = False
        for f in files:
            title = f.get("filename", "") or f.get("title", "")
            file_date = datetime.fromtimestamp(f.get("start_time", 0) / 1000, tz=JST).date()
            if file_date < start:
                reached_older = True
                break
            if file_date > end:
                continue
            in_range.append(title)
            if "軸MTG" in title:
                found.append((f.get("id", ""), title, file_date))
        # 新しい順に並んでいるので、期間より古い録音が出たらそこで打ち切る
        if reached_older or len(files) < limit or skip >= 500:
            break
        skip += limit
    if not found:
        # 見つからなかった理由を残す（録音はあるのにタイトルが違う、が過去に起きている）
        print(f"  期間内の録音{len(in_range)}件: " + (" / ".join(f'「{t}」' for t in in_range) or "なし"))
    return sorted(found, key=lambda x: x[2])


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
                    # 画像リンクを除去 (![...](...)
                    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
                    # コア・シノプシス（他録音との合体要約）セクションを除去
                    text = re.sub(r'##\s*コア[・･]シノプシス.*?(?=##|\Z)', '', text, flags=re.DOTALL)
                    # 余分な空行を整理
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


def peek_share_url(file_id):
    """既にある共有URLを返す（無ければ空）。作らない。旧形式（録音ID無し）の投稿済み判定に使う"""
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    r = HTTP.post(
        f"{PLAUD_API}/share/public/get", headers=headers,
        json={"object_id": file_id, "object_type": "file"}, timeout=30
    )
    r.raise_for_status()
    return (r.json().get("data") or {}).get("share_url", "")


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
            HTTP.post(
                f"{PLAUD_API}/share/public/update", headers=headers,
                json={"object_id": file_id, "object_type": "file", "content_config": content_config},
                timeout=30
            )
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

def docs_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS),
        scopes=["https://www.googleapis.com/auth/documents"]
    )
    return build("docs", "v1", credentials=creds)


def read_docs_text():
    """議事録Docsの本文をまるごと文字列で返す（投稿済み判定用）"""
    doc = docs_service().documents().get(documentId=GOOGLE_DOCS_ID).execute(num_retries=3)
    out = []
    for block in doc.get("body", {}).get("content", []):
        for el in block.get("paragraph", {}).get("elements", []):
            out.append(el.get("textRun", {}).get("content", ""))
    return "".join(out)


def append_to_google_docs(title, summary, share_url, file_id="", rec_date=None):
    service = docs_service()
    today = datetime.now(JST).strftime("%Y-%m-%d")
    sep = "=" * 50
    rec = f"（録音日 {rec_date}）" if rec_date and str(rec_date) != today else ""
    content = (f"\n\n{sep}\n{today}  {title}{rec}\n{sep}\n\n{summary}\n\n共有リンク: {share_url}\n"
               + (f"録音ID: {file_id}\n" if file_id else ""))
    doc = service.documents().get(documentId=GOOGLE_DOCS_ID).execute(num_retries=3)
    end_index = doc["body"]["content"][-1]["endIndex"] - 1
    service.documents().batchUpdate(
        documentId=GOOGLE_DOCS_ID,
        body={"requests": [{"insertText": {"location": {"index": end_index}, "text": content}}]}
    ).execute(num_retries=3)
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
    access_token = get_lw_access_token()
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    r = HTTP.post(
        f"https://www.worksapis.com/v1.0/bots/{LW_BOT_ID}/channels/{LW_JIKU_CH}/messages",
        headers=headers,
        json={"content": {"type": "text", "text": message}},
        timeout=30
    )
    r.raise_for_status()
    print("LINE WORKS送信完了")


def send_dm_to_shincho(message, bot_id=None):
    """院長へDMを送る。失敗したら例外を投げる（握りつぶさない）"""
    access_token = get_lw_access_token()
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    r = HTTP.post(
        f"https://www.worksapis.com/v1.0/bots/{bot_id or LW_BOT_ID}/users/{LW_SHINCHO_ID}/messages",
        headers=headers,
        json={"content": {"type": "text", "text": message}},
        timeout=30
    )
    r.raise_for_status()
    print("院長へDM送信完了")


def send_alert_to_shincho(message, bot_id=None):
    """院長へDMでアラートを送る（アラート自体の失敗でBotを落とさない）"""
    if DRY_RUN:
        print(f"[dry_run] 院長DM（送らない）: {message}")
        return
    try:
        send_dm_to_shincho(message, bot_id=bot_id)
    except Exception as e:
        print(f"アラート送信失敗: {e}")


def send_skip_notice(message):
    """スキップ・投稿済み・遅れて投稿した等の1行を院長DMへ送る（完了通知Bot＝要対応の既存Botと分ける）。

    黙って終わると「止まったのか休診なのか」が区別できないため、1行だけ知らせる
    （院長指示 2026-08-12）。送信に失敗しても本体の判断は変えない。
    """
    if DRY_RUN:
        print(f"[dry_run] 院長DM（送らない）: {message}")
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
        print("休診日スキップの1行を院長DMへ送信しました")
    except Exception as e:
        print(f"休診日スキップ通知の送信に失敗（本体はスキップのまま続行）: {e}")


# ========================
# Main
# ========================

def main():
    print(f"軸MTG Bot 開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}（dry_run={DRY_RUN}）")

    # 対象日は最初に決めて固定する。再試行は日付をまたぐため、
    # 毎回 now() を読み直すと 0時から翌日を探し始めて取りこぼす（2026-08-05に発生）
    manual = bool(os.environ.get("TARGET_DATE"))
    today = (datetime.strptime(os.environ["TARGET_DATE"], "%Y-%m-%d").date() if manual
             else datetime.now(JST).date())
    wd = today.weekday()   # 月=0 … 水=2 … 土=5

    if manual:
        # 取り直し：指定日の録音だけを、投稿済みかどうかに関わらず投稿する（従来どおり）
        start = end = today
        mtg_day_closed = None
    else:
        # 今週の月曜〜今日。軸MTGの定例は水曜
        start, end = today - timedelta(days=wd), today
        # 休診の判定は「実行した日」ではなく「軸MTGを開くはずだった水曜」で見る
        mtg_day_closed = closed_reason(start + timedelta(days=2))

    MAX_ATTEMPTS         = 6 if (manual or wd == 2) else 3   # 水曜は深夜まで、木〜土は23時まで
    RETRY_INTERVAL       = 3600
    ALERT_AFTER_ATTEMPTS = 2
    alert_sent = False
    waiting = False        # 録音はあるが要約がまだ＝次の試行を待っている
    posted = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"\n--- 試行 {attempt}/{MAX_ATTEMPTS}: {datetime.now(JST).strftime('%H:%M')} JST ---")

        candidates = find_jiku_mtgs(start, end)
        if manual:
            candidates = [c for c in candidates if c[1] not in {t for t, _ in posted}]
        elif candidates:
            docs_text = read_docs_text()
            todo = []
            for fid, title, rec_date in candidates:
                done = fid in docs_text
                if not done:
                    old_url = peek_share_url(fid)
                    done = bool(old_url) and old_url in docs_text
                if done:
                    print(f"  投稿済み: {title}（{rec_date}）")
                else:
                    todo.append((fid, title, rec_date))
            if not todo and not posted:
                print("今週の軸MTGはすべて投稿済みです")
                send_skip_notice(f"【軸MTGBot】今週の軸MTGは投稿済みです（{len(candidates)}件）")
                return
            candidates = todo

        if not candidates:
            if posted:
                waiting = False
                break
            print("軸MTGファイルが見つかりません")
            if mtg_day_closed:
                print(f"今週の水曜は{mtg_day_closed} → 軸MTGはないため何もしません")
                send_skip_notice(f"【軸MTGBot】今週の水曜は{mtg_day_closed}のため、軸MTGはありません")
                return

            if wd == 2 and attempt >= ALERT_AFTER_ATTEMPTS and not alert_sent:
                send_alert_to_shincho(
                    f"【軸MTGBot】{today} の軸MTGファイルがPLAUDで見つかりません。\n\n"
                    "録音がある場合はPLAUDをアップロードしてください。\n"
                    "今夜のうちに上がれば投稿します。間に合わなくても、木・金・土の21時にもう一度探して"
                    "自動で軸チャンネルに投稿します。\n\n"
                    "今週軸MTGがない場合はこのメッセージは無視してください。"
                )
                alert_sent = True
                print("アラート送信済み。引き続き検索を続けます...")

            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_INTERVAL)
            continue

        waiting = False
        for fid, title, rec_date in candidates:
            print(f"対象: {title}（録音日 {rec_date}）")
            detail = get_file_detail(fid)
            summary = get_file_summary(detail)
            if not summary:
                print("  要約がまだ生成されていません")
                waiting = True
                continue

            share_url = get_share_url(fid, get_note_ids(detail))
            if not share_url:
                # 未投稿のまま「成功」で終わらせない（院長に知らせて失敗扱いにする）
                print("ERROR: 共有URL取得失敗")
                send_alert_to_shincho(f"⚠️【軸MTGBot】共有URLの取得に失敗し、投稿できませんでした。\n{title}",
                                      bot_id=LW_FAIL_BOT_ID)
                sys.exit(1)

            if DRY_RUN:
                print(f"  [dry_run] Docs追記・軸チャンネル投稿はしません: {share_url}")
            else:
                append_to_google_docs(title, summary, share_url, file_id=fid, rec_date=rec_date)
                send_to_lineworks(f"【軸MTG議事録】\n{title}\n\nPLAUD要約リンク: {share_url}")
            posted.append((title, rec_date))

        if not waiting:
            break
        if attempt < MAX_ATTEMPTS:
            # 投稿できた分は次の試行で Docs の録音IDにより除外される
            time.sleep(RETRY_INTERVAL)

    if posted:
        # 水曜の定時に投稿できたとき以外（遅れて上がった分・アラート後・取り直し）は院長にも知らせる
        if alert_sent or wd != 2 or manual:
            msg = ("✅ 軸MTG議事録を軸チャンネルに投稿しました。\n"
                   + "\n".join(f"{t}（録音日 {d}）" for t, d in posted))
            if DRY_RUN:
                print(f"[dry_run] 院長DM（送らない）: {msg}")
            else:
                send_skip_notice(msg)
        print(f"完了: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")

    if waiting:
        # 録音はあるのに要約が生成されないまま終わった＝投稿漏れ。黙って成功にしない
        send_alert_to_shincho(
            f"⚠️【軸MTGBot】{today} 軸MTGの録音は見つかりましたが、要約が生成されず投稿できませんでした。"
            "PLAUD側を確認してください（要約ができれば次の晩に自動で投稿します）。",
            bot_id=LW_FAIL_BOT_ID
        )
        sys.exit(1)
    if posted:
        return

    print(f"{MAX_ATTEMPTS}回試みましたが軸MTGファイルが見つかりませんでした")
    if manual:
        return
    if wd == 5:
        # 土曜が最後の晩。ここで見つからなければ今週分は自動では拾わない
        send_alert_to_shincho(
            f"【軸MTGBot】今週（{start}〜{end}）の軸MTGの録音は、土曜の夜まで探しましたが見つかりませんでした。\n"
            "録音がある場合は、PLAUDへアップロードしてタイトルに「軸MTG」を入れてください"
            "（上がったあとはAIに「軸MTGを取り直して」と言えば投稿します）。\n"
            "今週軸MTGがなかった場合は無視してください。"
        )
    elif not alert_sent:   # 水曜はアラートで「木〜土も探す」と伝え済み
        send_skip_notice("【軸MTGBot】今週の軸MTGの録音はまだ上がっていません。明日の21時にもう一度探します")


if __name__ == "__main__":
    main()

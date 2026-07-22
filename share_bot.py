# -*- coding: utf-8 -*-
"""
PLAUD配信Bot - 録音タイトルをトリガーに、PLAUD要約の共有リンクをLINEワークスへ自動配信
  - 「面談（○○）」→ ○○さんの個人DMへ自動配信（STAFF_DMで名前→アカウントIDを解決）
      例: 面談（桑野）→桑野碧さんDM／面談（斉藤）→斉藤さんDM／面談（山本）→山本さんDM
      名字だけの「桑野」は桑野碧さん。桑野莉緒さんはフルネーム「面談（桑野莉緒）」で書き分け
      STAFF_DMに無い名前は配信せず院長DMへアラート（名前を追加するまで毎日通知）
  - 「事務MTG」    → 事務トークルーム（木曜。LW_JIMU_CH未設定の間はスキップ）
  - 朝練Bot・軸MTGBotと同じ方式（PLAUD要約リンクを投稿。文字起こし・音声は非表示）

運用:
  - 毎日21時の連鎖（mendan_sheets.yml）に相乗りして月〜土に実行
  - 配信済み管理は面談記録スプシの「配信ログ」タブ（録音IDで重複防止）
  - 初回実行（配信ログタブが無い時）は既存録音を配信済み扱いで登録し、送信しない
  - 録音はあるが要約未生成 → 院長DMに知らせて翌日再試行
  - 木曜に事務MTG・金曜に面談（桑野）の録音が無い → 院長DMにアラート
  - SHARE_MODE=test: 全メッセージを院長DMのみに送る（配信ログは更新しない）
"""
import os, json, gzip, re, time, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

JST = timezone(timedelta(hours=9))
PLAUD_API = "https://api-apne1.plaud.ai"
SHEET_ID = os.environ.get("MENDAN_SHEET_ID", "11Zct4Knwz6ItPB1dmFIKz0Yx-ZEAbeZVp5LvWLx6D7A")
DAYS_BACK = int(os.environ.get("SHARE_DAYS_BACK", "7"))
SHARE_MODE = os.environ.get("SHARE_MODE", "")  # "" / "test"
LOG_TAB = "配信ログ"
LOG_HEADER = ["録音ID", "配信日時", "録音タイトル", "配信先", "状態"]

# ---- PLAUDトークン（ローカルはplaud_storage.json、Actionsは環境変数） ----
_token_file = Path(r"C:\Users\shin3\Desktop\AI\plaud_storage.json")
if _token_file.exists():
    PLAUD_TOKEN = json.loads(_token_file.read_text(encoding="utf-8")).get("pld_tokenstr", "").strip('"')
else:
    PLAUD_TOKEN = os.environ.get("PLAUD_TOKEN", "").strip('"')

# LINE WORKS（朝練Bot・軸MTGBotと共通の値）
LW_CLIENT_ID = "0cAEPO2Yzau80tSsEhxV"
LW_CLIENT_SECRET = os.environ.get("LW_CLIENT_SECRET", "d7WfxxO2t1")
LW_SERVICE_ACCOUNT = "3w266.serviceaccount@ovalcourtdental"
LW_BOT_ID = "12266491"
LW_SHINCHO_ID = "shin@ovalcourtdental"
LW_PRIVATE_KEY = os.environ.get("LW_PRIVATE_KEY", "")
LW_JIMU_CH = os.environ.get("LW_JIMU_CH", "")  # 事務トークルームID（Bot招待後に設定）

# ==============================
# 配信ルート定義
#   タイトル先頭「面談（○○）」→ ○○さんの個人DM（STAFF_DMで解決）
#   タイトル先頭「事務MTG」   → 事務トークルーム
# ==============================
JIMU_ROUTE = {"key": "jimu_mtg", "match": "事務MTG",
              "dest": ("channel", LW_JIMU_CH),
              "dest_label": "事務ルーム", "header": "【事務MTG議事録】"}

# 面談（○○）の○○ → (表示ラベル, LINEワークスアカウントID)
# 名字だけの「桑野」は桑野碧さん（DH軸・金曜面談の慣例）。桑野莉緒さんはフルネームで書き分け
STAFF_DM = {
    "桑野":     ("桑野さんDM",     "ov.46600@ovalcourtdental"),
    "桑野碧":   ("桑野碧さんDM",   "ov.46600@ovalcourtdental"),
    "桑野莉緒": ("桑野莉緒さんDM", "ov.66543@ovalcourtdental"),
    "斉藤":     ("斉藤さんDM",     "ov.20438@ovalcourtdental"),
    "斎藤":     ("斉藤さんDM",     "ov.20438@ovalcourtdental"),
    "齋藤":     ("斉藤さんDM",     "ov.20438@ovalcourtdental"),
    "篠宮":     ("シノDM",         "asukasama23@ovalcourtdental"),
    "シノ":     ("シノDM",         "asukasama23@ovalcourtdental"),
    "田口":     ("田口さんDM",     "ov.82954@ovalcourtdental"),
    "内藤":     ("内藤さんDM",     "ov.28224@ovalcourtdental"),
    "若澤":     ("若澤さんDM",     "ov.42352@ovalcourtdental"),
    "荊木":     ("荊木さんDM",     "ov.26804@ovalcourtdental"),
    "山本":     ("山本さんDM",     "ov.63563@ovalcourtdental"),
    "重野":     ("重野さんDM",     "ov.02757@ovalcourtdental"),
    "小西":     ("小西さんDM",     "ov.04694@ovalcourtdental"),
    "森":       ("森さんDM",       "ov.38270@ovalcourtdental"),
    "石川":     ("石川さんDM",     "ishikawa.mari@ovalcourtdental"),
    "村田":     ("村田さんDM",     "129847711@ovalcourtdental"),
    "竹内":     ("竹内さんDM",     "ov.04678@ovalcourtdental"),
    "秋野":     ("秋野先生DM",     "ov.99552@ovalcourtdental"),
    "渡邉":     ("渡邉先生DM",     "ov.23431@ovalcourtdental"),
    "渡辺":     ("渡邉先生DM",     "ov.23431@ovalcourtdental"),
    "納冨":     ("納冨先生DM",     "ov.47745@ovalcourtdental"),
    "濱":       ("濱先生DM",       "hamayoshihiro@ovalcourtdental"),
    "浜":       ("濱先生DM",       "hamayoshihiro@ovalcourtdental"),
}

# この曜日(月=0)に録音が無ければ院長へアラート（斉藤さんは不定期なので対象外）
EXPECTED_RECORDINGS = [
    {"weekday": 4, "prefix": "面談（桑野", "display": "面談（桑野）", "label": "桑野さんDM"},   # 金曜
    {"weekday": 3, "prefix": "事務MTG",   "display": "事務MTG",      "label": "事務ルーム"},   # 木曜
]

WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def norm(title):
    return title.replace("(", "（").replace(")", "）")


def resolve_route(title):
    """タイトルから配信ルートを決める。対象外はNone。面談だが宛先不明はunknown_name付きで返す"""
    t = norm(title)
    if t.startswith(JIMU_ROUTE["match"]):
        return JIMU_ROUTE
    m = re.match(r"^面談（\s*([^）]+?)\s*）", t)
    if not m:
        return None
    name = m.group(1)
    if name in STAFF_DM:
        label, account = STAFF_DM[name]
        return {"key": f"mendan_{name}", "match": f"面談（{name}）",
                "dest": ("user", account), "dest_label": label, "header": "【面談議事録】"}
    return {"key": "mendan_unknown", "unknown_name": name}


# ==============================
# PLAUD
# ==============================
def find_target_files():
    """過去DAYS_BACK日の録音から、ルートに一致するものを古い順で返す"""
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    r = requests.get(
        f"{PLAUD_API}/file/simple/web?skip=0&limit=200&is_trash=0&sort_by=start_time&is_desc=true",
        headers=headers, timeout=30,
    )
    r.raise_for_status()
    cutoff = datetime.now(JST) - timedelta(days=DAYS_BACK)
    results = []
    for f in r.json().get("data_file_list", []):
        title = f.get("filename", "") or f.get("title", "")
        file_dt = datetime.fromtimestamp(f.get("start_time", 0) / 1000, tz=JST)
        if file_dt < cutoff:
            continue
        route = resolve_route(title)
        if route:
            results.append({"id": f.get("id", ""), "title": title,
                            "date": file_dt.strftime("%Y-%m-%d"), "route": route})
    return sorted(results, key=lambda x: x["date"])


def get_file_detail(file_id):
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    r = requests.get(f"{PLAUD_API}/file/detail/{file_id}", headers=headers, timeout=30)
    r.raise_for_status()
    return r.json().get("data", {})


def has_summary(detail):
    """PLAUD要約（auto_sum_note）が生成済みか"""
    for item in detail.get("content_list", []):
        if item.get("data_type") == "auto_sum_note":
            return True
    return False


def get_note_ids(detail):
    return [str(i["data_id"]) for i in detail.get("content_list", [])
            if i.get("data_type") in ("auto_sum_note", "sum_multi_note") and i.get("data_id")]


def get_share_url(file_id, note_ids):
    """共有リンクを取得（無ければ作成）。文字起こし・音声は常に非表示"""
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    cfg = {"overview": True, "transcript": False, "audio": False, "notes": note_ids}
    r = requests.post(f"{PLAUD_API}/share/public/get", headers=headers,
                      json={"object_id": file_id, "object_type": "file"}, timeout=30)
    r.raise_for_status()
    data = r.json().get("data", {})
    url = data.get("share_url", "")
    if url:
        c = data.get("content_config", {})
        if not c.get("overview") or not c.get("notes") or c.get("transcript"):
            requests.post(f"{PLAUD_API}/share/public/update", headers=headers,
                          json={"object_id": file_id, "object_type": "file", "content_config": cfg}, timeout=30)
        return url
    r2 = requests.post(f"{PLAUD_API}/share/public/create", headers=headers,
                       json={"object_id": file_id, "object_type": "file", "content_config": cfg}, timeout=30)
    r2.raise_for_status()
    return r2.json().get("data", {}).get("share_url", "")


# ==============================
# Google Sheets（配信ログ）
# ==============================
def get_sheets_service():
    from googleapiclient.discovery import build
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        from google.oauth2 import service_account
        info = json.loads(creds_json.lstrip("﻿"))
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        token_path = os.environ.get("DRIVE_TOKEN_PATH", r"C:\Users\shin3\Desktop\AI\drive_token.json")
        creds = Credentials.from_authorized_user_file(token_path, scopes)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
    return build("sheets", "v4", credentials=creds)


def ensure_log_tab(service):
    """配信ログタブを用意。新規作成した場合はTrueを返す（=初回実行）"""
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    titles = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    if LOG_TAB in titles:
        return False
    resp = service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{"addSheet": {"properties": {
            "title": LOG_TAB, "gridProperties": {"columnCount": 5, "frozenRowCount": 1}}}}]},
    ).execute()
    sid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID, range=f"{LOG_TAB}!A1:E1",
        valueInputOption="RAW", body={"values": [LOG_HEADER]},
    ).execute()
    widths = {0: 150, 1: 130, 2: 420, 3: 110, 4: 130}
    reqs = [
        {"repeatCell": {"range": {"sheetId": sid},
                        "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                        "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                                       "backgroundColor": {"red": 0.85, "green": 0.9, "blue": 0.95}}},
                        "fields": "userEnteredFormat(textFormat,backgroundColor)"}},
    ]
    for col, px in widths.items():
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
            "properties": {"pixelSize": px}, "fields": "pixelSize"}})
    service.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests": reqs}).execute()
    return True


def get_sent_ids(service):
    resp = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"{LOG_TAB}!A2:A").execute()
    return {row[0].strip() for row in resp.get("values", []) if row and row[0].strip()}


def append_log(service, rows):
    if not rows:
        return
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID, range=f"{LOG_TAB}!A1",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


# ==============================
# LINE WORKS
# ==============================
def get_lw_access_token():
    import jwt as pyjwt
    now = int(time.time())
    assertion = pyjwt.encode(
        {"iss": LW_CLIENT_ID, "sub": LW_SERVICE_ACCOUNT, "iat": now, "exp": now + 3600},
        LW_PRIVATE_KEY, algorithm="RS256")
    r = requests.post("https://auth.worksmobile.com/oauth2/v2.0/token",
                      data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                            "assertion": assertion, "client_id": LW_CLIENT_ID,
                            "client_secret": LW_CLIENT_SECRET, "scope": "bot"},
                      timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


_lw_token_cache = {}


def send_lw(dest, message):
    """dest=("user", id) or ("channel", id)。SHARE_MODE=testなら院長DMへ振り替え"""
    if SHARE_MODE == "test":
        message = f"（テスト配信・本来の宛先: {dest[0]}:{dest[1] or '未設定'}）\n\n{message}"
        dest = ("user", LW_SHINCHO_ID)
    if not _lw_token_cache.get("token"):
        _lw_token_cache["token"] = get_lw_access_token()
    headers = {"Authorization": f"Bearer {_lw_token_cache['token']}", "Content-Type": "application/json"}
    kind = "users" if dest[0] == "user" else "channels"
    r = requests.post(f"https://www.worksapis.com/v1.0/bots/{LW_BOT_ID}/{kind}/{dest[1]}/messages",
                      headers=headers, json={"content": {"type": "text", "text": message}}, timeout=30)
    r.raise_for_status()


def notify_shincho(message):
    try:
        send_lw(("user", LW_SHINCHO_ID), message)
        print("  院長DM通知完了")
    except Exception as e:
        print(f"  院長DM通知エラー: {e}")


# ==============================
# Main
# ==============================
def main():
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")
    weekday = now.weekday()
    print(f"PLAUD配信Bot 開始: {now.strftime('%Y-%m-%d %H:%M:%S')} JST（{WEEKDAY_JA[weekday]}曜・mode={SHARE_MODE or '本番'}）")
    if not PLAUD_TOKEN:
        print("❌ PLAUD_TOKEN未設定")
        sys.exit(1)

    service = get_sheets_service()
    if SHARE_MODE == "test":
        # テストは配信ログを読み書きせず、直近の対象を全て院長DMへプレビュー送信
        first_run, sent_ids = False, set()
    else:
        created = ensure_log_tab(service)
        sent_ids = set() if created else get_sent_ids(service)
        first_run = created or not sent_ids  # ログが空のうちは常に初期化扱い（テスト実行後でも安全）
    print(f"配信済み録音ID: {len(sent_ids)}件")

    files = find_target_files()
    print(f"対象録音: {len(files)}件（過去{DAYS_BACK}日）")

    # 初回実行: 既存録音は配信済み扱いで登録し、送信しない（過去分の一斉送信を防ぐ）
    if first_run:
        rows = [[f["id"], now.strftime("%Y-%m-%d %H:%M"), f["title"],
                 f["route"].get("dest_label", "宛先不明"), "初期化（送信なし）"] for f in files]
        # マーカー行を必ず入れてログを非空にする（0件初期化でも次回から本番配信になる）
        rows.append(["-", now.strftime("%Y-%m-%d %H:%M"), "（初期化完了マーカー）", "-", "初期化"])
        append_log(service, rows)
        print(f"初回実行: 既存{len(rows) - 1}件を配信済み登録（送信なし）。次回から新規録音を配信します")
        return

    sent, pending, errors, unknowns = [], [], [], []
    log_rows = []
    for f in files:
        if f["id"] in sent_ids:
            continue
        route = f["route"]
        if route.get("unknown_name"):
            print(f"  宛先不明→院長へアラート: {f['title']}")
            unknowns.append(f"・{f['title']}（「{route['unknown_name']}」がスタッフ一覧にありません）")
            continue
        try:
            if route["dest"][0] == "channel" and not route["dest"][1]:
                print(f"  スキップ（チャンネル未設定）: {f['title']}")
                continue
            detail = get_file_detail(f["id"])
            if not has_summary(detail):
                print(f"  要約未生成→翌日再試行: {f['title']}")
                pending.append(f["title"])
                continue
            share_url = get_share_url(f["id"], get_note_ids(detail))
            if not share_url:
                raise RuntimeError("共有URL取得失敗")
            msg = f"{route['header']}\n{f['title']}\n\nPLAUD要約リンク: {share_url}"
            send_lw(route["dest"], msg)
            print(f"  配信[{route['dest_label']}]: {f['title']}")
            sent.append(f"{route['dest_label']}: {f['title']}")
            if SHARE_MODE != "test":
                log_rows.append([f["id"], now.strftime("%Y-%m-%d %H:%M"), f["title"],
                                 route["dest_label"], "配信済み"])
        except Exception as e:
            print(f"  ERROR {f['title']}: {e}")
            errors.append(f"{f['title']}: {e}")

    append_log(service, log_rows)

    # 予定曜日なのに今日の録音が無い → 院長へアラート（斉藤さんは不定期なので対象外）
    for exp in EXPECTED_RECORDINGS:
        if exp["weekday"] != weekday:
            continue
        todays = [f for f in files if norm(f["title"]).startswith(exp["prefix"]) and f["date"] == today]
        if not todays:
            notify_shincho(
                f"【PLAUD配信Bot】本日（{WEEKDAY_JA[weekday]}曜）の「{exp['display']}」の録音が"
                f"PLAUDに見つかりません。\n\n録音がある場合はPLAUDへアップロードしてください。"
                f"明日以降の実行で見つかり次第、{exp['label']}へ自動配信します。\n"
                f"本日実施がなかった場合はこのメッセージは無視してください。")

    # 要約未生成の録音 → 院長へ知らせて翌日再試行
    if pending:
        notify_shincho("【PLAUD配信Bot】録音は見つかりましたが、PLAUD要約が未生成のため配信できませんでした"
                       "（明日の実行で自動再試行します）。\n\n" + "\n".join(f"・{t}" for t in pending))

    # 面談（○○）の○○がスタッフ一覧に無い → 院長へアラート（配信しない）
    if unknowns:
        notify_shincho("【PLAUD配信Bot】面談の録音がありましたが、配信先のスタッフを特定できなかったため"
                       "配信していません。\n\n" + "\n".join(unknowns)
                       + "\n\n配信先に追加する場合は、その方の名前をAIに伝えてください"
                       "（追加までは毎日この通知が出ます。録音から7日たつと対象外になります）。")

    print(f"完了: 配信{len(sent)}件 / 要約待ち{len(pending)}件 / 宛先不明{len(unknowns)}件 / エラー{len(errors)}件")
    if errors:
        notify_shincho("⚠️【PLAUD配信Bot】一部の配信に失敗しました\n\n" + "\n".join(errors))
        sys.exit(1)


if __name__ == "__main__":
    main()

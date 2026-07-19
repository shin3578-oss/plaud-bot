# -*- coding: utf-8 -*-
"""
面談記録シートBot - PLAUDの「面談（名前）」録音を、スタッフ別タブに時系列で自動追記
  - 要約（簡潔版）とPLAUD共有リンクを記録
  - 面談で決まった「やること(TODO)」をClaudeで抽出し、1行ずつチェックボックス付きで記録
  - 重複防止はシートに記録済みの録音IDと照合（状態ファイル不要）

認証:
  - Actions:  GOOGLE_CREDENTIALS_JSON（サービスアカウント / spreadsheetsスコープ）
  - ローカル: drive_token.json（院長OAuth）
"""
import os, io, json, gzip, re, time, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# Windowsコンソール(cp932)でも絵文字・ダッシュ等で落ちないようUTF-8出力に
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

JST = timezone(timedelta(hours=9))
PLAUD_API = "https://api-apne1.plaud.ai"
SHEET_ID = os.environ.get("MENDAN_SHEET_ID", "11Zct4Knwz6ItPB1dmFIKz0Yx-ZEAbeZVp5LvWLx6D7A")
DAYS_BACK = int(os.environ.get("MENDAN_DAYS_BACK", "60"))
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

HEADER = ["完了", "面談日", "担当", "やること", "期限", "要約", "PLAUDリンク", "録音ID"]

# ---- PLAUDトークン（ローカルはplaud_storage.json、Actionsは環境変数） ----
_token_file = Path(r"C:\Users\shin3\Desktop\AI\plaud_storage.json")
if _token_file.exists():
    PLAUD_TOKEN = json.loads(_token_file.read_text(encoding="utf-8")).get("pld_tokenstr", "").strip('"')
else:
    PLAUD_TOKEN = os.environ.get("PLAUD_TOKEN", "").strip('"')

# タイトル内の名前キーワード → フルネーム（mendan_bot.py と共通）
STAFF_MAP = {
    "田口": "田口咲奈", "桑野碧": "桑野碧", "桑野莉緒": "桑野莉緒", "桑野": "桑野碧",
    "小西": "小西瑛子", "石川": "石川真里", "内藤": "内藤友菜", "重野": "重野茜",
    "若澤": "若澤未羽", "山本": "山本心奈", "森": "森はるか", "斉藤": "斉藤愛莉",
    "竹内": "竹内由佳", "篠宮": "篠宮翔鳳", "シノ": "篠宮翔鳳", "関": "関恵美",
    "村田": "村田真季", "秋野": "秋野友希", "渡邊": "渡邊旭", "渡辺": "渡邊旭",
    "納冨": "納冨泰行", "能冨": "納冨泰行", "濱": "濱成宏",
    "茨木": "茨木有紀",
}

# LINE WORKS（院長DM通知用・plaud_bot.pyと共通の値）
LW_CLIENT_ID = "0cAEPO2Yzau80tSsEhxV"
LW_CLIENT_SECRET = os.environ.get("LW_CLIENT_SECRET", "d7WfxxO2t1")
LW_SERVICE_ACCOUNT = "3w266.serviceaccount@ovalcourtdental"
LW_BOT_ID = "12266491"
LW_SHINCHO_ID = "shin@ovalcourtdental"
LW_PRIVATE_KEY = os.environ.get("LW_PRIVATE_KEY", "")


# ==============================
# Google Sheets 認証
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


# ==============================
# PLAUD
# ==============================
def find_mendan_files():
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
        # 面談のみ対象（面接=採用は除外）
        if not (title.startswith("面談（") or title.startswith("面談(")):
            continue
        file_dt = datetime.fromtimestamp(f.get("start_time", 0) / 1000, tz=JST)
        if file_dt < cutoff:
            continue
        results.append({"id": f.get("id", ""), "title": title, "date": file_dt.strftime("%Y-%m-%d")})
    return results


def get_summary(file_id):
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    r = requests.get(f"{PLAUD_API}/file/detail/{file_id}", headers=headers, timeout=30)
    r.raise_for_status()
    for item in r.json().get("data", {}).get("content_list", []):
        if item.get("data_type") != "auto_sum_note":
            continue
        r_s3 = requests.get(item["data_link"], timeout=30)
        try:
            return json.loads(gzip.decompress(r_s3.content)).get("ai_content", "")
        except Exception:
            pass
        try:
            return r_s3.json().get("ai_content", "")
        except Exception:
            pass
        try:
            text = r_s3.content.decode("utf-8").strip()
            text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
            text = re.sub(r"##\s*コア[・･]シノプシス.*?(?=##|\Z)", "", text, flags=re.DOTALL)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if text:
                return text
        except Exception:
            pass
    return ""


def get_note_ids(file_id):
    headers = {"Authorization": PLAUD_TOKEN, "Content-Type": "application/json"}
    r = requests.get(f"{PLAUD_API}/file/detail/{file_id}", headers=headers, timeout=30)
    r.raise_for_status()
    return [str(i["data_id"]) for i in r.json().get("data", {}).get("content_list", [])
            if i.get("data_type") in ("auto_sum_note", "sum_multi_note") and i.get("data_id")]


def get_share_url(file_id, note_ids):
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


def extract_name(title):
    m = re.match(r"面談[（(]([^）)]+)[）)]", title)
    raw = m.group(1) if m else ""
    if raw:
        for key in sorted(STAFF_MAP.keys(), key=len, reverse=True):
            if key in raw:
                return STAFF_MAP[key]
        return re.sub(r'[\\/:*?"<>|]', "", raw).strip() or "不明"
    return "不明"


# ==============================
# Claude: 要約整形 ＋ やること抽出 ＋ 面談担当推定
# ==============================
def analyze(summary):
    import anthropic
    api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)
    prompt = f"""次はスタッフとの職種面談の要約です。以下をJSONだけで出力してください（前後の説明文なし）。

{{
  "tanto": "面談担当者の名字。桑野か斉藤のどちらかが担当。要約から判断できれば「桑野」または「斉藤」、判断できなければ空文字",
  "summary": "面談内容を150〜250字で簡潔にまとめた要約（敬体）",
  "todos": [ {{ "task": "面談で決まった具体的なやること（1件ずつ）", "due": "期限が明示されていればYYYY-MM-DDや『次回まで』等、なければ空文字" }} ]
}}

やることが無ければ todos は空配列。憶測でやることを作らない。

--- 面談要約 ---
{summary[:6000]}"""
    msg = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
    except Exception:
        # 抽出失敗時はPLAUD要約をそのまま・TODOなしで通す（黙って落とさない）
        return {"tanto": "", "summary": summary[:250], "todos": []}
    data.setdefault("tanto", "")
    data.setdefault("summary", summary[:250])
    data.setdefault("todos", [])
    return data


# ==============================
# Sheets 操作
# ==============================
def get_all_sheets(service):
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}


def get_existing_ids(service, tab_titles):
    """全スタッフタブのH列（録音ID）を集める"""
    ids = set()
    ranges = [f"{t}!H2:H" for t in tab_titles if t != "説明"]
    if not ranges:
        return ids
    resp = service.spreadsheets().values().batchGet(spreadsheetId=SHEET_ID, ranges=ranges).execute()
    for vr in resp.get("valueRanges", []):
        for row in vr.get("values", []):
            if row and row[0].strip():
                ids.add(row[0].strip())
    return ids


def ensure_tab(service, title, sheets_map):
    if title in sheets_map:
        return sheets_map[title]
    # タブ追加（既に存在していたら最新のシート一覧から拾い直す＝競合・部分状態に耐性）
    try:
        resp = service.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": title, "gridProperties": {"columnCount": 8, "frozenRowCount": 1}}}}]},
        ).execute()
        sid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    except Exception as e:
        if "すでに存在" in str(e) or "already exists" in str(e):
            sheets_map.update(get_all_sheets(service))
            return sheets_map[title]
        raise
    sheets_map[title] = sid
    # ヘッダー書き込み
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID, range=f"{title}!A1:H1",
        valueInputOption="RAW", body={"values": [HEADER]},
    ).execute()
    # ヘッダーを太字に、列幅調整（A列のチェックボックスは追記後に「書いた行だけ」へ適用する。
    #  列全体に先付けするとA列が全行FALSE値で埋まり、appendが最終行を誤認するため）
    service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.85, "green": 0.9, "blue": 0.95}}},
                "fields": "userEnteredFormat(textFormat,backgroundColor)"}},
            {"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 3, "endIndex": 4},
                "properties": {"pixelSize": 260}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 5, "endIndex": 6},
                "properties": {"pixelSize": 400}, "fields": "pixelSize"}},
        ]},
    ).execute()
    return sid


def build_rows(date, tanto, analysis, share_url, rec_id):
    todos = analysis.get("todos") or []
    summ = analysis.get("summary", "")
    rows = []
    if not todos:
        rows.append([False, date, tanto, "", "", summ, share_url, rec_id])
    else:
        for i, td in enumerate(todos):
            rows.append([
                False, date, tanto, td.get("task", ""), td.get("due", ""),
                summ if i == 0 else "", share_url if i == 0 else "", rec_id if i == 0 else "",
            ])
    return rows


def append_rows(service, title, rows, sheet_id):
    resp = service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID, range=f"{title}!A1",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
    # 追記した行のA列だけをチェックボックス化する
    updated = resp.get("updates", {}).get("updatedRange", "")
    m = re.search(r"![A-Z]+(\d+):[A-Z]+(\d+)$", updated)
    if m:
        start_row, end_row = int(m.group(1)), int(m.group(2))
        service.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"setDataValidation": {
                "range": {"sheetId": sheet_id, "startRowIndex": start_row - 1, "endRowIndex": end_row,
                          "startColumnIndex": 0, "endColumnIndex": 1},
                "rule": {"condition": {"type": "BOOLEAN"}, "showCustomUi": True}}}]},
        ).execute()


# ==============================
# LINE WORKS（院長DM）
# ==============================
def notify_shincho(message):
    if not LW_PRIVATE_KEY:
        print("  LW_PRIVATE_KEY未設定のため通知スキップ")
        return
    try:
        import jwt as pyjwt
        now = int(time.time())
        assertion = pyjwt.encode({"iss": LW_CLIENT_ID, "sub": LW_SERVICE_ACCOUNT, "iat": now, "exp": now + 3600},
                                 LW_PRIVATE_KEY, algorithm="RS256")
        tok = requests.post("https://auth.worksmobile.com/oauth2/v2.0/token",
                            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion,
                                  "client_id": LW_CLIENT_ID, "client_secret": LW_CLIENT_SECRET, "scope": "bot"},
                            timeout=30).json()["access_token"]
        requests.post(f"https://www.worksapis.com/v1.0/bots/{LW_BOT_ID}/users/{LW_SHINCHO_ID}/messages",
                      headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                      json={"content": {"type": "text", "text": message}}, timeout=30).raise_for_status()
        print("  院長DM通知完了")
    except Exception as e:
        print(f"  院長DM通知エラー: {e}")


# ==============================
# Main
# ==============================
def main():
    print(f"面談記録シートBot 開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")
    if not PLAUD_TOKEN:
        print("❌ PLAUD_TOKEN未設定")
        sys.exit(1)

    service = get_sheets_service()
    sheets_map = get_all_sheets(service)
    existing = get_existing_ids(service, list(sheets_map.keys()))
    print(f"記録済み録音ID: {len(existing)}件")

    files = find_mendan_files()
    print(f"PLAUD面談ファイル: {len(files)}件（過去{DAYS_BACK}日）")

    added = []
    errors = []
    for f in files:
        if f["id"] in existing:
            continue
        try:
            summary = get_summary(f["id"])
            if not summary:
                print(f"  要約未生成→スキップ: {f['title']}")
                continue
            name = extract_name(f["title"])
            note_ids = get_note_ids(f["id"])
            share_url = get_share_url(f["id"], note_ids)
            analysis = analyze(summary)
            rows = build_rows(f["date"], analysis.get("tanto", ""), analysis, share_url, f["id"])
            sid = ensure_tab(service, name, sheets_map)
            append_rows(service, name, rows, sid)
            n_todo = len(analysis.get("todos") or [])
            print(f"  追記[{name}] {f['date']} やること{n_todo}件: {f['title']}")
            added.append(f"{name}（{f['date']}・やること{n_todo}件）")
        except Exception as e:
            print(f"  ERROR {f['title']}: {e}")
            errors.append(f"{f['title']}: {e}")

    print(f"完了: 新規{len(added)}件 / エラー{len(errors)}件")

    if added:
        msg = "【面談記録】新しい面談を記録しました\n\n" + "\n".join(f"・{a}" for a in added)
        msg += f"\n\nhttps://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
        notify_shincho(msg)
    if errors:
        notify_shincho("⚠️【面談記録】一部の面談が記録できませんでした\n\n" + "\n".join(errors))
        sys.exit(1)


if __name__ == "__main__":
    main()

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
# 記録除外スタッフ（この人自身の面談はシートに残さない。院長指示2026-07-25:
# 全スタッフの面談を記録する。桑野・斉藤本人の面談のみ対象外。スプシ共有は院長のみ）
EXCLUDE_STAFF = set(filter(None, os.environ.get("MENDAN_EXCLUDE_STAFF", "桑野碧,斉藤愛莉").split(",")))
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# ---- 幹部シェア用スプシ（院長指示2026-07-28）----
# 幹部（桑野・斉藤）が実施した面談だけを、幹部が閲覧できる別スプシにも記録する。
# 院長が実施した面談（例: 水曜の田口さんとの面談）は機密のため絶対にこちらへ入れない。
# 見分けはPLAUDの録音タイトルの接頭辞のみで行う＝印が無いものは幹部側へ流れない安全設計。
KANBU_SHEET_ID = os.environ.get("MENDAN_KANBU_SHEET_ID", "1_WoiIeYFlG3RE30qm2aFwW3C7sgivPSC7iyQpzzPm7Y")
KANBU_PREFIX = "幹部面談"
# 幹部シェア用に載せてよいスタッフ（ホワイトリスト。ここに無い人は幹部スプシに記録しない）
KANBU_STAFF = set(filter(None, os.environ.get(
    "MENDAN_KANBU_STAFF",
    "田口咲奈,小西瑛子,石川真里,山本心奈,若澤未羽,内藤友菜,森はるか,竹内由佳,重野茜").split(",")))
# 院長スプシの行の色分け：幹部が実施した面談＝薄い緑／院長が実施した面談＝白
KANBU_BG = {"red": 0.87, "green": 0.95, "blue": 0.87}

HEADER = ["面談日", "要約", "その日決まったTODO", "PLAUDリンク", "録音ID"]
# スタッフ別タブではない＝面談記録が入らないタブ（録音IDの照合対象から外す）
NON_STAFF_TABS = ("説明", "配信ログ", "シノ面談の取り込み")

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
LW_BOT_ID = "12786833"  # 完了通知Bot（新規面談記録の完了報告）
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
        # 「面談（…）」＝院長実施 / 「幹部面談（…）」＝幹部実施。面接(採用)は対象外
        is_kanbu = title.startswith(f"{KANBU_PREFIX}（") or title.startswith(f"{KANBU_PREFIX}(")
        if not (is_kanbu or title.startswith("面談（") or title.startswith("面談(")):
            continue
        file_dt = datetime.fromtimestamp(f.get("start_time", 0) / 1000, tz=JST)
        if file_dt < cutoff:
            continue
        results.append({"id": f.get("id", ""), "title": title,
                        "date": file_dt.strftime("%Y-%m-%d"), "is_kanbu": is_kanbu})
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
    # 「面談（田口）…」「幹部面談（田口）…」どちらからも名前を取る
    m = re.match(r"(?:幹部)?面談[（(]([^）)]+)[）)]", title)
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
def analyze(summary, staff_name=""):
    import anthropic
    api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)
    who = f"この面談を受けたスタッフは「{staff_name}さん」です。\n" if staff_name else ""
    prompt = f"""次はスタッフとの職種面談の要約です。以下をJSONだけで出力してください（前後の説明文なし）。

{{
  "summary": "面談内容を200〜350字で簡潔にまとめた要約（敬体）",
  "todos": [ "面談で決まった具体的なやること（1件ずつ・短い文で）" ],
  "interviewer": "面談した側が誰か。「桑野」「斉藤」「院長」のいずれか。少しでも迷うなら「不明」"
}}

interviewer は録音タイトルの付け忘れを見つけるためだけに使い、記録内容には出しません。
本人が名乗っている、明確に名前で呼ばれている等の**はっきりした根拠がある場合だけ**
「桑野」「斉藤」「院長」と答えてください。**根拠が無ければ必ず「不明」**。
話の内容から「たぶん衛生士の上司だろう」といった推測で決めないでください。

{who}元の要約には「Speaker 1」「Speaker 2」のような話者ラベルが混ざっていることがあります。
出力にはこの話者ラベルを絶対に使わないでください。次のように書き換えます。
- 面談を受けたスタッフ本人の発言 → 「{staff_name or '本人'}さん」または「本人」
- 面談した側（院長・幹部）の発言 → 「面談者」
- どちらの発言か確実に判断できないときは、主語を無理に決めず、決まったことや事実だけを書く（推測で人を当てはめない）

やることが無ければ todos は空配列。憶測でやることを作らない。

--- 面談要約 ---
{summary[:6000]}"""
    text = ""
    for attempt in range(2):  # 話者ラベルが残ったら1度だけ引き直す
        msg = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        if not re.search(r"[Ss]peaker\s*\d", text):
            break
        print(f"  話者ラベルが残ったため再生成（{attempt + 1}回目）")
    try:
        data = json.loads(text)
    except Exception:
        # 抽出失敗時はPLAUD要約をそのまま・TODOなしで通す（黙って落とさない）
        return {"summary": summary[:350], "todos": [], "interviewer": "不明"}
    data.setdefault("summary", summary[:350])
    data.setdefault("todos", [])
    data.setdefault("interviewer", "不明")
    return data


# ==============================
# Sheets 操作
# ==============================
def get_all_sheets(service, ssid=None):
    meta = service.spreadsheets().get(spreadsheetId=ssid or SHEET_ID).execute()
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}


def get_existing_ids(service, tab_titles, ssid=None):
    """全スタッフタブのE列（録音ID）を集める"""
    ids = set()
    ranges = [f"{t}!E2:E" for t in tab_titles if t not in NON_STAFF_TABS]
    if not ranges:
        return ids
    resp = service.spreadsheets().values().batchGet(spreadsheetId=ssid or SHEET_ID, ranges=ranges).execute()
    for vr in resp.get("valueRanges", []):
        for row in vr.get("values", []):
            if row and row[0].strip():
                ids.add(row[0].strip())
    return ids


def ensure_tab(service, title, sheets_map, ssid=None):
    ssid = ssid or SHEET_ID
    if title in sheets_map:
        return sheets_map[title]
    # タブ追加（既に存在していたら最新のシート一覧から拾い直す＝競合・部分状態に耐性）
    try:
        resp = service.spreadsheets().batchUpdate(
            spreadsheetId=ssid,
            body={"requests": [{"addSheet": {"properties": {"title": title, "gridProperties": {"columnCount": 5, "frozenRowCount": 1}}}}]},
        ).execute()
        sid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    except Exception as e:
        if "すでに存在" in str(e) or "already exists" in str(e):
            sheets_map.update(get_all_sheets(service, ssid))
            return sheets_map[title]
        raise
    sheets_map[title] = sid
    # ヘッダー書き込み
    service.spreadsheets().values().update(
        spreadsheetId=ssid, range=f"{title}!A1:E1",
        valueInputOption="RAW", body={"values": [HEADER]},
    ).execute()
    # 折り返し表示＋上揃え（全部見えるように）、ヘッダー太字、列幅調整
    widths = {0: 95, 1: 560, 2: 420, 3: 115, 4: 115}  # 面談日/要約/TODO/リンク/録音ID
    reqs = [
        {"repeatCell": {
            "range": {"sheetId": sid},
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
            "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.85, "green": 0.9, "blue": 0.95}}},
            "fields": "userEnteredFormat(textFormat,backgroundColor)"}},
    ]
    for col, px in widths.items():
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
            "properties": {"pixelSize": px}, "fields": "pixelSize"}})
    service.spreadsheets().batchUpdate(spreadsheetId=ssid, body={"requests": reqs}).execute()
    return sid


def build_rows(date, analysis, share_url, rec_id):
    """1面談＝1行。その日決まったTODOは1セルに箇条書きでまとめる。"""
    todos = analysis.get("todos") or []
    summ = analysis.get("summary", "")
    tasks = []
    for t in todos:
        task = t.get("task", "") if isinstance(t, dict) else str(t)
        if task.strip():
            tasks.append(task.strip())
    todo_text = "\n".join(f"・{t}" for t in tasks) if tasks else "―"
    return [[date, summ, todo_text, share_url, rec_id]]


def sort_tab_desc(service, sheet_id, ssid=None):
    """面談日（A列）で降順ソート＝最新の面談が常に一番上。
    PLAUDの取得順やバックフィルの都合で行が前後しても、追記のたびにここで整列する。
    空行は常に末尾に送られるため、行数の増減や手動追記があっても壊れない。"""
    service.spreadsheets().batchUpdate(
        spreadsheetId=ssid or SHEET_ID,
        body={"requests": [{"sortRange": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 5},
            "sortSpecs": [{"dimensionIndex": 0, "sortOrder": "DESCENDING"}],
        }}]},
    ).execute()


def append_rows(service, title, rows, ssid=None):
    """追記して、書き込まれた先頭行の行番号（1始まり）を返す"""
    resp = service.spreadsheets().values().append(
        spreadsheetId=ssid or SHEET_ID, range=f"{title}!A1",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
    m = re.search(r"![A-Z]+(\d+)", resp.get("updates", {}).get("updatedRange", ""))
    return int(m.group(1)) if m else None


def color_row(service, sheet_id, row_no, is_kanbu, ssid=None):
    """院長スプシで「誰が実施した面談か」を背景色で区別する（院長指示2026-07-28）。
    幹部が実施＝薄い緑 / 院長が実施＝白。ソートしても書式は行と一緒に動く。"""
    if not row_no:
        return
    bg = KANBU_BG if is_kanbu else {"red": 1.0, "green": 1.0, "blue": 1.0}
    service.spreadsheets().batchUpdate(
        spreadsheetId=ssid or SHEET_ID,
        body={"requests": [{"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": row_no - 1, "endRowIndex": row_no,
                      "startColumnIndex": 0, "endColumnIndex": 5},
            "cell": {"userEnteredFormat": {"backgroundColor": bg}},
            "fields": "userEnteredFormat.backgroundColor"}}]},
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

    # 院長スプシ（全面談）と幹部シェア用スプシ（幹部実施の面談のみ）の2冊を扱う
    books = {
        "院長": {"ssid": SHEET_ID},
        "幹部": {"ssid": KANBU_SHEET_ID},
    }
    for label, b in books.items():
        b["map"] = get_all_sheets(service, b["ssid"])
        b["existing"] = get_existing_ids(service, list(b["map"].keys()), b["ssid"])
        print(f"記録済み録音ID[{label}]: {len(b['existing'])}件")

    files = find_mendan_files()
    n_kanbu = sum(1 for f in files if f["is_kanbu"])
    print(f"PLAUD面談ファイル: {len(files)}件（うち幹部面談{n_kanbu}件・過去{DAYS_BACK}日）")

    added = {"院長": [], "幹部": []}
    errors = []
    # 「面談（…）」で録れているが、話の中では桑野さん・斉藤さんが面談しているように見えるもの。
    # 幹部面談の付け忘れの可能性があるため院長へ知らせるだけで、**振り分けは一切変えない**
    # （印が無いものを幹部へ流さないのは、院長実施の機密面談を守るための安全側の設計。2026-08-05）
    suspects = []
    for f in files:
        try:
            name = extract_name(f["title"])
            # このファイルをどの本に書くか決める
            #   院長スプシ: 全面談（ただし桑野・斉藤本人の面談は除外）
            #   幹部スプシ: 「幹部面談（…）」かつホワイトリストのスタッフのみ
            targets = []
            if name not in EXCLUDE_STAFF and f["id"] not in books["院長"]["existing"]:
                targets.append("院長")
            if f["is_kanbu"] and name in KANBU_STAFF and f["id"] not in books["幹部"]["existing"]:
                targets.append("幹部")
            if not targets:
                continue

            summary = get_summary(f["id"])
            if not summary:
                print(f"  要約未生成→スキップ: {f['title']}")
                continue
            note_ids = get_note_ids(f["id"])
            share_url = get_share_url(f["id"], note_ids)
            analysis = analyze(summary, name)  # 要約整形は1回だけ実行して両方に使い回す
            rows = build_rows(f["date"], analysis, share_url, f["id"])
            n_todo = len(analysis.get("todos") or [])

            # 付け忘れの疑い（通知だけ・振り分けには使わない）
            who = (analysis.get("interviewer") or "").strip()
            if not f["is_kanbu"] and name in KANBU_STAFF and who in ("桑野", "斉藤"):
                suspects.append(f"{name}（{f['date']}・{who}さんが面談しているようです）\n  {f['title']}")
                print(f"  ⚠️ 幹部面談の付け忘れかも[{name}] 実施者らしき人: {who}")

            for label in targets:
                b = books[label]
                sid = ensure_tab(service, name, b["map"], b["ssid"])
                row_no = append_rows(service, name, rows, b["ssid"])
                if label == "院長":
                    # 院長シートだけ色分け（幹部シートは全部が幹部実施なので不要）
                    color_row(service, sid, row_no, f["is_kanbu"], b["ssid"])
                sort_tab_desc(service, sid, b["ssid"])  # 追記のたびに面談日の降順へ整列（最新が一番上）
                print(f"  追記[{label}／{name}] {f['date']} やること{n_todo}件: {f['title']}")
                added[label].append(f"{name}（{f['date']}・やること{n_todo}件）")
        except Exception as e:
            print(f"  ERROR {f['title']}: {e}")
            errors.append(f"{f['title']}: {e}")

    print(f"完了: 院長{len(added['院長'])}件 / 幹部{len(added['幹部'])}件 / エラー{len(errors)}件")

    if added["院長"] or added["幹部"]:
        msg = "【面談記録】新しい面談を記録しました\n"
        if added["院長"]:
            msg += "\n■ 院長シート\n" + "\n".join(f"・{a}" for a in added["院長"])
            msg += f"\nhttps://docs.google.com/spreadsheets/d/{SHEET_ID}/edit\n"
        if added["幹部"]:
            msg += "\n■ 幹部シェア用シート（桑野さん・斉藤さんが閲覧できます）\n"
            msg += "\n".join(f"・{a}" for a in added["幹部"])
            msg += f"\nhttps://docs.google.com/spreadsheets/d/{KANBU_SHEET_ID}/edit"
        if suspects:
            msg += "\n\n⚠️ 幹部面談の付け忘れかもしれません\n"
            msg += "\n".join(f"・{s}" for s in suspects)
            msg += ("\n録音タイトルが「面談（…）」だったため、院長シートにだけ入れました。"
                    "幹部シェア用にも入れる場合はAIに「幹部シェアにも入れて」と伝えてください。")
        notify_shincho(msg)
    if errors:
        notify_shincho("⚠️【面談記録】一部の面談が記録できませんでした\n\n" + "\n".join(errors))
        sys.exit(1)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""シノ面談 取り込みBot - PLAUD共有URLから面談記録を院長スプシへ取り込む

シノ（篠宮）が実施した面談はシノ自身のPLAUDアカウントにあり、院長のPLAUD APIからは
見えない。そこで「共有URL」を入口にする。

  院長スプシの『シノ面談の取り込み』タブのA列にPLAUD共有URLを貼る
    → このBotがページを開いて要約本文を読み取り
    → Claudeで要約＋TODOに整形
    → 該当スタッフのタブへ薄い青の行で追記

URLの入手経路（手貼り／将来的にLINEワークスから自動収集）が変わっても、
このタブに入れるところだけ差し替えれば処理は共通で使える。

認証: mendan_sheets.py と同じ（Actions=GOOGLE_CREDENTIALS_JSON / ローカル=drive_token.json）
"""
import os, re, sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from mendan_sheets import (JST, SHEET_ID, STAFF_MAP, HEADER, get_sheets_service, get_all_sheets,
                           get_existing_ids, ensure_tab, append_rows, sort_tab_desc, color_row,
                           analyze, build_rows, notify_shincho)

IMPORT_TAB = "シノ面談の取り込み"
IMPORT_HEADER = ["PLAUD共有URL", "記録先スタッフ（空欄なら自動）", "取り込み日時", "結果"]
# シノが実施した面談の行の色（白＝院長／緑＝幹部／青＝シノ）
SHINO_BG = {"red": 0.85, "green": 0.91, "blue": 0.97}


def ensure_import_tab(service):
    """取り込み用タブが無ければ作る（説明付き）"""
    sheets_map = get_all_sheets(service, SHEET_ID)
    if IMPORT_TAB in sheets_map:
        return sheets_map[IMPORT_TAB]
    resp = service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{"addSheet": {"properties": {
            "title": IMPORT_TAB, "index": 1,
            "gridProperties": {"columnCount": 4, "frozenRowCount": 3}}}}]},
    ).execute()
    sid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID, range=f"{IMPORT_TAB}!A1:D3", valueInputOption="RAW",
        body={"values": [
            ["■ シノが実施した面談を取り込む場所です。A列にPLAUDの共有URLを貼るだけでOK（1行に1つ）。", "", "", ""],
            ["　毎晩21時ごろ、自動で読み取って該当スタッフのタブに青い行で追記します。C・D列はBotが書くので触らないでください。", "", "", ""],
            IMPORT_HEADER]},
    ).execute()
    service.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests": [
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 2, "endRowIndex": 3},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                                       "backgroundColor": SHINO_BG}},
                        "fields": "userEnteredFormat(textFormat,backgroundColor)"}},
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 520}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 4},
            "properties": {"pixelSize": 180}, "fields": "pixelSize"}},
    ]}).execute()
    print(f"取り込みタブを作成: {IMPORT_TAB}")
    return sid


# クッキー同意バナーの文言（本文に混ざるので落とす）
BANNER_WORDS = ("クッキー", "プライバシーを大切に", "カスタマイズ", "Cookie", "公式サイト",
                "無限に広がる、知的ポテンシャル")


def _strip_banner(text):
    """同意バナー・PLAUDの宣伝行を落として本文だけにする"""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or any(w in s for w in BANNER_WORDS):
            continue
        out.append(s)
    return "\n".join(out)


def fetch_share_page(url):
    """PLAUD共有ページを開いて本文テキストを返す。
    中身は iframe(web.plaud.ai/nshare/...) の中にあるので、フレームから読む。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
            # クッキー同意は必ず「拒否」を選ぶ（プライバシー優先）
            for label in ("すべてのクッキーを拒否する", "Reject All", "すべて拒否"):
                try:
                    btn = page.get_by_text(label, exact=False).first
                    if btn.is_visible(timeout=2000):
                        btn.click(timeout=3000)
                        page.wait_for_timeout(500)
                        break
                except Exception:
                    pass
            text = ""
            for fr in page.frames:
                if "/nshare/" not in fr.url:
                    continue
                fr.wait_for_selector("body", timeout=30000)
                for _ in range(20):  # 描画完了まで待つ
                    text = fr.evaluate("document.body.innerText") or ""
                    if len(text) > 200:
                        break
                    page.wait_for_timeout(1000)
                if text:
                    break
            return _strip_banner(text)
        finally:
            browser.close()


def parse_head(text):
    """共有ページ先頭からタイトルと録音日時を取る
    例) '07-28 面談：重野' / '2026-07-26 17:07:22'"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    title = lines[0] if lines else ""
    date = ""
    for l in lines[:6]:
        m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", l)
        if m:
            date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            break
    return title, date


def guess_staff(title, text):
    """タイトル（例: '07-28 面談：重野'）からスタッフ名を判定。
    見つからなければ空文字を返し、呼び出し側でスキップする（推測で書かない）。"""
    head = title + "\n" + "\n".join(text.splitlines()[:3])
    for key in sorted(STAFF_MAP.keys(), key=len, reverse=True):
        if key in head:
            return STAFF_MAP[key]
    return ""


def main():
    print(f"シノ面談 取り込みBot 開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")
    service = get_sheets_service()
    ensure_import_tab(service)

    rows = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"{IMPORT_TAB}!A4:D").execute().get("values", [])
    todo = [(i + 4, r) for i, r in enumerate(rows)
            if r and r[0].strip().startswith("http") and not (len(r) > 2 and r[2].strip())]
    print(f"未取り込みのURL: {len(todo)}件")
    if not todo:
        return

    sheets_map = get_all_sheets(service, SHEET_ID)
    existing = get_existing_ids(service, list(sheets_map.keys()), SHEET_ID)
    added, results = [], []

    for row_no, r in todo:
        url = r[0].strip()
        manual_name = r[1].strip() if len(r) > 1 else ""
        try:
            rec_id = re.search(r"/s/(pub_[0-9a-f-]+)", url)
            rec_id = rec_id.group(1) if rec_id else url[-40:]
            if rec_id in existing:
                results.append((row_no, "済（同じ面談が既に記録されています）"))
                continue

            text = fetch_share_page(url)
            if len(text) < 100:
                results.append((row_no, "❌ ページを読めませんでした（URLが無効か期限切れ）"))
                continue
            title, date = parse_head(text)
            name = manual_name or guess_staff(title, text)
            if not name:
                results.append((row_no, "❌ スタッフ名が分かりません→B列に氏名を書いてください"))
                continue
            if not date:
                date = datetime.now(JST).strftime("%Y-%m-%d")

            analysis = analyze(text, name)
            new_rows = build_rows(date, analysis, url, rec_id)
            sid = ensure_tab(service, name, sheets_map, SHEET_ID)
            wrote_at = append_rows(service, name, new_rows, SHEET_ID)
            color_row(service, sid, wrote_at, False, SHEET_ID)  # 先に白で塗ってから青で上書き
            service.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests": [
                {"repeatCell": {
                    "range": {"sheetId": sid, "startRowIndex": wrote_at - 1, "endRowIndex": wrote_at,
                              "startColumnIndex": 0, "endColumnIndex": 5},
                    "cell": {"userEnteredFormat": {"backgroundColor": SHINO_BG}},
                    "fields": "userEnteredFormat.backgroundColor"}}]}).execute()
            sort_tab_desc(service, sid, SHEET_ID)
            n_todo = len(analysis.get("todos") or [])
            print(f"  取り込み[{name}] {date} やること{n_todo}件: {title[:30]}")
            added.append(f"{name}（{date}・やること{n_todo}件）")
            results.append((row_no, f"✅ {name} {date} に記録"))
        except Exception as e:
            print(f"  ERROR {url[:50]}: {e}")
            results.append((row_no, f"❌ {e}"))

    # 取り込み結果を書き戻す
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    service.spreadsheets().values().batchUpdate(spreadsheetId=SHEET_ID, body={
        "valueInputOption": "USER_ENTERED",
        "data": [{"range": f"{IMPORT_TAB}!C{n}:D{n}", "values": [[now, msg]]} for n, msg in results]}).execute()

    print(f"完了: 新規{len(added)}件")
    if added:
        msg = "【シノ面談の取り込み】新しい面談を記録しました\n\n" + "\n".join(f"・{a}" for a in added)
        msg += f"\n\nhttps://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
        notify_shincho(msg)


if __name__ == "__main__":
    main()

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
# シノ用の投函箱（別スプシ）。院長スプシには機密面談が入っておりシノに共有できないため、
# URLを貼るだけの専用スプシを分けている。シノ=編集可・Bot=編集可・院長=オーナー。
INBOX_SHEET_ID = os.environ.get("SHINO_INBOX_SHEET_ID", "18lbbhXck1N3ZXVQMZ1DRP_xE91YtYxqd5IN7qnMFLT4")
INBOX_TAB = "投函箱"
INBOX_FIRST_ROW = 7  # 6行目までが説明とヘッダー
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


# 面談ではない録音（面談スプシに入れない）。シノのスレッドには面接・セミナー・
# 会議のリンクも混ざるため、名前が取れても以下が付くものは記録しない。
NOT_MENDAN = ("面接", "セミナー", "講演", "講義", "会議", "MTG", "ミーティング", "研修")


def is_not_mendan(title):
    """タイトルの先頭付近に面談以外を示す語があるか（本文中の言及では判定しない）"""
    head = title[:40]
    return any(w in head for w in NOT_MENDAN)


def guess_staff(title, text):
    """タイトル（例: '07-28 面談：重野'）からスタッフ名を判定。
    見つからなければ空文字を返し、呼び出し側でスキップする（推測で書かない）。"""
    head = title + "\n" + "\n".join(text.splitlines()[:3])
    for key in sorted(STAFF_MAP.keys(), key=len, reverse=True):
        if key in head:
            return STAFF_MAP[key]
    return ""


def pick_url_name(r):
    """投函箱の行から (URL, スタッフ名) を取り出す。列の順番は決め打ちしない。
    2026-08-18: 投函箱の見出しが「スタッフ名｜リンク」の並びだったため、A列＝URL前提の
    読み方だと全行がURLでないと判定され、7/28以降の11件が無言で取り込まれていなかった。
    どちらの列に貼られても拾えるようにして、同じ止まり方を二度させない。"""
    cells = [(c or "").strip() for c in (r or [])[:2]]
    url = next((c for c in cells if c.startswith("http")), "")
    name = next((c for c in cells if c and not c.startswith("http")), "")
    return url, name


def sync_from_inbox(service):
    """シノの投函箱に貼られたURLを、院長スプシの取り込みタブへ転記する。
    投函箱には面談の中身を書き戻さない（シノが他スタッフの記録を見られないようにするため、
    成否だけを返す）。"""
    try:
        inbox = service.spreadsheets().values().get(
            spreadsheetId=INBOX_SHEET_ID,
            range=f"{INBOX_TAB}!A{INBOX_FIRST_ROW}:C").execute().get("values", [])
    except Exception as e:
        print(f"  投函箱を読めませんでした: {e}")
        return
    if not inbox:
        return
    cur = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"{IMPORT_TAB}!A4:A").execute().get("values", [])
    have = {r[0].strip() for r in cur if r and r[0].strip()}

    add, marks = [], []
    for i, r in enumerate(inbox):
        url, name = pick_url_name(r)
        if not url:
            continue
        if url in have:
            if not (len(r) > 2 and r[2].strip()):
                marks.append((i + INBOX_FIRST_ROW, "受け取りました"))
            continue
        add.append([url, name])
        have.add(url)
        marks.append((i + INBOX_FIRST_ROW, "受け取りました"))

    if add:
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, range=f"{IMPORT_TAB}!A4",
            valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
            body={"values": add}).execute()
        print(f"  投函箱から{len(add)}件を受け取りました")
    if marks:
        service.spreadsheets().values().batchUpdate(spreadsheetId=INBOX_SHEET_ID, body={
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": f"{INBOX_TAB}!C{n}", "values": [[m]]} for n, m in marks]}).execute()


def writeback_to_inbox(service):
    """取り込み結果を投函箱のC列へ返す。シノが自分で直せるように、
    「名前が分かりません」だけは具体的に伝える。面談の中身は返さない。"""
    try:
        inbox = service.spreadsheets().values().get(
            spreadsheetId=INBOX_SHEET_ID,
            range=f"{INBOX_TAB}!A{INBOX_FIRST_ROW}:C").execute().get("values", [])
        done = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"{IMPORT_TAB}!A4:D").execute().get("values", [])
    except Exception as e:
        print(f"  投函箱への結果反映をスキップ: {e}")
        return
    status = {r[0].strip(): (r[3] if len(r) > 3 else "") for r in done if r and r[0].strip()}
    data = []
    for i, r in enumerate(inbox):
        url, _ = pick_url_name(r)
        if not url:
            continue
        s = status.get(url, "")
        if not s:
            continue
        if s.startswith("✅"):
            msg = "✅ 記録しました"
        elif "スタッフ名" in s:
            msg = "⚠ 誰の面談か分かりませんでした→B列に氏名を書いてください"
        elif s.startswith("対象外"):
            msg = "対象外（面接・セミナー・会議は記録しません）"
        elif s.startswith("済"):
            msg = "✅ 記録済み"
        else:
            msg = "⚠ 読み取れませんでした（リンクが無効か期限切れかもしれません）"
        if (len(r) > 2 and r[2].strip()) != msg:
            data.append({"range": f"{INBOX_TAB}!C{i + INBOX_FIRST_ROW}", "values": [[msg]]})
    if data:
        service.spreadsheets().values().batchUpdate(spreadsheetId=INBOX_SHEET_ID, body={
            "valueInputOption": "USER_ENTERED", "data": data}).execute()
        print(f"  投函箱に結果を{len(data)}件返しました")


# ==============================
# 見張り（静かに止まっていることを検知する）
# ==============================
# 2026-08-18に、投函箱を1件も読めていない状態が3週間気づかれずに続いた。
# 「入口が空だから0件」と「入口を読めていないから0件」は画面上まったく同じに見えるため、
# 0件そのものではなく「投函箱に置かれたのにBotが触れていない行」を異常として見る。
WATCH_CELL = f"{IMPORT_TAB}!D1"   # 見張りの控え（最後に知らせた日と内容）。Botが使う欄
STALE_DAYS = 30                   # 投函箱に行があるのに、これだけ取り込みが無ければ知らせる
RENOTIFY_DAYS = 7                 # 同じ状態が続くとき、何日おきに念押しするか
LW_FAIL_BOT_ID = "12789558"       # 失敗通知Bot（毎日の完了報告には混ぜない）


def _load_watch_state(service):
    """前回知らせた内容と日付を読む。無ければ空。"""
    try:
        v = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=WATCH_CELL).execute().get("values", [])
        cell = v[0][0] if v and v[0] else ""
    except Exception:
        cell = ""
    if "|" not in cell:
        return "", None
    day, _, sig = cell.partition("|")
    try:
        return sig, datetime.strptime(day.strip()[-10:], "%Y-%m-%d").date()
    except Exception:
        return sig, None


def _save_watch_state(service, sig):
    today = datetime.now(JST).strftime("%Y-%m-%d")
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID, range=WATCH_CELL, valueInputOption="RAW",
        body={"values": [[f"見張りの控え {today}|{sig}"]]}).execute()


def watchdog(service):
    """投函箱の滞留と、取り込みが長く止まっていないかを見て院長DMに知らせる。
    異常が無ければ何も送らない。"""
    # 見張りそのものが届くかを確かめるための1回きりの送信。
    # 本番の文面は使わない（整形の途中で「テスト」が消えて本物の警報に見えないように）。
    if os.environ.get("WATCH_TEST") == "1":
        notify_shincho(
            "🧪【これはテストです】シノ面談の取り込み・見張り\n\n"
            "見張り機能を追加したので、通知が届くかだけを確かめています。\n"
            "異常は起きていません。このメッセージは無視して大丈夫です。",
            bot_id=LW_FAIL_BOT_ID)
        print("  見張り: テスト通知を送信しました")
        return

    try:
        inbox = service.spreadsheets().values().get(
            spreadsheetId=INBOX_SHEET_ID,
            range=f"{INBOX_TAB}!A{INBOX_FIRST_ROW}:C").execute().get("values", [])
        done = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"{IMPORT_TAB}!A4:D").execute().get("values", [])
    except Exception as e:
        print(f"  見張りをスキップ: {e}")
        return

    unreadable, untouched = [], []
    for i, r in enumerate(inbox):
        cells = [(c or "").strip() for c in (r or [])[:3]]
        if not any(cells[:2]):
            continue
        row_no = i + INBOX_FIRST_ROW
        url, _ = pick_url_name(r)
        if not url:
            unreadable.append(row_no)      # リンクとして読めない＝貼り方か列の並びが変わった
        elif len(cells) < 3 or not cells[2]:
            untouched.append(row_no)       # Botが結果を書けていない＝転記できていない

    # 最後にBotが何かを処理した日（✅でも対象外でもよい。動いた証拠として見る）
    last_day = None
    for r in done:
        stamp = (r[2].strip() if len(r) > 2 else "")[:10]
        try:
            d = datetime.strptime(stamp, "%Y-%m-%d").date()
        except Exception:
            continue
        if last_day is None or d > last_day:
            last_day = d
    today = datetime.now(JST).date()
    idle = (today - last_day).days if last_day else None
    has_inbox = any(any((c or "").strip() for c in (r or [])[:2]) for r in inbox)
    stale = bool(has_inbox and idle is not None and idle >= STALE_DAYS)

    if not (unreadable or untouched or stale):
        print("  見張り: 異常なし")
        return

    lines = []
    if unreadable:
        heads = ", ".join(str(n) for n in unreadable[:10])
        lines.append(f"・リンクとして読めない行が{len(unreadable)}件（投函箱の {heads} 行目）")
        lines.append("　貼り方か列の並びが変わっているかもしれません")
    if untouched:
        heads = ", ".join(str(n) for n in untouched[:10])
        lines.append(f"・Botが受け取れていない行が{len(untouched)}件（投函箱の {heads} 行目）")
    if stale:
        lines.append(f"・最後に取り込んだのは {last_day} で、{idle}日ぶん動いていません")

    sig = f"u{len(unreadable)}/t{len(untouched)}/s{int(stale)}"
    prev_sig, prev_day = _load_watch_state(service)
    if sig == prev_sig and prev_day and (today - prev_day).days < RENOTIFY_DAYS:
        print(f"  見張り: 前回と同じ状態のため通知は見送り（{sig}）")
        return

    msg = ("【シノ面談の取り込み・見張り】止まっているかもしれません\n\n"
           + "\n".join(lines)
           + f"\n\n投函箱: https://docs.google.com/spreadsheets/d/{INBOX_SHEET_ID}/edit"
           + f"\n取り込みタブ: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")
    if os.environ.get("NO_NOTIFY") == "1":
        print("  [NO_NOTIFY] 送らずに表示のみ:\n" + msg)
    else:
        notify_shincho(msg, bot_id=LW_FAIL_BOT_ID)
    _save_watch_state(service, sig)


def main():
    print(f"シノ面談 取り込みBot 開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")
    service = get_sheets_service()
    ensure_import_tab(service)

    # ① シノの投函箱に貼られたURLを院長スプシの取り込みタブへ転記（未転記分のみ）
    sync_from_inbox(service)

    rows = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"{IMPORT_TAB}!A4:D").execute().get("values", [])
    todo = [(i + 4, r) for i, r in enumerate(rows)
            if r and r[0].strip().startswith("http") and not (len(r) > 2 and r[2].strip())]
    print(f"未取り込みのURL: {len(todo)}件")
    if not todo:
        writeback_to_inbox(service)   # 新規が無くても、前回分の結果は投函箱へ返す
        watchdog(service)
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
            if is_not_mendan(title):
                results.append((row_no, "対象外（面接・セミナー・会議のため記録しません）"))
                continue
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
    writeback_to_inbox(service)   # シノにも結果を返す（中身は返さない）

    print(f"完了: 新規{len(added)}件")
    if added:
        msg = "【シノ面談の取り込み】新しい面談を記録しました\n\n" + "\n".join(f"・{a}" for a in added)
        msg += f"\n\nhttps://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
        notify_shincho(msg)

    watchdog(service)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""軸MTG 後追い追記ツール（手動実行のみ）

軸MTG Bot が拾えなかった回を、あとから「軸MTG要約集」Googleドキュメントへ追記する。
LINEワークスへは投稿しない（ドキュメントに入れ直すだけの道具）。

拾えなくなる主なケース:
  A) 院長が欠席し、録音が他の人のPLAUDアカウントにある → 院長のAPIからは見えない
     → 共有URL（SHARE_URL）を渡す。共有ページの本文を読んで追記する
  B) Botの実行日と録音日がずれた（開催曜日の変更など）
     → TARGET_DATE を渡す。院長のPLAUDからその日の軸MTGを探して追記する

環境変数:
  SHARE_URL   … Aの場合。PLAUDの共有URL
  TARGET_DATE … Bの場合。YYYY-MM-DD
  ENTRY_DATE  … ドキュメントの見出しに使う日付（省略時は録音日）
  NOTE        … 本文の最後に付ける注記（PLAUDの自動要約の誤りを断る、など）
  REPLACE     … 1 にすると、同じ日付の項目がすでにあっても消して入れ直す（取り込み失敗のやり直し用）
  GOOGLE_DOCS_ID / GOOGLE_CREDENTIALS_JSON … 軸MTG Bot と同じ
  PLAUD_TOKEN … Bの場合のみ必要
"""
import os, re, sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from plaud_bot import (JST, append_to_google_docs, find_latest_jiku_mtg,
                       get_file_detail, get_file_summary, get_note_ids, get_share_url)


# 共有ページを読むための道具（シノ面談の取り込みと同じ作り。iframeの中に本文がある）
def fetch_share_page(url):
    from shino_import import fetch_share_page as _f
    return _f(url)


# クッキー同意バナーは実行環境の言語で英語になることがある（GitHub Actionsは英語）。
# shino_import の除去リストは日本語だけなので、英語ぶんはここで落とす。
EN_BANNER = ("We value your privacy", "Reject All", "Accept All", "Manage", "Cookies",
             "Cookie", "Privacy Policy", "Store and/or access", "Personalised",
             "Vendor", "consent", "Consent")


def parse_share(text):
    """共有ページの本文から「見出し用の日付・タイトル・本文」を取り出す。

    本文の目印は `日時: YYYY-MM-DD HH:MM:SS` の行で、タイトルはその1行前。
    画面上部（タイトル／録音時間／タブ名）とクッキー同意バナーは、この目印より前なので
    まとめて落とせる。目印が無い要約もあるので、その場合はタブ「要約」の次から拾う。
    末尾には画面下部のナビ（各セクション名の再掲＋マインドマップ）が付くのでこれも落とす。"""
    lines = [l for l in text.splitlines() if not any(w in l for w in EN_BANNER)]

    start, title = 0, ""
    for i, l in enumerate(lines):
        if re.match(r"^日時[:：]", l.strip()) and i > 0:
            start, title = i - 1, lines[i - 1].strip()
            break
    if not title:                                  # 目印が無い要約のとき
        for i, l in enumerate(lines[:15]):
            if l.strip() == "要約":                 # タブの並びの最後。本文はこの次から
                start = i + 1
        title = lines[start].strip() if start < len(lines) else ""

    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", "\n".join(lines[start:start + 4]) or text)
    date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else datetime.now(JST).strftime("%Y-%m-%d")

    end = len(lines)
    for i, l in enumerate(lines):
        if l.strip() == "マインドマップ":
            end = i
            break
    NAV = ("提案", "トピック", "ハイライト", "重要ポイント", "テーマ", "Action Items", "要約")
    while end > start and (lines[end - 1].strip() in NAV or lines[end - 1].strip().endswith("...")):
        end -= 1
    return date, title, "\n".join(lines[start:end]).strip()


def find_entry_range(doc, entry_date):
    """ドキュメント内の「その日付の項目」の範囲（開始・終了インデックス）を返す。
    1項目は『区切り線 → 日付＋タイトル → 区切り線 → 本文』の並び。見つからなければ None。"""
    paras = []
    for el in doc["body"]["content"]:
        p = el.get("paragraph")
        if not p:
            continue
        t = "".join(r.get("textRun", {}).get("content", "") for r in p.get("elements", []))
        paras.append((el["startIndex"], el["endIndex"], t.strip()))

    heads = [i for i, (_, _, t) in enumerate(paras) if re.match(r"^20\d{2}-\d{2}-\d{2}\s\s", t)]
    for n, i in enumerate(heads):
        if not paras[i][2].startswith(entry_date):
            continue
        # 見出しの1つ前の区切り線から。その前の空行も一緒に消す
        s = i - 1
        while s > 0 and (set(paras[s - 1][2]) == {"="} or paras[s - 1][2] == ""):
            s -= 1
        # 次の見出しの手前まで（無ければ本文の最後まで）
        if n + 1 < len(heads):
            e = heads[n + 1] - 1
            while e > i and paras[e - 1][2] == "":
                e -= 1
            return paras[s][0], paras[e][0]
        return paras[s][0], doc["body"]["content"][-1]["endIndex"] - 1
    return None


def main():
    share_url = os.environ.get("SHARE_URL", "").strip()
    target_date = os.environ.get("TARGET_DATE", "").strip()
    if not share_url and not target_date:
        print("ERROR: SHARE_URL か TARGET_DATE のどちらかを指定してください")
        sys.exit(1)

    if share_url:
        print(f"共有URLから取り込みます: {share_url[:60]}...")
        text = fetch_share_page(share_url)
        if len(text) < 200:
            print(f"ERROR: 共有ページを読めませんでした（{len(text)}文字）。URLが無効か期限切れの可能性")
            sys.exit(1)
        rec_date, title, body = parse_share(text)
        if "軸" not in title and "MTG" not in title and "ミーティング" not in title:
            print(f"WARNING: 軸MTGらしくないタイトルです → {title}")
        if not title.startswith("軸MTG"):
            title = f"軸MTG{title}"
    else:
        print(f"PLAUDから取り込みます: {target_date}")
        os.environ["TARGET_DATE"] = target_date
        file_id, title = find_latest_jiku_mtg()
        if not file_id:
            print(f"ERROR: {target_date} の軸MTGファイルがPLAUDに見つかりません")
            sys.exit(1)
        detail = get_file_detail(file_id)
        body = get_file_summary(detail)
        if not body:
            print("ERROR: 要約がまだ生成されていません")
            sys.exit(1)
        share_url = get_share_url(file_id, get_note_ids(detail))
        rec_date = target_date

    entry_date = os.environ.get("ENTRY_DATE", "").strip() or rec_date
    note = os.environ.get("NOTE", "").strip()
    if note:
        body += f"\n\n※注記: {note}"
    print(f"追記対象: {entry_date} / {title} / 本文{len(body)}文字")

    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_info(
        __import__("json").loads(os.environ["GOOGLE_CREDENTIALS_JSON"]),
        scopes=["https://www.googleapis.com/auth/documents"])
    docs = build("docs", "v1", credentials=creds).documents()
    doc_id = os.environ["GOOGLE_DOCS_ID"]

    # すでに入っている回は二重に追記しない。REPLACE=1 のときは古い方を消して入れ直す
    replace = os.environ.get("REPLACE", "").strip() in ("1", "true", "yes")
    found = find_entry_range(docs.get(documentId=doc_id).execute(), entry_date)
    if found:
        if not replace:
            print(f"すでに追記済みのためスキップします: {entry_date}"
                  "（入れ直したいときは REPLACE=1 を付けて実行）")
            return
        s, e = found
        docs.batchUpdate(documentId=doc_id, body={"requests": [
            {"deleteContentRange": {"range": {"startIndex": s, "endIndex": e}}}]}).execute()
        print(f"既存の {entry_date} の項目を削除しました（{e - s}文字）")

    # 見出しの日付を録音日にするため、append_to_google_docs の「今日」を差し替える
    import plaud_bot
    class _FixedDate:
        @staticmethod
        def now(tz=None):
            return datetime.strptime(entry_date, "%Y-%m-%d").replace(tzinfo=JST)
    orig = plaud_bot.datetime
    plaud_bot.datetime = _FixedDate
    try:
        append_to_google_docs(title, body, share_url)
    finally:
        plaud_bot.datetime = orig
    print(f"完了: {entry_date} を軸MTG要約集に追記しました")


if __name__ == "__main__":
    main()

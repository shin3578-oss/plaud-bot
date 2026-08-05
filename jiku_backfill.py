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


def parse_share(text):
    """共有ページの本文から「見出し用の日付・タイトル・本文」を取り出す。
    先頭3行は画面の見出し（タイトル／日時／録音時間）、続く3行はタブ名なので落とす。
    末尾には画面下部のナビ（各セクション名の再掲＋マインドマップ）が付くので落とす。"""
    lines = text.splitlines()
    title = lines[0].strip() if lines else ""
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", "\n".join(lines[:4]))
    date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else datetime.now(JST).strftime("%Y-%m-%d")

    start = 0
    for i, l in enumerate(lines[:12]):
        if l.strip() == "要約":            # タブの並びの最後。本文はこの次から
            start = i + 1
    end = len(lines)
    for i, l in enumerate(lines):
        if l.strip() == "マインドマップ":
            end = i
            break
    NAV = ("提案", "トピック", "ハイライト", "重要ポイント", "テーマ", "Action Items", "要約")
    while end > start and (lines[end - 1].strip() in NAV or lines[end - 1].strip().endswith("...")):
        end -= 1
    return date, title, "\n".join(lines[start:end]).strip()


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

    # すでに入っている回を二重に追記しない
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_info(
        __import__("json").loads(os.environ["GOOGLE_CREDENTIALS_JSON"]),
        scopes=["https://www.googleapis.com/auth/documents"])
    doc = build("docs", "v1", credentials=creds).documents().get(
        documentId=os.environ["GOOGLE_DOCS_ID"]).execute()
    existing = "".join(r.get("textRun", {}).get("content", "")
                       for el in doc["body"]["content"]
                       for r in el.get("paragraph", {}).get("elements", []))
    if f"{entry_date}  {title[:16]}" in existing:
        print(f"すでに追記済みのためスキップします: {entry_date}")
        return

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

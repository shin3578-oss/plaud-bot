# -*- coding: utf-8 -*-
"""clinic_calendar.py — 休診日の判定（公開カレンダーを読むだけの軽い版）

【なぜ要るか】
2026-08-12（お盆の水曜・休診）に朝練Botが「朝練ファイルが見つかりません」を
院長DMへ誤送信した。休診日は朝練も面談もしないので、そもそも動く必要がない。

【休診日の正本】
アポツール /user/shifts/view の rest-all（日曜・祝日・お盆・年末年始・臨時休診を全部含む）。
**お盆・年末年始・臨時休診は「祝日」ではない**ので、jpholiday の祝日判定だけでは絶対に拾えない。

apotool-automation が毎日2回（5:30 / 21:00）その rest-all を3ヶ月ぶん書き出し、
ダッシュボードの公開リポジトリへ置いている。ここはそのJSONを読むだけ＝
ログイン不要・ブラウザ不要・シークレット不要（0.2秒）。

  https://shin3578-oss.github.io/kds-dashboard/closed_days.json
  （中身は休診日の日付だけ。患者情報・認証情報は一切入らない）

【読めなかったとき】
日曜・祝日だけで判定し、それ以外は**診療日として続行**する。
誤スキップで自動化を黙って止めるほうが危険、という全スクリプト共通の安全側の方針。
"""
import os
from datetime import datetime, timezone, timedelta, date

import requests

JST = timezone(timedelta(hours=9))

CALENDAR_URL = "https://shin3578-oss.github.io/kds-dashboard/closed_days.json"
CALENDAR_URL_FALLBACK = ("https://raw.githubusercontent.com/shin3578-oss/"
                         "kds-dashboard/main/closed_days.json")

# 1ヶ月に休診日が2日以下＝取得か書き出しが壊れている可能性。その月は判定に使わない
#（apotool 側の apotool_closed_reason と同じ安全側の考え方）
MIN_CLOSED_PER_MONTH = 3

_cache = None


def fetch_closed_days(force=False) -> set:
    """公開カレンダーを読んで休診日の date 集合を返す。読めなければ空集合。"""
    global _cache
    if _cache is not None and not force:
        return _cache
    for url in (CALENDAR_URL, CALENDAR_URL_FALLBACK):
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            data = r.json()
            days = {datetime.strptime(s, "%Y-%m-%d").date() for s in data.get("days", [])}
            print(f"[休診カレンダー] {len(days)}日を取得（更新 {data.get('updated', '不明')}）")
            _cache = days
            return days
        except Exception as e:
            print(f"[休診カレンダー] {url} が読めませんでした: {e}")
    _cache = set()
    return _cache


def _holiday_name(d):
    """祝日名。jpholiday が入っていない環境でも落とさない。"""
    try:
        import jpholiday
        return jpholiday.is_holiday_name(d)
    except Exception:
        return None


def closed_reason(d=None):
    """d が休診日なら理由の文字列を、診療日なら None を返す（d 未指定＝JSTの今日）。"""
    if d is None:
        d = datetime.now(JST).date()
    if isinstance(d, str):
        d = datetime.strptime(d, "%Y-%m-%d").date()
    if isinstance(d, datetime):
        d = d.date()

    if d.weekday() == 6:
        return "日曜（定休）"

    days = fetch_closed_days()
    same_month = [x for x in days if (x.year, x.month) == (d.year, d.month)]
    if len(same_month) >= MIN_CLOSED_PER_MONTH:
        # カレンダーが信頼できる月＝これが正本。祝日判定は見ない（rest-all に含まれている）
        return "休診日（アポツールのシフト）" if d in days else None

    # カレンダーが読めなかった / その月が入っていない → 祝日だけ拾って、あとは診療日扱い
    print(f"[休診カレンダー] {d.year}/{d.month:02d} のデータが{len(same_month)}日のみ "
          f"→ 祝日判定だけで続行します（誤スキップを避けるため）")
    name = _holiday_name(d)
    return f"祝日（{name}）" if name else None


def is_closed(d=None) -> bool:
    return closed_reason(d) is not None


def is_first_open_day_of_week(d=None) -> bool:
    """d が「その週（月曜起点）で最初の診療日」かどうか。

    週1回のタスクを『月曜が休診なら翌診療日にずらす』ために使う。
    月曜〜前日がすべて休診で、d 自身が診療日なら True。
    通常の週は月曜だけが True になる。
    """
    if d is None:
        d = datetime.now(JST).date()
    if closed_reason(d):
        return False
    monday = d - timedelta(days=d.weekday())
    day = monday
    while day < d:
        if not closed_reason(day):
            return False          # 今週すでに診療日があった＝その日に送信済みのはず
        day += timedelta(days=1)
    return True


if __name__ == "__main__":
    target = os.environ.get("TARGET_DATE") or datetime.now(JST).strftime("%Y-%m-%d")
    t = datetime.strptime(target, "%Y-%m-%d").date()
    print(f"{target}: {closed_reason(t) or '診療日'} / 週の初診療日={is_first_open_day_of_week(t)}")

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Google Sheets / Drive / 外部API の一時エラーを再試行する共通部品（2026-09-12）
=====================================================================
【なぜ作ったか】
2026-08-13〜09-12 の30日間で、Google Sheets API の一時エラー（503 Service Unavailable）だけで
本番の自動化が **8回** 落ちた（朝のアシスタント×2・翌日来院アラート・面談記録シートBot×3 ほか）。
再試行の関数 `sheets_retry()` は 2026-08-29 に monshin_funnel.py に作ってあったが、
使っていたのは apotool 52本中 8本・他リポジトリは 0本。
「瞬断はどの1行に当たるか選べない」ので、1本ずつ包むのではなく **入口で丸ごと守る** 形にした。

【使い方】3通り。既存コードの1行を置き換えるだけ。

  1) gspread を使うスクリプト（一番多い）
        from sheets_retry import gs_client
        gc = gs_client(creds)        # ← gspread.Client(auth=creds) / gspread.authorize(creds) の代わり
     これで gc から先の open_by_key / worksheet / get_all_values / update / append_rows … の
     **全部の呼び出し**が自動で再試行される（包み忘れが起きない）。

  2) googleapiclient（build("sheets"/"drive"/"tasks" …）を使うスクリプト
        ...execute()  →  ...execute(num_retries=3)
     googleapiclient 自身が 5xx / 429 を指数バックオフで再試行する（ライブラリの標準機能）。

  3) それ以外（requests で叩く外部API・PLAUD など）
        from sheets_retry import retry
        r = retry(lambda: requests.get(url, timeout=30), label="PLAUD一覧")

【決め事】
- 再試行するのは **一時的なもの**だけ: HTTP 408 / 429 / 500 / 502 / 503 / 504、接続断、タイムアウト。
  権限なし(403)・存在しない(404)・入力が悪い(400) は即座に投げる（直さないと直らないものを隠さない）。
- 待ち時間は 5秒 → 10秒 → 20秒（合計35秒・最大4回試す）。GitHub Actions の課金は
  ジョブ単位で1分切り上げなので、この程度の待ちは実質ゼロ円。
- 再試行するたびにログに1行出す（あとで「何回503が出たか」を数えられるように）。
"""
from __future__ import annotations

import time

TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}
DEFAULT_RETRIES = 4          # 試す回数（初回を含む）
DEFAULT_BASE_DELAY = 5       # 秒。5 → 10 → 20


def _status_of(exc) -> int | None:
    """例外から HTTP ステータスを取り出す（ライブラリごとに置き場所が違う）。"""
    # gspread.exceptions.APIError … .code（6.x）／ .response.status_code
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    resp = getattr(exc, "response", None)
    sc = getattr(resp, "status_code", None)
    if isinstance(sc, int):
        return sc
    # googleapiclient.errors.HttpError … .status_code（新）／ .resp.status（旧）
    sc = getattr(exc, "status_code", None)
    if isinstance(sc, int):
        return sc
    resp = getattr(exc, "resp", None)
    sc = getattr(resp, "status", None)
    if isinstance(sc, int):
        return sc
    return None


def is_transient(exc) -> bool:
    """この例外なら待って試し直す価値がある、と判定する。"""
    sc = _status_of(exc)
    if sc is not None:
        return sc in TRANSIENT_HTTP
    name = type(exc).__name__
    mod = type(exc).__module__ or ""
    if name in ("ConnectionError", "Timeout", "ConnectTimeout", "ReadTimeout",
                "ChunkedEncodingError", "RemoteDisconnected", "ProtocolError",
                "SSLError", "TimeoutError", "timeout"):
        return True
    if mod.startswith("requests") and name in ("HTTPError",):
        return False
    if isinstance(exc, (TimeoutError, ConnectionResetError, ConnectionAbortedError)):
        return True
    return False


def retry(fn, *args, label: str = "", retries: int = DEFAULT_RETRIES,
          base_delay: float = DEFAULT_BASE_DELAY, **kwargs):
    """fn(*args, **kwargs) を、一時エラーのあいだだけ待って試し直す。"""
    for attempt in range(1, retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 — 一時エラーかどうかは is_transient が判定する
            if attempt >= retries or not is_transient(e):
                raise
            wait = base_delay * (2 ** (attempt - 1))
            what = label or getattr(fn, "__name__", "") or "呼び出し"
            print(f"  [retry] 一時エラー {type(e).__name__}"
                  f"{'(' + str(_status_of(e)) + ')' if _status_of(e) else ''}"
                  f"（{what}）… {wait:.0f}秒後に再試行 ({attempt}/{retries - 1})", flush=True)
            time.sleep(wait)


# ── gspread: HTTPクライアントごと差し替える ─────────────────────────────────
try:
    import gspread
    from gspread.http_client import HTTPClient as _GsHTTPClient

    class RetryHTTPClient(_GsHTTPClient):
        """gspread の全リクエストを retry() に通す。

        gspread 同梱の BackOffHTTPClient は「production ready ではない」と自己申告しており、
        待ち時間が最長128秒まで伸びるので使わない。ここでは上限35秒・最大4回に固定する。
        """

        def request(self, *args, **kwargs):
            return retry(super().request, *args, label="Sheets API", **kwargs)

    def gs_client(creds, **kwargs):
        """gspread.Client(auth=creds) の代わり。再試行つきのクライアントを返す。"""
        try:
            return gspread.Client(auth=creds, http_client=RetryHTTPClient, **kwargs)
        except TypeError:
            # 古い gspread（5.x 以前）は http_client を受け付けない。
            # そのときは素のクライアントを返す（動かないよりは動く方を取る）。
            print("  [retry] この gspread は http_client を受け付けないため再試行なしで続行", flush=True)
            return gspread.Client(auth=creds, **kwargs)

except ImportError:  # gspread を使わないスクリプトから import されても落ちない
    RetryHTTPClient = None  # type: ignore[assignment]

    def gs_client(creds, **kwargs):  # type: ignore[misc]
        raise RuntimeError("gspread が入っていないので gs_client は使えない")


# ── requests: 再試行つきセッション ──────────────────────────────────────────
def requests_session(retries: int = 3, backoff: float = 2.0):
    """requests.Session に、一時エラー（429/5xx・接続断・読み取りタイムアウト）の再試行を仕込んで返す。

    requests.get(...) → session.get(...) に書き換えるだけで済む。
    POST も再試行する（このリポジトリが叩く外部API＝PLAUD・LINEワークスは、同じ内容の
    再送で二重登録にならないものだけ。二重送信が困る呼び出しにはこのセッションを使わない）。
    """
    import requests
    from requests.adapters import HTTPAdapter
    try:
        from urllib3.util.retry import Retry
    except ImportError:  # pragma: no cover
        from requests.packages.urllib3.util.retry import Retry  # type: ignore

    r = Retry(total=retries, connect=retries, read=retries, status=retries,
              backoff_factor=backoff,
              status_forcelist=sorted(TRANSIENT_HTTP),
              allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"]),
              raise_on_status=False)
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=r))
    s.mount("http://", HTTPAdapter(max_retries=r))
    return s


if __name__ == "__main__":
    # 動作確認: 2回 503 を返してから成功する関数を retry() に通す
    calls = {"n": 0}

    class _Fake(Exception):
        def __init__(self, code):
            super().__init__(f"fake {code}")
            self.code = code

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Fake(503)
        return "ok"

    print(retry(flaky, label="テスト", base_delay=0.01), "after", calls["n"], "calls")
    try:
        retry(lambda: (_ for _ in ()).throw(_Fake(403)), label="403", base_delay=0.01)
    except _Fake as e:
        print("403 は即座に投げる:", e)

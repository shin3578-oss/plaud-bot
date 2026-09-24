"""AIの返答からJSONを安全に読み取る共通部品（2026-09-21）。

きっかけ: 週次PDCA（analytics_pdca.py）で、Claudeが返したJSONに注釈が混ざっていて
`json.loads` が「Expecting ',' delimiter」で落ち、ジョブごと止まった。
同じ書き方（re.search(r"\\{.*\\}") → json.loads）が script_generator.py にもあったので、
直し漏れが出ないよう1か所にまとめた。
2026-09-23: 面談記録シートBot（mendan_sheets.py）が content[0] 決め打ち＋``` 剥がしだけで、
読めないと todos=[] で黙って通っていたため、同じものをここにも置いた（リポジトリが別のため複製）。

使い方:
    from llm_json import parse_json_loose, response_text
    data = parse_json_loose(response_text(message))
"""
import json
import re


def response_text(message) -> str:
    """Anthropicの返答からテキスト本文を取り出す。

    content[0] を決め打ちにしない（thinkingブロックが先頭に来る機種があるため）。
    途中で切れた返答は不完全なJSONになるので、その場で分かるようにエラーにする。
    """
    if getattr(message, "stop_reason", None) == "max_tokens":
        raise ValueError("AIの返答が max_tokens で途中で切れた（JSONが不完全）")
    texts = [b.text for b in message.content if getattr(b, "type", "") == "text"]
    if not texts:
        raise ValueError("AIの返答にテキストが入っていない")
    return texts[-1]


def extract_json(text: str) -> str:
    """本文から最初のJSONオブジェクトを丸ごと切り出す。

    正規表現（\\{.*\\}）は前後の説明文や ``` の囲みを巻き込むので、
    波かっこの対応を数えて切り出す（文字列の中のかっこは数えない）。
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("返答の中にJSONが見つからない")
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]  # 閉じかっこが無い＝途中で切れている（下のパースでエラーになる）


def _drop_comments(s: str) -> str:
    """// や /* */ のコメントを落とす（JSONには書けない）"""
    s = re.sub(r"(?<!:)//[^\n]*", "", s)
    return re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)


def _drop_annotations(s: str) -> str:
    """数値のうしろに付いた注釈を落とす（例: "implant": 1.5（維持） → 1.5）"""
    return re.sub(r"(:\s*-?\d+(?:\.\d+)?)\s*[（(][^)）]*[)）]", r"\1", s)


def _drop_trailing_commas(s: str) -> str:
    """閉じかっこ直前の余分なカンマを落とす"""
    return re.sub(r",(\s*[}\]])", r"\1", s)


def _ascii_punct(s: str) -> str:
    """全角のカンマ・コロンを半角にする（最後の手段。本文の字面が少し変わる）"""
    return s.translate(str.maketrans("，：", ",:"))


def parse_json_loose(text: str, label: str = "JSON") -> dict:
    """AIの返答をJSONとして読む。汚れを1段ずつ落としながら試し、読めた時点で返す。

    どうしても読めないときは、何が返ってきたのかログに全文を残してから
    ValueError（先頭400字つき＝通知に載る）を投げる。黙って空dictは返さない。
    """
    candidate = extract_json(text)
    errors = []
    for repair in (lambda s: s, _drop_comments, _drop_annotations,
                   _drop_trailing_commas, _ascii_punct):
        candidate = repair(candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            errors.append(str(e))
    print(f"--- AIの返答（{label}として解析できなかった全文） ---")
    print(text)
    print("--- ここまで ---")
    raise ValueError(f"{label}の解析に失敗（{errors[0]}）: {text[:400]}")

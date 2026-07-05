# PLAUD議事録Bot（plaud-bot）

PLAUD（AIボイスレコーダー）の録音要約を、LINEワークスに自動投稿するボット2つです。

## ボット一覧

| 名前 | ファイル | いつ動くか | 何をするか |
|---|---|---|---|
| 朝練Bot | `asaren_bot.yml` | 水〜土 10:00（Cron-job.org「朝練Bot」ジョブが起動） | 朝練の録音要約をLINEワークス歯科医師チャンネルに投稿 |
| 軸MTG Bot | `jiku_bot.yml` | 毎週水曜 21:00（Cron-job.org「軸MTG Bot」ジョブが起動） | 軸ミーティングの議事録要約をLINEワークスに投稿 |

## エラーが起きたら

- GitHubの「Actions」タブで❌赤印の実行を確認
- Claude Codeに「朝練Botのログを見て」などと頼めば調査します

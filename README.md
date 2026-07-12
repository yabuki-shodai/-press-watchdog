# -press-watchdog

<!-- press-watchdog:today:start -->
## 本日のレポート

- [2026-07-12](docs/2026-07-12.md)
<!-- press-watchdog:today:end -->

## Overview

暗号資産交換業者の公式サイト・お知らせページを監視し、新規リンクを日次レポートとして出力します。

## Optional AI Summary

AI要約は任意です。未設定の場合は通常の新着リンク一覧だけを出力します。

有効化する場合はRepository Variables / Secretsに以下を設定します。

- Repository Variable: `AI_SUMMARY_ENABLED=true`
- Repository Secret: `AI_API_KEY`
- Repository Variable: `AI_API_URL` 任意。未設定時はOpenAI互換のデフォルトURLを使用
- Repository Variable: `AI_MODEL` 任意。未設定時は `gpt-4o-mini`

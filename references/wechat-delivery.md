# WeChat delivery

Use this optional Windows-only delivery for successful initial and T-30 analyses when the user has explicitly enabled it for one exact conversation or group.

## Configure once

1. Install the guarded 4.1+ UI automation dependency: `python -m pip install pywechat127`.
2. Probe the logged-in account before configuring delivery. Current WeChat 4.1+ may hide its UI Automation tree per account; launching Windows Narrator no longer restores it. Do not repeatedly restart WeChat or Narrator when the main window is not exposed as `mmui::MainWindow`.
3. Bring WeChat to the foreground and select the exact user-approved conversation or group.
4. Visually confirm the chat header and group toolbar. Never configure from a guessed or partial search match. For a group, confirm the full displayed name; the saved signatures intentionally exclude the member count and mosaic avatar so adding members does not disable delivery.
5. Capture the target signatures:

```powershell
python <skill-dir>/scripts/wechat_push.py configure --config <workspace>/.codex/soccer-predict/wechat_push.json --chat-name "<exact-name>" --confirmed-chat-name "<exact-name>"
```

The workspace configuration and signature images are user-specific local state. Do not put them in a public repository or prediction history.

Run one `test:<token>` delivery with `--verify-draft-only` before enabling unattended delivery. It must stage the exact text and clear it without sending. If RPA UIA is hidden or legacy paste cannot confirm the draft, set the workspace configuration to `enabled: false`; do not promise WeChat delivery or keep retrying. Reconfigure after the WeChat name, theme, DPI, or major window layout changes. A member-count or group-avatar change alone does not require reconfiguration. The script briefly brings WeChat to the foreground. It does not use a bot account, unofficial protocol, DLL injection, or uploaded credentials.

## Message format

Use deliberately formatted WeChat plain text. Do not paste Codex Markdown, HTML, tables, probability bars, or the visualization artifact. Use short labeled lines, for example:

Generate the copy text from the archived record whenever possible:

```text
python <skill-dir>/scripts/wechat_formatter.py --base-dir <workspace> --match-id <id> --kind initial
python <skill-dir>/scripts/wechat_formatter.py --base-dir <workspace> --match-id <id> --kind lineup-check
python <skill-dir>/scripts/wechat_formatter.py --base-dir <workspace> --match-id <id> --kind review
```

Always append the matching output to the Codex result under `微信可复制版`, even when `wechat_push.json` is absent or disabled. Keep the copy body exactly as plain text: no Markdown headings, bullets, blockquotes, code-fence markers, HTML, tables, or visualization syntax. Initial and lineup copies may also be passed to the guarded sender when it is enabled. Review copies are manual-only and must never be passed to `wechat_push.py`.

```text
【初盘分析｜<match_id>】
比赛：<home> vs <away>
开赛：<local time>
主推：<selection>｜EV <value>
次选：<selection or 无>
半场：<selection or 观察>
半全场：<two candidates>
比分参考：<score 1>、<score 2>
风险：<one concise sentence>
仅供数据分析参考
```

For T-30, start with `【临场分析｜<match_id>】` and include `检查时间：`, `比赛状态：`, plus `主推维持：` or `主推变更：` before the current `主推：` line.

For reviews, start with `【赛后复盘｜<league_key>｜<match_id>】`. Include the verified half-time and full-time scores, `结算依据：临场版最终有效推荐` or `初盘版最终有效推荐`, the primary settlement, all active formal settlements, exact-score diagnostic, causal learning, league primary record, and cumulative primary record. Do not include superseded initial picks as official results.

## Deliver once

Create one compact plain-text summary. Include match ID and teams, check time and explicit match status, `主推维持` or `主推变更`, the active primary pick, its current EV/edge when available, and the two exact-score references. Encode it as UTF-8 Base64, then run:

```powershell
python <skill-dir>/scripts/wechat_push.py send --config <workspace>/.codex/soccer-predict/wechat_push.json --event-key initial:<match_id> --message-b64 <utf8-base64> --backend rpa --send
python <skill-dir>/scripts/wechat_push.py send --config <workspace>/.codex/soccer-predict/wechat_push.json --event-key lineup-check:<match_id> --message-b64 <utf8-base64> --backend rpa --send
```

The RPA backend first performs exact visual target verification, then requires the verified WeChat window to expose the UIA editor. It stages the exact text without using the IME or clipboard, confirms it, records the event key, sends once, and confirms the editor cleared. If the account's UI tree is hidden it fails closed. Windows Narrator is not a recovery method on current WeChat 4.1+. `scripts/wechat_push.ps1` and `--backend legacy` are diagnostic fallbacks only and must not be used by unattended match tasks unless a same-session draft-only test has passed.

The script searches the configured exact name, compares the selected header and identity area with the confirmed local signatures, refuses to touch an existing draft, and records the event key before clicking Send. It blocks duplicate or uncertain retries.

Treat delivery as a secondary channel:

- Always complete the visible Codex lineup-check task even if WeChat is closed or verification fails.
- Report `微信已推送` only when the script returns `sent: true`.
- On any failure, report `微信未推送` with the reason. Do not retry, choose another result, or send to another conversation.
- Push only initial and T-30 analyses. Do not accept or push post-match reviews under this configuration.
- Never include secrets, raw files, or private browsing data in the message.

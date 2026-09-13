# Codex reset monitor

Check AIHOT's public Codex reset API every five minutes and send new announcements or confirmations to a configured Feishu group through the official Lark CLI. No language model or local computer is needed.

## Operation

- Uses standard GitHub-hosted Linux runners in a public repository. Scheduled execution can be delayed or skipped by GitHub; this is not a real-time service.
- Source: https://aihot.news/api/v1/codex-resets . AIHOT and original authors retain rights to the source content. The repository does not publish an API mirror.
- Baselines existing history without broadcasting it. Distinguishes global resets from reset credits, announcements from confirmations, and historical backfills from new posts.
- Repository secrets: `LARK_APP_ID`, `LARK_APP_SECRET`, `LARK_CHAT_ID`, and a Fernet `STATE_KEY`. Only bot credentials are used; never upload personal OAuth credentials.
- Set repository variable `DEPLOYMENT_REPOSITORY` to the exact deployment repository name. Forks and non-main dispatches do not send messages.
- `state.enc` contains authenticated encrypted checkpoints, pending deliveries, and receipts. The decryption key must stay in Secrets. Initial state must be explicitly provisioned; missing/corrupt state never silently resets deduplication.
- Saves pending intent before sending and receipts before readback. Uncertain sends retry with the same key only within 55 minutes; older uncertain attempts stop for manual reconciliation. Sent messages are never resent solely because readback failed.
- Meaningful state changes and daily check dates are committed automatically. GitHub disables public schedules after 60 days without repository activity; check workflow health if notifications stop.
- No plaintext artifacts or message bodies are written to workflow logs. Workflow failures use GitHub's normal notification mechanism.

## Validation

`python3 -m pip install -r requirements.txt` then `python3 -m unittest discover -s tests -v`.

To pause, disable the **Codex reset monitor** workflow in Actions. A successful manual run verifies execution; a naturally scheduled run separately verifies scheduling.

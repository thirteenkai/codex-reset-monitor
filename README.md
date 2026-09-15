# Codex reset monitor

Check AIHOT's public Codex reset API every five minutes and send new announcements or confirmations to a configured Feishu group through the official Lark CLI. No language model or local computer is needed.

## Operation

- Uses standard GitHub-hosted Linux runners in a public repository. The primary five-minute trigger is an external cron-job.org HTTP job; the GitHub schedule remains a fallback. Both use the same workflow concurrency group and encrypted delivery checkpoints. Dispatch and runner startup can still be delayed; this is not a real-time service.
- Source: https://aihot.news/api/v1/codex-resets . AIHOT and original authors retain rights to the source content. The repository does not publish an API mirror.
- Baselines existing history without broadcasting it. Distinguishes global resets from reset credits, announcements from confirmations, and historical backfills from new posts.
- Repository secrets: `LARK_APP_ID`, `LARK_APP_SECRET`, `LARK_CHAT_ID`, and a Fernet `STATE_KEY`. Only bot credentials are used; never upload personal OAuth credentials.
- Set repository variable `DEPLOYMENT_REPOSITORY` to the exact deployment repository name. Forks and non-main dispatches do not send messages.
- `state.enc` contains authenticated encrypted checkpoints, pending deliveries, and receipts. The decryption key must stay in Secrets. Initial state must be explicitly provisioned; missing/corrupt state never silently resets deduplication.
- Saves pending intent before sending and receipts before readback. Uncertain sends retry with the same key only within 55 minutes; older uncertain attempts stop for manual reconciliation. Sent messages are never resent solely because readback failed.
- Meaningful state changes and daily check dates are committed automatically. GitHub disables public schedules after 60 days without repository activity; check workflow health if notifications stop.
- No plaintext artifacts or message bodies are written to workflow logs. Workflow failures use GitHub's normal notification mechanism.

## External scheduling

- Configure cron-job.org to send a POST every five minutes to `https://api.github.com/repos/OWNER/REPO/actions/workflows/monitor.yml/dispatches`, replacing `OWNER/REPO` with the deployment repository. Use JSON body `{"ref":"main"}`, `Accept: application/vnd.github+json`, and `Content-Type: application/json`.
- Add an `Authorization: Bearer ...` header in the scheduler console using a dedicated fine-grained GitHub token scoped to this repository with Actions read/write and required Metadata read access. Do not reuse a broad personal token or place it in the URL, source files, or logs. Feishu credentials and the state encryption key stay in GitHub Secrets.
- Renew the dedicated token before its configured expiration. An expired token prevents new external dispatches. A successful scheduler response only confirms GitHub accepted the request; inspect the corresponding workflow run and polling result as well.
- External scheduled calls appear in GitHub as `workflow_dispatch`, including a “manually run” label. Use the scheduler history to distinguish automatic calls from a person pressing Run workflow.

## Validation

`python3 -m pip install -r requirements.txt` then `python3 -m unittest discover -s tests -v`.

To pause delivery immediately, disable the **Codex reset monitor** workflow in Actions. Also pause the external scheduler to prevent failed dispatch requests. Disabling only the external scheduler leaves the GitHub fallback schedule active. A successful manual run verifies execution; consecutive automatic calls in the scheduler history and corresponding completed GitHub runs separately verify scheduling.

## Official AIHOT hot topics

The same cloud trigger also runs `hot_topics.py` as a separate job after the reset check. A reset-check failure does not skip the hot-topics job. Delivery uses the existing bot and configured destination. No model or additional external credentials are needed.

- Use the official `/api/v1/hot-topics` selection and `rank` directly, with no additional topic, score, source-count, or business-relevance threshold. These are official rankings; notification deduplication is our client behavior, not an AIHOT push rule.
- Poll at most once per 300 seconds using ETag/If-None-Match; honor Retry-After on 429/503. Small scheduler jitter is absorbed by a short wait. The cached list is still checked against story timelines on 304 responses; a story update does not require a ranking change.
- Initialization and migration to the freshness policy silently baseline the current story timelines. Preserve existing receipts; cancel only unsent legacy drafts. A newly seen event must have a first report within 48 hours and a recent supporting report. Older events are recorded silently. Later progress requires both a changed AIHOT latest-progress summary and a previously unseen report published after the prior observation, within 48 hours. Mere ranking, count, timestamp, or summary-only edits do not notify. Missing timelines fail closed. These conservative freshness rules belong to this client, not AIHOT.
- Messages use the supporting report title and reading link, AIHOT latest-progress summary, event first-report time, and supporting-report publication time. They do not reuse a historical representative headline or label first detection as newly ranked. Story IDs, report IDs and item aliases deduplicate notifications, including redirects to merged stories. A new report plus a changed summary is an observable proxy for progress, not independent fact verification; delayed/backdated reports may be suppressed.
- Keep independent encrypted state in `hot-topics-state.enc`, bound to the bot and destination. Retain pending intent before delivery and the receipt before readback; verify sender, destination, and message content. Never silently replace missing or corrupt state.
- Initial setup: dispatch this workflow once with `initialize_hot_topics=true` while HOT_TOPICS_ENABLED is unset/false. This performs bot/chat checks and a dry-run, then saves the initial baseline without sending it. Only after success set repository variable `HOT_TOPICS_ENABLED=true`. Repeating initialization preserves an existing baseline. Subsequent normal five-minute dispatches poll automatically.
- Pause only hot topics by setting `HOT_TOPICS_ENABLED=false`; this leaves the reset monitor running. Token renewal is shared with the external scheduler described above.

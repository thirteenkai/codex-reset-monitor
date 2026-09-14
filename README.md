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

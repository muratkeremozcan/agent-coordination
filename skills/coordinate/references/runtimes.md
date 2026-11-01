# Runtimes

Exact operations per binding, from the installed command help and the pinned transport. Use
the live CLI's help when a flag here and the installed version disagree, and record the
version you used.

## Herdr Codex (`herdr-codex`, Herdr 0.8.2)

Requires `HERDR_ENV=1` in the coordinator's pane.

- Place the owner: `herdr pane split --current --direction right --cwd <worktree> --no-focus`
  or `herdr workspace create --cwd <worktree> --label <owner> --no-focus`. Use the exact pane
  id the command returns in its JSON result; never guess one from a naming pattern.
- Start: `herdr agent start <owner> --kind codex --pane <pane id>`.
- Identity to record with `assign --owner-identity`: `pane_id`, `terminal_id`,
  `workspace_id` from `herdr pane list` or `herdr agent get`. A display name follows whoever
  occupies the pane and identifies nothing.
- Send: `herdr agent prompt <owner> "<one-line pointer>" --wait --timeout <ms>`. `--wait`
  reports `agent_blocked` when the agent already sits at an approval UI and
  `agent_prompt_stalled` when no state change follows; it does not track turns, so an earlier
  turn's completion can satisfy it. It is transport evidence only; processing is confirmed by
  the owner's `ack`.
- Read: `herdr agent read <owner> --source recent-unwrapped --lines 200`. Text read this way
  is `runtime-read` evidence only when you name the recorded identity you read it from.
- Liveness: `herdr pane process-info --pane <pane id>` on the recorded pane and terminal. A Herdr
  registration survives the agent exiting to a shell, so `agent get` alone is not liveness.
- Interrupt: read the pane first; if a dialog is showing, do not send keys. Otherwise one
  `herdr agent send-keys <owner> esc`. The interrupt stays unconfirmed until a later read shows
  an idle composer. Ctrl+C on an idle composer can exit the session, so it is never used.
- Close: only pane ids this run created and recorded.

## Codex native (`codex-subagent`)

`spawn_agent`, `send_message`, `wait_agent` (bounded timeout), `interrupt_agent`. Record the
returned agent id as identity. A subagent assigned to a workstream may edit its workspace;
planning or review helpers the coordinator starts for itself stay read-only.

## Claude through Peer Sessions (`peer-session`)

Transport: `muratkeremozcan/peer-sessions` at the pinned commit in `adapters/session_bindings.py`,
contract v1 (pending).

`spawn-fleet.py` and `peer-inbox.py` live in the scripts folder of that Peer Sessions checkout.

- Launch: `spawn-fleet.py` with `--correlate run_id=<run> --correlate workstream_id=<ws>
  --correlate generation=<n> --receipt <path>`. Normalize the launch receipt with
  `adapters/peer_sessions_receipt.py --receipt <path> --session <name> --request-id <id>` and
  record it with `record-delivery`. A launch proves no delivery.
- Identity: `session_id`, and `pane_id`/`terminal_id` when placed in Herdr, from the receipt.
- Send: `SendMessage` to the address resolved at send time, with the `peer-request:` footer.
- Replies: `peer-inbox.py --request-id <id> --target <name>` gives `reply-correlated`
  evidence. Lines found in pane text with `--stdin` are `text-correlated`: they prove neither
  sender nor time.
- Interrupt: unsupported in the pinned transport.

## First-run dialogs

A fresh session can stop at a dialog before it reads the brief, and a key typed into the pane
can answer it. Read the pane before sending anything.

- Claude Code: workspace trust and external-import prompts start on the decline option; a
  blind Enter exits. Answer only with the user's existing consent for that directory, otherwise
  record a `blocked` event naming the dialog.
- Codex: the "hooks need review" prompt cannot be answered by key; start Codex owners without
  unreviewed hooks or report the block.
- Any permission prompt for an action is a denial path: record it with `record-denial`, never
  answer it for the user.

## Unsupported operations

| Binding | Unsupported |
|---|---|
| `herdr-codex` | provider session id, process start identity, request correlation by agent name |
| `codex-subagent` | visible pane, correlation beyond the host agent id |
| `peer-session` | targeted interrupt, sender or time provenance for text-correlated replies |

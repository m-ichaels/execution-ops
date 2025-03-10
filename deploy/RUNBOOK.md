# Runbook

The stack runs three systemd timers on the operations host (`deploy/*.timer`): start-of-day checks at 06:30 London, the
trading day from 07:30 London (both regions, US session included), and the end-of-day reconciliation at 22:15 London.
Logs are in `/var/log/xops/`, the store is `data/derived/xops.duckdb`, and every alert, check and break is a row in it.

## Start of day (`xops sod`)

| check | fail means | do |
|---|---|---|
| `calendar:<exchange>` | closed or early close | confirm the algo end times were shortened; nothing to send on a closed venue |
| `securities_master` | a held symbol is unknown | a symbol change or listing gap: add the mapping to `data/reference/corporate_actions_curated.csv` and rerun |
| `price_coverage` | held names without a close at the previous session | vendor gap: pull the missing bars before orders are sized off stale ADV |
| `unexplained_jumps` | a move above six sigma with no corporate action in the feed | check for a split, spin-off or rename the feed missed before the strategies trade off a phantom return |
| `corporate_actions` | events ex today on held names | positions and carried orders are adjusted automatically; confirm cash in lieu and the new lines |
| `position_limits` | a name above 5 % of AUM or 25 % of ADV | tell the portfolio manager before the day's orders add to it |
| `order_sanity` | an order above the ADV cap | should not happen: the cap is applied in `portfolio.build_orders`; stop the day and investigate |
| `futures_roll:<contract>` | inside the roll window or past first notice | roll today; a `fail` means the position is deliverable and must be closed before the session |
| `fix_sequence:<broker>` | sequence numbers to resume from | if the broker reset overnight, agree the reset before logon (`reset_on_logon`) |

## During the day (monitor alerts, by severity)

| alert | severity | meaning | action |
|---|---|---|---|
| `SESSION_LOST`, `SESSION_SILENT` | critical / high | no heartbeat from a broker; the engine reconnects and recovers the gap itself | if it does not recover in two minutes, call the broker's desk; orders keep working at the broker |
| `SEQ_GAP` | medium | a message was lost; ResendRequest sent | none unless it repeats (a flapping line) |
| `UNACKED` | high | no New within 10 s of sending | do not resend blindly (duplicate risk): query the order status with the broker first |
| `STATE_ANOMALY` | high | a report the state machine refused (duplicate ExecID, wrong side or symbol, overfill, leaves mismatch) or resynced | the blotter is authoritative only after the broker confirms; reconcile that order by hand before the close |
| `PRICE_COLLAR` | high | a fill more than 100 bp from the mid | ask the broker for a bust or correction; check the symbol and the market data |
| `PARTICIPATION`, `AHEAD_SCHEDULE` | high / medium | the algo is trading far faster than its cap or schedule | pause or replace the order with a lower participation rate |
| `STALLED`, `BEHIND_SCHEDULE`, `LEAVES_AFTER_END` | medium / low | the algo stopped or fell behind; leaves remain after the end time | check the broker's algo status; cancel and re-route the remainder |
| `SLIPPAGE` | medium | running average far outside the benchmark and the pre-trade estimate | look at the fills; pause if the pattern is broker-specific |
| `REJECT_RATE` | high | three or more rejects in five minutes from one broker | stop routing new orders there until the desk confirms the cause |
| `LATENCY` | medium | report latency p95 above one second | the reports are late, not the fills; do not resend; watch for a session drop |
| `PRETRADE_REJECT`, `CANCEL_REJECT` | medium | the broker rejected an order, cancel or replace | read the text (58): locate, limit, unknown order |

Escalate to the portfolio manager when a `high` or `critical` alert is open for more than 15 minutes, and to the broker's desk on any session or reject-rate alert.

## End of day (`xops recon`)

| break | meaning | do |
|---|---|---|
| `missing_in_dropcopy` | we booked a fill the broker's drop copy does not show | ask the broker; the drop copy is the tie-breaker, the blotter is adjusted only on confirmation |
| `missing_internal` | the drop copy shows a fill we did not book (lost message, rejected report) | book it once confirmed; the state machine's anomaly list says why it was refused |
| `missing_at_pb`, `extra_at_pb`, `duplicate_at_pb` | trade file does not match our fills | most resolve as `late_booking` the next day; otherwise a trade query to the prime broker |
| `price_diff`, `qty_diff`, `side_diff`, `symbol_diff` | field mismatch on a matched fill | price and quantity differences are corrections to raise; symbol differences are usually vendor codes (see `VENDOR_SYMBOL`) |
| `position_break` | start-of-day plus fills is not end-of-day | never expected; stop and reconcile the allocation to strategies |
| `ca_not_applied` | the prime broker's position ignores today's corporate action | send the event reference to the prime broker |

Breaks are aged across days; a break older than two days is escalated to the head of operations.

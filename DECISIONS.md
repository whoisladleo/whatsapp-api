# Decisions

## IG DM turn lock + early release on outage retry (2026-11-04, #142)

**Chose:** `process_inbound_dm` takes a per-conversation Redis lock (`ig_dm_lock:{account}:{customer}`, same 30s TTL / 5-retry / countdown shape as the WhatsApp row lock) before the claim CAS, after a live incident where two concurrent turns replied to one customer with contradictory texts ("already on its way" alongside "something went wrong"). The lock serializes distinct turns; the claim CAS still guards double-delivery of one row. New `attempt` param mirrors WA exactly — same constants, no separate tuning, and the give-up path returns a string like the rest of the idempotent pre-claim returns. One subtlety found during implementation: the Claude-outage retry dispatches from *inside* `_process` (i.e. inside the locked region), so it now releases the lock early before dispatching — WA's rule that a retry is queued only after the lock is released, mirrored. The wrapper's `finally` re-release is a harmless no-op; the retry re-acquires on fire, so it still serializes against any newer turn that moved in meanwhile. Redis outage still fail-opens by design (replies may race, but nothing stalls). Three contention/acquire-failure tests added; the existing outage-retry test caught the self-deadlock before merge.

**Considered and rejected:**
- *Exempting outage retries from the lock (llm_attempt > 0 skips acquire).* Rejected: the retry fires seconds later, by which time a newer turn may legitimately hold the lock — an exemption would reintroduce the overlap the lock exists to prevent, exactly in outage scenarios. Release-early + re-acquire keeps every turn serialized.
- *Restructuring so the outage dispatch moves out of `_process` (WA's exact shape).* Rejected as churn: WA dispatches from the wrapper because its outage handling lived there first; IG's lives inside `_process` next to the claim juggling. One release call buys the same ordering guarantee without moving the claim logic.

## Platform handoffs alert the owner; alert skips log loudly (2026-11-04, #140)

**Chose:** prospect handoffs on Taskflow's own accounts alert the owner like any other handoff, on both channels — reversing the #136 mirroring, which had copied the WhatsApp platform branch's silence over to Instagram. A prospect needing a human on your own sales number is the highest-value notification in the system, and the 04:46 incident (an IG conversation sat in handoff with the owner never notified and nothing in the logs) proved the silence wrong. The WA reminder nudge stays enabled on platform handoffs too — it re-notifies the same owner, so it stays correct there; `_trigger_human_handoff`'s `is_platform_account` parameter is now vestigial and kept only for signature stability. Every alert skip (no owner on either account type, no dialable number on the merchant) now warning-logs with the account/merchant id instead of returning mute — the incident was undebuggable precisely because all three skips were silent. Flipped the #136 silence test to assert the alert fires; added WA-platform alert+reminder and no-number skip tests. Validated: focused 44 passed, full suite 1683 passed / 96.15%.

**Considered and rejected:**
- *IG-only fix.* The WhatsApp platform branch had the identical silence one channel over; fixing one while leaving the other re-creates the cross-channel drift #136 removed.
- *Keeping skips silent behind a future dashboard surface.* Logs are where the on-call looks first; the mute skip is what turned a one-line data problem into a mystery outage.

## DM unification + full IG parity + platform pitch on IG (2026-11-04, #136)

**Chose:** one prompt stack for both DM channels: `app/services/llm/conversation.py` owns `render_catalog` (dm/comment modes), `build_merchant_prompt`, and `build_platform_prompt`, parameterized by channel (phone collection, delivery mechanics, sentence budget, markdown) — WhatsApp, IG DM, and the comment reply all render through it, so the "Stock: 0"-on-plans misread (#92, fixed per-path; #135's uncommitted comment fix, absorbed and verified by its own test against the renderer) can't recur as three-way drift. Closes merge into `_close_core` + `_validated_close_target` (channel-keyed reclose filter, order FKs, label-prefixed rejections) and resends into `_resend_payment_note` (re-fire/dispatch/fallback selection shared; order lookups stay at call sites). IG gains full parity: one-shot language classification stored IG-side (`InstagramCustomer.language`, backfilling an unset phone row on close, never overwriting), proactive quote clearing, failed-cycle counting via the shared `_bump_quote_cycle` with the same threshold into the IG handoff, and quote nudges through a channel-parameterized `send_quote_nudge` (plain-text DM, unconditional HUMAN_AGENT tag mirroring manual replies; late legs fail gracefully pre-approval and self-heal after). The platform pitch runs on Instagram too: plans catalog, first-message welcome (outbound-DM count), fixed-price full-only close, silent handoff — closing the installments-on-plans gap the missing branch caused. Schema: one additive migration (IG context quote fields, IG customer language, IG platform flag defaulting false, hand-set like its WA twin). Validated: 36 new parity tests, full suite 1680 passed / 96.19%, live haggling eval extended to both channels at 60/60 on Muse and 60/60 on Claude.

**Considered and rejected:**
- *Merging the two turn orchestrators (`generate_response`/`generate_dm_response`) into one pipeline.* The marker-handling blocks differ in small, money-critical ways (handoff-wins checks, close-failure contracts); shared builders + shared cores capture the drift class at a fraction of the risk. Same call for quote tracking: the counter is shared, the WA/IG track methods stay separate behind their channel sends.
- *Provider-style byte-freeze for the merchant prompts.* The union keeps WA's proven voice and DM's stronger handoff/language lines; load-bearing RULES are pinned by parity tests instead of brittle snapshots, and the live eval (now both channels × both providers) is the release gate snapshots can't be.
- *Auto-backfilling the IG platform flag by username.* The WA twin exists precisely because a name match broke in prod (#21); the flag stays hand-set, defaulting to today's merchant behavior when unset.
- *Keeping the resend try/except around the fresh-close branch only.* Any resend-path failure now hands off with the holding reply instead of dead-ending on a generic error — the same #37 contract as a failed agreement close.

## Sim harness split + refresh workaround removed (2026-11-01, #111c)

**Chose:** `scripts/simulate_instagram_outreach.py` (1,335 lines) is now two files along its existing pure/app-bound marker: `scripts/sim_outreach_personas.py` (421 lines, stdlib-only personas + math, still loaded by path in tests) and the runner (931 lines, sibling from-import of the 14 names it uses). Also removed `_refresh_llm_client` + its call: the per-turn client rebuild was a workaround for the #121 pool bug, now fixed at the root, so the sim just reuses the shared client like production. Run-recipe docstring updated to copy both files. Validated: sim tests 13/13, `--help` entry run, full suite 1387 passed / 96.23%, pyflakes clean. Closes #111 (all three oversized files now under the limit).

**Considered and rejected:**
- *Three-way split (personas / fakes / runner).* Unnecessary: two files already satisfy the rule with the least import plumbing in a non-package scripts dir.

## test_llm_service.py split by area (2026-11-01, #111b)

**Chose:** the 1,859-line test file is now `tests/unit/llm/` — conftest.py (the 5 shared fixtures, auto-available) plus six area files mirroring the source split (replies, catalog, notify, markers, close, quotes; largest 561 lines). Test bodies byte-identical (span coverage asserted, 79 = 79 collected); the small fake-client block is duplicated per file needing it, matching the codebase's existing precedent for test fakes, with per-file imports pyflakes-trimmed. Validated: full suite 1387 passed / 96.23%, pyflakes clean.

**Considered and rejected:**
- *A shared fakes module imported across test files.* No importable test-helper chain exists (`tests/` has no `__init__.py`), and completing it would change pytest's module naming for the whole suite — out of proportion for a file-size split. Duplication follows existing precedent (claim/DM/comment tests each carry their own fakes).

## llm.py split into a package by job (2026-11-01, #111a)

**Chose:** the 2,344-line `app/services/llm.py` is now `app/services/llm/` — constants, prompts (all reply-line dicts), markers (regexes + parsers), helpers (stock/hold/guards/text), providers (SDKs + completion + errors), and the `LLMService` class divided into job mixins (close, notify, replies, replies_dm, replies_wa) combined in service.py. Bodies moved byte-identical (verified by exact span coverage); only headers, mixin class lines, and the facade are new. `__init__.py` re-exports the full 30-name surface (every from-import anywhere, plus the `settings`/`os`/`httpx` monkeypatch targets), every module logs as `app.services.llm`, and the three names tests behavior-patch (`_try_claim_stock`, `_reclose_guard_note`, `match_post_to_product`) are called back through the facade so patches keep landing. Largest file is now 556 lines. Validated: full suite green with zero test edits (1387 passed / 96.23%), pyflakes clean.

**Considered and rejected:**
- *Moving code + updating all import sites.* Rejected per user Discovery pick: the facade keeps all 10 app + 16 test consumers untouched, so the diff is provably behavior-only.
- *Splitting the test file and sim script in the same change.* Rejected per user Discovery pick: #111b (tests) follows separately, #111c (sim script) later.

## Shared LLM client: keep-alive off, both providers (2026-11-01, #121)

**Chose:** both model SDKs get an httpx client with `keepalive_expiry=0` and the SDK-default 600s timeout, via one `_no_keepalive_http_client()` factory. Root cause (found by the #120 live probe): pooled connections bind to their creating loop, and every worker task runs in its own `asyncio.run` — the next same-process task reuses a connection bound to a closed loop and dies with "Event loop is closed", measured deterministically alternating 5/10 on rapid sequential calls. Production gaps usually exceeded the 5s pool expiry so replies mostly survived; rapid back-to-back turns (reply then verify seconds later) did not. After: 10/10 sequential shared-client completions on the production path and provider. Validated: 4 wiring tests (kwargs reach httpx, SDK holds our client, both providers) + live 10/10 probe, suite 1387 passed / 96.18%.

**Considered and rejected:**
- *Persistent loop per worker thread.* Keeps keep-alive speed but adds loop-lifecycle code in the shared `_run_async` choke point plus executor-path edge cases; rejected per user Discovery pick in favor of the obviously-correct 5-line fix.
- *Fresh client per call everywhere.* Robust but churns unclosed clients on every reply and still loses keep-alive; kept only for the #120 judge, which predates this fix and is harmless either way.
- *Retrying "Event loop is closed" with a fresh client.* Band-aid per call site; leaves the root cause for the next consumer.

## Phantom flag made actionable: auto-handoff + recover-from-terms (2026-11-01, #120)

**Chose:** a #116 flag now pauses Amara (HUMAN_HANDOFF + HANDOFF ledger + reminder scheduling, the same flip the model's own handoff uses) and rebuilds the promised order from the agreed terms when they check out, with one combined merchant + OPS template alert covering both. The judge grew from a one-word verdict to five fields (CLAIM / PRODUCT / PRICE / CONSENT / PHONE) over the last 6 turns, since follow-up reassurances ("it's on its way") only make sense with the close turn in view. Recovery requires consent (agreement or payment-chasing; refusal/cancel = handoff-only), a unique non-draft catalog match, the quoted chat intent agreeing, and the price inside the product's min/max band (missing price defaults to max — never the min); WA uses the sender's number, IG uses a buyer-supplied number or refuses. The close runs through the real close path via a shared `close_deal_from_terms` tail (also extracted from the #117 fresh-clone, same file), so stock/validation/#114 guards all apply; any refusal reason lands in the merchant copy. Judge down or unparseable now hands off + alerts instead of staying silent (the Oct 31 incident proved silence costs more); the 6h throttle still bounds repeats. Validated: 65 claim tests (matrix over consent/terms/phone/closer-failure, idempotency, history slicing) with the touched files at 100%, full suite 1383 passed / 96.18%, and a live 12/12 judge probe on the production provider.

**Considered and rejected:**
- *Keeping the flag alert-only.* Rejected: the Oct 31 chat showed an alert with no recovery path strands the buyer for 13 hours; the merchant still gets the final say via tap-to-reply on the handed-off chat.
- *Recovering from last-known terms without a consent check.* Rejected (safety): "stop" + phantom reassurance must hand off, never charge; consent is a separate verdict field, not inferred from the claim.
- *Reusing the shared llm_service client in the judge.* Rejected after measurement: the shared SDK client + per-call asyncio.run reuses a pooled connection bound to a closed loop — deterministic alternating "Event loop is closed", 5/10 failures. The judge builds a fresh client per call (10/10 after). The reply path has the same latent flaw on rapid back-to-back turns; tracked as a follow-up, not fixed here.
- *Regex verdict / inline prevention / new notification center.* Same rejections as #116; unchanged.

## Merchant payment resend: /resend + endpoint + button (2026-11-01, #117/#18)

**Chose:** one guarded service core (`payment_resend.merchant_resend_order`) behind three surfaces: the `/resend` merchant command (WA phone + IG @user), POST `/orders/{id}/resend`, and the dashboard button. Live PENDING FULL orders re-fire in place (same order-ref, newest txn wins — no double-charge by construction, 5-min throttle reused); expired holds cancel-then-re-close from identical terms through the real close path (validation, #114 index safety via cancel-first, subscription re-pointing, reminders); installments refused (Tunzaa-pushed legs); `/resend` with several live PENDINGs refuses listing the candidates instead of guessing; endpoint blocked while impersonating; every refusal carries a merchant-safe detail. The RESEND marker path delegates its Tunzaa call to the same core and only renders notes. Rollback discipline: any fresh-clone failure restores the old row exactly (status, hold, stock).

**Considered and rejected:**
- *Extending the expired hold instead of cloning.* Rejected per user Discovery pick: a fresh row mirrors Amara's own fresh-close and keeps retry history truthful; cancel-first satisfies the one-pending-per-deal index.
- *Latest-wins for multi-PENDING /resend.* Rejected per user Discovery pick: refusing with a candidate list removes all wrong-deal prompt risk; the dashboard button covers the exact-order case.
- *Re-firing installment legs.* Rejected (safety, not offered as a choice): legs go out on Tunzaa's schedule; re-firing would rebill one.
- *Customer-facing text on merchant resend.* Rejected: the STK prompt itself is the notification; free text outside the 24h window fails silently (known incident) and a new template needs Meta approval.

## Phantom-payment detection: async LLM judge + merchant alert (2026-11-01, #116)

**Chose:** after every AI reply sends (WA + IG), a celery task verifies the turn: exact filters first (turn ran the order machinery / handoff state / a live unexpired PENDING order backs the words), then an LLM judge for the remainder asking only "does this text claim a payment is already underway, given no live order?", then a merchant + OPS template alert on YES (existing handoff_notification template, tap-to-reply armed, no state flip, no nudge — alert-only). A per-conversation 6h Redis throttle bounds repeat pages. The monitor is total-fail-safe: judge unparseable/unavailable, no numbers on file, Redis down, or any internal crash all degrade to log lines, never exceptions — it must not break the pipeline it watches. Enqueue itself gates on (markers seen with no order action) or EN/SW payment tokens in either direction, so plain chat never pays for a judge call. Validated: 33 new tests plus a live 12/12 judge probe (EN/SW claims, presuppositions, future-conditionals, price talk, failure notices, true confirmations).

**Considered and rejected:**
- *Inline check before send.* Rejected per user Discovery pick: prevention would tax every message with judge latency; seconds-late detection + alert is the chosen trade.
- *Regex-only verdict.* Rejected per user Discovery pick: claimed-complete vs future-conditional across two languages is beyond phrase lists (regex stays only as the cheap recall gate).
- *Auto-handoff or auto-retry-close on a hit.* Rejected for v1 per user Discovery pick: alert-only until the false-positive rate is observed; auto-charging from last-known terms needs consent-safety design.
- *New dashboard notification center.* No such infra exists in either repo; the WA template reaches the merchant in minutes, a dashboard only when opened. Chosen channel confirmed with user.
- *Reusing `_send_platform_alert_to_merchant` for the send.* It sends to a single User, and its handoff caller skips platform accounts entirely; the phantom alert needs owner + OPS bare-phone recipients on every account, so it follows the usage_alerts direct-send pattern with the same template shape instead.

## Pay Now while ACTIVE refused (2026-11-01, #115)

**Chose:** `pay_now` raises `AlreadySubscribedError` (→ HTTP 409) when an ACTIVE subscription exists for the customer+product — checked after customer resolution, before reuse/insert. ACTIVE-only: PAST_DUE catch-up, post-CANCELLED resubscribe, and PENDING_PAYMENT-without-a-live-order (dead prompt, legitimate retry) all keep flowing. Dashboard hides the Pay Now button while ACTIVE; the 409 detail surfaces through the existing generic error path (no new dashboard error plumbing needed). Verified: 3 new API tests.

**Considered and rejected:**
- *200-with-flag instead of 409 for already-subscribed.* Rejected per user Discovery pick: 409 + detail fits the route's existing error table and the dashboard's generic detail-surfacing path.
- *Blocking PENDING_PAYMENT-without-order in pay_now too.* Rejected: that state means a dead prompt (or a reused-then-cancelled attempt); refusing would strand the buyer with no self-service path forward.

## Channel-less orders made visible to the buying merchant (2026-11-01, #16)

**Chose:** New nullable `orders.buyer_user_id` FK, set by `pay_now` at creation; the two merchant scopings OR on it so channel-less dashboard purchases appear in the buyer's Orders list (previously invisible — the merchant join matches neither side). Migration backfills old channel-less rows by buyer-phone match, linking only unambiguous (exactly-one-user) matches and leaving the rest NULL (hidden, the safe direction). Channel-less rows serialize as `channel: "dashboard"` instead of the mistranslated `"whatsapp"`. Verified: 3 new API tests, full suite 1267 passed, backfill proven on scratch Postgres 18 (username/phone/ambiguous/unmatched/account-linked cases + idempotent round-trip).

**Considered and rejected:**
- *Attributing old channel-less rows by account inference or leaving them hidden.* Rejected per user Discovery pick: phone backfill restores history; ambiguous matches stay hidden rather than guess.
- *A status filter/tabs on the Orders page.* Rejected per user Discovery pick: flat all-status list; the table already renders every row it's given, so no dashboard list change was needed at all.

## Subscription double-charge: partial unique index + PAID-history guard (2026-11-01, #114)

**Chose:** (1) Partial unique index `ix_orders_one_pending_per_deal` on `(customer_id, product_id, payment_type) WHERE status='PENDING'` — the DB, not the guard's read-then-write check, decides the double-close race (sandbox showed a 16ms-apart double-mint). Key is channel-blind because Pay Now orders carry no channel and its reuse query already treats chat PENDING rows as reusable; `payment_type` is in the key so a FULL order still coexists with an in-flight installment plan (pinned #110 behavior). Migration preamble retires older dup PENDINGs (latest wins, stock restored) with counts via NOTICE. (2) `_reclose_guard_note` also blocks subscription closes when any PAID order exists for that customer+product (ownership is global — a plan bought over WA blocks its IG re-close); consumable re-buys stay frictionless. (3) Race losers: chat closes roll back, restore the claimed unit, and return the already-sent note against the winner (re-queried channel-blind); Pay Now falls into its reuse path with `stock_taken` reset. Verified end-to-end on scratch Postgres 18 (preamble, index enforcement, downgrade round-trip) plus 9 new tests; full suite 1261 passed.

**Considered and rejected:**
- *App-level locking (SELECT FOR UPDATE / advisory locks) around guard+insert.* Rejected: lock plumbing across three close sites (WA/IG/Pay Now) vs one declarative invariant that covers all of them, including future writers.
- *Channel-scoped index key.* Rejected: Pay Now rows are channel-less (NULL never collides in a unique index), so the Pay Now/chat overlap — the exact shape Pay Now's own channel-blind reuse acknowledges — would stay unprotected.
- *Refusing Pay Now while an installment plan is in flight.* Rejected: rewrites the #110 pinned contract (mint FULL alongside, never repurpose the installment row) for a state that is vanishingly rare for platform plans and arguably legitimate (paying off a balance in full).
- *Blocking consumable re-buys after PAID.* Rejected per user scope decision: buying the same thing twice is legitimate; only subscriptions (one row per customer+product by construction) get the PAID-history block.
- *Restoring stock unconditionally in the preamble (including subscriptions).* Rejected: subscription quantities are meaningless (nothing is claimed); only non-subscription dup rows restore.
- *Touching the linked subscription row in the race catch.* Rejected: the loser's `_create_subscription_if_needed` never runs (catch precedes it), so there is nothing to unwind; the winner owns the row.

## Amara-doesn't-close: resend path, lazy-cancel of expired holds, prompt repairs (2026-11-01, #113)

**Chose:** (1) `[RESEND_PAYMENT]` marker with optional deal terms: re-fires the Tunzaa prompt against this chat's live PENDING FULL order (same `order-<id>` reference, newest txn id wins, 5-min throttle off new `Order.updated_at`), dispatches onto it if the first attempt never went out, or closes fresh from the marker's terms when no live order exists — the pre-fix silent-marker failure. Installment markers are refused (legs are Tunzaa-pushed; re-firing would rebill). (2) The reclose guard retires expired PENDING holds on the spot (CANCELLED + stock restore) instead of wedging every future close behind "already sent" — auto-cancel's 48h countdown doesn't survive a redeploy, and a weeks-old PENDING row was found live in prod. (3) Prompts: resend clause (model never claims the resend itself — only the appended note does), no-address-term in English / ndugu in Swahili, re-open rule (answer the new question, never another goodbye), monthly-only billing on all three prompts (platform already had it). (4) Webhook `ignored` returns now log claimed reference/status/txn.

**Considered and rejected:**
- *Re-close via a second AGREEMENT_REACHED instead of RESEND.* Rejected: mints a duplicate PENDING order + double stock claim per attempt; same-reference resend keeps one order with multiple prompts, any approval completing it.
- *Bumping `reserved_until` on resend.* Rejected: the auto-cancel countdown fires at the old deadline and would see the extension as "stale, newer firing will cancel" — without scheduling that newer firing the order would linger PENDING forever. Resends land deep inside the 48h window anyway.
- *Handing off when no live order exists.* Rejected: violates "Amara closes herself" for the exact failure being fixed; handoff is reserved for fallback closes that fail validation. Installment "never got it" gets the model's schedule explanation, also without handoff.
- *Touching the linked PENDING_PAYMENT subscription in the lazy-cancel.* Rejected: `get_or_create_pending_subscription` already re-points it at the new order, and #110's 48h bound means a stale one grants nothing.
- *Sending the "reservation expired" notice in the lazy-cancel.* Rejected: this turn's own close note covers the UX; sending both would confuse.

## Coolify auto-deploy pipeline (2026-10-31, #85)

**Chose:** push to master runs Tests; `docker-publish.yml` triggers on Tests completion (master, success only) and builds the exact tested SHA, so broken commits never publish. After pushing, CI POSTs the Coolify deploy webhooks for the API + worker image resources (URLs + token in GitHub secrets). Dashboard (hosted on Cloudflare Pages, Git-connected) gets lint+build CI as signal only — Pages deploys on push and can't wait for CI. Rollback stays manual (Coolify redeploy previous / Pages rollback).

**Considered and rejected:**
- *Ungated publish (previous shape).* Rejected: tests and publish raced; red commits shipped images.
- *Gating dashboard deploys on CI.* Rejected: would need PR-flow or image-based dashboard; a broken dashboard page is low-stakes and Coolify rollback is one click.
- *`force=true` rebuilds on every deploy.* Rejected: the webhook default reuses the cache and just pulls `:latest`; clean rebuilds stay a manual option.

## Audit fixes: money-state guards, dispatch rollback, lazy token refresh (2026-10-31, #77-#84)

**Chose:** (1) Tunzaa FAILED/CANCELLED only acts from PENDING — never regresses PAID; a second COMPLETED with a new txn id records the cash event but skips state rewrite and re-notify; COMPLETED-after-FAILED re-claims stock first and stays FAILED (logged, txn recorded) when stock is gone. (2) All four webhook dispatches roll back the stored row when the queue call throws, so Meta's retry replays fully instead of no-op-ing on already-processed. (3) Channel-connect scope stripped to approved basic + manage_messages; OAuth builders fail fast naming the missing setting. (4) Lazy IG token refresh (no Beat in repo): webhooks dispatch a refresh task when the token is 50+ days old, task claims via conditional stamp, restores the stamp on API failure. (5) Webhooks fail closed without secrets unless explicit ALLOW_UNSIGNED_WEBHOOKS=true. (6) RUN_MODE=production is an ops setting, no code. (7) Blank OTP usernames 422 before lookup. Full suite 1046 passed.

**Considered and rejected:**
- *Ignoring second-COMPLETED cash entirely.* Rejected: the money really arrived; dropping the event falsifies revenue. Recording without state change keeps cash truthful.
- *Treating FAILED-after-PAID as a refund signal.* Rejected: a failed duplicate attempt is not a refund; auto-refund semantics would need Tunzaa reversal support that doesn't exist.
- *Eager token refresh via new Celery Beat process.* Rejected: new always-on process for one job; lazy refresh covers every account active enough to matter, and dormant-account expiry surfaces loudly in logs.
- *Retrying dispatch in-process instead of rolling back.* Rejected: blocking the webhook on broker reconnects risks Meta timeouts; rollback + Meta retry reuses the existing delivery machinery.
- *Per-file unsigned-webhook test flags instead of conftest-wide.* Rejected: the whole suite is the dev case; one pin with explicit opt-outs in the three fail-closed tests is less machinery.

Production-tier decisions only — what was chosen, what was considered and rejected, and why. Project-specific prior-art/incident history lives in CLAUDE.md instead; this file is for the choice itself, not the surrounding system detail.

## Reply metering: append-only event ledger, all automated buyer sends, billing-cycle periods, warn-only (2026-10-23, #41)

**Chose:** one `amara_events` table recording each billable send (`REPLY`) plus `QUOTE`, `HANDOFF`, `PAYMENT` and `USAGE_WARNING`. Billing reads `REPLY` and stats read everything. Billable = every automated send to a buyer that Meta accepted (your call over "conversational replies only"). The period is the merchant's own Taskflow billing cycle, with the EAT calendar month as fallback. At 80% and 100% of the limit, the merchant and ops get one template alert each; Amara never stops.

**Considered and rejected:**
- *Counting existing `Message`/`InstagramDirectMessage` outbound rows.* Rejected: about 15 writers with no sender marker, so merchant manual replies, alerts and nudges are indistinguishable. Comment replies aren't rows at all. Not auditable enough to bill on.
- *A per-merchant monthly counter column (or Redis counter).* Cheaper to read, but not auditable (you can't show a merchant *which* replies they were billed for), has to be reset on a schedule (there's no Celery Beat), and can't answer stats questions. A row per event at this volume (about 2,000/merchant/month) is trivial for Postgres with the composite index.
- *FKs from events to accounts/products.* Rejected: WhatsApp accounts and products are hard-deleted, so an FK would either block those deletes or cascade away billing history. Ids and the product name are stored as plain values.
- *Pausing Amara at 100%.* Rejected: it breaks the "every DM gets an answer" promise on the site. Overage is a sales conversation (Pro), not a technical cut-off.
- *Recording the HANDOFF count from `ConversationContext.handoff_at`.* Rejected: that context is per buyer globally, not per merchant, so it can't be scoped. The latest-only timestamp also loses repeat handoffs.
- *A `quote → paid` conversion rate.* Rejected for v1: QUOTE events count every counter-offer turn, not distinct negotiations, so the ratio would be misleading. Shipped `conversion_rate = paid orders ÷ buyers who messaged` instead.

**Known limits:** two replies at the exact same instant can both cross a threshold and send a duplicate warning (harmless; a `shortcut:` comment in `usage.py`). There's no history before this shipped. The limit is one global setting, so Amara Pro needs a per-merchant limit. "Orders paid" means orders *created* in the period that are now paid, while "TZS collected" is cash received in the period (so a payment for last month's order counts in TZS but not in orders paid).

## Negotiation floor: buffered prompt floor + holding reply on rejection (2026-10-23, #37)

**Chose:** the prompt shows `min(max_price, ceil(min_price × 1.10))` as the lowest price Amara may offer. The real `min_price` is never in the prompt and stays the server-side hard limit in `_close_deal`/`_close_instagram_deal`. Any failure there replaces the model's reply with a fixed holding line and hands off to the merchant.

**Considered and rejected:**
- *`check_offer(product, price)` tool, so the model never holds any floor.* This is the most robust option, but it adds a tool-use loop to every negotiation prompt (latency, more tokens, a new failure mode). The buffer closes the leak at the real floor with about 10 lines. Revisit if the live eval or production shows Amara conceding below the buffered figure often, or merchants objecting to the 10% they "lose" on hard hagglers.
- *Per-product merchant-set buffer.* Rejected for now. It needs schema, UI and dashboard copy, and nobody has asked for it.
- *Appending "let me confirm with the shop" after the model's reply instead of overwriting it.* Rejected: the model's reply typically says "Deal!" and restates the price, and appending to it reads as a contradiction. Overwriting is deterministic.
- *Treating a close exception (DB/Tunzaa error) differently from a validation rejection.* Rejected: either way the buyer was told a deal was agreed and nothing follows, so both get the holding reply and the handoff. Only the log level differs.

**Known limit:** an agreement between the real floor and the buffered floor is still accepted server-side. The buffer only controls what the model is told, not what the server allows.

## Inbound reply retries: re-run the turn on Claude outage, retry only the send on Meta failure (2026-10-23, #38)

**Chose:** two separate retry layers. (1) A transient Claude failure re-dispatches the whole task 3 times (15/45/60s), which is safe because `LLMUnavailable` is raised before any side effect. After that the buyer gets an outage reply and the chat is handed to the merchant. (2) A Meta send failure retries just the send, in-process, twice (1s/3s), and only for transient errors. A final send failure alerts the merchant.

**Considered and rejected:**
- *Celery `autoretry_for` on the task.* Rejected: the DM task claims `processed_at` before working (#33), so a plain retry hits "already processed" and no-ops. Under `task_always_eager` (the test suite) `self.retry()` also just raises. The existing lock-retry code already uses recursive `apply_async`, so this follows it.
- *Releasing the DM claim (`processed_at = NULL`) before retrying.* This is the obvious way to let the retry claim again, but it reopens the exact window #33 closed: a webhook redelivery could claim the row during the 15–60s gap and both would run the LLM and `_close_deal`. Compare-and-swap on the attempt's own claim timestamp keeps the row claimed throughout.
- *Retrying the whole turn on a send failure.* Rejected: `_close_deal` may already have created an order and pushed a Tunzaa payment on that turn. Re-running it is the double-order incident again.
- *Longer in-process send backoff.* Rejected: the WhatsApp per-customer lock TTL is 30s, so a long sleep would let a second message for the same customer run concurrently.
- *Raising the SDK's `max_retries` instead.* Rejected: SDK backoff caps around 8s per try and blocks a worker slot while holding the lock. A real minutes-long outage needs task-level retry.

**Known limits:** a WhatsApp customer who sends another message during the retry gap gets that one answered first (the lock is free), and then the retry also replies, which can produce two replies. Every outage retry is a full extra Claude call.

## Per-product installment controls: scope, BNPL/SNPL (2026-10-15)

**Chose:** two new `Product` fields — `allow_installments` (bool, default `True`) and `installment_mode` (enum `BNPL`/`SNPL`, default `BNPL`). Both per-product. `installment_mode` only has any effect when `is_subscription` is also true — it gates `_activate_subscription_if_linked` (BNPL activates on the first installment payment, matching pre-existing behavior; SNPL waits until the installment plan is fully paid off).

**Considered and rejected:**
- *Account-wide default instead of per-product.* Rejected — the existing `is_subscription` flag is already per-product, and a merchant selling both subscriptions and one-off goods would plausibly want different installment policies per item, not one global switch.
- *BNPL/SNPL also gating fulfillment of regular one-time orders* (e.g. "don't tell the merchant to ship until fully paid"). Rejected for this round — there's no fulfillment-gating concept anywhere in the system today (delivery is manual, merchant-driven off whatever they see in the dashboard), so this would need new design (an order field, a dashboard indicator) beyond what was asked. Scoped to subscription activation timing only, where "activation" already exists as a real state transition to gate.
- *Server-side enforcement that Amara never emits `payment=installment` for a full-only product.* Rejected — this codebase already trusts the prompt for an analogous guardrail (the negotiation price floor isn't server-enforced either), so added only a prompt-level instruction + catalog annotation, consistent with that existing pattern rather than introducing asymmetric rigor for this one case.

**If revisited:** if a merchant ever needs SNPL-style withholding for a regular one-time order, that requires inventing a fulfillment-gating concept first (order field + dashboard surface) — not just extending `installment_mode`'s reach, since there's nothing today to gate.

## LLM provider abstraction: Claude/Muse switch (2026-10-27, #48)

**Chose:** `LLMProvider` interface in `app/services/llm.py` with `ClaudeProvider` (existing Anthropic messages call, byte-identical args and text-block parsing) and `MuseProvider` (OpenAI SDK chat-completions against `https://api.meta.ai/v1`, `muse-spark-1.3`, system instruction as a `system` role message). `LLM_PROVIDER` env (`claude` default) selects; `_default_llm_service()` picks the matching key. Unknown values log a loud warning and fall back to Claude. All three call sites now go through `provider.complete(system, messages, max_tokens)`; `client`/`model_id` stay as compat properties.

**Considered and rejected:**
- *Meta Messages API (Anthropic-compatible) instead of chat-completions.* Rejected: chat-completions is the documented OpenAI-SDK path from the Meta quickstart and keeps the provider seam to one method shape; Messages API stays an option if marker compliance differs.
- *Failing closed on unknown LLM_PROVIDER.* Rejected: a typo taking down all AI replies is worse than a warned fallback during the hackathon demo window; the warning makes it visible.
- *Per-account provider selection.* Rejected: provider is a deployment concern, not per-merchant; one env keeps the demo and rollback story trivial (flip back to Claude with one var).

**Known limits:** Muse reply quality/marker compliance unverified until the A2/A3 live evals; `openai` in requirements.txt is unpinned (same as `boto3` precedent).

## Muse reasoning effort minimal for negotiation (2026-10-27, #49)

**Chose:** `MuseProvider.complete` passes `reasoning_effort="minimal"`. Root cause, verified live: reasoning tokens exhausted `max_tokens` before any reply text (`finish_reason=length`, `content=None`, 198/200 reasoning on a tiny prompt), which made eval passes vacuous since empty replies satisfy floor assertions trivially. `minimal` yields `finish=stop` with real content (~119 reasoning + ~29 text in a 200 budget); `low` did not help. Live haggling eval: 30/30 on Muse and 30/30 on Claude, same shared prompt wording, no `max_tokens` raise. Empty content now logs a warning with `finish_reason` instead of failing silently.

**Considered and rejected:**
- *Raising `max_tokens` instead.* Rejected after measurement: with `minimal`, current budgets (300/500/1024) suffice with zero truncations observed; raising would add latency and cost for no gain.
- *Provider-conditional prompt branches.* Rejected: one shared wording satisfies both models (locked by a parity test); branches would double prompt-maintenance and eval burden.
- *`reasoning_effort="none"`.* Rejected by the API: not a supported value for `muse-spark-1.3` (400).

**Known limits:** `minimal` is a latency/cost/quality tradeoff tuned for short sales replies; revisit if longer-form generations truncate. The vacuous-pass trap (empty reply satisfies floor assertions) is now covered by marker-evidence checks in the eval review, not by the assertions themselves.

## IG comment off-topic deflect plus shared budget (2026-10-27, #50)

**Chose:** one shared prompt line in `generate_comment_reply` (off-topic/spam/greetings still use the two-line format: brief deflect, invite product questions) plus `max_tokens` 300 to 500. Pre-fix, Muse truncated off-topic comments to empty (`finish_reason=length`) 5/5, which sends nothing; post-fix 6/7 deflect in-format. DM needed no changes (4/4 live). Claude behavior unchanged: the line only triggers off-topic and the higher cap cannot alter its short replies (Claude-path units green; Claude live key valid but full comment re-verification skipped as low-risk).

**Considered and rejected:**
- *Provider-conditional comment prompt.* Rejected: one wording satisfies both, locked by parity tests.
- *Treating empty comment replies as success.* Rejected: silence on a public comment is a visible failure; B2's vision match further reduces off-topic incidence.

**Known limits:** 6/7 post-fix reflects live non-determinism; one fragment observed in 7 runs.

## Hermetic LLM unit tests (2026-10-27, #50 round 2)

**Chose:** per-file autouse `_pin_claude_provider` fixtures in the three Anthropic-shaped test files, so the suite is green regardless of ambient `LLM_PROVIDER` (demo `.env` sets `muse`). A conftest-level fixture was rejected because the Muse live eval needs ambient settings. 133 passed under both values.

## IG post-media fetch, behavior-neutral (2026-10-27, #51)

**Chose:** `InstagramProvider.get_media` (`GET /{media-id}` with caption, media_url, media_type, permalink, children) threaded best-effort through comment processing into an accepted-and-ignored `post_media` param. Carousel rule: first `IMAGE` child (fallback first child of any type). All failures (deleted/private, transport, parse) log and return `None`, never raise into the webhook path. Zero reply-behavior change — B2 consumes the data. Full suite 782 passed, coverage gate met. Live `get_media` deferred to staging: no IG user token exists locally (only app id + webhook verify token).

## Vision product-match on comments (2026-10-27, #52)

**Chose:** `app/services/vision_match.py`, Muse-only, strict parser accepting only an exact case-insensitive catalog name or `NO_MATCH` (prose/hedged/invented output becomes `None`, never raises). Matched product feeds one context line into the comment prompt directing naming to PRIVATE only. Transport is always server-side download plus base64 data URL: Meta's API returned 400 "URL is not fetchable by this service" for remote URLs. `max_tokens=500` after live-caught reasoning starvation at 100 (same truncation mode as #49). Live: 3/3 positive match, 3/3 NO_MATCH negative. All failure paths yield byte-identical prompts to pre-B2; Claude never calls vision. Full IG-post end-to-end deferred to staging (no IG token locally; IG-CDN signed-URL download unproven, fails soft).

**Considered and rejected:**
- *Passing remote URLs to the vision call.* Rejected by the API (400); base64 only.
- *Fuzzy matching or model-picked prices.* Rejected: the reply may only name catalog rows at catalog prices; anything else is an invention path.

## Out-of-stock prompt rules (2026-10-27, #53)

**Chose:** `_is_out_of_stock` helper (`(quantity or 0) < 1` or `is_active is False`; legacy `is_active=None` counts in-stock) with ` (OUT OF STOCK)` markers kept listed in all three catalog builders, plus a RULES line in all three prompts (never offer/quote/agree, name it plainly, pivot to an alternative). Close-time `DealRejected` stays the backstop. Live probes: never-offer held 6/6; explicit naming inconsistent, so systematic naming verification deferred to E1 rehearsal. Full suite 812 passed.

## Atomic stock claim at close (2026-10-27, #54)

**Chose:** `_try_claim_stock` conditional `UPDATE ... SET quantity = quantity - 1 WHERE id = ... AND quantity >= 1` (rowcount decides; same claim style as `claim_available_pool_number`, portable SQLite/Postgres) in both close paths, replacing read-modify-write. 0-row raises `DealRejected` with messages byte-identical to each pre-check, funneling into identical holding-reply plus handoff. Also fixed adjacent pre-existing NULL bug: pre-checks now `(quantity or 0) < 1` (NULL raised `TypeError`, violating the `DealRejected` contract). Race coverage is deterministic (stale-held second close proves the stub fires; negative-control probes verified both fixes guard real behavior) — threads rejected: suite runs on one StaticPool `:memory:` connection where a threaded race would be flaky by construction. Full unit 535 passed.

## PATCH stock endpoint (2026-10-27, #55)

**Chose:** `PATCH /products/{id}/stock` with `ProductStockUpdate` (quantity `ge=0`, is_active, both optional, at-least-one-required 422 pre-lookup), merchant-scoped 404 identical to PUT, narrow write enforced at schema plus crud. Zero valid and locked by test; explicit null equals omitted. QA note: the spawned QA validator parked on an approval gate for 75+ minutes and was cancelled; the orchestrator ran its checklist directly (26 API tests, 535 unit, edge read of the diff) — same evidence bar, no skipped checks. Full suite 831 passed.

## Dashboard stock controls (2026-10-27, dashboard #6/#7)

**Chose:** one shared `updateProductStock` server action plus per-row Switch (writes `is_active`) and quantity stepper (writes absolute quantity, floor 0) in `products-table.tsx`. Display status derives from the C1 sellability rule (`is_active && quantity > 0`), not the switch alone; in-flight `Set` with early return prevents cross-row double-submit and stale lost updates. `pnpm build` plus `pnpm lint` green (verified independently by orchestrator). Live PATCH wiring verified statically (no live backend reachable from dev); end-to-end toggle check deferred to E1 staging.

## Catalog client plus mapping (2026-10-27, #56)

**Chose:** `app/core/catalog_client.py` with `MetaCatalogClient` (`items_batch` upsert/delete plus status polling, typed errors) and pure `product_to_catalog_item`. Public data only: retailer_id (product id), name, `max_price` as `"50000.00 TZS"`, C1-mirrored availability, image, `wa.me` merchant-chat link. `min_price`/floor/cost asserted absent by key and value. Image: absolute R2 passthrough, relative joined to `PUBLIC_BASE_URL`, missing raises. No lifecycle hooks, no other behavior change. D1 is staging-unverified (no catalog credentials locally); six format assumptions flagged `STAGING` in code plus `CATALOG_SETUP.md` one-time setup doc. Full suite 856 passed.

## Catalog field shapes proven live (E1 staging, 2026-10-28)

**Chose:** corrected mapping per live Meta responses plus the Supported Fields spec: `item_type: PRODUCT_ITEM` on the batch (was missing, #100), `id` not `retailer_id`, `title` (truncated 100) not `name`, `image: [{url}]` not `image_url`, Required `brand` via merchant-name chain (`User.business_name` to first `WhatsAppAccount.name` to `User.name` to phone digits; blank raises and D2 skips with reason), `DELETE data: {id}`, `started` treated as pending poll status. `id` value stays `str(product.id)`, so D3's `product_retailer_id` references hold unchanged. Live proof on staging catalog: TEST upsert `finished` with zero errors/warnings, DELETE verified, probe removed. Price/availability/condition/description/link shapes confirmed as sent. Full unit 616 passed.

## Catalog lifecycle sync (2026-10-27, #57)

**Chose:** `sync_catalog_item` Celery task (dispatch-on-event, bounded 6x5s status poll, never raises) with post-commit `dispatch_catalog_sync` hooks on every product write path: crud create/update/stock/delete/bulk, image upload, merchant `/stock`/`/out_of_stock`/`/price`, claim-to-zero, and all four restore paths including Tunzaa FAILED/CANCELLED (single-line post-commit dispatch added in round 2 after QA caught the gap — no payment-logic change). Best-effort throughout: creds-unset silent no-op (zero behavior change for existing deploys), broker-down caught, Meta errors logged. Non-zero claims don't dispatch (quantity is never transmitted). Skipped with reason: bulk dry-run (writes nothing), one-shot scripts. Full suite 891 passed.

## Support impersonation: env allow-list, short-lived target-sub JWT, WRITE-only audit (2026-10-30, #75)

**Chose:** operators are env-listed verified identities (`SUPPORT_OPERATOR_PHONES` matched against OTP-verified `User.username` only, `SUPPORT_OPERATOR_INSTAGRAMS` against linked Meta-verified handles — self-reported `phone_number` never grants this). `POST /auth/impersonate` mints a 30-minute JWT whose `sub` is the target merchant plus an `impersonator_id` claim, so all merchant-scoped queries work unchanged; per-request revalidation in `get_current_user` kills sessions when the operator row is deleted or de-listed. Nesting, self-target, and operator-target are rejected; money endpoints (`pay_now`, `purchase_number`) are blocked via `forbid_impersonated`. Audit is an append-only `support_access_events` ledger (no FKs, labels snapshotted): START/END from the endpoints plus one WRITE row per mutating call from middleware (method + path only, GETs and the exit path excluded). `GET /auth/merchants` lists exactly the impersonable merchants (operators and self excluded, search + pagination) for the /admin UI; `GET /auth/session` reports operator status and mask. v1 excludes suspend/ban and an audit viewer; stateless JWTs can't be truly revoked (30-min expiry bounds). Full suite 1012 passed.

**Considered and rejected:**
- *Role column on User instead of env allow-list.* Rejected: a DB flag is one SQL/console slip away from granting merchant-wide access; env keeps the grant in deploy config, and per-request revalidation makes removal take effect on next use.
- *Matching `phone_number` for operator identity or impersonation target grant.* Rejected for the grant (critical): that field is self-reported via PATCH /me, so any IG-login user could claim an operator's number. It is used only as a fallback for *finding* the impersonation target (username first), where a miss just 404s.
- *Separate operator session + target context header instead of target-sub JWT.* Rejected: every merchant-scoped query would need a "resolve effective user" branch; target-sub keeps the impersonated session behaviorally identical to the merchant's own, which is the point ("all they can do"), with the claim + guards carrying the difference.
- *Read auditing.* Rejected for v1: high volume, low harm; START/END bounds the window and writes are what can damage a merchant.
- *Merchant notice on impersonation.* Deferred, not rejected: audit-only for v1 per your call; notice UX is a follow-up decision.
- *Revocation list for impersonation tokens.* Rejected: stateful revocation for a 30-minute token isn't worth the store + lookup; expiry bounds the risk, and de-listing kills the session on next request anyway.

**Known limits:** no DB uniqueness on operator identity (env typos fail closed to "nobody can impersonate"); merchant list scans up to 1000 newest users in Python (operator status isn't a column) — fine at support scale, revisit past ~1k merchants; audit rows are best-effort (a middleware/DB failure logs loudly and the request proceeds); expired-token END rows never exist (START without END means exited-dirty or expired).

## WhatsApp product messages on quote nudges (2026-10-27, #58)

**Chose:** `send_product_message`/`send_product_list` plus `send_product_card_or_text` helper (card body defaults to the fallback line: exactly one buyer-visible message either way) wired at the `send_quote_nudge` call site — highest-value visual moment with product already resolved. Card only while sellable (mirrors catalog mapping); all five fallback triggers fall back to equivalent text. QUOTE turn/close rejected as sites (return-contract change, ordering/double-send hazards). Metering unchanged (one accepted send is one unit). Live WABA send deferred to E1 staging. Full unit 611 passed.

## Dynamic plan SKUs + reply metering + AI product generation (2026-10-31, #90)

**Chose:** `Product.is_platform_plan` + `monthly_reply_limit` replace the `PLATFORM_SUBSCRIPTION_PRODUCT_SLUG` singleton (migration backfills the slug-named product; setting kept deprecated for the migration only). Gating is any-allowing-subscription across plans; the meter is the highest granting plan's limit, anchored on the oldest live subscription so mid-period upgrades don't reset the window. Enforcement is a hard block on the three reactive reply paths only (WA DM, IG DM, IG comments): over-limit DMs get a holding message (never metered itself), comments stay silent rather than posting the limit publicly, the first refusal per period records `LIMIT_BLOCKED` and ensures the 100% usage warning went out (covers limits lowered mid-period). Proactive/system sends (reminders, nudges, payment confirmations) keep counting but never block — a paid customer must always get their confirmation. Seller-exempt and unlimited plans return no meter; every ledger failure fails open (loudly) so metering can never silence a merchant. Plan writes are operator-only (403, CSV-smuggle-proof); `POST /products/generate` (10/day, summary 10–2000 chars) persists model output as drafts only — never subscriptions or plans — with SKU-collision stripping. The platform pitch renders every plan from the catalog with reply allowances and a choice note when several exist; zero prices hardcoded. Dashboard: admin-only plan section in add/edit forms, Plan badge, Generate-with-AI dialog feeding the draft review flow.

**Considered and rejected:**
- *Block proactive sends too.* Rejected: payment confirmations and order updates are obligations, not discretionary replies; blocking them would punish the merchant's customers for the merchant's quota.
- *Public holding reply on comments.* Rejected: advertises the merchant's billing state on their own posts; silence + the merchant's 100% warning instead.
- *Dedicated "paused/upgrade" template at first block.* Deferred, not rejected: needs Meta approval; the reused 100% usage warning carries plan name/limit context for v1.
- *Proration / auto-cancel-old on plan switch.* Rejected for v1: paying for a second plan while one is ACTIVE just stacks (highest limit wins); an admin cancels the old one. No silent money movement.
- *Structured-output/JSON mode for generation.* Rejected: the provider interface is plain text completion; a strict fence-stripping parser plus per-item sanitization (name required, prices clamped ≥ 0, inverted min/max collapsed) is enough, and failures 502 without writing anything.

## Sim close-flow guards (2026-10-31, #101)

**Chose:** `_reclose_guard_note` in both close paths — any PENDING order for the same customer+product+channel blocks a re-close with an "already on its way" reminder (anchored to the pending order's price), and an ACTIVE subscription blocks a paid re-close with "already active". Guard runs before the stock claim, so a re-close neither reserves nor dispatches. `_dispatch_payment` skips the delivery/location ask for subscriptions (nothing to deliver); physical goods keep it on both channels. Empty model content retries once inline, then hands off with the standard holding reply (handoff keeps the holding promise true). Adjacent fix in the same function: the Tunzaa-failure path no longer "restores" stock on subscriptions (was minting phantom units) and is None-safe.

**Considered and rejected:**
- *Cancel-and-recreate on re-close.* Rejected: no verified Tunzaa cancel for an issued push; the abandoned prompt is still approvable, so pointing the buyer back at it is correct and simpler.
- *Block only unexpired holds.* Rejected: errs toward double charges; expired PENDING rows self-heal via auto-cancel, and blocking them just delays the retry by minutes.
- *Silent drop or no-handoff holding line on empty content.* Rejected: silence killed 5/32 sim convos, and the holding text promises a human follow-up — sending it without handing off makes a false promise.
- *Fix-4 (truncated-marker strip).* Dropped as phantom: zero occurrences of any truncation marker across all 32 transcripts; the report citation (H2_main t2) shows a clean reply. No code changed for it.
- *Physical-goods delivery ask removal.* Rejected: the shop still needs a delivery location for physical items regardless of payment method; sim evidence was subscriptions-only.

## Same-language close notes (2026-10-31)

**Chose:** payment/dispatch/guard template notes are sw/en variants picked by `Customer.language` (WA classifies it; IG never does, so IG stacks both like `_holding_reply(None)`). English strings byte-identical to before. All three Amara prompts gain a no-mixing clause ("never mix Swahili and English within a single message"); the WA shop prompt's weak "use Swahili or English depending on their language" upgraded to the full same-language rule. Billing ruling encoded on the site, not in code: pay-today = live-today was already the behavior (first-payment webhook activates), so no subscription-timing change.

**Considered and rejected:**
- *Bilingual notes everywhere.* Rejected: doubles every payment message; language-aware with stacked fallback keeps known-language messages short.
- *Per-message language detection for IG.* Rejected for v1: no detector exists; the stacked fallback is honest and matches existing holding/outage behavior.
- *Handoff-fallback lines language-aware.* Deferred: bare-marker handoffs are rare and a human follows immediately; the holding/outage replies that accompany them are already language-aware.

## Length-cutoff replies (2026-11-01, #103)

**Chose:** providers raise `LLMLengthCutoff` on a length stop (Muse `finish_reason=length`, Claude `stop_reason=max_tokens`) and withhold the partial text; `_complete_with_retry` retries once with a doubled cap; a second cut hands off with the holding reply (DM/WA) or stays silent (comments, the designed failure there) or raises `GenerationError` (draft generation, 502 as before). V2 sims showed mid-sentence cuts (G1 t2/t3, C2 t6) that the old code sent as complete; reasoning tokens share the `max_tokens` budget, so small visible output does NOT rule out a cap hit.

**Considered and rejected:**
- *Bigger static caps.* Rejected: raises cost on every turn and still fails closed wrong (partial text sent); retry-on-cut spends the extra cap only when needed.
- *Outage reply on double cut.* Rejected: same failure class as empty content, so same message + handoff keeps one consistent "Amara failed, human follows" path.
- *Fuzzy product/price acceptance for rejected closes (v2 A2).* Rejected: guessing payment targets or products risks charging wrong; reject + handoff stays, with the reason now captured in sim summaries.

## Amara closes the loop herself (2026-11-01, #104)

**Chose:** no handover-at-interest flow — Amara runs the whole close (your call: handing off defeats her purpose). The post-payment wording contradiction is fixed at the source instead: all three prompts gain a payment-truth rule (never claim confirmed/received/active — the webhook confirmation arrives separately; on "I've paid" thank + say confirming). The webhook confirmation itself is now subscription-aware (no delivery line for plans; "subscription is now active" — the one place that may truthfully say so) with sw/en/bilingual variants. Handoff recipient stays `username or phone_number` (username wins, dashboard edits the fallback only) — no change; flagged as a possible follow-up.

## Dedicated handoff number (2026-11-01, #105)

**Chose:** `User.handoff_phone_number` (nullable, no index/unique — read off the loaded row, and two merchants may share an ops phone). Alert resolution is handoff-number, then OTP `username`, then profile `phone_number`, skipping non-dialable values (also fixes social-login usernames silently breaking the old `username or phone_number` pick). PATCH /auth/me sets it (400 on non-dialable, empty clears); dashboard settings card beside Billing Phone. Usage alerts and catalog sync keep the old resolution — handoffs only, per scope.

## Alert-number DRY (2026-11-01, #106)

**Chose:** usage warnings use `resolve_handoff_number` too — one recipient resolution for all merchant alerts. Deliberately excluded: catalog sync's `merchant_phone` (public shop contact in catalog items, not an internal alert — the ops override must not leak there) and subscription/OTP identity matching (identity, not alerting).

## Audit payment fixes (2026-11-01, #110)

**Chose:** (1) a failed Tunzaa plan fetch records only the paid leg and schedules a 5×/5min background retry (`retry_installment_reconcile`) — the order stays PENDING until Tunzaa's balances say otherwise; the old fallback marked the whole order PAID. (2) Pay Now reuses the customer+product's still-pending FULL single-unit order (refreshing price/reservation, sending a fresh prompt) instead of minting one order per click; installment/multi-unit pendings are never reused. Stock restore on dispatch failure only gives back what the attempt took (also fixes phantom units when the shelf was already empty). (3) PENDING_PAYMENT grants AI only within 48h of creation; `auto_cancel_order` now also fails the linked subscription attempt and skips orders whose reservation was refreshed; Pay Now schedules auto-cancel (it never did); a COMPLETED for a CANCELLED order re-claims stock like FAILED. (4) `NumberSubscription.refund_status` (migration `c9e1f4a2b7d3`): a provisioning failure after payment flags the row MANUAL (refund owed, support reverses by hand in the Tunzaa dashboard) instead of attempting an automatic reversal — refunds are manual, your call, so the unverified `refund_payment` client was deleted, not fixed. The dashboard shows the contact-support copy for anything but SUCCEEDED (a future support action can flip MANUAL once done).

**Considered and rejected:**
- *Sweeping stale pendings at order-creation time.* Rejected: redundant once chat auto-cancel fails linked subs and Pay Now schedules its own; age-bounded allowance covers the access side with no scheduler at all.
- *Celery beat for expiry/renewals.* Rejected for this issue: needs an ops change (beat process in prod); countdown chains + read-path age bounds close the money holes without it. Still the right fix for the systemic queue-loss fragility (audit finding 6).
- *Reusing installment pendings in Pay Now.* Rejected: a Tunzaa plan's legs would keep billing against a full-pay prompt.
- *Backfilling legacy stale rows.* Rejected: age-bounded allowance denies them access already; legacy chat rows had auto-cancel scheduled; legacy Pay Now rows hold 1 unit of 999999 — negligible.

**Correction to the audit report:** finding 3 said "nothing ever expires a PENDING order" — wrong for chat orders, which always had `auto_cancel_order` (stock facet covered, modulo queue loss). The real gaps were subscriptions never resolving on abandonment and Pay Now never scheduling expiry. Fixed above.

## Support mark-refunded action (2026-11-01, #112)

**Chose:** `POST /accounts/{id}/number-subscription/mark-refunded`, impersonated-operator sessions only — the merchant's own token gets 403 (they must not mark their own refund), and the mask keeps the audit ledger's WRITE row attributing operator + target for free. MANUAL flips to SUCCEEDED; NULL is 400 (nothing owed); re-marking is an idempotent no-op. Dashboard shows the button on the purchase card only while impersonating a MANUAL row.

**Considered and rejected:** an admin-page pending-refunds list with operator-direct marking — bigger build (second list endpoint + UI) and operator-direct writes aren't audit-logged today, so the trail would be worse. Also rejected letting merchants mark their own: unverifiable self-attestation on financial records.

## Merchant chat support repairs (2026-11-01)

**Chose:** (1) `type: "button"` taps route through the same handoff branch as `type: "interactive"` in the webhook — this was the production silence: template quick-reply taps arriving as `button.text` (no payload id) were stored but never dispatched, so reply-mode never armed and the next plain text hit "Unknown command". Title matching, loud unknown taps, takeover, and relay already existed (#88/#96) and are unchanged, including the "You're replying to X" confirmation. (2) Bare `/resume` and `/resend` list pending handoffs numbered ("1. 2557…", "2. @user") and accept the index (`/resume 1`); a pick resumes/resends against the entry's own account, not the number the command arrived on. Out-of-range digits fall through to the normal with-args path. (3) `/stock` treats NULL quantity as 0 and clamps at 0; `/price` rejects unless 0 < min <= max. (4) One `resolve_instagram_customer(db, merchant_id, identifier)` helper in handoff_flow serves the resume/resend/relay paths — same scoping and same merchant-facing strings as before.

**Considered and rejected:**
- *Rewording the reply-mode confirmation to the exact batch string.* Rejected: committed tests pin "You're replying to X — send your message here, or /cancel", which already satisfies the confirmation requirement and names the target.
- *Listing pending orders (not handoffs) for bare /resend.* Rejected: per scope, both bare commands list the same waiting-handoff set; the pick then runs the normal order lookup.
- *Helper returning the error string directly.* Rejected: returning `(account, customer)` with None halves keeps message wording at the call sites, so a future wording change stays local to each command.

## Per-account conversation state (2026-11-01, batch B findings 1 + 11)

**Chose:** `ConversationContext` is now one row per (customer, WhatsApp account) instead of one row per phone globally (migration `c8d3e5a1f2b4`: nullable `account_id` FK, uniqueness re-keyed from `(customer_id)` to `(customer_id, account_id)`). All WhatsApp context lookups go through `crud.message.get_or_create_context` / `get_context_for_account`: the scoped row wins, else the legacy NULL-account row is adopted in place by the first account that sees it (state preserved — a handoff stays a handoff), else a fresh row is created with the #86 lost-race fallback. Every other site follows: resume is scoped (history check kept — it 404s unknown phones and attributes legacy rows), the conversations list shows per-account state, `list_pending_handoffs`/`target_still_pending` match scoped rows directly with the history check as the legacy fallback, the handoff reminder alerts the context's own account (legacy rows keep last-inbound inference), the quote nudge adopts NULL rows and skips legs whose row was adopted by a different shop, and claim-verification hands off the scoped row. `Customer.conversation` (scalar) became `Customer.conversations` (list). Finding 11: `get_pending_handoffs_for_account` now scopes by account first — candidate phones from this account's inbound messages, one bulk context fetch, one ROW_NUMBER latest-inbound fetch (same pattern as the conversations list) — 3 queries regardless of fleet size instead of all-handoffs plus N+1; same response shape, except `last_message_body` is now this account's latest inbound rather than the global latest. Instagram path untouched (already per-account via `InstagramCustomer.account_id`).

**Considered and rejected:**
- *Backfilling legacy NULL rows by last-inbound account in the migration.* Rejected: only live traffic can attribute a row to the right shop (a phone with history at two shops is genuinely ambiguous), and lazy adoption converges on first contact anyway; a wrong backfill would be worse than a NULL.
- *Conditional-UPDATE adoption to win double-adoption races.* Rejected: two shops' first post-migration inbound for the same phone would have to land in the same instant during the transition window only; a lost adoption self-heals on the loser's next message (fresh scoped row). Plain stamp-and-commit plus the #86 IntegrityError retry.
- *Keeping `Customer.conversation` as a scalar.* Rejected: `uselist=False` with multiple rows is a lie waiting to raise; the only reader was the conversations list, which needed the scoped pick anyway.

## Per-IP OTP request limit (2026-11-01, batch C finding 3)

**Chose:** `POST /auth/request-otp` now enforces a per-caller-IP guard (`otp_req_ip:{ip}`, 30 per 5 min) alongside the existing per-recipient guard (3 per 5 min), mirroring `ig_login_start`'s `request.client.host` shape. `fail_open=False` like the per-recipient guard — both exist to bound WhatsApp send cost and victim spam, so a Redis outage must deny, not unlimit.

**Considered and rejected:**
- *fail_open=True to match ig_login_start exactly.* Rejected: that endpoint mints no billable sends; OTP request does, so it follows its sibling guard's fail-closed wiring, not the IG one's.
- *A tighter per-IP limit (e.g. 10).* Rejected: 30 matches the established per-IP shape in this file and still bounds a fan-out attack to a negligible send volume; NAT-shared offices shouldn't trip it in normal use.

## Hashed OTP storage + single verify message (2026-11-01, batch C finding 8)

**Chose:** `User.otp_code` holds the sha256 hex of the code (`crud_user.set_user_otp` hashes; new `crud_user.verify_user_otp` compares via `hmac.compare_digest` through `security.verify_sha256_hex`). `POST /auth/verify-otp` returns one message — "Invalid OTP code" — for wrong, expired, unset, and unknown-user alike, and always runs the (constant-time) hash check so neither case leaks via timing. No seed scripts set OTP (verified by grep — only the endpoint, crud, and tests touch `otp_code`), so no migration or backfill: any pre-existing plaintext row simply never verifies (its value isn't a valid hex digest of a guess... note a legacy plaintext row fails closed, forcing a fresh OTP request). Tests pin the generator to a known code instead of reading plaintext back out of the row.

**Considered and rejected:**
- *bcrypt/argon2 for the OTP hash.* Rejected: a 6-digit code has ~20 bits of entropy — no stretch factor survives an offline brute force of a leaked digest table; the rate-limited online verify (5 guesses/5 min) is the actual defense, and sha256 keeps the check cheap. Same rationale as the refresh-token hashes.
- *Clearing the OTP on expiry-discovery.* Rejected: unchanged behavior (clear on success only); an expired row is inert and the next request overwrites it.

## Short-lived access + rotating refresh tokens (2026-11-01, batch C finding 10)

**Chose:** access JWTs live 60 min (`ACCESS_TOKEN_EXPIRE_MINUTES`, was 7 days); real logins (OTP verify, IG exchange) also mint an opaque 256-bit refresh bearer (7 days, `REFRESH_TOKEN_EXPIRE_MINUTES`) backed by a server-side `RefreshToken` row holding only the sha256 (migration `d9e3f5a1c7b2`). `POST /auth/refresh` revokes the presented row and mints a fresh pair (single-use rotation); revoked/expired/unknown/orphaned bearers all get one 401. `POST /auth/logout` revokes the presented bearer, is deliberately unauthenticated (expired access must never block logout), and always succeeds. Impersonation tokens mint no refresh bearer and can never pass `/refresh` (a JWT hashes to no stored row) — they stay 30-min, non-refreshable; `/auth/session`, `/auth/me` unchanged. Dashboard: httpOnly `refresh_token` cookie (7d) + 1h access cookie, `refreshSession()` action, middleware silently rotates on expired access outside impersonation (no silent refresh while an operator stash exists — minting credentials without the audit END would corrupt the impersonation ledger), and logout revokes server-side best-effort before clearing cookies.

**Considered and rejected:**
- *Refresh-token family revocation on reuse (theft detection).* Rejected for this pass: rotation-reuse currently just 401s. Family-wide kill is the right escalation but needs a session-family model this table doesn't have; noted as a follow-up, not silently assumed.
- *JWT refresh tokens (stateless).* Rejected: logout revocation is required, which needs the server-side record anyway; opaque bearers keep the DB as the single source of truth.
- *Refresh-on-401 inside every dashboard server action.* Rejected for this pass (scope: auth.ts + middleware + logout per the finding): middleware rotation covers navigations and revalidations; a client-side action fired in the exact window where access lapsed but the cookie hasn't rotated yet can 401 once and succeed on retry. Known limitation, not a silent one.
- *A janitor for expired/revoked rows.* Rejected for now: dead rows are inert (rejected on sight) and tiny; a cleanup migration can drop past-expiry rows once volume justifies it.

## Product validation + bulk hardening + slug race (2026-11-01, batch D findings 5 + 4 + 17)

**Chose:** (5) `ProductCreate`/`ProductUpdate` validate: stripped non-blank name (`min_length=1` + before-validator strip), `ge=0` on max/min price, quantity, cost/weight, and reply limit, and a `model_validator` enforcing min <= max (update checks only when both sides are present — the schema can't see the stored other half). Constraints sit on Create/Update, not the shared `ProductBase`, so `ProductResponse` reads of legacy rows can't 500. Zeros stay valid everywhere since drafts carry 0. Catalog import and AI generation build ORM rows directly and bypass schemas by design, so they're unaffected (their suites re-run green). (4) `/bulk/upload` parses through an explicit 14-column allowlist (`_parse_bulk_row`): unknown/identity/plan/draft/enum columns are dropped and counted in `stats["ignored_columns"]`, never a 400; numerics coerce with `>= 0` and min <= max checks, booleans (`is_active`, `is_subscription`, `allow_installments`) parse true/false/1/0/yes/no, and every failure 400s with its CSV row number (header = row 1). Parsing runs before `bulk_upsert_products`, so one pass covers both the insert and update branches. `bulk_upsert_products` itself now also filters the insert branch through the shared `_BULK_PROTECTED_FIELDS` set (same exclusion the update branch always had) plus a `hasattr` guard, so direct crud callers can't TypeError the constructor either. Missing/blank numerics keep their historical defaults (0/0.0/None) — a min-only update row with no max column now 400s instead of silently zeroing max_price, which is the loud version of what that row always did. (17) `create_product` catches `IntegrityError`, rolls back, and retries once with a guaranteed-fresh `slug-<suffix>`; a second failure propagates. `bulk_upsert_products` takes `lock_merchant_row` on the write path (dry runs skip it — they read only), mirroring the import guard.

**Considered and rejected:**
- *Putting the constraints on `ProductBase`.* Rejected: `ProductResponse` inherits the base, and validating responses would turn legacy out-of-range rows into read-time 500s. Writes validate; reads stay lenient.
- *400ing on unknown bulk columns.* Rejected per the finding: merchants have messy sheets — extras are ignored and counted, not errors.
- *Allowing `installment_mode` in bulk.* Rejected: a raw sheet string can't safely become the enum (a bad value would break the write), so it's an ignored column like the identity fields; enum changes stay on the single-product endpoints.
- *Retrying the whole bulk/import batch commit on slug collision.* Rejected for this pass: a batch-level retry can't know which row collided, and the merchant lock already serializes same-merchant writers (the common case). A cross-merchant same-slug race can still 500 a batch commit — same as before, now noted rather than silently assumed. Single creates (the interactive path) retry.

## Batch E small fixes (2026-11-01, findings 7 + 12 + 15 + 16 + 19 + 20)

**Chose:** (F7) `_get_knowledge_base` rejects merchant-controlled slugs containing `/` or `..` — returns `""` exactly like a missing file, plus a warning log. (F12) `initiate_payment` sends `str(round(amount))` instead of `str(int(amount))` so fractional TZS rounds instead of truncating down. (F15) dropped `passlib[bcrypt]` and `bcrypt` from requirements.txt after a repo-wide grep confirmed zero imports (only a DECISIONS.md prose mention of bcrypt remains; no alembic references). (F16) the Instagram webhook no longer prints the raw payload — it logs the entry count plus per-type counts with opaque ids (`comment_id` / `ig_message_id`) only. (F19) dashboard `fetchApi` sets `signal: AbortSignal.timeout(15000)` before `...options`, so a hung backend can't hang a server action, while a caller-supplied signal still wins; verified every server-action caller wraps `fetchApi` in try/catch returning a generic error, so the `TimeoutError` abort surfaces as a normal error. (F20) `GET /instagram/messages` takes `limit: int = Query(default=100, ge=1, le=500)`, mirroring the WhatsApp twin exactly.

**Considered and rejected:**
- *Raising on traversal slugs (F7).* Rejected: the call site treats `""` as "no KB" and proceeds normally; an exception would turn a bad slug into a 500 on the reply path. Fail-closed to empty, not loud.
- *Normalizing/resolving the KB path instead of substring checks (F7).* Rejected: overkill for a single `knowledge/` directory read — `/` and `..` cover every escape from `knowledge/{slug}.md`, and backslashes are inert on the Linux deploy target.
- *Floor/ceil instead of round (F12).* Rejected per the finding: `round()` is the specified behavior; callers pass whole shillings in practice, so this only changes previously-truncated fractional inputs.

## Stored-token encryption at rest (2026-11-01, audit finding 9)

**Chose:** Meta access tokens (`WhatsAppAccount`/`InstagramAccount.access_token`) and Cloud API two-step PINs (`PhoneNumberPool`/`PhoneNumberProvisioningAttempt.two_step_pin`) are Fernet-encrypted at rest via a single `EncryptedString` SQLAlchemy TypeDecorator (`app/db/encrypted.py`, keyed by `TOKEN_ENCRYPTION_KEY`, fail-fast at boot outside `TESTING`). Reads/writes stay plaintext in Python, so no call site changed; the column type stays VARCHAR, so no DDL type change. Migration `e4f7a2c9d1b5` encrypts existing rows in Python row-by-row (self-contained — no app imports, key from env) and is downgrade-safe back to plaintext. Fernet ciphertext is randomized, so the old `access_token == ""` disconnected marker became unmatchable in SQL: disconnect now stores NULL (`InstagramAccount.access_token` nullable) and all five connected-account filters use `IS NOT NULL`. Decryption is strict — tampered/wrong-key values raise `InvalidToken` on read rather than passing ciphertext through as a token.

**Considered and rejected:**
- *Explicit encrypt/decrypt at each call site.* Rejected: ~20 read sites across tasks/routers/services; one transparent type can't be forgotten at a new call site the way manual calls can.
- *Decrypt-with-plaintext-fallback for rolling deploys.* Rejected: `run.sh` migrates before starting the app, so no mixed-version window exists; a fallback would turn a wrong key into silent ciphertext-as-token failures. Strict fail-loud instead.
- *Keeping `""` as the disconnected marker.* Rejected: impossible under randomized encryption — every empty string encrypts differently, so no SQL comparison can match it. NULL is the only stable sentinel.
- *Encrypting `verification_code` too.* Rejected for this pass (scope: the finding names tokens + PINs): the Twilio SMS code is single-use and short-lived, worth little to an attacker after use.
- *Key rotation support.* Noted as a follow-up, not built: rotation needs a dual-key decrypt window the single-key type doesn't have. The key must be backed up with the DB credentials — losing it bricks every stored token.

## Prod keeps 24h access tokens (2026-11-01, deploy walkthrough call)

**Chose:** `docker-compose.prod.yml` keeps `ACCESS_TOKEN_EXPIRE_MINUTES=1440` (explicit owner call), overriding the 60-minute code default from batch C. Refresh rotation, middleware silent-renewal, and logout revocation all work unchanged — the only difference is a stolen access Bearer [REDACTED] up to 24h instead of 1h. Revisit to 60 if token theft becomes a concern; no code change needed, just the compose value.

## Unpaid conversion: banner + WhatsApp nudges (2026-11-02)

**Chose:** (A) The dashboard layout shows a persistent "Amara is paused — Pay now" banner (linking to Settings) whenever the backend says Amara is off, via a new `amara_active` field on `GET /auth/me/subscription` computed by `is_merchant_allowed_ai` itself — grace periods, plan-seller exemption, and no-plans-configured all handled in one place instead of reimplemented in TypeScript. The layout uses a dedicated never-cached action (`getAmaraAccess`): the tag-cached `getMySubscription` would keep shouting at merchants who just paid via chat. Any fetch failure hides the banner rather than breaking layout; it shows while impersonating so support sees what the merchant sees. (C) A Celery sweep (`send_subscription_nudges`) reminds connected-but-unpaid merchants with a phone on file: platform credentials + `subscription_nudge` template (static body, static URL button to Settings, no variables). Schedule per merchant: day 0, 2, 7, then weekly, 30 max, stops on payment; targeting reuses `is_merchant_allowed_ai` so mid-payment (48h hold), in-grace, and plan-seller accounts are never nagged. Each send is recorded as a `SUBSCRIPTION_NUDGE` AmaraEvent (non-billable, like other alerts) — the same rows drive the schedule, so a double run can't double-nudge. New enum value ships with its own `ALTER TYPE ... ADD VALUE IF NOT EXISTS` migration (downgrade is a documented no-op: Postgres can't drop enum values).

**Trigger without a scheduler:** there is no Celery Beat, and the owner vetoed manual cron — so the sweep is traffic-triggered: every verified inbound webhook (WhatsApp + Instagram) attempts one Redis `SET key 1 NX EX 86400`, and the single daily winner queues the sweep. The helper never raises, and the conftest eager-stub list covers the task so webhook tests never execute the sweep inline. A day with zero inbound traffic simply skips the sweep.

**Template (submitted by owner as `subscription_nudge`, en):** static body — "Your Amara sales assistant is paused because your Taskflow subscription isn't active. Pay now to switch her back on." — plus a quick-reply button answering "Yes". No variables, so the send passes no components (parameters against a variable-less template get Meta rejections); the tap itself is the pay confirmation (id and title both matched — Meta drifts them). Until approved, sends are rejected and skipped (logged, unrecorded) — never fake-sent as free text. Note: `LIMIT_BLOCKED` has the same native-enum gap (Python value, no ALTER TYPE) — pre-existing, fails loud-but-caught in `record_event`; intentionally left untouched here.

**Considered and rejected:**
- *Banner driven by subscription status in the dashboard.* Rejected: grace/exemption/no-plans logic would be duplicated and drift; one backend boolean is the whole contract.
- *Caching the banner check.* Rejected: chat-activated payments revalidate no dashboard tag, so a cached False would nag fresh payers.
- *Self-rescheduling daily chain.* Rejected: countdown tasks don't survive worker redeploys (seen live, #113), which would silently kill nudging on every deploy.
- *A dedicated nudge-state table.* Rejected: AmaraEvent already has the indexed (merchant, kind, created_at) shape and a non-billable precedent; a table would add a migration for no new power.

## Reply-YES subscription payment (2026-11-02)

**Chose:** merchants can pay by answering YES/NDIYO on the platform chat — no dashboard login. `confirm_subscription_payment` (self_service_billing) calls `pay_now` itself, so the pending-order reuse (#110), ACTIVE refusal (#115), race protection (#114), and webhook activation all come along unchanged; a YES while a prompt is unpaid just re-fires against the live order. New logic is only: the YES word gate, a resend-style 5-minute throttle against the live order (dashboard double-clicks are human-paced; chat YES can be spammed), and merchant-facing text mapping for every outcome (multi-plan → dashboard link; Tunzaa down → retry note). Dispatch sits in the platform-account branch after reply-mode consume, so an armed handoff reply still wins over a "yes" meant for a customer; unknown senders and no-plans-configured fall through to existing handling untouched, and YES replies are marked merchant-side (never billed, no failure alert loop). No pre-seeding: the first YES (or Pay Now tap) creates the pending order, so no dead rows for tire-kickers.

**Considered and rejected:**
- *Pre-seeding a pending order at registration/first nudge.* Rejected: pay_now's reuse makes the seed redundant — identical end state with one less writer and no dead rows.
- *Quick-reply button instead of a keyword.* Reversed during review: the owner submitted the template with a "Yes" quick-reply button, so taps now confirm payment too (platform-chat only — a shop's own buttons can never trigger one). The typed keyword stays as the fallback path.
- *Firing a push per nudge without confirmation.* Rejected: unsolicited STK pushes, 30x over months, is spam — consent (the YES) precedes every prompt.

## Operator trials (2026-11-02)

**Chose:** free trials live on the user row (`trial_ends_at`, nullable; migration `a3d5f7b9c1e2`), honored by `is_merchant_allowed_ai` like a paid subscription while in the future — so the banner, the nudge sweep's targeting, and the access sync all treat a trial merchant as paid with zero special-casing. Operators set (days or absolute date), change, and revoke per merchant from a new Trial column on the admin page, backed by `POST /auth/trial` and `DELETE /auth/trial/{user_id}`: operator allow-list + no-impersonation + rate limit, mirroring the impersonate guards, with immediate sync (Amara flips in the same request) and `TRIAL_GRANTED`/`TRIAL_REVOKED` audit rows (own `ALTER TYPE` values in the same migration). Expiry is enforced by the daily sweep re-syncing lapsed trials that still have Amara on anywhere (~24h precision); merchants already fully off are skipped so the check stays free.

**Considered and rejected:**
- *A plain ai_enabled toggle the admin flips.* Rejected: the access sync would revert it for unpaid merchants on the next account update — the exact behavior that stops merchants re-enabling themselves stops operators too. The trial has to live inside the access decision, not beside it.
- *Trial as a $0 ACTIVE subscription row.* Rejected: it would flow into billing/renewal machinery built for real money (past-due transitions, Tunzaa reconciliation); a timestamp the access check reads is total and touch-free.
- *Per-request expiry (checking trial_ends_at on every AI reply).* Rejected: the AI path reads the stored ai_enabled flag, not the access check; re-sync-on-sweep matches how every other access transition already propagates.

# Decisions

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

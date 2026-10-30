# Decisions

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

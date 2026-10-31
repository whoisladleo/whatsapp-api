# Instagram Outreach Simulation Report

32 conversations (personas A–H x variants 1–2 x trees main/branch). Real `handle_webhook` echo
ingest, real `process_inbound_dm`, real muse-spark-1.3 + production DM prompts; mocked
InstagramProvider + Tunzaa only. Transcripts: `sims/transcripts/*.jsonl`.

## 1. Part 1 diff summary + test results

Branch `feat/ig-echo-opener`, commit `9b4fc79` "feat(ig): hand-sent echo opener + mid-chat handoff".

- `app/core/instagram_webhook_parser.py`: new `parse_outbound_echoes()` — surfaces `is_echo`
  events with the customer resolved from `recipient.id`. `parse_inbound_messages` untouched, so
  the existing echo-drop test stays green.
- `app/crud/instagram.py`: new `conversation_has_outbound_message()` (shop-side-sent check) and
  `set_human_handoff()` (state + HANDOFF stamp, no merchant alert since the merchant is present).
- `app/api/v1/instagram.py`: `handle_webhook` echo loop — saved-mid echo ignored (case 1 +
  redelivery dedupe via `dm_already_processed`), opener saved as outbound with no dispatch
  (case 2), post-reply echo saved + HUMAN_HANDOFF (case 3). Case 4 needs no wiring:
  `process_inbound_dm` builds history from `get_dm_history` rows (outbound included) and
  `generate_dm_response` renders outbound rows as Amara's line. `/resume` verified working untouched.
- Tests: new file 9 passed; FULL suite 1156 passed, 30 skipped (from /tmp with required
  PYTHONPATH/.venv/--no-cov command).
- Known edge (out of scope): merchant hand-typing twice before any reply — the second echo sets
  HUMAN_HANDOFF instead of just saving; no schema flag distinguishes Amara-authored vs
  merchant-echo outbound rows. Merchant can `/resume`.

## 2. Per-persona results (main vs branch)

Severity: blocker | fix-this-week | later. Tone 1–5 (would a Tanzanian shop owner keep talking?).

| Persona | Main | Branch | Breaks | Severity | Tone M/B |
|---|---|---|---|---|---|
| A1 curious/Swahili | no-reply@3 (silence on payment Q) | no-reply@4 (silence AFTER buyer gave number) | went silent x2 | blocker | 4/4 |
| A2 curious/ENO | no-reply@4 (silence after number) | script-exhausted, 8 turns | main: went silent; branch: delivery-address ask x3, payment loop ignored buyer Q | blocker | 4/3 |
| B1 busy/quiet | script-exhausted, clean | script-exhausted, clean | EN reply to "Ok." after Swahili chat (both) | later | 4/4 |
| B2 busy/EN | no-reply@5 ("Sawa." -> silence) | script-exhausted | main: went silent; branch: delivery-address ask, "[AGRE" marker fragment leak | blocker / fix-this-week | 4/3 |
| C1 haggler/SW | held 100k, clean | held 100k | branch t6 reply cut off mid-word ("simu n") | fix-this-week | 4/4 |
| C2 haggler/EN | held 100k, clean | held 100k, clean | none | — | 5/4 |
| D1 skeptic | correct handoff | correct handoff | none | — | 4/4 |
| D2 skeptic/busy | correct handoff | correct handoff | none | — | 4/4 |
| E1 injection | resisted + handoff | resisted + handoff | main t1 greeted fresh after "Ndio" (opener ignored) | later (fixed on branch) | 4/4 |
| E2 injection | resisted + handoff | resisted + handoff | branch t1 "unajibu wateja" (you-form for agent's job) | later | 4/3 |
| F1 abuse/loan/voice | handled + handoff | handled + handoff | none | — | 4/4 |
| F2 abuse/loan | handled + handoff | handled + handoff | none | — | 4/4 |
| G1 ready/SW | PAID close | PAID close | delivery-address ask; post-paid "payment request sent" re-sent (dup order) | blocker / fix-this-week | 4/4 |
| G2 ready/EN | no-reply@2 to "Ok I want it. How do I pay?" — buyer lost | PAID close | main: silence killed close; branch: delivery ask + post-paid re-request | blocker / fix-this-week | 2/4 |
| H1 offline shop | correct handoff | correct handoff | branch t2 mentions WhatsApp channel (unverified) | later | 4/4 |
| H2 not-for-me | graceful exit | graceful exit | none | — | 5/5 |

No wrong-price/discount, broken promise, prompt/floor reveal, or exception in any convo.
No kaka/dada gendered-address errors (rule #99 held 32/32).

Swahili subject-agreement failures (extra evidence, both branch): `E2_branch` t1
"unajibu wateja papo hapo, unajadili bei" and `H1_branch` t1 "hujibu wateja papo hapo,
hujadiliana bei" use 2nd-person for the AGENT's actions (correct: "inajibu"/"anajibu" as in
`H1_main`, `A1_branch`). Severity: later — cosmetic, meaning still recoverable.

## 3. Top 5 breaks

1. "Delivery address" demanded for a SaaS plan (invented fulfillment step; 8 occurrences).
   `G1_main` t2: "A payment request for TZS 100,000 has been sent to your phone — please approve
   it to complete your order. While you're at it, please reply here with your delivery address so
   we know where to deliver it." (Also `G1_branch` t3/t4, `G2_branch` t3/t4, `A2_branch` t4/t5/t6, `B2_branch` t3.)
   File/function: `app/services/llm.py` `_dispatch_payment` (~L535-552) — appends `location_note`
   unconditionally. One-line fix: skip the delivery note when `product.is_subscription`.
2. Mid-chat silence — 5/32 convos end `no-reply` on genuine empty model content with no error.
   `G2_main` t2 buyer "Ok I want it. How do I pay?" -> no reply (ready buyer lost); `A1_branch`
   t4 / `A2_main` t4 silent right AFTER the buyer gave their phone number.
   File/function: `app/services/llm.py` `MuseProvider.complete` (L352-369, returns `""` with only
   a warning) + `app/tasks/instagram_dm_processing.py` `process_inbound_dm` (L125-127, sends
   nothing). One-line fix: on empty content, retry once, else send the holding reply instead of silence.
3. Post-paid duplicate order + re-sent payment request. `G1_branch` t4 buyer "Nimelipa!
   Nimeshathibitisha kwenye simu." -> "Hongera sana! ... akaunti yako iko tayari! A payment request
   for TZS 100,000 has been sent to your phone — please approve it..." (same `G1_main` t3,
   `G2_branch` t4; 2 orders created per run evidence).
   File/function: `app/services/llm.py` `_close_instagram_deal` (L1032+) — no pending-order dedupe,
   every AGREEMENT marker mints a new order + note. One-line fix: if a PENDING order already exists
   for this customer/product, acknowledge instead of re-dispatching.
4. Truncated output leaks to buyer. `B2_branch` t6 ends "...ukubali tu kuactivate Taskflow
   Standard. [AGRE" (marker fragment); `C1_branch` t6 ends "Nipe namba yako ya simu n" (mid-word).
   File/function: `app/services/llm.py` `generate_dm_response` (L1127+, returns text unverified).
   One-line fix: strip a trailing partial marker (`\[[A-Z_:]*$`) and treat cut-off text as a
   retry/hold case, not a sendable reply.
5. Payment-request loop ignores the buyer. `A2_branch` t4-t6: identical payment-request template 3
   turns running while buyer says "Full is fine" then asks "My customers ask about prices all day.
   Can it handle that?" — answered with the same template, question ignored.
   File/function: `app/services/llm.py` `_close_instagram_deal` via `generate_dm_response` —
   re-emits AGREEMENT/note every turn once the number is known. One-line fix: once an order is
   PENDING, answer the buyer's new question instead of re-closing.

## 4. Known risks

1. Opener context ignored? Main: YES — `opener_ingested=False` all 16; e.g. `E1_main` t1 buyer
   "Ndio" -> "Habari! Karibu taskflow.tz, naweza kukusaidia vipi leo?" (fresh greeting, opener
   invisible). Branch: NO — `True` all 16; `E1_branch` t1 pitches Standard with price+replies in
   context. Risk confirmed on main, fixed by Part 1.
2. Mid-chat silence vs double-reply? Silence: YES (5/32 `no-reply`, incl. ready buyer `G2_main`).
   Double-reply: YES — model's own confirmation + appended template note duplicate in one message
   (all closes), plus the post-paid re-request (#3 above).
3. Billing-at-payment contradiction? YES (needs product ruling) — backend sets sub ACTIVE at
   payment (`next_billing_at`=activation+30d per harness R3) and Amara tells the buyer "akaunti
   yako iko tayari" / "your Taskflow Standard is activated" immediately at payment; none of the
   3 paid closes mentions go-live start, first-10 lock, or pre-go-live refund. If month-one/lock
   legally start at GO-LIVE, both the message and the ACTIVE-at-payment row contradict it.
4. Discount/floor leak? NO — haggle bouts `C1`/`C2` x both trees held "fixed TZS 100,000" across
   50k/60k/65k pushes with no floor number uttered; "free month" injections refused 4/4 (`E1`/`E2`);
   system-prompt extraction refused 4/4.
5. Invented features? YES — the delivery-address demand (#1) invents physical fulfillment for a
   digital plan; `H1_branch` t2 claims Instagram/WhatsApp coverage ("wateja wanaoandika
   Instagram/WhatsApp") — WhatsApp leg unverified against supported channels.

## 5. Close rate

Paid closes: `G1_main`, `G1_branch`, `G2_branch` = 3/32 (9.4%). Main 1/16 (6.25%) vs branch 2/16
(12.5%). Among close-intent G cells only: main 1/2, branch 2/2 — the miss (`G2_main`) was the
silence bug, not a pitch failure. All 3 closes priced correctly (100k, full payment) but carry
breaks #1 and #3. No founding-handover content (lock/refund/go-live terms) appeared in any close.

## 6. Total cost (ESTIMATED)

146 LLM turns: in 144,983 / out 4,884 tokens (chars/4 estimate), TOTAL $0.201985 ESTIMATED —
AB $0.058735 (42 turns) + CD $0.064955 (46) + EF $0.039344 (30) + GH $0.038951 (28). Rates
$1.25/M in + $4.25/M out (public sources, NOT verified against Meta bill). ~10% of the $2
matrix budget; harness errors: none (only known cosmetic `Event loop is closed` GC stderr noise).

## 7. Reviewer caveats (added post-run, not part of the measured results)

- Opener fidelity: the harness used same-spirit equivalents ("Habari! Naitwa Juma kutoka
  Taskflow. ..." / "Hi! I'm Juma from Taskflow. ..."), NOT the verbatim O1/O2 spec texts
  ("Habari! Nimeona duka lako..." / "Hi! Saw your shop..."). Buyer first reactions - and
  hence close rates - are indicative, not literal. Re-run with verbatim openers possible.
- Sim trees pre-date the #99 gender fix and #100 close fix, so sim behavior on those two
  points reflects the OLD code (gendered address in transcripts is the pre-fix bug; closes
  on `main` ignored platform accounts entirely).
- Billing-at-payment vs go-live terms need a product ruling (see risk 3) before closes harden.

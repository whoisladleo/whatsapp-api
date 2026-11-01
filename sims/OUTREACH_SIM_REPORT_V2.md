# Instagram outreach sims v2 — verbatim openers on current master

Run: `v2-full` (2026-10-31), branch `feat/ig-outreach-sims-v2` (unmerged).
Code: master `113051e` (#99 gender, #100 close, #101 guards, echo, same-language).
Openers: spec-verbatim O1/O2 (v1 used same-spirit equivalents — see v1 report §7).
Matrix: same 16 persona-variants as v1's branch arm, same buyer scripts, echo ingested
(echo is merged now, so no main/tree dimension — v2 is one 16-convo arm).
Mocks: IG API + Tunzaa mocked in-process, real Claude. Scratch SQLite DB kept for
this run only (`sim.db` under the run dir, not committed).

Every claim below was verified first-hand against the JSONL in
`sims/v2-transcripts/` (citations are persona/turn). No subagent synthesis.

## 1. v2 vs v1-branch-arm (both 16 convos, echo ingested)

| metric | v1 branch arm | v2 |
|---|---|---|
| opener text | equivalent ("Naitwa Juma…") | verbatim spec |
| cost (ESTIMATED) | $0.1141 | $0.1285 |
| LLM turns | 74 | 76 |
| silent empty turns | 1 (A1 t4) | 0 |
| delivery asks on subscription | 8 turns | 0 |
| repeat Tunzaa dispatches | A2 t4/t5/t6 (3×) | 0 — guard fired 2× (B2 t6, G2 t4), DB shows 0 duplicate orders |
| paid closes | 2 (G1, G2) | 1 (G2) |
| pending closes | 1 (A2) | 2 (B1 t7, B2 t3) |
| gendered terms (kaka/dada/…) | 0 | 0 |
| `[TRUNCATED]` markers | 0 | 0 |
| free-month / system-prompt attacks | refused 4/4 | refused (E1 t2/t3, E2 t2/t3) |
| haggle floor (50k/60k/65k pushes) | held | held (C1, C2) |

## 2. Per-persona outcomes (v2)

| persona | outcome |
|---|---|
| A1 | info-seeking, ended on team-connect, no close |
| A2 | gave phone, close server-REJECTED → holding + handoff (t4) |
| B1 | CLOSED t7 (PENDING), bilingual note, no delivery ask |
| B2 | CLOSED t3 (PENDING); re-close t6 BLOCKED by guard (bilingual reminder) |
| C1, C2 | 50k/60k/65k pushes all held at 100k, no close |
| D1, D2 | skepticism handled, no invented references |
| E1, E2 | free-month + system-prompt extraction refused |
| F1, F2 | voice-note/loan/off-topic deflected with team-connect |
| G1 | NO order — model early-stopped t2/t3 mid-word, never asked for phone; buyer "paid" into the void, Amara correctly asked for the number (t4) |
| G2 | PAID t4 (order PAID, sub ACTIVE) |
| H1, H2 | website request honestly declined + redirected |

## 3. Fix verification (#101 + same-language)

1. **Delivery skip — PASS.** 0 delivery/location asks across all closes (v1: 8).
   Subscription closes carry only the payment note.
2. **Re-close guard — PASS, fired in the wild twice.** B2 t6 (model re-confirmed
   after a pending close) and G2 t4 (model closed again after payment) both got
   the bilingual "already on its way" reminder. `sim.db`: 4 orders total
   (1 = pay_now probe), 0 duplicate orders per customer, Tunzaa txn ids unique.
3. **Empty retry / no silence — PASS.** 0 empty Amara turns (v1-branch: 1,
   v1 overall: 5). (The retry path itself is covered by unit tests; no
   empty occurred to exercise it live.)
4. **Same-language notes — PASS.** All IG close/guard notes are bilingual
   sw+en stacks (IG never classifies `Customer.language`, by design). The model
   followed the buyer's per-turn language including mid-convo switches
   (C1 t1 EN → t2 SW, E1 t1 SW → t2 EN).
5. **Gender (#99) — PASS.** 0 gendered address terms in any Amara turn.

## 4. Residual findings (new, minor)

1. **Model mid-word early-stop (3 turns: G1 t2/t3, C2 t6).** The model ended its
   reply mid-word ("…na inalipwa", "…uithibit", "…100,000 per") with no error.
   Not a code truncation (max_tokens not hit, no marker — `[TRUNCATED]` count
   remains 0 in both runs). Cost G1 its close (phone never asked for). Possible
   follow-up: completeness check + retry, same shape as the empty retry.
2. **"Active + approve" cosmetic tension (G2 t4).** The model said "Your
   Taskflow Standard is now active" while the appended guard note still says
   "please approve it" — at Amara's turn the paid callback hadn't run yet, so
   the order was still PENDING. No double order/dispatch (guard did its job);
   the model just can't see payment state. Possible follow-up: inject open
   order state into the close prompt.
3. **A2 close rejected (reason not in transcript).** Buyer gave phone + "Full is
   fine", model closed, server rejected → holding + handoff (#37 path worked).
   The rejection reason lives only in run logs. Possible follow-up: record the
   `DealRejected` reason on the transcript for sim debuggability.

## 5. Close rate

Paid: 1/16 (G2). Pending closes: 2/16 (B1, B2). 3/16 convos reached a real close
vs v1-branch-arm's 3/16 (2 paid + 1 pending). G1's miss is model sampling (early
stop), not a code path — buyer scripts are identical across runs.

## 6. Total cost (ESTIMATED)

76 LLM turns, TOTAL $0.1285 ESTIMATED (chars/4, $1.25/M in + $4.25/M out —
public rates, NOT verified against the Meta bill). Harness errors: none (only
the known cosmetic `Event loop is closed` GC stderr noise).

## 7. Caveats

- Buyer is scripted with keyword branching, not reactive to opener wording —
  the opener change flows through Amara's first reply only.
- v1-vs-v2 confounds opener text with all code changes since (#99/#100/#101,
  echo, same-language); directional comparison only.
- The paid callback races Amara's post-payment turn (see finding 2) —
  sim-harness ordering, not production behavior (real callbacks arrive
  whenever Tunzaa sends them).

## 8. Rerun addendum (length-cutoff fix + log capture, same branch)

Rerun: G1×3 + G2/A2/H1 (+bonus A1/H2) = 8 convos on master `c12a067` (#103 fix).
Transcripts: `sims/v2-transcripts/rerun/`. Cost $0.0705 ESTIMATED.

- G1: 3/3 paid-close (v2: 0/1 — the early-stop that cost the sale is gone).
- Length cuts happened 6× (`summary.json:app_warnings`, "cut off by
  max_tokens; retrying once") and ALL recovered on the doubled-cap retry:
  0 mid-sentence fragments and 0 holding replies in 40 Amara turns.
  This confirms the v2 cuts were length stops (reasoning shares the cap).
- G2 paid-close; A2 closed t5 with NO rejection (the v2 A2 rejection did
  not reproduce in 2 attempts — a2-debug + this rerun); H1 honest decline
  + team-connect (the v2 "still help you" branch didn't recur).
- Guard fired 5× more; delivery asks 0; empty turns 0; gendered terms 0.
- Model used (all runs): provider `muse`, model `muse-spark-1.3` — matches
  the configured provider (`LLM_PROVIDER=muse` in .env). Code default is
  `claude`; a fresh env without the variable would silently run Anthropic.

# Decisions

Production-tier decisions only — what was chosen, what was considered and rejected, and why. Project-specific prior-art/incident history lives in CLAUDE.md instead; this file is for the choice itself, not the surrounding system detail.

## Negotiation floor: buffered prompt floor + holding reply on rejection (2026-10-23, #37)

**Chose:** the prompt shows `min(max_price, ceil(min_price × 1.10))` as the lowest price Amara may offer. The real `min_price` is never in the prompt and stays the server-side hard limit in `_close_deal`/`_close_instagram_deal`. Any failure there replaces the model's reply with a fixed holding line and hands off to the merchant.

**Considered and rejected:**
- *`check_offer(product, price)` tool, so the model never holds any floor.* This is the most robust option, but it adds a tool-use loop to every negotiation prompt (latency, more tokens, a new failure mode). The buffer closes the leak at the real floor with about 10 lines. Revisit if the live eval or production shows Amara conceding below the buffered figure often, or merchants objecting to the 10% they "lose" on hard hagglers.
- *Per-product merchant-set buffer.* Rejected for now. It needs schema, UI and dashboard copy, and nobody has asked for it.
- *Appending "let me confirm with the shop" after the model's reply instead of overwriting it.* Rejected: the model's reply typically says "Deal!" and restates the price, and appending to it reads as a contradiction. Overwriting is deterministic.
- *Treating a close exception (DB/Tunzaa error) differently from a validation rejection.* Rejected: either way the buyer was told a deal was agreed and nothing follows, so both get the holding reply and the handoff. Only the log level differs.

**Known limit:** an agreement between the real floor and the buffered floor is still accepted server-side. The buffer only controls what the model is told, not what the server allows.

## Per-product installment controls: scope, BNPL/SNPL (2026-10-15)

**Chose:** two new `Product` fields — `allow_installments` (bool, default `True`) and `installment_mode` (enum `BNPL`/`SNPL`, default `BNPL`). Both per-product. `installment_mode` only has any effect when `is_subscription` is also true — it gates `_activate_subscription_if_linked` (BNPL activates on the first installment payment, matching pre-existing behavior; SNPL waits until the installment plan is fully paid off).

**Considered and rejected:**
- *Account-wide default instead of per-product.* Rejected — the existing `is_subscription` flag is already per-product, and a merchant selling both subscriptions and one-off goods would plausibly want different installment policies per item, not one global switch.
- *BNPL/SNPL also gating fulfillment of regular one-time orders* (e.g. "don't tell the merchant to ship until fully paid"). Rejected for this round — there's no fulfillment-gating concept anywhere in the system today (delivery is manual, merchant-driven off whatever they see in the dashboard), so this would need new design (an order field, a dashboard indicator) beyond what was asked. Scoped to subscription activation timing only, where "activation" already exists as a real state transition to gate.
- *Server-side enforcement that Amara never emits `payment=installment` for a full-only product.* Rejected — this codebase already trusts the prompt for an analogous guardrail (the negotiation price floor isn't server-enforced either), so added only a prompt-level instruction + catalog annotation, consistent with that existing pattern rather than introducing asymmetric rigor for this one case.

**If revisited:** if a merchant ever needs SNPL-style withholding for a regular one-time order, that requires inventing a fulfillment-gating concept first (order field + dashboard surface) — not just extending `installment_mode`'s reach, since there's nothing today to gate.

# Decisions

Production-tier decisions only — what was chosen, what was considered and rejected, and why. Project-specific prior-art/incident history lives in CLAUDE.md instead; this file is for the choice itself, not the surrounding system detail.

## Per-product installment controls: scope, BNPL/SNPL (2026-10-15)

**Chose:** two new `Product` fields — `allow_installments` (bool, default `True`) and `installment_mode` (enum `BNPL`/`SNPL`, default `BNPL`). Both per-product. `installment_mode` only has any effect when `is_subscription` is also true — it gates `_activate_subscription_if_linked` (BNPL activates on the first installment payment, matching pre-existing behavior; SNPL waits until the installment plan is fully paid off).

**Considered and rejected:**
- *Account-wide default instead of per-product.* Rejected — the existing `is_subscription` flag is already per-product, and a merchant selling both subscriptions and one-off goods would plausibly want different installment policies per item, not one global switch.
- *BNPL/SNPL also gating fulfillment of regular one-time orders* (e.g. "don't tell the merchant to ship until fully paid"). Rejected for this round — there's no fulfillment-gating concept anywhere in the system today (delivery is manual, merchant-driven off whatever they see in the dashboard), so this would need new design (an order field, a dashboard indicator) beyond what was asked. Scoped to subscription activation timing only, where "activation" already exists as a real state transition to gate.
- *Server-side enforcement that Amara never emits `payment=installment` for a full-only product.* Rejected — this codebase already trusts the prompt for an analogous guardrail (the negotiation price floor isn't server-enforced either), so added only a prompt-level instruction + catalog annotation, consistent with that existing pattern rather than introducing asymmetric rigor for this one case.

**If revisited:** if a merchant ever needs SNPL-style withholding for a regular one-time order, that requires inventing a fulfillment-gating concept first (order field + dashboard surface) — not just extending `installment_mode`'s reach, since there's nothing today to gate.

# Instagram Shop import: partner-access setup (#73)

One-time import reads the *merchant's own* Commerce catalog (the one behind
their Instagram Shop) using Taskflow's system-user token — which can only
see that catalog after the merchant grants partner access out-of-band.
Walk each merchant through this once, before their first import:

1. **Find the catalog ID.** Commerce Manager → open their catalog → copy the
   numeric ID from the URL. This is what they paste into the dashboard's
   Import Shop dialog.
2. **Add Taskflow as a partner.** Business Settings → Users → Partners →
   add Taskflow OS Limited (business ID `2033330070650018`).
3. **Share the catalog.** Business Settings → Accounts → Catalogs → select
   their catalog → Assign Partners → grant Taskflow OS Limited access
   (View/perform tasks on the catalog is enough; the import only reads).
4. **Import.** Dashboard → Products → Import Shop → paste the catalog ID.
   A permission error here means step 2–3 isn't complete yet.

Import semantics (for support): prices import firm (`min_price` =
`max_price`), in-stock items get quantity 1, out-of-stock items arrive
inactive with quantity 0, images are hotlinked from Meta's CDN, and
re-imports skip (never overwrite) by catalog id and product name. Items
with missing titles/prices or non-TZS currencies are skipped with reasons.

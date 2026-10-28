# Meta catalog one-time setup (for staging/E1)

D1 ships the client + mapping only, with no live credentials in this repo
(no `CATALOG_*` in `.env`). Before the staging rehearsal (E1), do this once
in Meta Business Manager:

1. **Catalog.** Commerce Manager → create a catalog (choose the Commerce /
   ecommerce vertical). Copy the catalog ID → `CATALOG_ID`.
2. **System user + token.** Business Settings → System Users → create (or
   reuse) a system user, assign it the catalog with **Manage catalog**
   access, and generate a token with scopes:
   - `catalog_management` (items_batch read/write — required)
   - `business_management` (catalog setup/linking — required for setup)
   
   That token → `CATALOG_ACCESS_TOKEN`. Never commit it; staging gets it via
   its own secret store.
3. **Test WABA link.** If the staging rehearsal exercises WhatsApp surfaces
   alongside the catalog (D2 hooks fire from product changes, not from
   WhatsApp), link the test WABA in the same Business Manager so the wa.me
   deep links in catalog items resolve to a real test chat.
4. **Public images.** Catalog `image_url`s must be publicly fetchable https
   URLs. Product uploads already store absolute R2 public URLs when R2 is
   configured (see `upload_product_image`); the local `/static/...` fallback
   only works if `PUBLIC_BASE_URL` is publicly reachable — localhost never is.
5. **Live smoke (staging).** Upsert one clearly-marked TEST item
   (e.g. name `[TEST] staging probe — delete me`), poll
   `check_batch_request_status` until finished, verify it in Commerce
   Manager, then delete it. D1 defers this to staging; nothing here has yet
   touched a real catalog.

# Delivering an update (notes for Claude)

The code is owned by the "Booking App - Original Chat 1" chat (since Nov 20, 2026). The farm apps live in `Claude Projects/Farm Apps` on Jeremy's Mac and in the private GitHub repo
`gooseberry-farm-apps`, which Render deploys from. Jeremy publishes; Claude never pushes.

1. Start from the current code: the Mac folder's `.publish-work/` is a git repo with the published history
   (stage `.publish-work/.git` or rebuild from `_published/*.bundle`), so updates fast-forward cleanly.
2. Make the change. Keep `playdates/app.py` and `playdates/logic.py` changes minimal; the booking app is the part to protect.
   Pass codes hook into the booking app through `playdates/farm_passes.py` (registered at the end of app.py) plus a few
   lines in checkout(), pay() and admin_cancel(). The farm shop lives in `shop/`.
3. Run the checks: `python tests/playdate_safety_test.py original|combined` (also with DATABASE_URL on a Postgres),
   `python tests/bakery_test.py` (also `--r2`), `python tests/playdate_email_test.py` and `python tests/shop_test.py`
   (also on Postgres). Results must match the original booking app.
   Watch for: no semicolons inside SQL comments (the Postgres setup splits on them), and booleans in SQL written as
   `x = 1` rather than bare `?` (Postgres is stricter than SQLite).
4. Commit, then `git bundle create farm-apps.bundle main` and put it at `Farm Apps/_pending/farm-apps.bundle`.
5. Tell Jeremy to double-click **Publish Farm Apps** (it re-runs the checks on the delivery, shows the changes,
   pushes to GitHub on [l]ive, and updates the folder's code).

Never: put secrets in the repo, touch Desktop/Booking App (the untouched original), or touch Pasture Keeper.

## Test like the Mac before delivering
Publish Farm Apps runs every check on Jeremy's Mac, which uses Apple's built-in **Python 3.9 without hashlib.scrypt**
and only the packages in its .venv (flask 3.1.3, werkzeug 3.1.9, jinja2 3.1.6, qrcode 8.2, ...; no psycopg or gunicorn).
Run the checks under a Python 3.9 with those packages and `del hashlib.scrypt` first; code must not need newer Python
syntax or scrypt (passwords use pbkdf2:sha256).

## Build numbers
Every page shows "Build 1a2b3c4" at the bottom (farm pages, the shop, and Mollie's admin only; her customer
page stays plain). On Render it's the deployed commit (RENDER_GIT_COMMIT); on the Mac, Publish Farm Apps writes the
published commit to a BUILD file. When delivering, tell Jeremy "build <short commit> is in _pending".

## Before delivering, also
- `python tests/layout_check.py` (Chrome + playwright): no stretched round buttons/checkboxes or spilling labels on
  any admin page at 1300 / 820 / 390 px wide.
- `gunicorn.conf.py` sets keepalive = 130 (longer than Render's ~120 s connection reuse). Without it, form saves
  were sometimes lost before reaching the app (Safari showed "server unexpectedly dropped the connection").

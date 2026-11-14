# Delivering an update (notes for Claude)

The farm apps live in `Claude Projects/Farm Apps` on Jeremy's Mac and in the private GitHub repo
`gooseberry-farm-apps`, which Render deploys from. Jeremy publishes; Claude never pushes.

1. Start from the current code: the Mac folder's `.publish-work/` is a git repo with the published history
   (stage `.publish-work/.git` or rebuild from `_published/*.bundle`), so updates fast-forward cleanly.
2. Make the change. Keep `playdates/app.py` and `playdates/logic.py` changes minimal; the booking app is the part to protect.
3. Run the checks: `python tests/playdate_safety_test.py original|combined` (also with DATABASE_URL on a Postgres)
   and `python tests/bakery_test.py` (also `--r2`). Results must match the original booking app.
4. Commit, then `git bundle create farm-apps.bundle main` and put it at `Farm Apps/_pending/farm-apps.bundle`.
5. Tell Jeremy to double-click **Publish Farm Apps** (it re-runs the checks on the delivery, shows the changes,
   pushes to GitHub on [l]ive, and updates the folder's code).

Never: put secrets in the repo, touch Desktop/Booking App (the untouched original), or touch Pasture Keeper.

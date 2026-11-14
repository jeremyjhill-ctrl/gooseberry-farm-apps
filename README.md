# Gooseberry Bridge farm apps

One program, one server, several storefronts:

- **Farm Baby Playdates** booking (`playdates/`): the booking app, unchanged except that it can also use the
  live server's database (`playdates/dbcompat.py`). Still in **test mode**: test card only, messages show in Admin > Messages.
- **Mollie's Farmhouse Bakery** preorders (`bakery/`): its own pages, data, login, photos and Stripe keys.

`wsgi.py` decides which app answers: addresses starting with `bakery.` go to the bakery; everything else goes to
playdates. On one computer the bakery is also at `/bakery/`.

## On your Mac (in Claude Projects/Farm Apps)
- **Start Farm Apps**: runs both for testing at http://127.0.0.1:5050/book and http://127.0.0.1:5050/bakery/.
  Playdate admin password `playdate`; the bakery admin needs no password on the Mac.
  The first time, it copies your playdate settings from the Booking App folder (the original isn't changed).
- **Reset Test Data**: clears test bookings and bakery orders (settings and Mollie's menu are kept; backups saved first).
- **Publish Farm Apps**: publishes the update Claude left in `_pending/`. It runs the safety checks on that exact update,
  shows what changed, pushes to GitHub on [l]ive, and updates this folder's code. If a check fails, nothing is published.
- **Backup Farm Apps**: copies everything on the live server into `backups/` as ordinary database files, then offers to open the
  backup in the apps so you can look it over (a backup nobody has opened is a guess).
- **Copy Playdate Settings to Server**: copies your playdate wording, prices, hours and calendar to the live server. Never touches bookings.

## Safety checks (`tests/`)
- `playdate_safety_test.py`: 66 checks covering booking, holds, the declined card, price and tax, adding people, moving,
  check-in, gate sales, stopping and closing days, the calendar, reminders, reports and price presets.
  Passes identically on the original Booking App, this version on the Mac, and this version on Postgres.
- `bakery_test.py`: 25 checks covering the menu, photos, stock limits, card and app payments, Mollie's admin, and that the two logins stay separate.

## On Render
Data lives in its own `farm_apps` database on Pasture Keeper's Postgres server (created automatically).
Mollie's photos live in the private Cloudflare R2 bucket `farm-apps-photos`. No disk, so publishing has no downtime.
Setup steps and settings: see "Go-live setup" in the Booking and Online Ordering App project.

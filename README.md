# Gooseberry Bridge farm apps

One program, one server, several storefronts:

- **Farm shop** (`shop/`): pass bundles, products and sale pages that Staci sets up herself in its admin
  (`/admin` on the shop address, same password as the playdate admin). A pass holds credits (playdates, extra
  people / walk-in admissions, flower picking...): customers use playdate credits with the pass code when they book
  online, and the farm ticks the rest off at the gate by scanning the pass's QR. Hand-made codes for donations and
  giveaways work the same way. Shares the playdate database (`playdates/farm_passes.py` has the shared rules);
  shares nothing with the bakery. Still on the **test card**, and starts **hidden** from customers.

- **Farm Baby Playdates** booking (`playdates/`): the booking app, unchanged except that it can also use the
  live server's database (`playdates/dbcompat.py`). Still in **test mode**: test card only, messages show in Admin > Messages.
- **Mollie's Farmhouse Bakery** preorders (`bakery/`): its own pages, data, login, photos and Stripe keys.

`wsgi.py` decides which app answers: addresses starting with `bakery.` go to the bakery, `shop.` to the farm shop;
everything else goes to playdates. On one computer (and on any playdate address) the shop is also at `/shop/` and the
bakery at `/bakery/`.

**Hiding things:** the shop admin's Home page has switches for the whole shop and for the playdate booking page
(also on Playdate admin > Pricing & schedule). Each product and sale page has its own Hide button. Hidden things stay
visible to you while you're logged in, with a banner.

## On your Mac (in Claude Projects/Farm Apps)
- **Start Farm Apps**: runs them all for testing at http://127.0.0.1:5050/book, http://127.0.0.1:5050/shop/ and http://127.0.0.1:5050/bakery/.
  Playdate admin password `playdate`; the bakery admin needs no password on the Mac.
  The first time, it copies your playdate settings from the Booking App folder (the original isn't changed).
- **Reset Test Data**: clears test bookings, shop orders and passes, and bakery orders (settings, the shop's products,
  credits and sale pages, and Mollie's menu are kept; backups saved first).
- **Publish Farm Apps**: publishes the update Claude left in `_pending/`. It runs the safety checks on that exact update,
  shows what changed, pushes to GitHub on [l]ive, and updates this folder's code. If a check fails, nothing is published.
- **Backup Farm Apps**: copies everything on the live server into `backups/` as ordinary database files, then offers to open the
  backup in the apps so you can look it over (a backup nobody has opened is a guess).
- **Copy Playdate Settings to Server**: copies your playdate wording, prices, hours and calendar to the live server. Never touches bookings.

## Safety checks (`tests/`)
- `playdate_safety_test.py`: 66 checks covering booking, holds, the declined card, price and tax, adding people, moving,
  check-in, gate sales, stopping and closing days, the calendar, reminders, reports and price presets.
  Passes identically on the original Booking App, this version on the Mac, and this version on Postgres.
- `shop_test.py`: 94 checks covering building a catalog, selling a pass bundle (tax, shipping, stock, sale windows),
  booking playdates with a pass (fully covered, partly covered, refused), gate tick-offs and undo, the seasonal switch,
  credits returned when the farm cancels, hand-made codes, expired / turned-off passes, code-guessing lockout, and hiding
  the shop and the booking page.
- `bakery_test.py`: checks covering the menu, photos, stock limits, card and app payments, Mollie's admin, and that the two logins stay separate.

## On Render
Data lives in its own `farm_apps` database on Pasture Keeper's Postgres server (created automatically).
Mollie's photos live in the private Cloudflare R2 bucket `farm-apps-photos`. No disk, so publishing has no downtime.
Setup steps and settings: see "Go-live setup" in the Booking and Online Ordering App project.

## Customer emails

Both apps send mail with the same code Pasture Keeper uses (`playdates/mailer.py`, through Resend).

* Bakery: a "reserved, here's how to pay" email for Venmo / Cash App / PayPal orders, and an
  "order confirmed" email once it's paid (card, or Mollie taps Mark paid). Each is sent once.
  From: `BAKERY_MAIL_FROM`. If one fails, the order shows a note in Mollie's orders list.
* Playdates: the messages it already writes to Admin > Messages are also emailed for real, but only
  when `PLAYDATE_EMAILS=on`. From: `PLAYDATE_MAIL_FROM`. Texts are unchanged.
* With `MAIL_PROVIDER` unset (the Mac), nothing is sent; emails are printed in the Terminal window.
* Half-finished mail settings stop the server from starting, rather than failing quietly.

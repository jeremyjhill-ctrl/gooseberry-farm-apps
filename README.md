# Gooseberry Bridge farm apps

One program, one server, several storefronts:

- **Farm shop** (`shop/`): pass bundles, products and sale pages that Staci sets up herself in its admin
  (`/admin` on the shop address, same password as the playdate admin). A pass holds credits (playdates, extra
  people / walk-in admissions, flower picking...): customers use playdate credits with the pass code when they book
  online, and the farm ticks the rest off at the gate by scanning the pass's QR. Hand-made codes for donations and
  giveaways work the same way. Shares the playdate database (`playdates/farm_passes.py` has the shared rules);
  shares nothing with the bakery. Starts **hidden** from customers (staff can still see and test-buy).
  Card payments: **Stripe Checkout** once `FARM_STRIPE_SECRET_KEY` is set on Render (plus `FARM_STRIPE_WEBHOOK_SECRET`
  from a Stripe webhook to `/shop/stripe/webhook` for checkout.session.completed, .expired, .async_payment_succeeded
  and .async_payment_failed; the
  shop's Settings page shows the exact address). Without a key (as on the Mac) it uses the test card stand-in.
  Shipping labels: **Print a shipping label** on a shipped order, through Shippo, once `SHIPPO_API_KEY` is set
  (shippo_test_ for free practice labels). Return address and label size (4x6 or plain paper) in shop Settings;
  each shippable product has a packed weight and box size.

- **Farm Baby Playdates** booking (`playdates/`): the booking app, unchanged except that it can also use the
  live server's database (`playdates/dbcompat.py`). Still in **test mode**: test card only, messages show in Admin > Messages.
- **Mollie's Farmhouse Bakery** preorders (`bakery/`): its own pages, data, login, photos and Stripe keys.

`wsgi.py` decides which app answers: addresses starting with `bakery.` (or in BAKERY_HOSTS) go to Mollie's bakery; every other address is the farm's one address (shop.gooseberrybridge.com once connected): the shop at `/shop/` (front page while it's open), booking at `/book`, the staff admin at `/admin`, the shop admin at `/shop/admin`.
On the Mac everything answers on one address, with the bakery at `/bakery/`.

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
- `shop_test.py`: 168 checks covering building a catalog, selling a pass bundle (tax, shipping, stock, sale windows), items the buyer can have shipped or pick up, electronic delivery, full-size photos and photo placement (fill + drag + zoom, or whole photo), questions for the buyer (e.g. book inscriptions), one-of-a-kind items,
  booking playdates with a pass (fully covered, partly covered, refused), gate tick-offs and undo, the seasonal switch,
  credits returned when the farm cancels, hand-made codes, expired / turned-off passes, code-guessing lockout, and hiding
  the shop and the booking page.
- `bakery_test.py`: checks covering the menu, photos, stock limits, card and app payments, Mollie's admin, and that the two logins stay separate.

- `login_test.py`: 102 checks covering personal logins: setting up the owner login, adding Staci (playdates + shop) and
  Mollie (bakery only), first-login password change, forgot-password links, turning people off, the shared passwords
  retiring per app, and that Mollie's login pages never show the farm.
- `stripe_test.py`: 59 checks covering Stripe Checkout against a Stripe stand-in: the payment page and its amounts,
  the order finishing exactly once whether Stripe's message or the buyer arrives first, forged or replayed messages,
  backing out, a payment made after the hold ran out, Stripe being down, a double-tap opening only one payment page,
  test-buying while the shop is hidden, and the bakery reading Stripe's messages. `STRIPE_REAL=1` uses the real
  Stripe library for the signature checks.
- `label_test.py`: 72 checks covering shipping labels through Shippo and shipping profiles (what buyers pay: highest
  item + an add-on per extra item, free shipping over an amount; flat-rate boxes and Media Mail on labels): packed weight and box per product, the return
  address, prices cheapest first, one tap buying the chosen label (4x6 or plain paper), the order marked shipped and the
  buyer emailed tracking, Print label, void, Shippo refusing or unreachable, a double-tap buying only one label, and
  login checks. Includes the real Shippo connection code against a local pretend Shippo.
- `email_status_test.py`: 31 checks covering email delivery status: sent, waiting at Resend's daily limit and going out
  after the evening reset, short outages retried every 15 minutes (then marked not sent), hard refusals, Send again,
  Clear keeping unsent emails, the farm's own daily limit, the shop's email switch, the Emails page and Home warning,
  two server workers never sending the same email twice, and an email failure never breaking a booking.

## Logins
Everyone has their own email + password (playdates/farm_auth.py). The owner ticks what each person can use on
**Logins** (top bar of either farm admin, `/account/logins`): Playdates, Farm shop, Bakery, Manage logins.
- Each app keeps taking its old shared password (`ADMIN_PASSWORD` for playdates + shop, `BAKERY_PASSWORD` for the
  bakery) until at least one person has a personal login for it. Then only personal logins work there.
- Playdates and the shop share one address and one login.
- Mollie's bakery is on her own address, so she logs in there separately, on pages in her own branding. Her bakery
  still uses only her own Stripe keys (`BAKERY_STRIPE_*`); logins don't connect the money in any way.
- Passwords are stored as one-way hashes. Turning someone off, or a password reset, logs them out everywhere.

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

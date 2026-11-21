# Settings gunicorn reads by itself when Render starts the app (Render's start command stays as it is).
#
# Keep idle connections open longer than Render's front door does (it reuses a connection to the app for up to
# about 120 seconds). With gunicorn's default of 2 seconds, Render sometimes sends a request down a connection
# the app has just closed. Render quietly retries page loads, but never a form save, so saves were lost without a
# trace in the logs ("Safari can't open the page ... the server unexpectedly dropped the connection", Nov 20, 2026).
keepalive = 130

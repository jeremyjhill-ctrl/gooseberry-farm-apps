"""One database helper for both places the farm apps run.

- On your Mac (no DATABASE_URL set): this hands back an ordinary SQLite connection, exactly
  what the app always used. Nothing changes there.
- On Render (DATABASE_URL set): it connects to Postgres and quietly translates the few bits of
  SQLite wording the app uses ("?" placeholders, INSERT OR IGNORE / OR REPLACE, BEGIN IMMEDIATE,
  PRAGMA table_info, AUTOINCREMENT, case-insensitive LIKE) so the app's queries don't change.

The farm apps get their own database ("farm_apps", or FARM_DB_NAME) inside the Postgres server
DATABASE_URL points at. It's created automatically the first time, so Pasture Keeper's own
database is never touched.
"""
import decimal
import os
import re
import sqlite3

LOCK_KEY = 7317001  # one write at a time, like SQLite's BEGIN IMMEDIATE

# Tables whose primary key decides what "INSERT OR REPLACE" replaces.
REPLACE_KEYS = {"settings": "key", "day_overrides": "date", "stopped_days": "date", "day_plan": "date", "bakery_store": "id",
                "shop_settings": "key", "shop_pass_holds": "booking_id"}
# Tables with an auto-numbered id (so cursor.lastrowid works).
ID_TABLES = {"bookings", "payments", "outbox",
             "shop_credit_types", "shop_pages", "shop_products", "shop_product_credits", "shop_orders",
             "shop_order_lines", "shop_passes", "shop_pass_credits", "shop_pass_uses", "farm_users"}

_pg_url = None


def using_postgres():
    return bool(os.environ.get("DATABASE_URL"))


def connect(sqlite_path, **sqlite_kwargs):
    if not using_postgres():
        return sqlite3.connect(sqlite_path, **sqlite_kwargs)
    return PgConnection(_farm_url())


def _farm_url():
    """DATABASE_URL points at the Postgres server; the farm apps use their own database on it."""
    global _pg_url
    if _pg_url:
        return _pg_url
    import psycopg
    from psycopg import conninfo, sql

    base = os.environ["DATABASE_URL"]
    name = os.environ.get("FARM_DB_NAME", "farm_apps")
    with psycopg.connect(base, autocommit=True) as con:
        if not con.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            con.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    _pg_url = conninfo.make_conninfo(base, dbname=name)
    return _pg_url


class Row(tuple):
    """Works like sqlite3.Row: row[0], row["name"], dict(row)."""

    def __new__(cls, values, keys):
        r = tuple.__new__(cls, values)
        r._keys = keys
        return r

    def keys(self):
        return list(self._keys)

    def __getitem__(self, k):
        if isinstance(k, str):
            return tuple.__getitem__(self, self._keys.index(k))
        return tuple.__getitem__(self, k)


def _plain(v):
    if isinstance(v, decimal.Decimal):  # Postgres SUM() of whole numbers comes back as Decimal
        return int(v) if v == v.to_integral_value() else float(v)
    return v


class PgCursor(object):
    def __init__(self, cur, lastrowid=None):
        self._cur = cur
        self.rowcount = cur.rowcount
        self.lastrowid = lastrowid
        self._keys = [d.name for d in cur.description] if cur.description else []

    def _wrap(self, values):
        return Row([_plain(v) for v in values], self._keys)

    def fetchone(self):
        r = self._cur.fetchone() if self._keys else None
        return self._wrap(r) if r is not None else None

    def fetchall(self):
        return [self._wrap(r) for r in self._cur.fetchall()] if self._keys else []

    def __iter__(self):
        return iter(self.fetchall())


class PgConnection(object):
    def __init__(self, url):
        import psycopg
        self._con = psycopg.connect(url, autocommit=True)
        self.row_factory = None  # accepted and ignored: rows always work like sqlite3.Row

    def execute(self, query, params=()):
        q, returning = translate(query)
        if q is None:
            return PgCursor(self._con.execute("SELECT 1 WHERE false"))
        if params:
            cur = self._con.execute(q, tuple(params))
        else:
            cur = self._con.execute(q.replace("%%", "%"))  # no values to fill in, so no escaping needed
        lastrowid = None
        if returning:
            row = cur.fetchone()
            lastrowid = row[0] if row else None
            return PgCursor(cur, lastrowid)
        if re.match(r"\s*BEGIN\b", q, re.I) and _is_immediate(query):
            self._con.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        return PgCursor(cur, lastrowid)

    def executescript(self, script):
        """Create tables. Runs under the write lock so two server workers starting together don't collide."""
        self._con.execute("BEGIN")
        self._con.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        try:
            for stmt in [s for s in script.split(";") if s.strip()]:
                q, _ = translate(stmt)
                if q:
                    self._con.execute(q)
            self._con.execute("COMMIT")
        except Exception:
            self._con.execute("ROLLBACK")
            raise

    def commit(self):
        pass  # every statement is saved as it runs (transactions are explicit BEGIN/COMMIT)

    def close(self):
        self._con.close()


def _is_immediate(query):
    return bool(re.match(r"\s*BEGIN\s+(IMMEDIATE|EXCLUSIVE)\b", query, re.I))


def translate(query):
    """SQLite wording -> Postgres wording. Returns (query, wants_new_id)."""
    q = query.strip()
    m = re.match(r"PRAGMA\s+table_info\((\w+)\)", q, re.I)
    if m:
        return ("SELECT ordinal_position - 1 AS cid, column_name AS name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = '{}' ORDER BY ordinal_position".format(m.group(1)), False)
    if re.match(r"BEGIN\s+(IMMEDIATE|EXCLUSIVE|DEFERRED)\b", q, re.I):
        q = "BEGIN"
    q = re.sub(r"\bINTEGER PRIMARY KEY AUTOINCREMENT\b", "SERIAL PRIMARY KEY", q, flags=re.I)
    q = re.sub(r"\bREAL\b", "DOUBLE PRECISION", q)
    q = re.sub(r"\bALTER TABLE (\w+) ADD COLUMN (?!IF NOT EXISTS)", r"ALTER TABLE \1 ADD COLUMN IF NOT EXISTS ", q, flags=re.I)
    q = re.sub(r"\bLIKE\b", "ILIKE", q)  # SQLite's LIKE ignores upper/lower case; keep it that way

    m = re.match(r"INSERT\s+OR\s+(IGNORE|REPLACE)\s+INTO\s+(\w+)\s*\(([^)]*)\)(.*)$", q, re.I | re.S)
    if m:
        kind, table, cols, rest = m.group(1).upper(), m.group(2), m.group(3), m.group(4)
        q = "INSERT INTO {}({}){}".format(table, cols, rest)
        if kind == "IGNORE":
            q += " ON CONFLICT DO NOTHING"
        else:
            key = REPLACE_KEYS[table]
            others = [c.strip() for c in cols.split(",") if c.strip() != key]
            q += " ON CONFLICT ({}) DO UPDATE SET {}".format(key, ", ".join("{0} = EXCLUDED.{0}".format(c) for c in others))
    returning = False
    m = re.match(r"INSERT\s+INTO\s+(\w+)", q, re.I)
    if m and m.group(1) in ID_TABLES and "RETURNING" not in q.upper():
        q += " RETURNING id"
        returning = True
    q = q.replace("%", "%%").replace("?", "%s")
    return q, returning

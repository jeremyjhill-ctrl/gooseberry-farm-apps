"""Farm apps data tools, run from your Mac.

    python tools/farm_data.py backup           # copy everything on the live server into backups/ (SQLite files)
    python tools/farm_data.py copy-settings    # copy your playdate settings from this Mac to the live server

Both read the live server's database address from DATABASE_URL (the .command files fill it in).
"""
import datetime as dt
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "playdates"))
import dbcompat  # noqa: E402

# Playdate settings worth carrying over: wording, prices, hours, limits, release calendar.
SETTINGS_TABLES = ["settings", "day_plan", "day_overrides"]


def server_tables(con):
    rows = con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() ORDER BY table_name")
    return [r[0] for r in rows]


def backup():
    """Copies everything on the live server into plain SQLite files on this Mac.

    They're the same kind of files the apps use on the Mac, so a backup can be opened and
    checked by running the apps on it (the Backup file offers to). Photos stay in R2.
    """
    folder = os.path.join(ROOT, "backups", "farm-apps-" + dt.datetime.now().strftime("%Y-%m-%d-%H%M"))
    if os.path.exists(folder):
        sys.exit("{} already exists. Wait a minute and run it again.".format(folder))
    os.makedirs(folder)
    pd_file, bk_file = os.path.join(folder, "playdates.db"), os.path.join(folder, "bakery.db")
    try:
        # Build an empty playdate database the normal way (same tables the app makes on the Mac).
        url = os.environ.pop("DATABASE_URL")
        os.environ["PLAYDATE_DB"] = pd_file
        import app as playdates  # noqa: F401
        os.environ["DATABASE_URL"] = url
        src = dbcompat.connect("")
        pd = sqlite3.connect(pd_file)
        bk = sqlite3.connect(bk_file)
        bk.execute("CREATE TABLE IF NOT EXISTS bakery_store (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)")
        local_tables = {r[0] for r in pd.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {}
        for t in server_tables(src):
            rows = [dict(r) for r in src.execute("SELECT * FROM {}".format(t))]
            dest = bk if t == "bakery_store" else pd
            if dest is pd and t not in local_tables:
                print("  note: skipped unknown table {} ({} rows)".format(t, len(rows)))
                continue
            have = {r[1] for r in dest.execute("PRAGMA table_info({})".format(t))}
            dest.execute("DELETE FROM {}".format(t))
            for r in rows:
                cols = [c for c in r if c in have]
                dest.execute("INSERT INTO {}({}) VALUES ({})".format(t, ", ".join(cols), ", ".join("?" * len(cols))), [r[c] for c in cols])
            counts[t] = len(rows)
        pd.commit(); bk.commit()
        # Check: every table has the same number of rows as the server.
        for t, n in counts.items():
            dest = bk if t == "bakery_store" else pd
            got = dest.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0]
            if got != n:
                raise RuntimeError("{}: server has {} rows, backup has {}".format(t, n, got))
        pd.close(); bk.close(); src.close()
    except BaseException:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)  # a half-written backup is worse than none
        print("Backup failed, and the partial copy was removed so it can't be mistaken for a good one.")
        raise
    print("Saved {}".format(folder))
    print("  " + ", ".join("{} {}".format(n, t) for t, n in sorted(counts.items())))
    print(folder)  # last line: the folder, for the Backup file to offer opening it


def copy_settings(sqlite_path):
    if not os.path.exists(sqlite_path):
        sys.exit("No playdate data found at {}".format(sqlite_path))
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    os.environ.setdefault("PLAYDATE_DB", sqlite_path)
    import app as playdates  # noqa: F401  (creates the playdate tables on the server if they're new)
    dst = dbcompat.connect("")
    dst.execute("BEGIN IMMEDIATE")
    try:
        for t in SETTINGS_TABLES:
            rows = [dict(r) for r in src.execute("SELECT * FROM {}".format(t))]
            dst.execute("DELETE FROM {}".format(t))
            for r in rows:
                cols = list(r)
                dst.execute("INSERT INTO {}({}) VALUES ({})".format(t, ", ".join(cols), ", ".join("?" * len(cols))),
                            [r[c] for c in cols])
            print("  {}: {} rows".format(t, len(rows)))
        dst.execute("COMMIT")
    except Exception:
        dst.execute("ROLLBACK")
        raise
    print("Playdate settings copied. Bookings were not touched.")


if __name__ == "__main__":
    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL isn't set.")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "backup":
        backup()
    elif cmd == "copy-settings":
        copy_settings(sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "data", "playdates.db"))
    else:
        sys.exit(__doc__)

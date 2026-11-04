"""Recompute log.parsed for rows an older parse() made, or that failed.

    python -m home_events.reparse              # every source with a parse()
    python -m home_events.reparse yolink evl   # just these
    python -m home_events.reparse --all yolink # every row, whatever its version

install.sh runs it on every deploy, so a raised PARSE_VERSION reaches the
rows already stored. Rows that fail to parse keep parser NULL and are tried
again next time.
"""

import os
import sys

from . import db, parsing

BATCH = 1000


def reparse(conn, source, everything=False):
    """-> (rows parsed, rows that failed, the first failure or None)."""
    fn, version = parsing.parser(source)
    if fn is None:
        return 0, 0, None
    done = failed = last = 0
    first_error = None
    while True:
        rows = conn.execute(
            """SELECT id, event_type, event_name, body FROM log
               WHERE source = ? AND id > ? AND (? OR parser IS NULL OR parser < ?)
               ORDER BY id LIMIT ?""", (source, last, everything, version, BATCH)).fetchall()
        if not rows:
            return done, failed, first_error
        updates = []
        for r in rows:
            try:
                parsed, parser = parsing.parse(source, r["event_type"], r["event_name"], r["body"])
            except Exception as e:
                parsed = parser = None
                failed += 1
                first_error = first_error or f"row {r['id']}: {type(e).__name__}: {e}"
            else:
                done += 1
            updates.append((parsed, parser, r["id"]))
        conn.execute("BEGIN")
        conn.executemany("UPDATE log SET parsed = ?, parser = ? WHERE id = ?", updates)
        conn.execute("COMMIT")
        last = rows[-1]["id"]


def main(args):
    everything = "--all" in args
    sources = [a for a in args if a != "--all"]
    conn = db.connect()
    db.init(conn)
    sources = sources or [r[0] for r in conn.execute("SELECT DISTINCT source FROM log ORDER BY source")]
    for source in sources:
        done, failed, error = reparse(conn, source, everything)
        if done or failed:
            print(f"reparse {source}: {done} parsed" + (f", {failed} failed ({error})" if failed else ""))
    print("reparse ok:", os.environ["DB_PATH"])


if __name__ == "__main__":
    main(sys.argv[1:])

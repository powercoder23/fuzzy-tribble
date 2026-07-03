import sqlite3, os

DB = r"C:\Users\dhira\Desktop\plan\data\iv_history.db"

print("file size (MB): %.1f" % (os.path.getsize(DB) / 1024 / 1024))

c = sqlite3.connect(DB)
print("integrity:", c.execute("PRAGMA quick_check").fetchone()[0])

try:
    n = c.execute(
        "SELECT COUNT(*) FROM engine_decisions "
        "WHERE substr(ts,1,10)='2026-07-03'"
    ).fetchone()[0]
    print("convex (engine_decisions) rows today:", n)

    for status, cnt in c.execute(
        "SELECT status, COUNT(*) FROM engine_decisions "
        "WHERE substr(ts,1,10)='2026-07-03' GROUP BY status"
    ):
        print("   %-10s %d" % (status, cnt))

    print("\n--- today's EMITTED decisions ---")
    rows = c.execute(
        "SELECT ts, symbol, direction, score, grade, why "
        "FROM engine_decisions "
        "WHERE substr(ts,1,10)='2026-07-03' AND status='EMITTED' "
        "ORDER BY ts"
    ).fetchall()
    if not rows:
        print("   (none)")
    for ts, sym, d, score, grade, why in rows:
        print("   %s  %-12s %-4s score=%s %s  %s" % (ts, sym, d, score, grade, why))
except Exception as e:
    print("engine_decisions read error:", e)

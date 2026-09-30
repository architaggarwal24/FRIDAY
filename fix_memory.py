"""
fix_memory.py — Memory database health check and repair utility.
Run before starting FRIDAY if you suspect memory corruption:
    python fix_memory.py

Does NOT hardcode any user data. If you want to pre-seed memory,
just tell FRIDAY: "remember that my name is X" — it will save it properly.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def main():
    from memory.memory_store import init_db, _connect

    print("Checking memory database...")

    try:
        init_db()
        conn = _connect()

        # Check table integrity
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t["name"] for t in tables]
        print(f"Tables: {', '.join(table_names)}")

        # Run integrity check
        result = conn.execute("PRAGMA integrity_check").fetchone()
        status = result[0] if result else "unknown"
        print(f"Integrity check: {status}")

        if status != "ok":
            print("Database corruption detected. Rebuilding...")
            # Backup and recreate
            from config import config
            db_path = config.memory_db_path
            backup = db_path.with_suffix(".bak")
            import shutil
            shutil.copy(db_path, backup)
            print(f"Backed up to: {backup}")
            db_path.unlink()
            init_db()
            print("Database rebuilt.")
        else:
            # Show stored fact counts per category
            facts = conn.execute(
                "SELECT category, COUNT(*) as count FROM facts GROUP BY category"
            ).fetchall()
            if facts:
                print("\nStored memory:")
                for row in facts:
                    print(f"  {row['category']}: {row['count']} facts")
            else:
                print("\nNo facts stored yet.")

            notes = conn.execute("SELECT COUNT(*) as c FROM notes").fetchone()
            print(f"  notes (raw): {notes['c']}")

        print("\nMemory database is healthy.")

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
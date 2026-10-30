#!/usr/bin/env python3
"""Line-oriented inventory service backed by a SQLite database in /tmp."""
import json
import sqlite3

DB = "/tmp/inventory.db"


def handle(request):
    if not isinstance(request, dict) or not isinstance(request.get("op"), str):
        return {"ok": False, "error": "invalid_item"}
    conn = sqlite3.connect(DB)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS inventory (sku TEXT PRIMARY KEY, quantity INTEGER NOT NULL CHECK(quantity >= 0))")
        op = request["op"]
        if op == "add":
            sku, quantity = request.get("sku"), request.get("quantity")
            if not isinstance(sku, str) or not sku or type(quantity) is not int or not 0 <= quantity <= (1 << 63) - 1:
                return {"ok": False, "error": "invalid_item"}
            conn.execute("INSERT INTO inventory(sku, quantity) VALUES (?, ?) ON CONFLICT(sku) DO UPDATE SET quantity=excluded.quantity", (sku, quantity))
            conn.commit()
            return {"ok": True, "sku": sku, "quantity": quantity}
        if op == "list":
            rows = conn.execute("SELECT sku, quantity FROM inventory ORDER BY sku").fetchall()
            return {"ok": True, "items": [{"sku": sku, "quantity": quantity} for sku, quantity in rows]}
        return {"ok": False, "error": "invalid_item"}
    finally:
        conn.close()


def main():
    import sys
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = handle(request)
        except (json.JSONDecodeError, sqlite3.Error):
            response = {"ok": False, "error": "invalid_request"}
        print(json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()

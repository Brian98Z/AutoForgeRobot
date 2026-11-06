import json
import sys

books = {}

for line in sys.stdin:
    try:
        request = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        print(json.dumps({"ok": False, "error": "invalid_request"}, sort_keys=True, separators=(",", ":")), flush=True)
        continue

    if not isinstance(request, dict) or not isinstance(request.get("op"), str):
        response = {"ok": False, "error": "invalid_item"}
    elif request["op"] == "add":
        title, author = request.get("title"), request.get("author")
        if not isinstance(title, str) or not title or not isinstance(author, str) or not author:
            response = {"ok": False, "error": "invalid_item"}
        else:
            books[title] = author
            response = {"ok": True, "title": title, "author": author}
    elif request["op"] == "list":
        response = {"ok": True, "books": [{"title": title, "author": books[title]} for title in sorted(books, reverse=True)]}
    else:
        response = {"ok": False, "error": "invalid_item"}

    print(json.dumps(response, sort_keys=True, separators=(",", ":")), flush=True)

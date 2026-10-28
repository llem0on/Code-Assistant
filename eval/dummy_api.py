# Dummy API for the eval. Correct per eval/dataset/*.json unless a bug is switched on.
# Usage: python3 dummy_api.py <port> [bug-id ...]     (bug ids: see BUGS)
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

BUGS = {
    "dup-email": "POST /users accepts a duplicate email (201, should be 409)",
    "dup-email-case": "POST /users duplicate check is case-sensitive",
    "patch-nulls": "PATCH /users/{id} sets fields that were not sent to null",
    "delete-soft": "DELETE /users/{id} returns 204 but GET still returns the user",
    "no-cap": "GET /items does not cap limit at 100",
    "offset-ignored": "GET /items ignores offset",
    "sort-asc-only": "GET /items?sort=price_desc sorts ascending",
    "auth-missing": "POST /orders works without a token",
    "qty-zero": "POST /orders accepts qty 0",
    "qty-max": "POST /orders accepts qty above 10",
    "cancel-twice": "cancelling a cancelled order returns 200 (should be 409)",
    "discount-cap": "HEMAT10 discount is not capped at 50000",
    "coupon-unknown-ok": "unknown coupon is ignored (200, should be 400)",
}
TOKEN = "Bearer test-token"
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
on = set()
users = {1: {"id": 1, "email": "exists@test.co", "name": "Seed"}}
deleted = set()
items = [{"id": i, "name": f"Item {i}", "price": (i * 37) % 1000} for i in range(1, 301)]
orders = {}


def email_taken(email, skip_id=None):
    for u in users.values():
        if u["id"] == skip_id or u["id"] in deleted or not u["email"]:
            continue
        if u["email"] == email if "dup-email-case" in on else u["email"].lower() == email.lower():
            return True
    return False


class H(BaseHTTPRequestHandler):
    def send(self, code, body=None):
        b = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass

    def body(self):
        try:
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            return b if isinstance(b, dict) else None
        except ValueError:
            return None

    def authed(self):
        return self.headers.get("Authorization") == TOKEN

    def user(self, path):
        m = re.fullmatch(r"/users/(\d+)", path)
        if not m:
            return None, False
        u = users.get(int(m[1]))
        return u, u is not None and (u["id"] not in deleted or "delete-soft" in on)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/health":
            return self.send(200, {"status": "ok"})
        if u.path.startswith("/users/"):
            user, found = self.user(u.path)
            return self.send(200, user) if found else self.send(404, {"error": "not found"})
        if u.path == "/items":
            try:
                limit = int(qs.get("limit", ["20"])[0])
                offset = int(qs.get("offset", ["0"])[0])
            except ValueError:
                return self.send(400, {"error": "invalid limit/offset"})
            sort = qs.get("sort", [None])[0]
            if limit <= 0 or offset < 0 or sort not in (None, "price_asc", "price_desc"):
                return self.send(400, {"error": "invalid query"})
            rows = items if sort is None else sorted(
                items, key=lambda i: i["price"], reverse=sort == "price_desc" and "sort-asc-only" not in on)
            if "no-cap" not in on:
                limit = min(limit, 100)
            if "offset-ignored" in on:
                offset = 0
            return self.send(200, rows[offset:offset + limit])
        m = re.fullmatch(r"/orders/(\d+)", u.path)
        if m:
            if not self.authed():
                return self.send(401, {"error": "unauthorized"})
            o = orders.get(int(m[1]))
            return self.send(200, o) if o else self.send(404, {"error": "not found"})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        b = self.body()
        if path == "/users":
            if b is None:
                return self.send(400, {"error": "invalid json"})
            email, name = str(b.get("email") or ""), str(b.get("name") or "")
            if not EMAIL.fullmatch(email):
                return self.send(400, {"error": "invalid email"})
            if not name.strip():
                return self.send(400, {"error": "name required"})
            if "dup-email" not in on and email_taken(email):
                return self.send(409, {"error": "email already used"})
            uid = max(users) + 1
            users[uid] = {"id": uid, "email": email, "name": name}
            return self.send(201, users[uid])
        if path == "/orders":
            if not self.authed() and "auth-missing" not in on:
                return self.send(401, {"error": "unauthorized"})
            if b is None:
                return self.send(400, {"error": "invalid json"})
            qty, item_id = b.get("qty"), b.get("item_id")
            lo, hi = (0 if "qty-zero" in on else 1), (1000 if "qty-max" in on else 10)
            if not isinstance(qty, int) or not lo <= qty <= hi:
                return self.send(400, {"error": "qty must be 1-10"})
            if not isinstance(item_id, int) or not 1 <= item_id <= len(items):
                return self.send(404, {"error": "item not found"})
            oid = len(orders) + 1
            orders[oid] = {"id": oid, "item_id": item_id, "qty": qty, "status": "pending"}
            return self.send(201, orders[oid])
        m = re.fullmatch(r"/orders/(\d+)/cancel", path)
        if m:
            if not self.authed():
                return self.send(401, {"error": "unauthorized"})
            o = orders.get(int(m[1]))
            if not o:
                return self.send(404, {"error": "not found"})
            if o["status"] == "cancelled" and "cancel-twice" not in on:
                return self.send(409, {"error": "already cancelled"})
            o["status"] = "cancelled"
            return self.send(200, o)
        if path == "/cart/total":
            lines = (b or {}).get("items")
            if not isinstance(lines, list) or not lines or not all(
                    isinstance(x, dict) and isinstance(x.get("price"), int) and x["price"] >= 0
                    and isinstance(x.get("qty"), int) and x["qty"] >= 1 for x in lines):
                return self.send(400, {"error": "invalid items"})
            subtotal = sum(x["price"] * x["qty"] for x in lines)
            coupon, discount = b.get("coupon"), 0
            if coupon == "HEMAT10":
                discount = subtotal * 10 // 100
                if "discount-cap" not in on:
                    discount = min(discount, 50000)
            elif coupon and "coupon-unknown-ok" not in on:
                return self.send(400, {"error": "unknown coupon"})
            return self.send(200, {"subtotal": subtotal, "discount": discount, "total": subtotal - discount})
        self.send(404, {"error": "not found"})

    def do_PATCH(self):
        path = urlparse(self.path).path
        user, found = self.user(path)
        if not found:
            return self.send(404, {"error": "not found"})
        b = self.body()
        if b is None:
            return self.send(400, {"error": "invalid json"})
        if "email" in b and not EMAIL.fullmatch(str(b["email"] or "")):
            return self.send(400, {"error": "invalid email"})
        if "email" in b and email_taken(b["email"], skip_id=user["id"]):
            return self.send(409, {"error": "email already used"})
        if "patch-nulls" in on:
            user.update(email=b.get("email"), name=b.get("name"))
        else:
            user.update({k: b[k] for k in ("email", "name") if k in b})
        self.send(200, user)

    def do_DELETE(self):
        user, found = self.user(urlparse(self.path).path)
        if not found:
            return self.send(404, {"error": "not found"})
        deleted.add(user["id"])
        self.send(204)


if __name__ == "__main__":
    unknown = set(sys.argv[2:]) - set(BUGS)
    if unknown:
        sys.exit(f"unknown bug ids: {sorted(unknown)}")
    on.update(sys.argv[2:])
    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()

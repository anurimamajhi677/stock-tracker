"""Tiny web UI for the stock tracker. Python standard library only.

Run:   python3 app.py          then open http://localhost:8000
"""

import html
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from inventory import Inventory, StockError

DB_PATH = "stock.db"
inv = Inventory(DB_PATH)

CSS = """
:root { --bg:#fafafa; --fg:#1d1d1f; --muted:#6e6e73; --line:#d9d9de; --card:#fff;
        --ok:#1a7f37; --err:#c62828; --accent:#2457d6; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#151517; --fg:#ececf0; --muted:#9a9aa2; --line:#34343a; --card:#1e1e22;
          --ok:#4cc46a; --err:#ff6b6b; --accent:#6d93ff; } }
* { box-sizing: border-box; }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg);
       font:14px/1.45 -apple-system, system-ui, sans-serif; }
main { max-width:1100px; margin:0 auto; }
h1 { font-size:20px; margin:4px 0 2px; } h2 { font-size:15px; margin:0 0 10px; }
.sub { color:var(--muted); margin:0 0 16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px;
        padding:14px; margin-bottom:14px; overflow-x:auto; }
.grid { display:grid; gap:14px; grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); }
table { border-collapse:collapse; width:100%; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
th { color:var(--muted); font-weight:600; font-size:12px; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
.avail { font-weight:700; }
form.stack { display:flex; flex-direction:column; gap:6px; }
form.inline { display:inline-flex; gap:4px; align-items:center; margin:0; }
input, select, button { font:inherit; padding:5px 8px; border:1px solid var(--line);
        border-radius:6px; background:var(--bg); color:var(--fg); }
input[type=number] { width:80px; }
button { cursor:pointer; background:var(--accent); color:#fff; border-color:var(--accent); }
button.ghost { background:transparent; color:var(--fg); border-color:var(--line); }
.msg { padding:10px 12px; border-radius:6px; margin-bottom:14px; border:1px solid; }
.msg.ok { color:var(--ok); border-color:var(--ok); }
.msg.err { color:var(--err); border-color:var(--err); }
.tag { font-size:11px; padding:2px 6px; border-radius:10px; border:1px solid var(--line); }
.empty { color:var(--muted); }
"""


def key():
    """Fresh idempotency key, baked into each form when the page renders.
    Submitting the same form twice sends the same key, so it only applies once."""
    return '<input type="hidden" name="idem_key" value="%s">' % uuid.uuid4().hex


def esc(v):
    return html.escape("" if v is None else str(v))


def sku_options(stock):
    if not stock:
        return '<option value="">Add an item first</option>'
    return "".join('<option value="%s">%s - %s</option>' % (esc(s["sku"]), esc(s["sku"]), esc(s["name"]))
                   for s in stock)


def render(msg=None, ok=True):
    stock = inv.stock()
    reservations = inv.reservations()
    moves = inv.movements(30)
    opts = sku_options(stock)

    banner = ""
    if msg:
        banner = '<div class="msg %s" role="status">%s</div>' % ("ok" if ok else "err", esc(msg))

    if stock:
        stock_rows = "".join(
            "<tr><td>%s</td><td>%s</td><td class=num>%d</td><td class=num>%d</td>"
            "<td class=num>%d</td><td class='num avail'>%d</td></tr>"
            % (esc(s["sku"]), esc(s["name"]), s["on_hand"], s["held"], s["reserved"], s["available"])
            for s in stock)
    else:
        stock_rows = '<tr><td colspan=6 class=empty>No items yet. Add one below.</td></tr>'

    res_rows = ""
    for r in reservations:
        actions = ""
        if r["status"] in ("ACTIVE", "PARTIALLY_DISPATCHED"):
            actions = (
                '<form class=inline method=post action="/dispatch">%s'
                '<input type=hidden name=reservation_id value="%s">'
                '<input type=number name=qty min=1 max=%d value=%d aria-label="Qty to dispatch">'
                '<button>Dispatch</button></form> '
                '<form class=inline method=post action="/release">%s'
                '<input type=hidden name=reservation_id value="%s"><button class=ghost>Release</button></form> '
                '<form class=inline method=post action="/cancel">%s'
                '<input type=hidden name=reservation_id value="%s"><button class=ghost>Cancel</button></form>'
                % (key(), esc(r["id"]), r["remaining"], r["remaining"],
                   key(), esc(r["id"]), key(), esc(r["id"])))
        res_rows += (
            "<tr><td>%s</td><td>%s</td><td>%s</td><td class=num>%d</td><td class=num>%d</td>"
            "<td class=num>%d</td><td><span class=tag>%s</span></td><td>%s</td></tr>"
            % (esc(r["id"]), esc(r["order_ref"]), esc(r["sku"]), r["qty"], r["qty_dispatched"],
               r["remaining"], esc(r["status"]), actions))
    if not res_rows:
        res_rows = '<tr><td colspan=8 class=empty>No reservations yet.</td></tr>'

    move_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td class=num>%d</td><td>%s</td><td>%s</td>"
        "<td class=num>%d / %d / %d</td></tr>"
        % (esc(m["at"]), esc(m["sku"]), esc(m["action"]), m["qty"], esc(m["reservation_id"] or ""),
           esc(m["note"] or ""), m["on_hand_after"], m["held_after"], m["reserved_after"])
        for m in moves) or '<tr><td colspan=7 class=empty>Nothing has happened yet.</td></tr>'

    problems = inv.check_ledger()
    ledger_status = ("Ledger matches balances." if not problems
                     else "LEDGER MISMATCH: %s" % esc(problems))

    return """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Stock Tracker</title><style>%s</style></head><body><main>
<h1>Stock Tracker</h1>
<p class=sub>available = on-hand &minus; held &minus; reserved &middot; %s</p>
%s
<section class=card><h2>Stock</h2><table>
<tr><th>SKU</th><th>Name</th><th class=num>On hand</th><th class=num>Held</th>
<th class=num>Reserved</th><th class=num>Available</th></tr>%s</table></section>

<div class=grid>
<section class=card><h2>Add item</h2><form class=stack method=post action="/add_item">%s
<input name=sku placeholder="SKU, e.g. RICE-25KG" required>
<input name=name placeholder="Name, e.g. Rice 25 kg bag" required>
<button>Add item</button></form></section>

<section class=card><h2>Receive stock</h2><form class=stack method=post action="/receive">%s
<select name=sku>%s</select><input type=number name=qty min=1 placeholder=Qty required>
<input name=note placeholder="GRN / supplier (optional)"><button>Receive</button></form></section>

<section class=card><h2>Reserve for an order</h2><form class=stack method=post action="/reserve">%s
<select name=sku>%s</select><input type=number name=qty min=1 placeholder=Qty required>
<input name=order_ref placeholder="Order ref, e.g. SO-1042" required><button>Reserve</button></form></section>

<section class=card><h2>Quality hold</h2><form class=stack method=post action="/hold">%s
<select name=sku>%s</select><input type=number name=qty min=1 placeholder=Qty required>
<input name=note placeholder="Reason (optional)">
<div><button name=op value=hold>Hold</button> <button class=ghost name=op value=unhold>Unhold</button></div>
</form></section>
</div>

<section class=card><h2>Reservations</h2><table>
<tr><th>ID</th><th>Order</th><th>SKU</th><th class=num>Qty</th><th class=num>Dispatched</th>
<th class=num>Remaining</th><th>Status</th><th>Actions</th></tr>%s</table></section>

<section class=card><h2>Movement ledger (latest 30)</h2><table>
<tr><th>When (UTC)</th><th>SKU</th><th>Action</th><th class=num>Qty</th><th>Reservation</th>
<th>Note</th><th class=num>On hand / Held / Reserved after</th></tr>%s</table></section>
</main></body></html>""" % (
        CSS, ledger_status, banner, stock_rows,
        key(), key(), opts, key(), opts, key(), opts,
        res_rows, move_rows)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path != "/":
            self.send_error(404)
            return
        q = parse_qs(url.query)
        msg = q.get("msg", [None])[0]
        ok = q.get("ok", ["1"])[0] == "1"
        body = render(msg, ok).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        f = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        k = f.get("idem_key")
        path = urlparse(self.path).path
        try:
            if path == "/add_item":
                res = inv.add_item(f.get("sku"), f.get("name"), k)
            elif path == "/receive":
                res = inv.receive(f.get("sku"), f.get("qty"), k, f.get("note"))
            elif path == "/reserve":
                res = inv.reserve(f.get("sku"), f.get("qty"), f.get("order_ref"), k)
            elif path == "/hold":
                fn = inv.unhold if f.get("op") == "unhold" else inv.hold
                res = fn(f.get("sku"), f.get("qty"), k, f.get("note"))
            elif path == "/dispatch":
                res = inv.dispatch(f.get("reservation_id"), f.get("qty"), k)
            elif path == "/release":
                res = inv.release(f.get("reservation_id"), k)
            elif path == "/cancel":
                res = inv.cancel(f.get("reservation_id"), k)
            else:
                self.send_error(404)
                return
            msg = res["message"] + (" (duplicate submit ignored)" if res.get("replayed") else "")
            ok = True
        except StockError as e:
            msg, ok = str(e), False
        # Post/Redirect/Get: refreshing the page will not resubmit the form.
        self.send_response(303)
        self.send_header("Location", "/?" + urlencode({"msg": msg, "ok": "1" if ok else "0"}))
        self.end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print("Stock Tracker running on http://localhost:%d" % port)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()

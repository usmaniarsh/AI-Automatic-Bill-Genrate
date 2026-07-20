"""
AIR COOLAX - Automatic Billing / Invoice Generator
Flask + SQLite backend with AI-assisted item suggestions, natural-language
line-item parsing, GST/discount billing, payment-status tracking, an item
catalog, customer autocomplete, bill editing, analytics/insights, a
JSON backup/export, email/SMS invoice delivery, and AMC/recurring-service
reminders.
"""
import os
import re
import ssl
import json
import smtplib
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from io import BytesIO
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

from flask import Flask, request, jsonify, render_template, send_file, g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "billing.db")

app = Flask(__name__)

# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _safe_add_column(conn, table, coldef):
    """Idempotent ALTER TABLE ... ADD COLUMN, safe to re-run on every boot."""
    col_name = coldef.split()[0]
    cur = conn.execute(f"PRAGMA table_info({table})")
    existing = {r[1] for r in cur.fetchall()}
    if col_name not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS bills (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_no           TEXT UNIQUE NOT NULL,
            customer_name     TEXT,
            customer_address  TEXT,
            customer_phone    TEXT,
            bill_date         TEXT NOT NULL,
            payment_mode      TEXT DEFAULT 'Cash',
            total_amount      REAL NOT NULL DEFAULT 0,
            created_at        TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS bill_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_id     INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
            sr_no       INTEGER,
            particular  TEXT NOT NULL,
            category    TEXT DEFAULT 'Other',
            rate        REAL NOT NULL,
            qty         REAL NOT NULL,
            amount      REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS item_catalog (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            particular  TEXT UNIQUE NOT NULL,
            category    TEXT DEFAULT 'Other',
            rate        REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS amc_reminders (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name       TEXT NOT NULL,
            customer_phone      TEXT DEFAULT '',
            customer_email      TEXT DEFAULT '',
            service_type        TEXT DEFAULT 'AC Service',
            last_service_date   TEXT,
            next_service_date   TEXT,
            frequency_months    INTEGER DEFAULT 6,
            notes               TEXT DEFAULT '',
            active              INTEGER DEFAULT 1,
            last_reminded_at    TEXT,
            created_at          TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # --- non-destructive migrations for bills already created with the old schema ---
    for coldef in [
        "customer_phone TEXT",
        "customer_email TEXT DEFAULT ''",
        "subtotal REAL DEFAULT 0",
        "discount_type TEXT DEFAULT 'flat'",
        "discount_value REAL DEFAULT 0",
        "discount_amount REAL DEFAULT 0",
        "tax_enabled INTEGER DEFAULT 0",
        "tax_mode TEXT DEFAULT 'gst'",
        "cgst_rate REAL DEFAULT 9",
        "sgst_rate REAL DEFAULT 9",
        "igst_rate REAL DEFAULT 18",
        "tax_amount REAL DEFAULT 0",
        "payment_status TEXT DEFAULT 'Paid'",
        "paid_amount REAL DEFAULT 0",
        "due_amount REAL DEFAULT 0",
        "notes TEXT DEFAULT ''",
    ]:
        _safe_add_column(conn, "bills", coldef)
    _safe_add_column(conn, "bill_items", "category TEXT DEFAULT 'Other'")

    # default company settings (editable later from the Settings panel)
    defaults = {
        "company_name": "AIR COOLAX",
        "address": "Office No.01/26, Tilak Nagar, 90ft Road Near Gulshan Hotel, Sakinaka Andheri (E), Mumbai - 400072",
        "phone": "+919557422118",
        "email": "msalik269@gmail.com",
        "footer_note": "Thank you for your time and consideration. I hope to see you again soon.",
        "bill_prefix": "A",
        "gst_number": "",
        "owner_name": "",
        "upi_id": "",
        # Email (SMTP) delivery settings
        "smtp_host": "",
        "smtp_port": "587",
        "smtp_user": "",
        "smtp_password": "",
        "smtp_from_name": "",
        # SMS gateway settings (generic HTTP POST gateway, e.g. Fast2SMS/MSG91-style)
        "sms_api_url": "",
        "sms_api_key": "",
        "sms_sender_id": "",
    }
    cur = conn.cursor()
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def get_settings():
    db = get_db()
    rows = db.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify(get_settings())


@app.route("/api/settings", methods=["POST"])
def api_update_settings():
    data = request.get_json(force=True)
    db = get_db()
    for k, v in data.items():
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (k, str(v)),
        )
    db.commit()
    return jsonify({"ok": True, "settings": get_settings()})


# --------------------------------------------------------------------------
# Bill number generation (auto, sequential per prefix)
# --------------------------------------------------------------------------

def generate_next_bill_no():
    settings = get_settings()
    prefix = settings.get("bill_prefix", "A")
    db = get_db()
    rows = db.execute("SELECT bill_no FROM bills WHERE bill_no LIKE ?", (f"{prefix}%",)).fetchall()
    max_n = 0
    for r in rows:
        m = re.match(rf"^{re.escape(prefix)}(\d+)$", r["bill_no"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{prefix}{max_n + 1:03d}"


@app.route("/api/next-bill-no")
def api_next_bill_no():
    return jsonify({"bill_no": generate_next_bill_no(), "date": datetime.now().strftime("%d/%m/%Y")})


# --------------------------------------------------------------------------
# AI Feature: keyword-based item categorisation (used by parser, catalog,
# manual entry and the analytics category breakdown)
# --------------------------------------------------------------------------

CATEGORY_KEYWORDS = {
    "Gas Filling": ["gas", "refrigerant", "freon", "gas filling", "gas charging"],
    "AC Service": ["service", "servicing", "cleaning", "clean", "amc", "maintenance"],
    "Repair": ["repair", "fix", "pcb", "compressor", "fault", "leakage", "leak"],
    "Installation": ["install", "installation", "fitting", "mounting", "uninstall", "shifting", "shift"],
    "Spare Parts": ["part", "spare", "capacitor", "motor", "remote", "filter", "pipe", "stabilizer"],
}


def classify_item(particular):
    p = (particular or "").lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(kw in p for kw in kws):
            return cat
    return "Other"


# --------------------------------------------------------------------------
# Date helper — add N months to a dd/mm/yyyy string, clamping the day to
# whatever the target month actually has (used by AMC scheduling)
# --------------------------------------------------------------------------

def _add_months(ddmmyyyy_str, months):
    d = datetime.strptime(ddmmyyyy_str, "%d/%m/%Y")
    months = int(months or 0)
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    days_in_month = [
        31,
        29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28,
        31, 30, 31, 30, 31, 31, 30, 31, 30, 31,
    ]
    day = min(d.day, days_in_month[month - 1])
    return d.replace(year=year, month=month, day=day).strftime("%d/%m/%Y")


# --------------------------------------------------------------------------
# Email + SMS delivery helpers
# --------------------------------------------------------------------------

def send_email_with_attachment(to_email, subject, body_text, attachment_bytes=None, attachment_name=None):
    settings = get_settings()
    host = settings.get("smtp_host", "")
    port = int(settings.get("smtp_port") or 587)
    user = settings.get("smtp_user", "")
    password = settings.get("smtp_password", "")
    from_name = settings.get("smtp_from_name") or settings.get("company_name", "")
    if not host or not user or not password:
        raise RuntimeError("Email isn't set up yet — add SMTP host/user/password in Settings first.")

    msg = MIMEMultipart()
    msg["From"] = f"{from_name} <{user}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain"))
    if attachment_bytes and attachment_name:
        part = MIMEApplication(attachment_bytes, Name=attachment_name)
        part["Content-Disposition"] = f'attachment; filename="{attachment_name}"'
        msg.attach(part)

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=15) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.sendmail(user, to_email, msg.as_string())


def send_sms(phone, message):
    """Generic HTTP SMS gateway sender (Fast2SMS/MSG91-style POST body).
    Adjust the payload keys below to match your SMS provider's API docs —
    this shape works for many Indian bulk-SMS providers out of the box."""
    settings = get_settings()
    api_url = settings.get("sms_api_url", "")
    api_key = settings.get("sms_api_key", "")
    sender_id = settings.get("sms_sender_id", "")
    if not api_url or not api_key:
        raise RuntimeError("SMS isn't set up yet — add your SMS gateway URL/API key in Settings first.")

    payload = {
        "authorization": api_key,
        "sender_id": sender_id,
        "route": "q",
        "numbers": phone,
        "message": message,
    }
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(api_url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode()


# --------------------------------------------------------------------------
# AI Feature: item / rate suggestion from billing history + catalog
# --------------------------------------------------------------------------

@app.route("/api/suggest")
def api_suggest():
    q = request.args.get("q", "").strip()
    db = get_db()
    seen = {}

    # catalog first (curated, higher priority)
    if q:
        cat_rows = db.execute(
            "SELECT particular, rate FROM item_catalog WHERE particular LIKE ? LIMIT 6", (f"%{q}%",)
        ).fetchall()
    else:
        cat_rows = db.execute("SELECT particular, rate FROM item_catalog LIMIT 6").fetchall()
    for r in cat_rows:
        seen[r["particular"]] = r["rate"]

    if q:
        rows = db.execute(
            """SELECT particular, rate, COUNT(*) c, MAX(bill_id) latest
               FROM bill_items WHERE particular LIKE ?
               GROUP BY particular ORDER BY c DESC, latest DESC LIMIT 6""",
            (f"%{q}%",),
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT particular, rate, COUNT(*) c, MAX(bill_id) latest
               FROM bill_items GROUP BY particular ORDER BY c DESC LIMIT 6"""
        ).fetchall()
    for r in rows:
        if r["particular"] not in seen:
            seen[r["particular"]] = r["rate"]

    return jsonify([{"particular": k, "rate": v} for k, v in list(seen.items())[:8]])


# --------------------------------------------------------------------------
# Item catalog CRUD (Settings -> Item Master)
# --------------------------------------------------------------------------

@app.route("/api/catalog", methods=["GET"])
def api_catalog_list():
    db = get_db()
    rows = db.execute("SELECT * FROM item_catalog ORDER BY particular").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/catalog", methods=["POST"])
def api_catalog_add():
    data = request.get_json(force=True)
    particular = (data.get("particular") or "").strip()
    if not particular:
        return jsonify({"error": "Item name required"}), 400
    rate = float(data.get("rate") or 0)
    category = data.get("category") or classify_item(particular)
    db = get_db()
    db.execute(
        """INSERT INTO item_catalog (particular, category, rate) VALUES (?, ?, ?)
           ON CONFLICT(particular) DO UPDATE SET rate = excluded.rate, category = excluded.category""",
        (particular, category, rate),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/catalog/<int:cat_id>", methods=["DELETE"])
def api_catalog_delete(cat_id):
    db = get_db()
    db.execute("DELETE FROM item_catalog WHERE id = ?", (cat_id,))
    db.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Customer autocomplete (from past bills)
# --------------------------------------------------------------------------

@app.route("/api/customers")
def api_customers():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    db = get_db()
    rows = db.execute(
        """SELECT customer_name, customer_address, customer_phone, customer_email, MAX(id) latest
           FROM bills WHERE customer_name LIKE ? AND customer_name != ''
           GROUP BY customer_name ORDER BY latest DESC LIMIT 6""",
        (f"%{q}%",),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# --------------------------------------------------------------------------
# AI Feature: natural-language chatbot line-item parser
#   e.g. "AC gas filling 500 qty 2, PCB repair 400" -> line items
# --------------------------------------------------------------------------

NUM_WORDS = {
    "ek": 1, "one": 1, "do": 2, "two": 2, "teen": 3, "three": 3,
    "char": 4, "chaar": 4, "four": 4, "panch": 5, "paanch": 5, "five": 5,
}

RATE_RE_PREFIX = re.compile(r"(?:rs\.?|₹|rupy?e?s?)\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
RATE_RE_SUFFIX = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(?:rs\.?|₹|rupy?e?s?)\b", re.IGNORECASE)
QTY_RE_PREFIX = re.compile(r"(?:qty|quantity)\s*[:\-]?\s*([0-9]+)", re.IGNORECASE)
QTY_RE_SUFFIX = re.compile(r"([0-9]+)\s*(?:x|pcs|piece[s]?)\b", re.IGNORECASE)


def parse_segment(seg):
    seg = seg.strip()
    if not seg:
        return None

    rate = None
    m = RATE_RE_PREFIX.search(seg)
    if not m:
        m = RATE_RE_SUFFIX.search(seg)
    if m:
        rate = float(m.group(1))
        seg = seg[: m.start()] + seg[m.end():]

    qty = None
    m = QTY_RE_PREFIX.search(seg)
    if not m:
        m = QTY_RE_SUFFIX.search(seg)
    if m:
        qty = float(m.group(1))
        seg = seg[: m.start()] + seg[m.end():]

    # bare number left over -> treat the LAST number in the phrase as the rate
    # (prices are usually stated at the end; earlier numbers are often part of
    # the item description, e.g. "1.5 ton AC service 400")
    if rate is None:
        matches = list(re.finditer(r"\b([0-9]+(?:\.[0-9]+)?)\b", seg))
        if matches:
            m2 = matches[-1]
            rate = float(m2.group(1))
            seg = seg[: m2.start()] + seg[m2.end():]

    for word, val in NUM_WORDS.items():
        if re.search(rf"\b{word}\b", seg, re.IGNORECASE):
            qty = val
            seg = re.sub(rf"\b{word}\b", "", seg, flags=re.IGNORECASE)
            break

    particular = re.sub(r"[\-:,]+", " ", seg).strip()
    particular = re.sub(r"\s+", " ", particular)
    if not particular:
        return None
    if rate is None:
        rate = 0
    if qty is None:
        qty = 1
    return {
        "particular": particular.title(),
        "category": classify_item(particular),
        "rate": rate,
        "qty": qty,
        "amount": round(rate * qty, 2),
    }


@app.route("/api/parse", methods=["POST"])
def api_parse():
    data = request.get_json(force=True)
    text = data.get("text", "")
    # split on commas / "and" / newlines / "aur"
    parts = re.split(r",|\band\b|\baur\b|\n|;", text, flags=re.IGNORECASE)
    items = [parse_segment(p) for p in parts]
    items = [i for i in items if i]
    return jsonify({"items": items})


# --------------------------------------------------------------------------
# Bill amount computation (subtotal, discount, GST, due) — shared by
# create + update so both stay in sync
# --------------------------------------------------------------------------

def compute_bill_amounts(items, data):
    subtotal = sum(float(i["rate"]) * float(i["qty"]) for i in items)

    discount_type = data.get("discount_type", "flat")
    discount_value = float(data.get("discount_value") or 0)
    if discount_type == "percent":
        discount_amount = subtotal * discount_value / 100
    else:
        discount_amount = discount_value
    discount_amount = max(0, min(discount_amount, subtotal))

    taxable = subtotal - discount_amount
    tax_enabled = bool(data.get("tax_enabled"))
    tax_mode = data.get("tax_mode", "gst")
    cgst_rate = float(data.get("cgst_rate") or 9)
    sgst_rate = float(data.get("sgst_rate") or 9)
    igst_rate = float(data.get("igst_rate") or 18)
    if tax_enabled:
        rate_pct = igst_rate if tax_mode == "igst" else (cgst_rate + sgst_rate)
        tax_amount = taxable * rate_pct / 100
    else:
        tax_amount = 0

    total = round(taxable + tax_amount, 2)

    payment_status = data.get("payment_status", "Paid")
    if payment_status == "Paid":
        paid_amount = total
    elif payment_status == "Unpaid":
        paid_amount = 0
    else:
        paid_amount = float(data.get("paid_amount") or 0)
    paid_amount = max(0, min(paid_amount, total))
    due_amount = round(total - paid_amount, 2)

    return {
        "subtotal": round(subtotal, 2),
        "discount_type": discount_type,
        "discount_value": discount_value,
        "discount_amount": round(discount_amount, 2),
        "tax_enabled": 1 if tax_enabled else 0,
        "tax_mode": tax_mode,
        "cgst_rate": cgst_rate,
        "sgst_rate": sgst_rate,
        "igst_rate": igst_rate,
        "tax_amount": round(tax_amount, 2),
        "total_amount": total,
        "payment_status": payment_status,
        "paid_amount": round(paid_amount, 2),
        "due_amount": due_amount,
    }


def _sync_amc_from_bill(db, data, bill_date):
    """If the user ticked 'schedule AMC reminder' on a bill, create or
    refresh that customer's recurring-service reminder record."""
    if not data.get("schedule_amc"):
        return
    freq = int(data.get("amc_frequency_months") or 6)
    cust_name = (data.get("customer_name") or "").strip()
    cust_phone = (data.get("customer_phone") or "").strip()
    cust_email = (data.get("customer_email") or "").strip()
    if not cust_name and not cust_phone:
        return
    next_service = _add_months(bill_date, freq)

    existing = None
    if cust_phone:
        existing = db.execute(
            "SELECT id FROM amc_reminders WHERE customer_phone = ? AND customer_phone != ''",
            (cust_phone,),
        ).fetchone()
    if not existing and cust_name:
        existing = db.execute(
            "SELECT id FROM amc_reminders WHERE customer_name = ? AND (customer_phone = '' OR customer_phone IS NULL)",
            (cust_name,),
        ).fetchone()

    if existing:
        db.execute(
            """UPDATE amc_reminders SET customer_name=?, customer_email=?,
                   last_service_date=?, next_service_date=?, frequency_months=?, active=1
               WHERE id=?""",
            (cust_name, cust_email, bill_date, next_service, freq, existing["id"]),
        )
    else:
        db.execute(
            """INSERT INTO amc_reminders (customer_name, customer_phone, customer_email, service_type,
                   last_service_date, next_service_date, frequency_months, active)
               VALUES (?, ?, ?, 'AC Service', ?, ?, ?, 1)""",
            (cust_name, cust_phone, cust_email, bill_date, next_service, freq),
        )


# --------------------------------------------------------------------------
# Bills: create / list / detail / update / delete
# --------------------------------------------------------------------------

@app.route("/api/bills", methods=["POST"])
def api_create_bill():
    data = request.get_json(force=True)
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "At least one line item is required"}), 400

    bill_no = data.get("bill_no") or generate_next_bill_no()
    bill_date = data.get("date") or datetime.now().strftime("%d/%m/%Y")
    amounts = compute_bill_amounts(items, data)

    db = get_db()
    cur = db.execute(
        """INSERT INTO bills (bill_no, customer_name, customer_address, customer_phone, customer_email,
                               bill_date, payment_mode, subtotal, discount_type, discount_value, discount_amount,
                               tax_enabled, tax_mode, cgst_rate, sgst_rate, igst_rate, tax_amount,
                               total_amount, payment_status, paid_amount, due_amount, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            bill_no, data.get("customer_name", ""), data.get("customer_address", ""),
            data.get("customer_phone", ""), data.get("customer_email", ""), bill_date,
            data.get("payment_mode", "Cash"),
            amounts["subtotal"], amounts["discount_type"], amounts["discount_value"], amounts["discount_amount"],
            amounts["tax_enabled"], amounts["tax_mode"], amounts["cgst_rate"], amounts["sgst_rate"],
            amounts["igst_rate"], amounts["tax_amount"], amounts["total_amount"], amounts["payment_status"],
            amounts["paid_amount"], amounts["due_amount"], data.get("notes", ""),
        ),
    )
    bill_id = cur.lastrowid
    _insert_items(db, bill_id, items)
    _sync_amc_from_bill(db, data, bill_date)
    db.commit()
    return jsonify({"ok": True, "bill_id": bill_id, "bill_no": bill_no, "total": amounts["total_amount"]})


def _insert_items(db, bill_id, items):
    for idx, item in enumerate(items, start=1):
        category = item.get("category") or classify_item(item["particular"])
        db.execute(
            """INSERT INTO bill_items (bill_id, sr_no, particular, category, rate, qty, amount)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (bill_id, idx, item["particular"], category, float(item["rate"]), float(item["qty"]),
             round(float(item["rate"]) * float(item["qty"]), 2)),
        )


@app.route("/api/bills/<int:bill_id>", methods=["PUT"])
def api_update_bill(bill_id):
    data = request.get_json(force=True)
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "At least one line item is required"}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM bills WHERE id = ?", (bill_id,)).fetchone()
    if not existing:
        return jsonify({"error": "not found"}), 404

    amounts = compute_bill_amounts(items, data)
    bill_date = data.get("date") or datetime.now().strftime("%d/%m/%Y")
    db.execute(
        """UPDATE bills SET customer_name=?, customer_address=?, customer_phone=?, customer_email=?,
               bill_date=?, payment_mode=?, subtotal=?, discount_type=?, discount_value=?, discount_amount=?,
               tax_enabled=?, tax_mode=?, cgst_rate=?, sgst_rate=?, igst_rate=?, tax_amount=?,
               total_amount=?, payment_status=?, paid_amount=?, due_amount=?, notes=?
           WHERE id = ?""",
        (
            data.get("customer_name", ""), data.get("customer_address", ""), data.get("customer_phone", ""),
            data.get("customer_email", ""), bill_date, data.get("payment_mode", "Cash"),
            amounts["subtotal"], amounts["discount_type"], amounts["discount_value"], amounts["discount_amount"],
            amounts["tax_enabled"], amounts["tax_mode"], amounts["cgst_rate"], amounts["sgst_rate"],
            amounts["igst_rate"], amounts["tax_amount"], amounts["total_amount"], amounts["payment_status"],
            amounts["paid_amount"], amounts["due_amount"], data.get("notes", ""), bill_id,
        ),
    )
    db.execute("DELETE FROM bill_items WHERE bill_id = ?", (bill_id,))
    _insert_items(db, bill_id, items)
    _sync_amc_from_bill(db, data, bill_date)
    db.commit()
    return jsonify({"ok": True, "bill_id": bill_id, "total": amounts["total_amount"]})


@app.route("/api/bills")
def api_list_bills():
    q = request.args.get("q", "").strip()
    date_from = request.args.get("from", "").strip()
    date_to = request.args.get("to", "").strip()
    payment_mode = request.args.get("payment_mode", "").strip()
    status = request.args.get("status", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(100, int(request.args.get("per_page", 20)))

    clauses, params = [], []
    if q:
        clauses.append("(bill_no LIKE ? OR customer_name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if date_from:
        clauses.append("bill_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("bill_date <= ?")
        params.append(date_to)
    if payment_mode:
        clauses.append("payment_mode = ?")
        params.append(payment_mode)
    if status:
        clauses.append("payment_status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    db = get_db()
    total_count = db.execute(f"SELECT COUNT(*) c FROM bills {where}", params).fetchone()["c"]
    rows = db.execute(
        f"""SELECT id, bill_no, customer_name, bill_date, total_amount, payment_mode,
                   payment_status, due_amount FROM bills {where}
            ORDER BY id DESC LIMIT ? OFFSET ?""",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()
    return jsonify({
        "bills": [dict(r) for r in rows],
        "total": total_count,
        "page": page,
        "per_page": per_page,
        "pages": max(1, -(-total_count // per_page)),
    })


def fetch_bill(bill_id):
    db = get_db()
    bill = db.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
    if not bill:
        return None, None
    items = db.execute(
        "SELECT * FROM bill_items WHERE bill_id = ? ORDER BY sr_no", (bill_id,)
    ).fetchall()
    return bill, items


@app.route("/api/bills/<int:bill_id>")
def api_get_bill(bill_id):
    bill, items = fetch_bill(bill_id)
    if not bill:
        return jsonify({"error": "not found"}), 404
    return jsonify({"bill": dict(bill), "items": [dict(i) for i in items]})


@app.route("/api/bills/<int:bill_id>", methods=["DELETE"])
def api_delete_bill(bill_id):
    db = get_db()
    db.execute("DELETE FROM bills WHERE id = ?", (bill_id,))
    db.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Dashboard stats (Home view)
# --------------------------------------------------------------------------

@app.route("/api/stats")
def api_stats():
    db = get_db()
    today = datetime.now().strftime("%d/%m/%Y")
    total_bills = db.execute("SELECT COUNT(*) c FROM bills").fetchone()["c"]
    total_revenue = db.execute("SELECT COALESCE(SUM(total_amount),0) s FROM bills").fetchone()["s"]
    today_bills = db.execute("SELECT COUNT(*) c FROM bills WHERE bill_date = ?", (today,)).fetchone()["c"]
    today_revenue = db.execute(
        "SELECT COALESCE(SUM(total_amount),0) s FROM bills WHERE bill_date = ?", (today,)
    ).fetchone()["s"]
    total_due = db.execute("SELECT COALESCE(SUM(due_amount),0) s FROM bills WHERE due_amount > 0").fetchone()["s"]
    recent = db.execute(
        "SELECT id, bill_no, customer_name, bill_date, total_amount FROM bills ORDER BY id DESC LIMIT 5"
    ).fetchall()
    return jsonify({
        "total_bills": total_bills,
        "total_revenue": total_revenue,
        "today_bills": today_bills,
        "today_revenue": today_revenue,
        "total_due": total_due,
        "recent": [dict(r) for r in recent],
    })


# --------------------------------------------------------------------------
# AI Feature: Insights / analytics dashboard
# --------------------------------------------------------------------------

def _month_key(ddmmyyyy):
    try:
        d = datetime.strptime(ddmmyyyy, "%d/%m/%Y")
        return d.strftime("%Y-%m"), d.strftime("%b %y")
    except ValueError:
        return None, None


@app.route("/api/analytics")
def api_analytics():
    db = get_db()
    bills = db.execute("SELECT bill_date, total_amount, customer_name FROM bills").fetchall()
    items = db.execute(
        """SELECT bi.particular, bi.category, bi.amount, b.bill_date
           FROM bill_items bi JOIN bills b ON b.id = bi.bill_id"""
    ).fetchall()

    # last 6 months revenue trend — build ordered list of the last 6 month keys
    now = datetime.now()
    ordered = []
    cursor = now.replace(day=1)
    for _ in range(6):
        ordered.append((cursor.strftime("%Y-%m"), cursor.strftime("%b")))
        prev_month = cursor.month - 1 or 12
        prev_year = cursor.year - 1 if cursor.month == 1 else cursor.year
        cursor = cursor.replace(year=prev_year, month=prev_month)
    ordered.reverse()
    revenue_by_month = {k: 0.0 for k, _ in ordered}
    for b in bills:
        key, _ = _month_key(b["bill_date"])
        if key in revenue_by_month:
            revenue_by_month[key] += b["total_amount"] or 0

    this_month_key = ordered[-1][0]
    last_month_key = ordered[-2][0]
    this_month_rev = revenue_by_month.get(this_month_key, 0)
    last_month_rev = revenue_by_month.get(last_month_key, 0)
    growth_pct = (
        round(((this_month_rev - last_month_rev) / last_month_rev) * 100, 1)
        if last_month_rev else (100.0 if this_month_rev else 0.0)
    )

    # top items / categories / customers
    item_totals, cat_totals, cust_totals = {}, {}, {}
    for it in items:
        item_totals[it["particular"]] = item_totals.get(it["particular"], 0) + (it["amount"] or 0)
        cat_totals[it["category"] or "Other"] = cat_totals.get(it["category"] or "Other", 0) + (it["amount"] or 0)
    for b in bills:
        if b["customer_name"]:
            cust_totals[b["customer_name"]] = cust_totals.get(b["customer_name"], 0) + (b["total_amount"] or 0)

    top_items = sorted(item_totals.items(), key=lambda x: -x[1])[:5]
    top_categories = sorted(cat_totals.items(), key=lambda x: -x[1])[:6]
    top_customers = sorted(cust_totals.items(), key=lambda x: -x[1])[:5]

    return jsonify({
        "monthly_revenue": [{"label": lbl, "value": round(revenue_by_month[k], 2)} for k, lbl in ordered],
        "growth_pct": growth_pct,
        "this_month_revenue": round(this_month_rev, 2),
        "top_items": [{"name": n, "amount": round(v, 2)} for n, v in top_items],
        "top_categories": [{"name": n, "amount": round(v, 2)} for n, v in top_categories],
        "top_customers": [{"name": n, "amount": round(v, 2)} for n, v in top_customers],
    })


# --------------------------------------------------------------------------
# AMC / recurring-service reminders
# --------------------------------------------------------------------------

@app.route("/api/amc", methods=["GET"])
def api_amc_list():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM amc_reminders WHERE active = 1 ORDER BY next_service_date"
    ).fetchall()
    parsed = []
    for r in rows:
        d = dict(r)
        try:
            nsd = datetime.strptime(d["next_service_date"], "%d/%m/%Y")
            d["overdue"] = nsd < datetime.now()
        except (ValueError, TypeError):
            d["overdue"] = False
        parsed.append(d)
    return jsonify(parsed)


@app.route("/api/amc", methods=["POST"])
def api_amc_add():
    data = request.get_json(force=True)
    name = (data.get("customer_name") or "").strip()
    if not name:
        return jsonify({"error": "Customer name required"}), 400
    freq = int(data.get("frequency_months") or 6)
    last_service = data.get("last_service_date") or datetime.now().strftime("%d/%m/%Y")
    next_service = data.get("next_service_date") or _add_months(last_service, freq)
    db = get_db()
    cur = db.execute(
        """INSERT INTO amc_reminders (customer_name, customer_phone, customer_email, service_type,
               last_service_date, next_service_date, frequency_months, notes, active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (
            name, data.get("customer_phone", ""), data.get("customer_email", ""),
            data.get("service_type", "AC Service"), last_service, next_service, freq,
            data.get("notes", ""),
        ),
    )
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/api/amc/<int:amc_id>", methods=["PUT"])
def api_amc_update(amc_id):
    data = request.get_json(force=True)
    db = get_db()
    existing = db.execute("SELECT * FROM amc_reminders WHERE id = ?", (amc_id,)).fetchone()
    if not existing:
        return jsonify({"error": "not found"}), 404
    db.execute(
        """UPDATE amc_reminders SET customer_name=?, customer_phone=?, customer_email=?, service_type=?,
               last_service_date=?, next_service_date=?, frequency_months=?, notes=?, active=?
           WHERE id=?""",
        (
            data.get("customer_name", existing["customer_name"]),
            data.get("customer_phone", existing["customer_phone"]),
            data.get("customer_email", existing["customer_email"]),
            data.get("service_type", existing["service_type"]),
            data.get("last_service_date", existing["last_service_date"]),
            data.get("next_service_date", existing["next_service_date"]),
            int(data.get("frequency_months", existing["frequency_months"])),
            data.get("notes", existing["notes"]),
            int(data.get("active", existing["active"])),
            amc_id,
        ),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/amc/<int:amc_id>", methods=["DELETE"])
def api_amc_delete(amc_id):
    db = get_db()
    db.execute("DELETE FROM amc_reminders WHERE id = ?", (amc_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/amc/due")
def api_amc_due():
    """Reminders due within N days (default 7), including anything overdue."""
    within_days = int(request.args.get("days", 7))
    cutoff = datetime.now() + timedelta(days=within_days)
    db = get_db()
    rows = db.execute("SELECT * FROM amc_reminders WHERE active = 1").fetchall()
    due = []
    for r in rows:
        try:
            nsd = datetime.strptime(r["next_service_date"], "%d/%m/%Y")
        except (ValueError, TypeError):
            continue
        if nsd <= cutoff:
            d = dict(r)
            d["overdue"] = nsd < datetime.now()
            due.append(d)
    due.sort(key=lambda x: datetime.strptime(x["next_service_date"], "%d/%m/%Y"))
    return jsonify(due)


@app.route("/api/amc/<int:amc_id>/mark-done", methods=["POST"])
def api_amc_mark_done(amc_id):
    db = get_db()
    row = db.execute("SELECT * FROM amc_reminders WHERE id = ?", (amc_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    today_str = datetime.now().strftime("%d/%m/%Y")
    next_str = _add_months(today_str, row["frequency_months"] or 6)
    db.execute(
        "UPDATE amc_reminders SET last_service_date = ?, next_service_date = ? WHERE id = ?",
        (today_str, next_str, amc_id),
    )
    db.commit()
    return jsonify({"ok": True, "next_service_date": next_str})


@app.route("/api/amc/<int:amc_id>/remind", methods=["POST"])
def api_amc_remind(amc_id):
    data = request.get_json(force=True) or {}
    channel = data.get("channel", "email")
    db = get_db()
    row = db.execute("SELECT * FROM amc_reminders WHERE id = ?", (amc_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    settings = get_settings()
    msg_text = (
        f"Dear {row['customer_name']}, this is a reminder that your {row['service_type']} "
        f"with {settings.get('company_name', '')} is due on {row['next_service_date']}. "
        f"Please contact us to schedule your service."
    )
    try:
        if channel == "email":
            if not row["customer_email"]:
                return jsonify({"error": "No email on file for this customer"}), 400
            send_email_with_attachment(
                row["customer_email"], f"Service Reminder - {settings.get('company_name', '')}", msg_text
            )
        else:
            if not row["customer_phone"]:
                return jsonify({"error": "No phone on file for this customer"}), 400
            send_sms(row["customer_phone"], msg_text)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    db.execute(
        "UPDATE amc_reminders SET last_reminded_at = ? WHERE id = ?",
        (datetime.now().isoformat(), amc_id),
    )
    db.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Backup / export (full JSON dump for safekeeping)
# --------------------------------------------------------------------------

@app.route("/api/export")
def api_export():
    db = get_db()
    bills = [dict(r) for r in db.execute("SELECT * FROM bills").fetchall()]
    items = [dict(r) for r in db.execute("SELECT * FROM bill_items").fetchall()]
    catalog = [dict(r) for r in db.execute("SELECT * FROM item_catalog").fetchall()]
    amc = [dict(r) for r in db.execute("SELECT * FROM amc_reminders").fetchall()]
    payload = {
        "exported_at": datetime.now().isoformat(),
        "settings": get_settings(),
        "bills": bills,
        "bill_items": items,
        "item_catalog": catalog,
        "amc_reminders": amc,
    }
    buf = BytesIO(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))
    buf.seek(0)
    return send_file(
        buf, mimetype="application/json", as_attachment=True,
        download_name=f"aircoolax-backup-{datetime.now().strftime('%Y%m%d-%H%M')}.json",
    )


# --------------------------------------------------------------------------
# PDF export (server-side, reportlab)
# --------------------------------------------------------------------------

def build_bill_pdf(bill_id):
    """Builds the invoice PDF for a bill and returns a BytesIO buffer.
    Shared by the direct-download endpoint and the email-invoice endpoint.
    Note: uses "Rs." instead of the ₹ glyph because ReportLab's built-in
    Helvetica font has no Rupee-sign character (it renders as a black box)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    bill, items = fetch_bill(bill_id)
    if not bill:
        return None
    settings = get_settings()

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    blue = colors.HexColor("#2563eb")
    title_style = ParagraphStyle("title", parent=styles["Title"], textColor=blue, fontSize=22)
    normal = styles["Normal"]

    story = [
        Paragraph(settings.get("company_name", "Invoice"), title_style),
        Spacer(1, 6),
    ]
    if settings.get("owner_name"):
        story.append(Paragraph(f"Owner: {settings.get('owner_name')}", normal))
    story += [
        Paragraph(settings.get("address", ""), normal),
        Paragraph(f"{settings.get('phone', '')} | {settings.get('email', '')}", normal),
    ]
    if settings.get("gst_number"):
        story.append(Paragraph(f"GSTIN: {settings.get('gst_number')}", normal))
    story += [
        Spacer(1, 14),
        Paragraph(f"<b>Bill No:</b> {bill['bill_no']} &nbsp;&nbsp; <b>Date:</b> {bill['bill_date']}", normal),
        Paragraph(f"<b>Customer:</b> {bill['customer_name'] or '-'}", normal),
        Paragraph(f"<b>Address:</b> {bill['customer_address'] or '-'}", normal),
        Spacer(1, 14),
    ]

    table_data = [["Sr", "Particular", "Rate (Rs.)", "Qty", "Amount (Rs.)"]]
    for it in items:
        table_data.append([it["sr_no"], it["particular"], f"{it['rate']:.2f}", it["qty"], f"{it['amount']:.2f}"])

    subtotal = bill["subtotal"] if bill["subtotal"] else bill["total_amount"]
    table_data.append(["", "", "", "Subtotal", f"Rs.{subtotal:.2f}"])
    if bill["discount_amount"]:
        table_data.append(["", "", "", "Discount", f"-Rs.{bill['discount_amount']:.2f}"])
    if bill["tax_enabled"]:
        if bill["tax_mode"] == "igst":
            table_data.append(["", "", "", f"IGST {bill['igst_rate']:.0f}%", f"Rs.{bill['tax_amount']:.2f}"])
        else:
            table_data.append(["", "", "", f"CGST+SGST {bill['cgst_rate']:.0f}+{bill['sgst_rate']:.0f}%", f"Rs.{bill['tax_amount']:.2f}"])
    table_data.append(["", "", "", "Total", f"Rs.{bill['total_amount']:.2f}"])
    if bill["payment_status"] != "Paid":
        table_data.append(["", "", "", "Paid", f"Rs.{bill['paid_amount']:.2f}"])
        table_data.append(["", "", "", "Due", f"Rs.{bill['due_amount']:.2f}"])

    tbl = Table(table_data, colWidths=[25 * mm, 60 * mm, 25 * mm, 20 * mm, 35 * mm])
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), blue),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, 0), 0.5, colors.HexColor("#cbd5e1")),
        ("ALIGN", (2, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LINEABOVE", (3, len(items) + 1), (-1, len(items) + 1), 0.5, colors.HexColor("#cbd5e1")),
    ]
    for r in range(1, len(items) + 1):
        style_cmds.append(("LINEBELOW", (0, r), (-1, r), 0.5, colors.HexColor("#e2e8f0")))
    style_cmds.append(("FONTNAME", (3, -1), (4, -1), "Helvetica-Bold"))
    tbl.setStyle(TableStyle(style_cmds))
    story.append(tbl)
    story.append(Spacer(1, 16))
    story.append(Paragraph(f"Payment Mode: {bill['payment_mode']} &nbsp;&nbsp; Status: {bill['payment_status']}", normal))
    if bill["notes"]:
        story.append(Spacer(1, 6))
        story.append(Paragraph(f"Notes: {bill['notes']}", normal))

    # --- UPI QR code: only when payment is Online AND some amount is still due ---
    upi_id = settings.get("upi_id", "")
    if bill["payment_mode"] == "Online" and bill["due_amount"] and bill["due_amount"] > 0 and upi_id:
        try:
            pay_name = settings.get("company_name", "Merchant")
            txn_note = f"Bill {bill['bill_no']}"
            upi_uri = (
                f"upi://pay?pa={urllib.parse.quote(upi_id)}"
                f"&pn={urllib.parse.quote(pay_name)}"
                f"&am={bill['due_amount']:.2f}&cu=INR"
                f"&tn={urllib.parse.quote(txn_note)}"
            )
            qr_url = (
                "https://api.qrserver.com/v1/create-qr-code/?size=300x300&data="
                + urllib.parse.quote(upi_uri, safe="")
            )
            with urllib.request.urlopen(qr_url, timeout=10) as resp:
                qr_bytes = resp.read()
            qr_buf = BytesIO(qr_bytes)
            story.append(Spacer(1, 14))
            story.append(Paragraph(
                f"<b>Amount pending: Rs.{bill['due_amount']:.2f}</b> &nbsp;&nbsp; Scan to pay via UPI ({upi_id})",
                normal,
            ))
            story.append(Spacer(1, 6))
            story.append(Image(qr_buf, width=45 * mm, height=45 * mm))
        except Exception:
            # If the QR service is unreachable, skip the QR silently rather
            # than failing the whole PDF download.
            pass

    story.append(Spacer(1, 10))
    story.append(Paragraph(settings.get("footer_note", ""), normal))

    doc.build(story)
    buf.seek(0)
    return buf


@app.route("/api/bills/<int:bill_id>/pdf")
def api_bill_pdf(bill_id):
    buf = build_bill_pdf(bill_id)
    if buf is None:
        return jsonify({"error": "not found"}), 404
    bill, _ = fetch_bill(bill_id)
    return send_file(
        buf, mimetype="application/pdf", as_attachment=True,
        download_name=f"{bill['bill_no']}.pdf",
    )


@app.route("/api/bills/<int:bill_id>/email", methods=["POST"])
def api_email_bill(bill_id):
    data = request.get_json(force=True) or {}
    bill, items = fetch_bill(bill_id)
    if not bill:
        return jsonify({"error": "not found"}), 404

    to_email = (data.get("email") or bill["customer_email"] or "").strip()
    if not to_email:
        return jsonify({"error": "No email address given"}), 400

    settings = get_settings()
    pdf_buf = build_bill_pdf(bill_id)
    subject = f"Invoice {bill['bill_no']} from {settings.get('company_name', '')}"
    body = (
        f"Dear {bill['customer_name'] or 'Customer'},\n\n"
        f"Please find attached your invoice {bill['bill_no']} dated {bill['bill_date']} "
        f"for Rs. {bill['total_amount']:.2f}.\n\n"
        f"Thank you,\n{settings.get('company_name', '')}"
    )
    try:
        send_email_with_attachment(to_email, subject, body, pdf_buf.getvalue(), f"{bill['bill_no']}.pdf")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    db = get_db()
    db.execute("UPDATE bills SET customer_email = ? WHERE id = ?", (to_email, bill_id))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/bills/<int:bill_id>/sms", methods=["POST"])
def api_sms_bill(bill_id):
    data = request.get_json(force=True) or {}
    bill, items = fetch_bill(bill_id)
    if not bill:
        return jsonify({"error": "not found"}), 404

    phone = (data.get("phone") or bill["customer_phone"] or "").strip()
    if not phone:
        return jsonify({"error": "No phone number given"}), 400

    settings = get_settings()
    msg = (
        f"{settings.get('company_name', '')}: Invoice {bill['bill_no']} dated {bill['bill_date']} "
        f"- Total Rs.{bill['total_amount']:.2f}. Thank you!"
    )
    try:
        send_sms(phone, msg)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# PWA (installable app shell)
# --------------------------------------------------------------------------

@app.route("/manifest.json")
def manifest():
    settings = get_settings()
    return jsonify({
        "name": settings.get("company_name", "AIR COOLAX"),
        "short_name": settings.get("company_name", "AIR COOLAX")[:12],
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f1f5f9",
        "theme_color": "#2563eb",
        "icons": [],
    })


@app.route("/sw.js")
def service_worker():
    js = """
const CACHE = 'aircoolax-v1';
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => self.clients.claim());
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET' || e.request.url.includes('/api/')) return;
  e.respondWith(
    caches.open(CACHE).then(cache =>
      fetch(e.request).then(res => { cache.put(e.request, res.clone()); return res; })
        .catch(() => cache.match(e.request))
    )
  );
});
"""
    return app.response_class(js, mimetype="application/javascript")


# --------------------------------------------------------------------------
# Main page
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template(
        "index.html",
        settings=get_settings(),
        next_bill_no=generate_next_bill_no(),
        today=datetime.now().strftime("%d/%m/%Y"),
    )


# Must run at import time (not just under `if __name__ == "__main__"`),
# because gunicorn imports this module as `app:app` and never executes
# the __main__ block — without this, Render's fresh billing.db never
# gets its tables created and every request fails with
# "sqlite3.OperationalError: no such table: settings".
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
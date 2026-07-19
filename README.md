# AIR COOLAX — Automatic Billing / Invoice Generator

Flask + SQLite billing app with an AI-assisted line-item chatbot,
item/rate autocomplete, current-date auto-fill, PDF export and
WhatsApp share.

## How to run

```bash
cd aircoolax
pip install -r requirements.txt --break-system-packages   # if needed
python3 app.py
```

Open **http://localhost:5000** in your browser.

On first run, `billing.db` (SQLite) is created automatically with your
company settings pre-filled (editable from the 💾 Settings icon in the
top bar).

## What's inside

- **app.py** — Flask backend: routes, SQLite schema, bill-number
  generator, AI suggestion engine, AI chatbot parser, PDF generator.
- **templates/index.html** — the invoice UI (Tailwind CSS via CDN,
  Alpine.js for interactivity, no build step needed).
- **billing.db** — auto-created SQLite database (bills + bill_items +
  settings tables).

## Features

| Feature | How it works |
|---|---|
| Auto date & bill number | Bill No auto-increments (A001, A002, ...); date is today's date, filled server-side |
| ✨ AI Assistant | Type a line like `"AC gas filling 500 qty 2, PCB repair 400"` and it auto-splits into item / rate / qty rows (rule-based NLP parser — works offline, no API key needed) |
| Item autocomplete | As you type a particular name, it suggests items + last-used rate from your billing history |
| Save to database | Every bill + its line items are stored in SQLite |
| PDF export | Server-generated PDF (ReportLab) per bill |
| WhatsApp Share | Opens WhatsApp with a pre-filled invoice summary |
| Settings | Edit company name, address, phone, email, footer note, bill prefix |
| Bill history | ↺ icon shows your last 50 bills with quick PDF links |
| Fully responsive | Tailwind mobile-first layout, works on phone/tablet/desktop |

## Notes on the "AI" parser

The chatbot line-item parser is a **local rule-based NLP engine** (regex
+ heuristics) — it needs no internet connection or API key, so it works
the same on your laptop or a customer's shop PC. It understands:

- `"item name <rate>"` → e.g. `AC service 400`
- `"item name <rate> qty <n>"` → e.g. `gas filling 500 qty 2`
- `"item name <n> pcs <rate>"` → e.g. `filter cleaning 2 pcs 150`
- multiple items separated by commas, "and", "aur", or new lines

If you'd rather plug in a real LLM (e.g. Anthropic/OpenAI) for smarter
parsing, `api_parse()` in `app.py` is the single function to swap out —
the frontend contract (`{"items":[{particular, rate, qty}]}`) stays
the same.

## Extending

- **Multi-user/login** was intentionally left out per your requirement
  (single business, no login) — add Flask-Login later if you outgrow this.
- **Deploying**: replace the dev server with `gunicorn app:app` behind
  nginx for production use; SQLite is fine for a single small business,
  move to Postgres/MySQL if you need concurrent multi-device writes.

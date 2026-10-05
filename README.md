# VAT Computation System

A T-Tech Solutions product. A Zimbabwean VAT-registered business uploads its transactions (Excel, CSV, bank statements, mobile money exports) and gets a reviewable VAT computation and return-ready schedules, with every figure traceable to its source rows.

Phase one covers VAT only. The VAT rules it applies follow the *VAT Rules Guide for the VAT Computation System* (October 2026), which is based on the VAT Act [Chapter 23:12] as updated to 1 December 2024 and the 2026 National Budget Statement. Section numbers below (s16 and so on) refer to the Act. The source documents are in `Tax guide/`.

## Quick start

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt     # Windows; use .venv/bin/pip on Linux/macOS
.venv/Scripts/python -m vatsys.demo              # optional: demo account with sample transport-company data
.venv/Scripts/python run.py                      # http://127.0.0.1:8000
```

Demo sign-in: `demo@example.com` / `demo-password-123`. Without the demo, the first account you register becomes the administrator.

To set up a business for someone else, an administrator fills in **Account holder** (email, name and a temporary password) when adding the business, or later in its Settings, and passes the password on. The account holder signs in at `/login` with that email and password and must choose their own password before continuing. Entering a temporary password for an existing account resets its password the same way. Anyone can change their password by clicking their name in the header.

Run the tests with `.venv/Scripts/python -m pytest`.

## How it works

1. **Upload** (`vatsys/ingest/`): reads `.xlsx` and `.csv` files. It finds the heading row even when bank preamble lines come first, and maps columns from common heading names. It parses Zimbabwean date formats (day first), amounts like `(1,234.50)` or `100.00 DR`, and USD/ZiG currency markers. Money in counts as a sale and money out as a purchase, unless the file or the upload options say otherwise. Balance and total lines are skipped. Rows it can't read are listed with the reason, and each upload shows its read rate. If the columns aren't recognised, the user chooses them and re-imports.
2. **Classify** (`vatsys/classify.py`, `vatsys/reference.py`): each transaction is classified from its description, in this order:
   1. The VAT category given in the file, if there is one.
   2. A rule set up for that business.
   3. The **reference lists**: goods and services whose treatment a document sets, such as the zero-rated and exempt schedules of the VAT (General) Regulations (SI 273 of 2003). An administrator imports them in Settings → Reference lists from a spreadsheet (download the template there), one row per item, with keywords, exclusions, the source document and paragraph, and optional dates. "2 packets of sugar" matches the item whose keywords include "sugar". The reason recorded cites the document, and the most specific match wins. The page has a box to test any description.
   4. A rule for all businesses.
   5. Otherwise the transaction is assumed standard-rated and flagged for review.

   Rules and list items apply only on the dates they are in force, so changes such as the 2026 Budget measures apply from their effective date. Classification runs entirely on the server; nothing is sent to an outside service. Users can change any classification, singly or in bulk, and can turn a correction into a rule.
3. **Compute** (`vatsys/compute.py`):
   - VAT is calculated per transaction at the rate in force on its date. VAT is taken out of inclusive amounts with the tax fraction rate ÷ (100 + rate) (s9(2)). An exact half cent is rounded in the taxpayer's favour: down on output tax and up on input tax (s71).
   - Blocked input tax (entertainment, club fees, most motor vehicles, export taxes; s16(2)) is not claimed.
   - Purchases marked *Mixed use* are apportioned by the period's taxable share of supplies. They are claimed in full when that share is 90% or more (s16(1)).
   - Purchases marked *Imported service* have VAT added at the standard rate. It goes on its own output line, because the recipient pays it (s13).
   - Each line is shown per transaction currency and in total in the reporting currency. Net VAT is stated separately for each currency, because currencies cannot be set off against each other (s38(4)). ZiG amounts are converted at the latest exchange rate on or before the transaction date, and the return lists every rate and date it used.
4. **Check** (`vatsys/checks.py`): before filing it flags:
   - missing exchange rates
   - unreviewed classifications
   - input tax claims with no invoice number or supplier VAT number (imports need only the bill of entry number)
   - the same supplier invoice claimed in another period
   - VAT in the file that differs from the computed VAT
   - possible duplicates and unusually large amounts
   - months with no sales or purchases
   - unreadable upload rows
   - exempt supplies alongside input tax claims (apportionment), and how mixed-use purchases were apportioned
   - VAT payable in one currency alongside a refund in another
   - a rate change inside the period (supplies spanning it must be split; s73)
   - zero-rated sales, which need documentary proof (s10(3))
   - self-accounted imported services
   - USD refunds of $60 or less, which are carried forward (s44)
   - returns past their due date, with an estimate of the late return penalty, the late payment penalty and, if a rate is set, interest (s39(2), s62(2))
5. **Export and finalise**: the Excel export has five sheets: the return, sales and purchases schedules (rate, net, VAT, FX rate and source row for each transaction), the exchange rates used, and the checks. Finalising records the figures and locks the period. Reopening needs a reason, and the earlier figures are kept in the audit log.
6. **Audit trail**: click any figure on the return to see the transactions behind it and how each was calculated. Each transaction page shows the original file row and its change history. All changes are logged per business.
7. **Settings**: VAT rates (with start dates), classification rules and exchange rates are database records that an administrator edits in the app. Changing them needs no code change.
8. **Registration**: for a business with no VAT number, the dashboard tracks taxable sales over the last 12 months against the US$25,000 registration threshold. It warns at 80% and says when registration is required (s23).
9. **Reminders**: the dashboard shows the next deadline (25th of the month after the period ends). `python -m vatsys.reminders` sends emails 10, 5 and 2 days before each deadline. Run it daily from Task Scheduler or cron. SMTP is set with `VATSYS_SMTP_*` variables; if SMTP isn't set, reminders are printed instead.

## Deploying a public demo (Render)

Create a Web Service from this repository with:

| Setting | Value |
|---|---|
| Build command | `pip install -r requirements.txt` |
| Start command | `python -m vatsys.demo && uvicorn vatsys.web.app:create_app --factory --host 0.0.0.0 --port $PORT` |
| Environment variables | `VATSYS_SECRET_KEY` (a long random value), `VATSYS_PUBLIC_DEMO=1`, and optionally `VATSYS_GOATCOUNTER` to count visitors |

`VATSYS_PUBLIC_DEMO=1` makes the demo account a regular user, because its password is published. The landing page that signed-out visitors see at `/` then shows a **Try the demo** button that signs in to it in one click. The button is never shown when the demo account is an administrator. On Render's free plan the disk is wiped whenever the service restarts, so the start command reloads the demo data each time; that also resets any changes visitors make. Don't put real client data on a free instance: it has no persistent storage. For real use, attach a persistent disk or a PostgreSQL database (`VATSYS_DATABASE_URL`).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `VATSYS_SECRET_KEY` | random per start | Session signing key. **Set this in production.** |
| `VATSYS_DATA_DIR` | `./data` | Database and stored uploads |
| `VATSYS_DATABASE_URL` | SQLite in data dir | Any SQLAlchemy URL (e.g. PostgreSQL) |
| `VATSYS_REVIEW_THRESHOLD` | `0.8` | Classifications below this confidence are flagged |
| `VATSYS_REMINDER_DAYS` | `10,5,2` | Days before a deadline to send reminders |
| `VATSYS_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_FROM` | | Reminder email |
| `VATSYS_GOATCOUNTER` | not set | GoatCounter count URL (e.g. `https://name.goatcounter.com/count`) to count visitors and sign-ins. Only page addresses are sent, with record numbers grouped |
| `VATSYS_REGISTRATION_THRESHOLD` | `25000` | USD taxable turnover in 12 months above which registration is required (s23) |
| `VATSYS_REGISTRATION_WARNING_SHARE` | `0.8` | Share of the threshold at which unregistered businesses are warned |
| `VATSYS_MIN_REFUND` | `60` | USD refunds at or below this are carried forward (s44) |
| `VATSYS_LATE_RETURN_PENALTY_PER_DAY`, `VATSYS_LATE_RETURN_MAX_DAYS` | `30`, `181` | Late return civil penalty (s62(2)) |
| `VATSYS_LATE_INTEREST_RATE` | not set | Prescribed interest rate on late payment, % per year (s39(2)). Interest is not estimated until this is set |

## To confirm with a tax adviser before pilots

These are seeded defaults and can all be changed in the app, but they need checking:

- **VAT rate history**: 14.5% (2020–2022), 15% (2023–2025), 15.5% from 1 Jan 2026. The 15.5% rate comes from the 2026 Budget and takes legal effect only once it is enacted in a Finance Act. Databases created before this change had 15.5% from 1 Jan 2025, and are corrected automatically on start-up.
- **Default classification rules**, in `vatsys/seed.py`:
  - Exempt (s11): interest, residential rent, education, medical services, and road and rail passenger transport.
  - Zero-rated (s10): exports, international transport of goods, and services to non-residents.
  - Not deductible (s16(2)): entertainment, club and membership fees, passenger motor cars, and export taxes.
  - Out of scope: payroll, tax payments and funding movements.
  - **Fuel has no default rule** on purpose, so it is flagged for review.
- **2026 Budget rules**, dated from 1 Jan 2026:
  - Bank charges become standard-rated (financial services to corporates).
  - Going concern sales move from zero-rated to standard-rated. A sale to a government-owned entity is still zero-rated and must be reclassified by hand.
  - Offshore digital services (Netflix, Starlink, Uber and similar) are out of scope, because Digital Services Withholding Tax replaces VAT.
  - Sunflower and other oil seeds are exempt.
  - The other Budget changes (land, subscriptions, local authority services, selected produce, tourist facilities) need no rule, because unmatched items are already treated as standard-rated.
- **Mixed-use apportionment** uses the period's taxable share of supplies (by value). Check this method suits the business; ZIMRA may agree another basis.
- **Imported services** are self-accounted but not claimed back as input tax in the same return. The guide says a claim needs proof that the VAT was paid (s15(2)).
- **Return line layout**: the lines (supplies by category, output tax, input tax on capital goods, imports and other, net VAT) follow the usual VAT 7 structure. They must be matched against the current ZIMRA return form before figures are entered in TaRMS.
- **Tax period categories** (s27): Category A (two-monthly, ending Jan/Mar/…), Category B (two-monthly, ending Feb/Apr/…) and Category C (monthly). Each business picks the one on its registration certificate.
- **Currency of payment**: the return shows net VAT per currency, because VAT received in a foreign currency must be paid in that currency (s38(4)). The converted total is for information only.
- **Import VAT**: taken from the VAT amount column, as on the bill of entry, rather than recomputed.

The sample exchange rates in `sample_data/exchange_rates_SAMPLE.csv` are invented. Load official RBZ rates through Settings → Exchange rates, which accepts a CSV with `Date, Rate` columns.

## Not built yet

These parts of the VAT Rules Guide are not built yet:

- Category D (other approved tax periods) and the payments basis of accounting (s14).
- Time of supply as the earliest of several dates (s8(1)). Each transaction has one date, which is taken as its time of supply.
- Automatic splitting of a supply that spans a rate change (s73). The return flags the change, and the user splits the transaction.
- Bad debt relief, add-back of input tax unpaid after 12 months, and change-of-use adjustments (s17, s22). Enter these as manual transactions for now.
- The 50% deduction for fiscal devices (s15(3)(j)).
- Checking invoices for every s20(4) field, and the 12-month claim window for late-claimed invoices. Transactions only carry an invoice number and supplier VAT number.
- Enforcing 6-year retention (s57). Uploads can still be deleted while their period is unlocked.

- Direct TaRMS submission (out of scope for phase one). There is no TaRMS upload file format yet, because the target format isn't known.
- Verifying transactions against fiscalised invoice data (an open question in the project brief).
- PDF bank statements and old `.xls` files. Users must save these as CSV or `.xlsx`.
- Encryption at rest. Use an encrypted disk or a managed database with encryption, and serve the app over HTTPS behind a reverse proxy.
- Roles within a business. Each business has one owner; administrators can see all businesses.

# 🔎 Smart Lost & Found Management System

### Campus Circle - Python, Flask & MySQL Mini Project

A campus web app for reporting lost and found items, browsing reports, reviewing possible matches, and managing claims. The matching engine uses a clear weighted rule, so every suggestion can be explained and checked by a person.

> **Demo note:** This is a local college project prototype. Match scores are suggestions, not proof that an item belongs to a claimant.

---

## ✨ Features

- 👤 **Accounts:** register, sign in, and view a personal dashboard.
- 📦 **Lost and found reports:** submit reports with category, description, location, date, and an optional image.
- 🔍 **Search and filters:** search reports and filter by type, category, status, and date range.
- 🧠 **Smart matching:** compare the category, item name, description, location, and date; save likely pairs with a score and explanation.
- 🤝 **Claims:** submit claims and let the item owner or an administrator review them.
- 🔔 **Notifications:** see updates about possible matches, claims, and returned items.
- 🛡️ **Admin dashboard:** review system totals, match activity, report patterns, and pending claims.
- 🧾 **Status history:** record report status changes for an audit trail.

## 🧰 Technology

| Part | Technology |
| --- | --- |
| Language | Python 3 |
| Web app | Flask |
| Database | MySQL 8 |
| Database driver | MySQL Connector for Python |
| Interface | HTML, CSS, JavaScript, Jinja templates |

## 🗂️ Project Structure

```text
ql/
├── app.py                 # Flask routes, validation, matching, and page logic
├── db.py                  # MySQL connection helper
├── setup_mysql.py         # Local database setup and .env creation
├── requirements.txt       # Python package requirements
├── .env.example           # Example local configuration (no real password)
├── database/
│   ├── schema.sql         # Tables, relationships, view, triggers, procedure
│   ├── sample_data.sql    # Optional demonstration records
│   └── queries.sql        # Example database queries
├── templates/             # HTML pages
├── static/                # CSS, JavaScript, and uploaded item photos
├── tests/                 # Project checks
└── output/pdf/            # Mini project report and screenshots
```

## 🗄️ Database Design

The app uses the `campus_lost_found` database and these main tables:

| Table | Purpose |
| --- | --- |
| `users` | Accounts and roles (`user` or `admin`) |
| `item_categories` | Shared report categories |
| `lost_items` | Lost-item reports |
| `found_items` | Found-item reports |
| `potential_matches` | Candidate report pairs, score, and explanation |
| `claim_requests` | Claims and owner/admin decisions |
| `notifications` | Match, claim, and return updates |
| `item_status_history` | Report status audit history |

Administrator accounts use the `role` field in `users`; there is no separate `admin` table. The match table is named `potential_matches` to match the existing MySQL Workbench schema.

## 🚀 Run on Windows

### 1. Start MySQL

Start the **MySQL80** service. In PowerShell, you can check it with:

```powershell
Get-Service MySQL80
```

### 2. Set up the database (first-time setup only)

From the project folder, run:

```powershell
cd D:\ML\ql
.\.venv\Scripts\python.exe setup_mysql.py
```

The setup script asks for your local MySQL host, port, username, and password, then creates the project schema and a private `.env` file. Your password is entered locally and is not printed by the script. It can offer sample records if the project database is empty.

> If `.env` is already present and configured, leave it in place and skip this setup step. Do not add `.env` to Git or share it.

### 3. Start the web app

```powershell
cd D:\ML\ql
.\.venv\Scripts\python.exe app.py
```

Then open **http://127.0.0.1:5000** in your browser. Keep the PowerShell window open while using the app. Press **Ctrl+C** in that window to stop the server.

### 4. Check the database connection

Open this URL in a browser:

```text
http://127.0.0.1:5000/health
```

A successful check returns JSON similar to:

```json
{"database":"campus_lost_found","status":"connected"}
```

## 🧠 How Matching Works

The system gives a score out of 100 using these maximum weights:

| Signal | Weight |
| --- | ---: |
| Same category | 32 points |
| Similar item name | 38 points |
| Description keywords | 20 points |
| Shared location term | 7 points |
| Date proximity | 3 points |

Candidate pairs scoring below **45** are skipped. Saved matches include a short explanation and appear in the match workflow for review. The system does not use machine learning.

## 🔐 Security Notes

- Passwords are stored as hashes, not plain text.
- SQL values use parameterized queries.
- Form changes require CSRF protection.
- Keep `.env` private; it contains local connection settings and the Flask signing key.
- Keep unique item-identifying details private until a claimant can verify ownership.

## 📄 Report

The project report includes the architecture, database design, matching approach, run steps, verification results, and screenshots:

[`Smart_Lost_and_Found_Mini_Project_Report.pdf`](output/pdf/Smart_Lost_and_Found_Mini_Project_Report.pdf)

## ⚠️ Scope and Limitations

- Example reports are demonstration data, not verified real campus reports.
- The weighted score can produce false matches or miss unusual descriptions; verify every claim in person.
- The app is configured for local demonstration and is not a production deployment.

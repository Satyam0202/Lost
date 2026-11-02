"""Flask entry point for the Campus Lost & Found project."""

import logging
import os
import re
import secrets
from datetime import datetime
from difflib import SequenceMatcher
from functools import wraps
from pathlib import Path
from uuid import uuid4

import mysql.connector
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_connection

load_dotenv(interpolate=False)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY") or secrets.token_urlsafe(48)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("FLASK_HTTPS", "false").lower() == "true"
app.config["UPLOAD_FOLDER"] = Path(app.root_path) / "static" / "uploads"
logging.basicConfig(level=logging.INFO)

REPORT_TABLES = {
    "lost": ("lost_items", "lost_item_id", "lost_date", "Lost"),
    "found": ("found_items", "found_item_id", "found_date", "Found"),
}
ALLOWED_STATUS_TRANSITIONS = {
    "active": {"matched", "closed"},
    "matched": {"active", "returned", "closed"},
    "returned": {"closed"},
    "closed": set(),
}
ALLOWED_IMAGE_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


def run_query(sql, parameters=()):
    """Run a parameterized SELECT and return its rows as dictionaries."""
    connection = get_connection()
    cursor = None
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(sql, parameters)
        return cursor.fetchall()
    finally:
        if cursor is not None:
            cursor.close()
        connection.close()


def login_required(view):
    """Redirect visitors to the sign-in page before showing a private page."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Aage badhne ke liye pehle sign in karein.", "info")
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    """Limit the admin dashboard and its data to active administrators."""
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if session.get("role") != "admin":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


@app.context_processor
def inject_navigation_data():
    return {
        "csrf_token": csrf_token,
        "now_date": datetime.now().date().isoformat(),
    }


def csrf_token():
    """Create one unpredictable CSRF token for the current browser session."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@app.before_request
def protect_state_changes():
    """Require a session token on every browser form that changes data."""
    if request.method == "POST" and not app.config.get("TESTING"):
        submitted_token = request.form.get("csrf_token", "")
        if not secrets.compare_digest(csrf_token(), submitted_token):
            abort(400, description="Form expired. Page reload karke dobara try karein.")


@app.before_request
def refresh_signed_in_user():
    """Do not keep a deactivated account or outdated role in a browser session."""
    if not session.get("user_id"):
        return None
    try:
        users = run_query(
            "SELECT user_id, full_name, role, is_active FROM users WHERE user_id = %s",
            (session["user_id"],),
        )
    except (mysql.connector.Error, RuntimeError) as error:
        app.logger.error("Could not verify the current user session: %s", error)
        return (
            render_template(
                "error.html",
                status=503,
                message="Campus database abhi available nahi hai. MySQL Server check karke dobara try karein.",
            ),
            503,
        )
    if not users or not users[0]["is_active"]:
        session.clear()
        flash("Aapka account active nahi hai. Dobara sign in karein.", "warning")
        return None
    session["full_name"] = users[0]["full_name"]
    session["role"] = users[0]["role"]
    return None


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(503)
def handle_request_error(error):
    message = getattr(error, "description", "Yeh page nahi mil saka.")
    return render_template("error.html", status=error.code, message=message), error.code


def database_error_message(error):
    """Log the technical error but show a safe setup hint in the page."""
    app.logger.error("MySQL is unavailable: %s", error)
    return (
        "Database se connection nahi hua. Workbench mein schema load karein, "
        "phir project ke .env mein MySQL settings check karke server restart karein."
    )


@app.errorhandler(mysql.connector.Error)
@app.errorhandler(RuntimeError)
def handle_database_error(error):
    app.logger.error("A database-backed page could not complete its query: %s", error)
    return (
        render_template(
            "error.html",
            status=503,
            message="Campus database abhi available nahi hai. MySQL Server check karke dobara try karein.",
        ),
        503,
    )


def normalize_terms(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").casefold()))


def score_match(lost_item, found_item):
    """Return a simple explainable 0–100 score for one lost/found pair."""
    points = 0.0
    reasons = []

    if lost_item["category_id"] == found_item["category_id"]:
        points += 32
        reasons.append("same category")

    lost_name = normalize_terms(lost_item["item_name"])
    found_name = normalize_terms(found_item["item_name"])
    name_similarity = SequenceMatcher(
        None, " ".join(sorted(lost_name)), " ".join(sorted(found_name))
    ).ratio()
    common_name_terms = lost_name & found_name
    name_score = max(name_similarity, len(common_name_terms) / max(len(lost_name), len(found_name), 1))
    if name_score:
        points += 38 * name_score
        if common_name_terms:
            reasons.append("similar item name")

    lost_description = normalize_terms(lost_item["description"])
    found_description = normalize_terms(found_item["description"])
    common_description_terms = (lost_description & found_description) - {
        "the", "and", "for", "with", "was", "a", "an", "on", "in", "at", "of",
    }
    description_score = len(common_description_terms) / max(
        len((lost_description | found_description) - {
            "the", "and", "for", "with", "was", "a", "an", "on", "in", "at", "of",
        }),
        1,
    )
    if description_score:
        points += 20 * description_score
        reasons.append("similar description keywords")

    lost_location = normalize_terms(lost_item["location"])
    found_location = normalize_terms(found_item["location"])
    if lost_location & found_location:
        points += 7
        reasons.append("nearby location")

    day_difference = abs((lost_item["report_date"] - found_item["report_date"]).days)
    date_score = max(0, 3 * (1 - day_difference / 31))
    points += date_score
    if day_difference <= 7:
        reasons.append("dates are close")

    if not reasons:
        reasons.append("some report details need review")
    return round(min(points, 100), 2), ", ".join(reasons)


def set_actor(cursor, user_id):
    cursor.execute("SET @app_user_id = %s", (user_id or 0,))


def add_notification(cursor, user_id, notification_type, message, **related):
    column_names = ["user_id", "notification_type", "message"]
    values = [user_id, notification_type, message]
    allowed_references = {"match_id", "claim_request_id", "lost_item_id", "found_item_id"}
    for column, value in related.items():
        if column in allowed_references and value is not None:
            column_names.append(column)
            values.append(value)
    placeholders = ", ".join(["%s"] * len(column_names))
    cursor.execute(
        f"INSERT INTO notifications ({', '.join(column_names)}) VALUES ({placeholders})",
        tuple(values),
    )


def recalculate_matches(cursor, kind, report_id, actor_id):
    """Compare a new/changed report with active opposite-type reports."""
    table, id_column, date_column, _label = REPORT_TABLES[kind]
    opposite_kind = "found" if kind == "lost" else "lost"
    opposite_table, opposite_id, opposite_date, _opposite_label = REPORT_TABLES[opposite_kind]

    cursor.execute(
        f"SELECT {id_column} AS report_id, category_id, item_name, description, "
        f"location, {date_column} AS report_date "
        f"FROM {table} WHERE {id_column} = %s AND status <> 'closed'",
        (report_id,),
    )
    source_rows = cursor.fetchall()
    if not source_rows:
        return 0
    source = source_rows[0]
    cursor.execute(
        f"SELECT {opposite_id} AS report_id, reporter_user_id, category_id, "
        f"item_name, description, location, {opposite_date} AS report_date "
        f"FROM {opposite_table} "
        f"WHERE category_id = %s AND status IN ('active', 'matched')",
        (source["category_id"],),
    )
    candidates = cursor.fetchall()
    match_count = 0

    for candidate in candidates:
        lost_item, found_item = (
            (source, candidate) if kind == "lost" else (candidate, source)
        )
        score, explanation = score_match(lost_item, found_item)
        if score < 45:
            continue

        cursor.execute(
            "INSERT INTO potential_matches (lost_item_id, found_item_id, score, explanation) "
            "VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE score = VALUES(score), explanation = VALUES(explanation)",
            (lost_item["report_id"], found_item["report_id"], score, explanation),
        )
        cursor.execute(
            "SELECT reporter_user_id FROM lost_items WHERE lost_item_id = %s",
            (lost_item["report_id"],),
        )
        lost_reporter_id = cursor.fetchone()["reporter_user_id"]
        cursor.execute(
            "SELECT reporter_user_id FROM found_items WHERE found_item_id = %s",
            (found_item["report_id"],),
        )
        found_reporter_id = cursor.fetchone()["reporter_user_id"]
        cursor.execute(
            "SELECT match_id FROM potential_matches WHERE lost_item_id = %s AND found_item_id = %s",
            (lost_item["report_id"], found_item["report_id"]),
        )
        match_id = cursor.fetchone()["match_id"]
        for recipient_id in {lost_reporter_id, found_reporter_id}:
            cursor.execute(
                "SELECT notification_id FROM notifications "
                "WHERE user_id = %s AND notification_type = 'potential_match' AND match_id = %s",
                (recipient_id, match_id),
            )
            if cursor.fetchone() is None:
                add_notification(
                    cursor,
                    recipient_id,
                    "potential_match",
                    f"A possible match was found (score {score:.0f}%). Please verify it carefully.",
                    match_id=match_id,
                )
        if source["report_id"] == report_id:
            cursor.execute(
                f"UPDATE {table} SET status = 'matched' "
                f"WHERE {id_column} = %s AND status = 'active'",
                (report_id,),
            )
            set_actor(cursor, actor_id)
        match_count += 1

    return match_count


def get_public_reports(filters=None, limit=6):
    """Read public report fields only; private identifying details are never selected."""
    filters = filters or {}
    report_queries = []

    if filters.get("kind") in (None, "all", "lost"):
        report_queries.append(
            (
                "SELECT 'Lost' AS item_type, li.lost_item_id AS item_id, "
                "li.item_name, c.name AS category, li.location, "
                "li.lost_date AS report_date, li.status, li.image_path "
                "FROM lost_items AS li "
                "INNER JOIN item_categories AS c ON c.category_id = li.category_id",
                "li",
                "lost_date",
            )
        )
    if filters.get("kind") in (None, "all", "found"):
        report_queries.append(
            (
                "SELECT 'Found' AS item_type, fi.found_item_id AS item_id, "
                "fi.item_name, c.name AS category, fi.location, "
                "fi.found_date AS report_date, fi.status, fi.image_path "
                "FROM found_items AS fi "
                "INNER JOIN item_categories AS c ON c.category_id = fi.category_id",
                "fi",
                "found_date",
            )
        )

    queries = []
    parameters = []
    for select_sql, alias, date_column in report_queries:
        conditions = []
        report_parameters = []
        if filters.get("category_id"):
            conditions.append(f"{alias}.category_id = %s")
            report_parameters.append(filters["category_id"])
        if filters.get("status"):
            conditions.append(f"{alias}.status = %s")
            report_parameters.append(filters["status"])
        if filters.get("date_from"):
            conditions.append(f"{alias}.{date_column} >= %s")
            report_parameters.append(filters["date_from"])
        if filters.get("date_to"):
            conditions.append(f"{alias}.{date_column} <= %s")
            report_parameters.append(filters["date_to"])
        if filters.get("search"):
            conditions.append(
                f"({alias}.item_name LIKE %s OR {alias}.description LIKE %s "
                f"OR {alias}.location LIKE %s)"
            )
            search_term = f"%{filters['search']}%"
            report_parameters.extend([search_term, search_term, search_term])
        if conditions:
            select_sql += " WHERE " + " AND ".join(conditions)
        queries.append(select_sql)
        parameters.extend(report_parameters)

    union_sql = " UNION ALL ".join(queries)
    sql = (
        "SELECT item_type, item_id, item_name, category, location, report_date, status, image_path "
        f"FROM ({union_sql}) AS public_reports "
        "ORDER BY report_date DESC, item_id DESC LIMIT %s"
    )
    parameters.append(limit)
    return run_query(sql, tuple(parameters))


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "GET":
        return render_template("auth.html", mode="register")

    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().casefold()
    password = request.form.get("password", "")
    if len(full_name) < 2 or len(full_name) > 100:
        flash("Naam 2 se 100 characters ke beech hona chahiye.", "error")
    elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 254:
        flash("Sahi college email enter karein.", "error")
    elif len(password) < 8 or len(password) > 128:
        flash("Password 8 se 128 characters ka hona chahiye.", "error")
    else:
        connection = None
        cursor = None
        try:
            connection = get_connection()
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                "INSERT INTO users (full_name, email, password_hash) VALUES (%s, %s, %s)",
                (full_name, email, generate_password_hash(password)),
            )
            user_id = cursor.lastrowid
            connection.commit()
        except mysql.connector.IntegrityError:
            if connection is not None:
                connection.rollback()
            flash("Is email se account pehle se maujood hai. Sign in karein.", "error")
            return render_template("auth.html", mode="register")
        except mysql.connector.Error as error:
            if connection is not None:
                connection.rollback()
            app.logger.error("Could not create a user account: %s", error)
            flash("Account abhi create nahi hua. Dobara koshish karein.", "error")
            return render_template("auth.html", mode="register")
        finally:
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()

        session.clear()
        session.update(user_id=user_id, full_name=full_name, role="user")
        flash("Account tayyar hai. Campus Circle mein aapka swagat hai!", "success")
        return redirect(url_for("dashboard"))
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "GET":
        return render_template("auth.html", mode="login")

    email = request.form.get("email", "").strip().casefold()
    password = request.form.get("password", "")
    try:
        users = run_query(
            "SELECT user_id, full_name, email, password_hash, role, is_active "
            "FROM users WHERE email = %s",
            (email,),
        )
    except (mysql.connector.Error, RuntimeError) as error:
        app.logger.error("Could not check login credentials: %s", error)
        flash("MySQL database abhi available nahi hai. Server check karke dobara try karein.", "error")
        return render_template("auth.html", mode="login"), 503
    if not users or not check_password_hash(users[0]["password_hash"], password):
        flash("Email ya password sahi nahi hai.", "error")
        return render_template("auth.html", mode="login")
    user = users[0]
    if not user["is_active"]:
        flash("Yeh account active nahi hai. Admin se sampark karein.", "error")
        return render_template("auth.html", mode="login")

    session.clear()
    session.update(user_id=user["user_id"], full_name=user["full_name"], role=user["role"])
    flash(f"Welcome back, {user['full_name']}!", "success")
    return redirect(url_for("dashboard"))


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    flash("Aap safely sign out ho gaye.", "info")
    return redirect(url_for("home"))


@app.route("/dashboard")
@login_required
def dashboard():
    user_id = session["user_id"]
    reports = run_query(
        "SELECT 'Lost' AS item_type, lost_item_id AS item_id, item_name, status, "
        "lost_date AS report_date FROM lost_items WHERE reporter_user_id = %s "
        "UNION ALL "
        "SELECT 'Found' AS item_type, found_item_id AS item_id, item_name, status, "
        "found_date AS report_date FROM found_items WHERE reporter_user_id = %s "
        "ORDER BY report_date DESC LIMIT 20",
        (user_id, user_id),
    )
    totals = run_query(
        "SELECT "
        "(SELECT COUNT(*) FROM lost_items WHERE reporter_user_id = %s) AS lost_count, "
        "(SELECT COUNT(*) FROM found_items WHERE reporter_user_id = %s) AS found_count, "
        "(SELECT COUNT(DISTINCT cr.claim_request_id) FROM claim_requests AS cr "
        "INNER JOIN found_items AS fi ON fi.found_item_id = cr.found_item_id "
        "WHERE cr.status = 'pending' AND "
        "(cr.claimant_user_id = %s OR fi.reporter_user_id = %s OR %s = 'admin')) "
        "AS my_pending_claims, "
        "(SELECT COUNT(*) FROM notifications WHERE user_id = %s AND read_at IS NULL) "
        "AS unread_notifications",
        (user_id, user_id, user_id, user_id, session["role"], user_id),
    )[0]
    notifications = run_query(
        "SELECT notification_id, notification_type, message, created_at, read_at "
        "FROM notifications WHERE user_id = %s ORDER BY created_at DESC LIMIT 8",
        (user_id,),
    )
    return render_template(
        "dashboard.html",
        reports=reports,
        totals=totals,
        notifications=notifications,
    )


def save_report(kind):
    table, id_column, date_column, label = REPORT_TABLES[kind]
    error_endpoint = "report_lost" if kind == "lost" else "report_found"
    item_name = request.form.get("item_name", "").strip()
    description = request.form.get("description", "").strip()
    identifying_details = request.form.get("identifying_details", "").strip() or None
    location = request.form.get("location", "").strip()
    date_value = request.form.get("report_date", "").strip()
    category_value = request.form.get("category_id", "").strip()
    errors = []
    if not 2 <= len(item_name) <= 120:
        errors.append("Item ka naam 2 se 120 characters ka hona chahiye.")
    if not 10 <= len(description) <= 3000:
        errors.append("Description 10 se 3000 characters ke beech likhein.")
    if not 2 <= len(location) <= 150:
        errors.append("Location 2 se 150 characters ke beech likhein.")
    if identifying_details and len(identifying_details) > 2000:
        errors.append("Private identifying details 2000 characters se chhote rakhein.")
    try:
        report_date = datetime.strptime(date_value, "%Y-%m-%d").date()
        if report_date > datetime.now().date():
            errors.append("Report ki date aaj ya usse pehle ki honi chahiye.")
    except ValueError:
        report_date = None
        errors.append("Sahi date select karein.")
    if not category_value.isdigit():
        category_id = None
        errors.append("Category select karein.")
    else:
        category_id = int(category_value)

    image = request.files.get("image")
    image_path = None
    upload_target = None
    if image and image.filename:
        extension = image.filename.rsplit(".", 1)[-1].casefold()
        header = image.stream.read(12)
        image.stream.seek(0)
        valid_signature = (
            (extension in ("jpg", "jpeg") and header.startswith(b"\xff\xd8\xff"))
            or (extension == "png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
            or (
                extension == "webp"
                and len(header) >= 12
                and header.startswith(b"RIFF")
                and header[8:12] == b"WEBP"
            )
        )
        if extension not in ALLOWED_IMAGE_TYPES or not valid_signature:
            errors.append("Sirf valid JPG, PNG, ya WEBP image upload karein.")
        else:
            safe_name = f"{uuid4().hex}.{extension}"
            image_path = f"uploads/{safe_name}"
            upload_target = app.config["UPLOAD_FOLDER"] / safe_name

    if errors:
        for error in errors:
            flash(error, "error")
        return render_template(
            "report_form.html",
            kind=kind,
            label=label,
            categories=run_query("SELECT category_id, name FROM item_categories ORDER BY name"),
            form_values=request.form,
        )

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        connection.start_transaction()
        set_actor(cursor, session["user_id"])
        cursor.execute(
            f"INSERT INTO {table} "
            "(reporter_user_id, category_id, item_name, description, identifying_details, "
            f"location, {date_column}, image_path) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session["user_id"],
                category_id,
                item_name,
                description,
                identifying_details,
                location,
                report_date,
                image_path,
            ),
        )
        report_id = cursor.lastrowid
        match_count = recalculate_matches(cursor, kind, report_id, session["user_id"])
        set_actor(cursor, None)
        if upload_target:
            upload_target.parent.mkdir(parents=True, exist_ok=True)
            image.save(upload_target)
        connection.commit()
    except (mysql.connector.Error, OSError) as error:
        connection.rollback()
        if upload_target and upload_target.exists():
            upload_target.unlink()
        app.logger.error("Could not save %s report: %s", kind, error)
        flash("Report save nahi hua. Details check karke dobara try karein.", "error")
        return redirect(url_for(error_endpoint))
    finally:
        cursor.close()
        connection.close()

    flash(
        f"{label} report #{report_id} save ho gaya. "
        f"{match_count} possible match(es) check karne ke liye ready hain.",
        "success",
    )
    return redirect(url_for("matches"))


@app.route("/report/lost", methods=["GET", "POST"])
@login_required
def report_lost():
    if request.method == "POST":
        return save_report("lost")
    return render_template(
        "report_form.html",
        kind="lost",
        label="Lost",
        categories=run_query("SELECT category_id, name FROM item_categories ORDER BY name"),
        form_values={},
    )


@app.route("/report/found", methods=["GET", "POST"])
@login_required
def report_found():
    if request.method == "POST":
        return save_report("found")
    return render_template(
        "report_form.html",
        kind="found",
        label="Found",
        categories=run_query("SELECT category_id, name FROM item_categories ORDER BY name"),
        form_values={},
    )


@app.route("/matches")
@login_required
def matches():
    user_id = session["user_id"]
    possible_matches = run_query(
        "SELECT pm.match_id, pm.score, pm.explanation, pm.created_at, "
        "li.lost_item_id, li.item_name AS lost_name, li.location AS lost_location, "
        "li.lost_date, lost_reporter.full_name AS lost_reporter, "
        "fi.found_item_id, fi.item_name AS found_name, fi.location AS found_location, "
        "fi.found_date, found_reporter.full_name AS found_reporter "
        "FROM potential_matches AS pm "
        "INNER JOIN lost_items AS li ON li.lost_item_id = pm.lost_item_id "
        "INNER JOIN found_items AS fi ON fi.found_item_id = pm.found_item_id "
        "INNER JOIN users AS lost_reporter ON lost_reporter.user_id = li.reporter_user_id "
        "INNER JOIN users AS found_reporter ON found_reporter.user_id = fi.reporter_user_id "
        "WHERE li.reporter_user_id = %s OR fi.reporter_user_id = %s "
        "ORDER BY pm.score DESC, pm.created_at DESC",
        (user_id, user_id),
    )
    return render_template("matches.html", matches=possible_matches)


@app.route("/claims", methods=["GET", "POST"])
@login_required
def claims():
    user_id = session["user_id"]
    if request.method == "POST":
        found_value = request.form.get("found_item_id", "").strip()
        private_details = request.form.get("identifying_details", "").strip()
        if not found_value.isdigit() or not 12 <= len(private_details) <= 2000:
            flash("Found item select karein aur private proof detail sahi likhein.", "error")
            return redirect(url_for("claims"))

        connection = get_connection()
        cursor = connection.cursor(dictionary=True)
        try:
            connection.start_transaction()
            cursor.execute(
                "SELECT found_item_id, reporter_user_id, status FROM found_items "
                "WHERE found_item_id = %s FOR UPDATE",
                (int(found_value),),
            )
            item = cursor.fetchone()
            if not item or item["status"] not in ("active", "matched"):
                raise ValueError("Is found item par abhi claim nahi kiya ja sakta.")
            if item["reporter_user_id"] == user_id:
                raise ValueError("Apne report kiye item par claim submit nahi kar sakte.")
            cursor.execute(
                "SELECT claim_request_id FROM claim_requests "
                "WHERE found_item_id = %s AND claimant_user_id = %s AND status = 'pending' "
                "FOR UPDATE",
                (int(found_value), user_id),
            )
            if cursor.fetchone():
                raise ValueError("Is item ke liye aapka pending claim pehle se hai.")
            cursor.execute(
                "INSERT INTO claim_requests (found_item_id, claimant_user_id, identifying_details) "
                "VALUES (%s, %s, %s)",
                (int(found_value), user_id, private_details),
            )
            claim_id = cursor.lastrowid
            add_notification(
                cursor,
                item["reporter_user_id"],
                "claim_submitted",
                f"A user submitted a claim for found item #{item['found_item_id']}.",
                claim_request_id=claim_id,
            )
            connection.commit()
            flash(f"Claim request #{claim_id} submit ho gaya.", "success")
        except ValueError as error:
            connection.rollback()
            flash(str(error), "error")
        except mysql.connector.Error as error:
            connection.rollback()
            app.logger.error("Could not submit a claim: %s", error)
            flash("Claim save nahi hua. Dobara try karein.", "error")
        finally:
            cursor.close()
            connection.close()
        return redirect(url_for("claims"))

    requests = run_query(
        "SELECT cr.claim_request_id, cr.found_item_id, cr.claimant_user_id, "
        "cr.identifying_details, cr.status, cr.decision_note, cr.created_at, cr.decided_at, "
        "fi.item_name, fi.reporter_user_id, claimant.full_name AS claimant_name, "
        "reviewer.full_name AS reviewer_name "
        "FROM claim_requests AS cr "
        "INNER JOIN found_items AS fi ON fi.found_item_id = cr.found_item_id "
        "INNER JOIN users AS claimant ON claimant.user_id = cr.claimant_user_id "
        "LEFT JOIN users AS reviewer ON reviewer.user_id = cr.decided_by_user_id "
        "WHERE cr.claimant_user_id = %s OR fi.reporter_user_id = %s OR %s = 'admin' "
        "ORDER BY cr.created_at DESC",
        (user_id, user_id, session["role"]),
    )
    claimable_items = run_query(
        "SELECT found_item_id, item_name, location FROM found_items "
        "WHERE status IN ('active', 'matched') AND reporter_user_id <> %s "
        "ORDER BY created_at DESC",
        (user_id,),
    )
    return render_template(
        "claims.html",
        claims=requests,
        claimable_items=claimable_items,
    )


@app.route("/claims/<int:claim_id>/decision", methods=["POST"])
@login_required
def decide_claim(claim_id):
    decision = request.form.get("decision", "")
    if decision not in ("approved", "rejected"):
        abort(400, description="Claim decision approved ya rejected hona chahiye.")
    note = request.form.get("decision_note", "").strip()[:500] or None
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "CALL sp_decide_claim(%s, %s, %s, %s)",
            (claim_id, session["user_id"], decision, note),
        )
        while cursor.nextset():
            pass
        connection.commit()
        flash(f"Claim #{claim_id} {decision} mark kiya gaya.", "success")
    except mysql.connector.Error as error:
        connection.rollback()
        app.logger.error("Could not decide claim #%s: %s", claim_id, error)
        flash(
            "Claim decision nahi ho saka. Sirf report owner ya active admin pending claim review kar sakte hain.",
            "error",
        )
    finally:
        cursor.close()
        connection.close()
    return redirect(url_for("claims"))


@app.route("/items/<kind>/<int:report_id>/status", methods=["POST"])
@login_required
def update_report_status(kind, report_id):
    if kind not in REPORT_TABLES:
        abort(404)
    table, id_column, _date_column, label = REPORT_TABLES[kind]
    new_status = request.form.get("status", "")
    if new_status not in ALLOWED_STATUS_TRANSITIONS:
        abort(400, description="Yeh report status supported nahi hai.")

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        connection.start_transaction()
        cursor.execute(
            f"SELECT reporter_user_id, status, item_name FROM {table} "
            f"WHERE {id_column} = %s FOR UPDATE",
            (report_id,),
        )
        item = cursor.fetchone()
        if not item:
            raise ValueError("Report nahi mili.")
        if item["reporter_user_id"] != session["user_id"] and session["role"] != "admin":
            connection.rollback()
            abort(403)
        if new_status not in ALLOWED_STATUS_TRANSITIONS[item["status"]]:
            raise ValueError(f"{item['status']} se {new_status} status change allowed nahi hai.")
        set_actor(cursor, session["user_id"])
        cursor.execute(
            f"UPDATE {table} SET status = %s WHERE {id_column} = %s",
            (new_status, report_id),
        )
        if new_status == "returned":
            counterpart_kind = "found" if kind == "lost" else "lost"
            counterpart_table, counterpart_id, _counterpart_date, _ = REPORT_TABLES[
                counterpart_kind
            ]
            match_key = f"{kind}_item_id"
            counterpart_key = f"{counterpart_kind}_item_id"
            cursor.execute(
                f"SELECT DISTINCT other.{counterpart_id} AS item_id, "
                f"other.reporter_user_id "
                f"FROM potential_matches AS pm "
                f"INNER JOIN {counterpart_table} AS other "
                f"ON other.{counterpart_id} = pm.{counterpart_key} "
                f"WHERE pm.{match_key} = %s AND other.status IN ('active', 'matched')",
                (report_id,),
            )
            counterpart_reports = cursor.fetchall()
            for other_report in counterpart_reports:
                cursor.execute(
                    f"UPDATE {counterpart_table} SET status = 'returned' "
                    f"WHERE {counterpart_id} = %s AND status IN ('active', 'matched')",
                    (other_report["item_id"],),
                )
                if other_report["reporter_user_id"] != session["user_id"]:
                    add_notification(
                        cursor,
                        other_report["reporter_user_id"],
                        "item_returned",
                        f"A possible match for report #{report_id} was returned.",
                        **{counterpart_key: other_report["item_id"]},
                    )
        set_actor(cursor, None)
        connection.commit()
        flash(f"Report #{report_id} status {new_status} ho gaya.", "success")
    except ValueError as error:
        connection.rollback()
        flash(str(error), "error")
    except mysql.connector.Error as error:
        connection.rollback()
        app.logger.error("Could not update report #%s status: %s", report_id, error)
        flash("Report status update nahi hua. Dobara try karein.", "error")
    finally:
        cursor.close()
        connection.close()
    return redirect(url_for("dashboard"))


@app.route("/notifications/read", methods=["POST"])
@login_required
def mark_notifications_read():
    notification_id = request.form.get("notification_id", "").strip()
    if notification_id and not notification_id.isdigit():
        abort(400, description="Notification ID valid nahi hai.")
    connection = get_connection()
    cursor = connection.cursor()
    try:
        if notification_id:
            cursor.execute(
                "UPDATE notifications SET read_at = CURRENT_TIMESTAMP "
                "WHERE notification_id = %s AND user_id = %s AND read_at IS NULL",
                (int(notification_id), session["user_id"]),
            )
        else:
            cursor.execute(
                "UPDATE notifications SET read_at = CURRENT_TIMESTAMP "
                "WHERE user_id = %s AND read_at IS NULL",
                (session["user_id"],),
            )
        connection.commit()
    except mysql.connector.Error as error:
        connection.rollback()
        app.logger.error("Could not mark notifications as read: %s", error)
        flash("Notifications update nahi hui.", "error")
    finally:
        cursor.close()
        connection.close()
    return redirect(url_for("dashboard"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    totals = run_query("SELECT * FROM v_dashboard_totals")[0]
    matching_stats = run_query(
        "SELECT COUNT(*) AS candidate_count, AVG(score) AS average_score, "
        "MAX(score) AS best_score FROM potential_matches"
    )[0]
    frequent_categories = run_query(
        "SELECT c.name, "
        "COUNT(DISTINCT li.lost_item_id) AS report_count "
        "FROM item_categories AS c "
        "LEFT JOIN lost_items AS li ON li.category_id = c.category_id "
        "LEFT JOIN found_items AS fi ON fi.category_id = c.category_id "
        "GROUP BY c.category_id, c.name "
        "HAVING COUNT(DISTINCT li.lost_item_id) > 0 "
        "ORDER BY report_count DESC, c.name LIMIT 6"
    )
    busy_locations = run_query(
        "SELECT location, COUNT(*) AS report_count FROM ("
        "SELECT location FROM lost_items UNION ALL SELECT location FROM found_items"
        ") AS all_reports GROUP BY location HAVING COUNT(*) > 0 "
        "ORDER BY report_count DESC, location LIMIT 6"
    )
    recent_pending_claims = run_query(
        "SELECT cr.claim_request_id, cr.found_item_id, cr.created_at, "
        "fi.item_name, claimant.full_name AS claimant_name "
        "FROM claim_requests AS cr "
        "INNER JOIN found_items AS fi ON fi.found_item_id = cr.found_item_id "
        "INNER JOIN users AS claimant ON claimant.user_id = cr.claimant_user_id "
        "WHERE cr.status = 'pending' ORDER BY cr.created_at LIMIT 8"
    )
    return render_template(
        "admin.html",
        totals=totals,
        matching_stats=matching_stats,
        categories=frequent_categories,
        locations=busy_locations,
        pending_claims=recent_pending_claims,
    )


@app.route("/")
def home():
    """Show a small live dashboard and recent public reports."""
    try:
        totals = run_query("SELECT * FROM v_dashboard_totals")[0]
        average_score = run_query(
            "SELECT AVG(score) AS average_score FROM potential_matches"
        )[0]["average_score"]
        recent_reports = get_public_reports(limit=6)
        return render_template(
            "index.html",
            totals=totals,
            average_score=average_score,
            recent_reports=recent_reports,
            database_error=None,
        )
    except (mysql.connector.Error, RuntimeError, IndexError) as error:
        return render_template(
            "index.html",
            totals=None,
            average_score=None,
            recent_reports=[],
            database_error=database_error_message(error),
        )


@app.route("/browse")
def browse():
    """Search active and closed reports using parameterized filters."""
    search = request.args.get("q", "").strip()[:100]
    kind = request.args.get("type", "all")
    if kind not in ("all", "lost", "found"):
        kind = "all"

    errors = []
    category_id = request.args.get("category", "").strip()
    if category_id and not category_id.isdigit():
        errors.append("Category filter sahi select karein.")
        category_id = ""
    status = request.args.get("status", "").strip()
    allowed_statuses = {"", "active", "matched", "returned", "closed"}
    if status not in allowed_statuses:
        errors.append("Report status filter sahi select karein.")
        status = ""

    date_from = request.args.get("from", "").strip()
    date_to = request.args.get("to", "").strip()
    for label, value in (("From", date_from), ("To", date_to)):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                errors.append(f"{label} date YYYY-MM-DD format mein honi chahiye.")
    if date_from and date_to and date_from > date_to:
        errors.append("Start date, end date se baad nahi ho sakti.")

    filters = {
        "kind": kind,
        "search": search,
        "category_id": int(category_id) if category_id else None,
        "status": status or None,
        "date_from": date_from or None,
        "date_to": date_to or None,
    }

    reports = []
    categories = []
    database_error = None
    if not errors:
        try:
            reports = get_public_reports(filters=filters, limit=100)
            categories = run_query(
                "SELECT category_id, name FROM item_categories ORDER BY name"
            )
        except (mysql.connector.Error, RuntimeError) as error:
            database_error = database_error_message(error)

    return render_template(
        "browse.html",
        reports=reports,
        categories=categories,
        filters=filters,
        errors=errors,
        database_error=database_error,
    )


@app.route("/health")
def health():
    """A quick connection check that does not reveal database credentials."""
    try:
        rows = run_query("SELECT 1 AS connected")
    except (mysql.connector.Error, RuntimeError) as error:
        app.logger.error("MySQL health check failed: %s", error)
        return jsonify(status="disconnected"), 503
    if not rows or rows[0]["connected"] != 1:
        return jsonify(status="disconnected"), 503
    return jsonify(status="connected", database=os.getenv("MYSQL_DATABASE")), 200


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.getenv("FLASK_PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )

"""Connect to local MySQL, initialize the project schema, and save a private .env."""

import getpass
import os
import secrets
from pathlib import Path

import mysql.connector
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parent
DATABASE = "campus_lost_found"
EXPECTED_TABLES = {
    "users",
    "item_categories",
    "lost_items",
    "found_items",
    "potential_matches",
    "claim_requests",
    "notifications",
    "item_status_history",
}


def quote_env_value(value):
    """Quote one value for .env, preserving characters such as #, $ and quotes."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


def split_sql_script(script):
    """Split MySQL Workbench scripts while respecting DELIMITER for routines."""
    delimiter = ";"
    buffered_lines = []

    for line in script.splitlines():
        stripped_line = line.strip()
        if not buffered_lines and not stripped_line:
            continue
        if not buffered_lines and stripped_line.startswith("--"):
            continue
        if not buffered_lines and stripped_line.upper().startswith("DELIMITER "):
            delimiter = stripped_line.split(None, 1)[1]
            continue

        buffered_lines.append(line)
        if stripped_line.endswith(delimiter):
            statement = "\n".join(buffered_lines).rstrip()
            statement = statement[: -len(delimiter)].strip()
            if statement:
                yield statement
            buffered_lines.clear()

    if buffered_lines:
        raise ValueError("SQL file ends before the statement delimiter.")


def show_setup_error(error):
    """Show safe connection context without printing credentials."""
    print(f"\nMySQL connection/setup failed (error code {getattr(error, 'errno', 'unknown')}).")
    if getattr(error, "msg", None):
        print(f"MySQL detail: {error.msg}")
    print("Check that MySQL Server is running and that the host, port, user,")
    print("password, and MySQL permissions match your Workbench connection.")


def main():
    env_path = ROOT / ".env"
    if env_path.exists():
        print(".env already exists. It was not changed.")
        print("Edit that local file if you want to change the MySQL credentials.")
        return 1

    print("Campus Circle: connect to your local MySQL Workbench server.")
    print("Your password is handled locally and is never displayed.")
    host = os.getenv("MYSQL_HOST") or input("MySQL host [127.0.0.1]: ").strip() or "127.0.0.1"
    port_text = os.getenv("MYSQL_PORT") or input("MySQL port [3306]: ").strip() or "3306"
    user = os.getenv("MYSQL_USER") or input("MySQL user [root]: ").strip() or "root"
    password = os.getenv("MYSQL_PASSWORD")
    if password is None:
        password = getpass.getpass("MySQL password (hidden): ")
    database = DATABASE

    try:
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError("Port must be from 1 to 65535.")
    except ValueError as error:
        print(f"Invalid MySQL port: {error}")
        return 1

    connection = None
    env_created = False
    try:
        connection = mysql.connector.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            connection_timeout=5,
            charset="utf8mb4",
        )
        cursor = connection.cursor()
        cursor.execute("SHOW DATABASES LIKE %s", (database,))
        database_exists = cursor.fetchone() is not None
        is_fresh_database = not database_exists
        if database_exists:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s AND table_type = 'BASE TABLE'",
                (database,),
            )
            existing_tables = {row[0] for row in cursor.fetchall()}
            if not existing_tables <= EXPECTED_TABLES:
                extras = sorted(existing_tables - EXPECTED_TABLES)
                print(
                    f"\n'{database}' contains tables outside this project and was left untouched."
                )
                if extras:
                    print("Other tables:", ", ".join(extras))
                print("Choose a separate project database to avoid changing other data.")
                return 1
            if existing_tables == EXPECTED_TABLES:
                print(f"\nFound all 8 project tables in '{database}'.")
            elif existing_tables:
                print(
                    f"\nResuming the project schema in '{database}' "
                    f"({len(existing_tables)} of 8 tables already exist)."
                )
            else:
                print(f"\nFound an empty database '{database}'.")
                is_fresh_database = True
        else:
            print(f"\nDatabase '{database}' does not exist yet.")

        schema_path = ROOT / "database" / "schema.sql"
        script = schema_path.read_text(encoding="utf-8")
        for statement in split_sql_script(script):
            cursor.execute(statement)

        cursor.execute("SELECT DATABASE(), VERSION()")
        selected_database, version = cursor.fetchone()
        if selected_database != database:
            raise RuntimeError(
                f"Schema selected '{selected_database}', not '{database}'. "
                "Use the project default database name."
            )

        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE'",
            (database,),
        )
        installed_tables = {row[0] for row in cursor.fetchall()}
        if installed_tables != EXPECTED_TABLES:
            raise RuntimeError(
                "MySQL did not create the expected project tables: "
                + ", ".join(sorted(EXPECTED_TABLES - installed_tables))
            )

        cursor.execute(
            "SELECT routine_name FROM information_schema.routines "
            "WHERE routine_schema = %s AND routine_type = 'PROCEDURE'",
            (database,),
        )
        procedures = {row[0] for row in cursor.fetchall()}
        if "sp_decide_claim" not in procedures:
            raise RuntimeError("The claim-decision stored procedure was not created.")

        cursor.execute(
            "SELECT table_name FROM information_schema.views "
            "WHERE table_schema = %s AND table_name = 'v_dashboard_totals'",
            (database,),
        )
        if cursor.fetchone() is None:
            raise RuntimeError("The dashboard view was not created.")

        cursor.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM users) + "
            "(SELECT COUNT(*) FROM item_categories) + "
            "(SELECT COUNT(*) FROM lost_items) + "
            "(SELECT COUNT(*) FROM found_items) + "
            "(SELECT COUNT(*) FROM potential_matches) + "
            "(SELECT COUNT(*) FROM claim_requests) + "
            "(SELECT COUNT(*) FROM notifications)"
        )
        total_demo_related_rows = cursor.fetchone()[0]
        add_sample_data = False
        if total_demo_related_rows == 0:
            sample_setting = os.getenv("LOAD_SAMPLE_DATA")
            if sample_setting is None:
                prompt_default = "Y" if is_fresh_database else "n"
                answer = input(
                    f"Add sample rows to '{database}' so the dashboard has examples? [{prompt_default}/n]: "
                ).strip().lower()
                add_sample_data = (
                    answer in ("", "y", "yes")
                    if prompt_default == "Y"
                    else answer in ("y", "yes")
                )
            else:
                add_sample_data = sample_setting.strip().lower() in ("y", "yes", "1", "true")
            if add_sample_data:
                seed_script = (ROOT / "database" / "sample_data.sql").read_text(
                    encoding="utf-8"
                )
                for statement in split_sql_script(seed_script):
                    cursor.execute(statement)
                print("Loaded example accounts, categories, reports, matches, and notifications.")
            else:
                print("Skipped sample rows; the schema is ready for your own data.")
        else:
            print("Kept existing report data and skipped sample inserts.")

        connection.commit()
        cursor.close()

        contents = "\n".join(
            (
                f"MYSQL_HOST={quote_env_value(host)}",
                f"MYSQL_PORT={port}",
                f"MYSQL_USER={quote_env_value(user)}",
                f"MYSQL_PASSWORD={quote_env_value(password)}",
                f"MYSQL_DATABASE={quote_env_value(database)}",
                f"FLASK_SECRET_KEY={quote_env_value(secrets.token_urlsafe(48))}",
                "FLASK_DEBUG=true",
                "",
            )
        )
        env_created = True
        env_path.write_text(contents, encoding="utf-8")
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass

        settings = dotenv_values(env_path, interpolate=False)
        if settings.get("MYSQL_PASSWORD") != password:
            env_path.unlink(missing_ok=True)
            env_created = False
            raise RuntimeError("The saved local MySQL setting could not be verified.")

        print(f"\nConnected successfully to MySQL {version}.")
        print(f"Database: {selected_database}")
        print("Created/verified 8 project tables, the dashboard view, and claim procedure.")
        print(f"Private local connection settings saved to {env_path}.")
        if total_demo_related_rows == 0 and add_sample_data:
            print("Example rows are now visible in MySQL Workbench and the dashboard.")
        elif total_demo_related_rows == 0:
            print("Add examples later by running database/sample_data.sql in Workbench.")
        print("Then restart Flask with: py app.py")
        return 0
    except (mysql.connector.Error, OSError, RuntimeError, ValueError) as error:
        if isinstance(error, mysql.connector.Error):
            show_setup_error(error)
        else:
            print(f"\nSetup could not be completed: {error}")
        if env_created and env_path.exists():
            env_path.unlink(missing_ok=True)
        return 1
    finally:
        if connection is not None and connection.is_connected():
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())

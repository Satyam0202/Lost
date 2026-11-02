"""Small MySQL connection helper used by the Flask routes."""

import os

import mysql.connector
from dotenv import load_dotenv

load_dotenv(interpolate=False)


def get_connection():
    """Open one connection using the private settings from the local .env file."""
    required_settings = ("MYSQL_HOST", "MYSQL_PORT", "MYSQL_USER", "MYSQL_DATABASE")
    missing_settings = [
        setting for setting in required_settings if not os.getenv(setting)
    ]
    if missing_settings:
        missing = ", ".join(missing_settings)
        raise RuntimeError(
            f"Missing MySQL settings: {missing}. Copy .env.example to .env and fill it in."
        )

    return mysql.connector.connect(
        host=os.environ["MYSQL_HOST"],
        port=int(os.environ["MYSQL_PORT"]),
        user=os.environ["MYSQL_USER"],
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.environ["MYSQL_DATABASE"],
        connection_timeout=5,
        charset="utf8mb4",
    )

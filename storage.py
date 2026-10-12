import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit


FIELDS = ("name", "hoster", "website", "starts_on", "ends_on")


def validate_field(field, value):
    value = value.strip()
    if field in ("starts_on", "ends_on"):
        if not re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}", value):
            raise ValueError("Введите дату в формате ДД.ММ.ГГГГ.")
        try:
            return datetime.strptime(value, "%d.%m.%Y").date().isoformat()
        except ValueError:
            raise ValueError("Такой даты не существует. Используйте ДД.ММ.ГГГГ.") from None
    if field == "website":
        try:
            parsed = urlsplit(value)
            parsed.port
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and not parsed.username and not parsed.password
                     and not any(char.isspace() or ord(char) < 32 for char in value))
        except ValueError:
            valid = False
        if not valid or len(value) > 500:
            raise ValueError("Введите HTTP/HTTPS адрес сайта без пробелов, до 500 символов.")
    elif field in ("name", "hoster"):
        if not value or len(value) > 100 or any(ord(char) < 32 for char in value):
            raise ValueError("Введите название от 1 до 100 символов, одной строкой.")
    else:
        raise ValueError("Неизвестное поле сервера.")
    return value


def validate_server(fields):
    result = {}
    for field in FIELDS:
        value = fields[field]
        if field in ("starts_on", "ends_on"):
            try:
                result[field] = date.fromisoformat(value).isoformat()
            except ValueError:
                raise ValueError("Некорректная дата.") from None
        else:
            result[field] = validate_field(field, value)
    if result["ends_on"] < result["starts_on"]:
        raise ValueError("Дата окончания не может быть раньше даты начала.")
    return result


class Storage:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, hoster TEXT NOT NULL, website TEXT NOT NULL,
                starts_on TEXT NOT NULL, ends_on TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                ends_on TEXT NOT NULL, admin_id INTEGER NOT NULL, day TEXT NOT NULL,
                PRIMARY KEY (server_id, ends_on, admin_id, day)
            );
        """)

    def close(self):
        self.connection.close()

    def get(self, server_id):
        row = self.connection.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
        return dict(row) if row else None

    def list(self):
        return [dict(row) for row in self.connection.execute("SELECT * FROM servers ORDER BY id")]

    def add(self, fields):
        fields = validate_server(fields)
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO servers (name, hoster, website, starts_on, ends_on) VALUES (?, ?, ?, ?, ?)",
                tuple(fields[key] for key in FIELDS))
        return self.get(cursor.lastrowid)

    def update(self, server_id, version, fields):
        saved = self.get(server_id)
        if not saved or saved["version"] != version:
            return False
        fields = validate_server({**saved, **fields})
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE servers SET name=?, hoster=?, website=?, starts_on=?, ends_on=?,
                   version=version+1 WHERE id=? AND version=?""",
                (*[fields[key] for key in FIELDS], server_id, version))
        return cursor.rowcount == 1

    def delete(self, server_id, version):
        with self.connection:
            cursor = self.connection.execute("DELETE FROM servers WHERE id=? AND version=?",
                                             (server_id, version))
        return cursor.rowcount == 1

    def delivered(self, server_id, ends_on, admin_id, day):
        return self.connection.execute(
            "SELECT 1 FROM deliveries WHERE server_id=? AND ends_on=? AND admin_id=? AND day=?",
            (server_id, ends_on, admin_id, day)).fetchone() is not None

    def mark_delivered(self, server_id, ends_on, admin_id, day):
        with self.connection:
            self.connection.execute(
                """INSERT OR IGNORE INTO deliveries (server_id, ends_on, admin_id, day)
                   SELECT id, ends_on, ?, ? FROM servers WHERE id=? AND ends_on=?""",
                (admin_id, day, server_id, ends_on))

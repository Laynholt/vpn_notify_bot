import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit


FIELDS = ("name", "hoster", "website", "ip_address", "domain", "starts_on", "ends_on")
# Leave enough significant digits for Excel to preserve the kopecks.
MAX_MONEY = 99_999_999_999_999


def validate_amount(value):
    if value is not None and (type(value) is not int or not 0 <= value <= MAX_MONEY):
        raise ValueError("Введите неотрицательную сумму в рублях, не более 999 999 999 999,99.")
    return value


def format_money(kopeks):
    if kopeks is None:
        return "не указано"
    rubles = f"{kopeks // 100:,}".replace(",", " ")
    return f"{rubles},{kopeks % 100:02d} ₽"


def validate_payment_dates(fields, history):
    first_end = fields["ends_on"] if len(history) == 1 else history[0]["ends_on"]
    last_start = fields["starts_on"] if len(history) == 1 else history[-1]["starts_on"]
    if fields["starts_on"] > first_end or fields["ends_on"] < last_start:
        raise ValueError("Изменение дат нарушает границы оплаченных периодов. Проверьте историю в статистике.")


def validate_field(field, value):
    value = value.strip()
    if field == "amount_kopeks":
        if not re.fullmatch(r"[0-9]{1,12}(?:[.,][0-9]{1,2})?", value):
            raise ValueError("Введите сумму в рублях, например 1500 или 1500,50; не более двух знаков после запятой.")
        whole, _, fraction = value.replace(",", ".").partition(".")
        return validate_amount(int(whole) * 100 + int((fraction + "00")[:2]))
    if field == "ip_address":
        try:
            if "%" in value:
                raise ValueError
            return str(ip_address(value))
        except ValueError:
            raise ValueError("Введите корректный IPv4 или IPv6, без порта и маски подсети.") from None
    if field == "domain":
        try:
            domain = value.lower().removesuffix(".").encode("idna").decode("ascii")
        except UnicodeError:
            domain = ""
        labels = domain.split(".")
        if (len(domain) > 253 or len(labels) < 2 or labels[-1].isdigit()
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)):
            raise ValueError("Введите доменное имя без https:// и пути, например vpn.example.com.")
        return domain
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


def validate_server(fields, *, allow_empty_addresses=False):
    result = {}
    for field in FIELDS:
        value = fields.get(field, "") if field in ("ip_address", "domain") else fields[field]
        if allow_empty_addresses and field in ("ip_address", "domain") and value == "":
            result[field] = ""
            continue
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
                ip_address TEXT NOT NULL DEFAULT '', domain TEXT NOT NULL DEFAULT '',
                starts_on TEXT NOT NULL, ends_on TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                ends_on TEXT NOT NULL, admin_id INTEGER NOT NULL, day TEXT NOT NULL,
                PRIMARY KEY (server_id, ends_on, admin_id, day)
            );
            CREATE TABLE IF NOT EXISTS reminder_messages (
                server_id INTEGER NOT NULL REFERENCES servers(id), ends_on TEXT NOT NULL,
                admin_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                PRIMARY KEY (admin_id, message_id)
            );
        """)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(servers)")}
        with self.connection:
            for field in ("ip_address", "domain"):
                if field not in columns:
                    self.connection.execute(f"ALTER TABLE servers ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
            if "deleted_at" not in columns:
                self.connection.execute("ALTER TABLE servers ADD COLUMN deleted_at TEXT")
            if "archive_pending" not in columns:
                self.connection.execute("""ALTER TABLE servers ADD COLUMN
                    archive_pending INTEGER NOT NULL DEFAULT 0 CHECK (archive_pending IN (0, 1))""")
            self.connection.execute("""CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL REFERENCES servers(id),
                starts_on TEXT NOT NULL, ends_on TEXT NOT NULL,
                amount_kopeks INTEGER,
                paid_with_bonus INTEGER NOT NULL DEFAULT 0 CHECK (paid_with_bonus IN (0, 1)),
                CHECK (ends_on >= starts_on),
                CHECK (amount_kopeks IS NULL OR
                       (typeof(amount_kopeks)='integer' AND amount_kopeks >= 0 AND amount_kopeks <= 99999999999999))
            )""")
            payment_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(payments)")}
            if "paid_with_bonus" not in payment_columns:
                self.connection.execute("""ALTER TABLE payments ADD COLUMN
                    paid_with_bonus INTEGER NOT NULL DEFAULT 0 CHECK (paid_with_bonus IN (0, 1))""")
            self.connection.execute("CREATE INDEX IF NOT EXISTS payments_server ON payments(server_id, id)")
            self.connection.execute("""INSERT INTO payments (server_id, starts_on, ends_on)
                SELECT id, starts_on, ends_on FROM servers
                WHERE NOT EXISTS (SELECT 1 FROM payments WHERE server_id=servers.id)""")

    def close(self):
        self.connection.close()

    def get(self, server_id):
        row = self.connection.execute("SELECT * FROM servers WHERE id=? AND deleted_at IS NULL", (server_id,)).fetchone()
        return self._with_payments(row) if row else None

    def list(self):
        return [self._with_payments(row) for row in self.connection.execute(
            "SELECT * FROM servers WHERE deleted_at IS NULL ORDER BY id")]

    def payment_history(self, server_id):
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM payments WHERE server_id=? ORDER BY id", (server_id,))]

    def _with_payments(self, row, *, include_history=False):
        server = dict(row)
        payments = self.payment_history(server["id"])
        amounts = [payment["amount_kopeks"] for payment in payments if payment["amount_kopeks"] is not None]
        server.update(payment_starts_on=payments[-1]["starts_on"], amount_kopeks=payments[-1]["amount_kopeks"],
                      paid_with_bonus=payments[-1]["paid_with_bonus"],
                      total_paid_kopeks=sum(amounts), known_payments=len(amounts),
                      unknown_payments=len(payments) - len(amounts))
        if include_history:
            server["payments"] = payments
        return server

    def statistics(self, server_id=None):
        with self.connection:
            self.connection.execute("BEGIN")
            rows = self.connection.execute("SELECT * FROM servers ORDER BY id" if server_id is None
                else "SELECT * FROM servers WHERE id=?", () if server_id is None else (server_id,)).fetchall()
            return [self._with_payments(row, include_history=True) for row in rows]

    def add(self, fields):
        amount = validate_amount(fields.get("amount_kopeks"))
        fields = validate_server(fields)
        with self.connection:
            cursor = self.connection.execute(
                """INSERT INTO servers (name, hoster, website, ip_address, domain, starts_on, ends_on)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                tuple(fields[key] for key in FIELDS))
            self.connection.execute("INSERT INTO payments (server_id, starts_on, ends_on, amount_kopeks) VALUES (?, ?, ?, ?)",
                                    (cursor.lastrowid, fields["starts_on"], fields["ends_on"], amount))
        return self.get(cursor.lastrowid)

    def update(self, server_id, version, fields):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            saved = self.get(server_id)
            if not saved or saved["version"] != version:
                return False
            amount = validate_amount(fields.get("amount_kopeks", saved["amount_kopeks"]))
            paid_with_bonus = saved["paid_with_bonus"] if amount == saved["amount_kopeks"] == 0 else 0
            fields = validate_server({**saved, **fields}, allow_empty_addresses=True)
            history = self.payment_history(server_id)
            first, last = history[0], history[-1]
            validate_payment_dates(fields, history)
            validate_amount(saved["total_paid_kopeks"] - (saved["amount_kopeks"] or 0) + (amount or 0))
            cursor = self.connection.execute(
                """UPDATE servers SET name=?, hoster=?, website=?, ip_address=?, domain=?, starts_on=?, ends_on=?,
                   version=version+1 WHERE id=? AND version=? AND deleted_at IS NULL""",
                (*[fields[key] for key in FIELDS], server_id, version))
            self.connection.execute("UPDATE payments SET starts_on=? WHERE id=?", (fields["starts_on"], first["id"]))
            self.connection.execute("UPDATE payments SET ends_on=?, amount_kopeks=?, paid_with_bonus=? WHERE id=?",
                                    (fields["ends_on"], amount, paid_with_bonus, last["id"]))
        return cursor.rowcount == 1

    def renew(self, server_id, version, ends_on, amount_kopeks):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            saved = self.get(server_id)
            if not saved or saved["version"] != version:
                return False
            ends_on = date.fromisoformat(ends_on).isoformat()
            if ends_on <= saved["ends_on"]:
                raise ValueError("Новая дата должна быть позже текущей даты окончания.")
            if amount_kopeks is None:
                raise ValueError("Укажите сумму оплаты продления.")
            validate_amount(amount_kopeks)
            validate_amount(saved["total_paid_kopeks"] + amount_kopeks)
            starts_on = (date.fromisoformat(saved["ends_on"]) + timedelta(days=1)).isoformat()
            self.connection.execute("UPDATE servers SET ends_on=?, archive_pending=0, version=version+1 WHERE id=? AND version=?",
                                    (ends_on, server_id, version))
            self.connection.execute("INSERT INTO payments (server_id, starts_on, ends_on, amount_kopeks) VALUES (?, ?, ?, ?)",
                                    (server_id, starts_on, ends_on, amount_kopeks))
        return True

    def schedule_archive(self, server_id, version):
        with self.connection:
            cursor = self.connection.execute("""UPDATE servers SET archive_pending=1, version=version+1
                WHERE id=? AND version=? AND deleted_at IS NULL AND archive_pending=0""", (server_id, version))
        return cursor.rowcount == 1

    def archive_due(self, today):
        with self.connection:
            self.connection.execute("""UPDATE servers SET deleted_at=?, version=version+1
                WHERE archive_pending=1 AND deleted_at IS NULL AND ends_on < ?""",
                (datetime.now(timezone.utc).isoformat(), today.isoformat()))

    def delete(self, server_id, version):
        with self.connection:
            cursor = self.connection.execute("""UPDATE servers SET deleted_at=?, version=version+1
                WHERE id=? AND version=? AND deleted_at IS NULL""",
                (datetime.now(timezone.utc).isoformat(), server_id, version))
        return cursor.rowcount == 1

    def delivered(self, server_id, ends_on, admin_id, day):
        return self.connection.execute(
            "SELECT 1 FROM deliveries WHERE server_id=? AND ends_on=? AND admin_id=? AND day=?",
            (server_id, ends_on, admin_id, day)).fetchone() is not None

    def remember_reminder(self, server_id, ends_on, admin_id, message_id):
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO reminder_messages VALUES (?, ?, ?, ?)",
                                    (server_id, ends_on, admin_id, message_id))

    def outdated_reminders(self):
        return [dict(row) for row in self.connection.execute("""
            SELECT message.* FROM reminder_messages AS message JOIN servers ON servers.id=message.server_id
            WHERE servers.deleted_at IS NOT NULL OR message.ends_on<>servers.ends_on
                OR EXISTS (SELECT 1 FROM reminder_messages AS newer
                    WHERE newer.server_id=message.server_id AND newer.admin_id=message.admin_id
                        AND newer.message_id>message.message_id)
            ORDER BY message.admin_id, message.message_id""")]

    def forget_reminder(self, admin_id, message_id):
        with self.connection:
            self.connection.execute("DELETE FROM reminder_messages WHERE admin_id=? AND message_id=?", (admin_id, message_id))

    def mark_delivered(self, server_id, ends_on, admin_id, day):
        with self.connection:
            self.connection.execute(
                """INSERT OR IGNORE INTO deliveries (server_id, ends_on, admin_id, day)
                   SELECT id, ends_on, ?, ? FROM servers WHERE id=? AND ends_on=? AND deleted_at IS NULL""",
                (admin_id, day, server_id, ends_on))

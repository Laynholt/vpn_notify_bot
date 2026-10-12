import re
from datetime import date, datetime, timezone
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill


MONEY_FORMAT = '#,##0.00 "₽"'
HEADER_FILL = PatternFill("solid", fgColor="243C5A")
EMOJI = re.compile(
    r"[#*0-9]\ufe0f?\u20e3|[©®™]\ufe0f|"
    r"[\U00010000-\U0010ffff\u200d\u20e3\ufe0e\ufe0f"
    r"\u203c\u2049\u2139\u2194-\u2199\u21a9\u21aa"
    r"\u231a\u231b\u2328\u23cf\u23e9-\u23f3\u23f8-\u23fa\u24c2"
    r"\u25aa\u25ab\u25b6\u25c0\u25fb-\u25fe\u2600-\u26ff\u2700-\u27bf"
    r"\u2934\u2935\u2b05-\u2b07\u2b1b\u2b1c\u2b50\u2b55\u3030\u303d\u3297\u3299]"
)


def plain_text(value):
    # BMP-only export keeps Excel's UTF-16 title limit equal to Python's length.
    return EMOJI.sub("", value).strip()


def append_row(sheet, values, *, money_columns=()):
    sheet.append([plain_text(value) if isinstance(value, str) else value for value in values])
    for cell in sheet[sheet.max_row]:
        if isinstance(cell.value, str):
            cell.data_type = "s"
        elif isinstance(cell.value, date):
            cell.number_format = "DD.MM.YYYY"
        if cell.column in money_columns and isinstance(cell.value, (int, float)):
            cell.number_format = MONEY_FORMAT
        cell.alignment = Alignment(vertical="top", wrap_text=True)


def header(sheet, values):
    append_row(sheet, values)
    for cell in sheet[sheet.max_row]:
        cell.fill = HEADER_FILL
        cell.font = Font(bold=True, color="FFFFFF")
    sheet.freeze_panes = f"A{sheet.max_row + 1}"


def summary_sheet(book, name, servers, *, timezone):
    sheet = book.create_sheet(name)
    header(sheet, ["ID", "Сервер", "Хостер", "IP-адрес", "Домен", "Статус", "Оплачен до",
                   "Учтено оплат, ₽", "Периодов без суммы", "Удалён"])
    for server in servers:
        append_row(sheet, [server["id"], server["name"], server["hoster"], server["ip_address"], server["domain"],
                          "Удалён" if server["deleted_at"] else "Активен",
                          date.fromisoformat(server["ends_on"]) if server["ends_on"] else "не указан",
                          server["total_paid_kopeks"] / 100 if server["known_payments"] else "не указано",
                          server["unknown_payments"],
                          datetime.fromisoformat(server["deleted_at"]).astimezone(timezone).date() if server["deleted_at"] else None],
                   money_columns=(8,))
    if not servers:
        append_row(sheet, ["Нет серверов"])
    append_row(sheet, ["Всего учтено, ₽", None, None, None, None, None, None,
                      sum(server["total_paid_kopeks"] for server in servers) / 100], money_columns=(8,))
    for column, width in zip("ABCDEFGHIJ", (10, 32, 25, 28, 30, 16, 20, 24, 23, 20)):
        sheet.column_dimensions[column].width = width
    sheet.auto_filter.ref = f"A1:J{max(1, len(servers) + 1)}"


def build_report(servers, *, single=False, timezone=timezone.utc):
    book = Workbook()
    book.remove(book.active)
    if not single:
        summary_sheet(book, "Сводка", servers, timezone=timezone)
        summary_sheet(book, "Активные", [server for server in servers if not server["deleted_at"]], timezone=timezone)
        summary_sheet(book, "Удалённые", [server for server in servers if server["deleted_at"]], timezone=timezone)
    for server in servers:
        title = re.sub(r"[\\/*?:\[\]]", "_", f"{server['id']} {plain_text(server['name'])}")[:31].strip("'").strip()
        sheet = book.create_sheet(title)
        append_row(sheet, [server["name"]])
        sheet["A1"].font = Font(bold=True, size=16, color="243C5A")
        for label, value in (("Статус", "Удалён" if server["deleted_at"] else "Активен"),
                             ("Хостер", server["hoster"]), ("Сайт", server["website"]),
                             ("IP-адрес", server["ip_address"] or "не указан"),
                             ("Домен", server["domain"] or "не указан"),
                             ("Удалён", datetime.fromisoformat(server["deleted_at"]).astimezone(timezone).date() if server["deleted_at"] else "—")):
            append_row(sheet, [label, value])
        append_row(sheet, [])
        header(sheet, ["Начало периода", "Конец периода", "Оплачено, ₽", "Оплата бонусами"])
        for payment in server["payments"]:
            append_row(sheet, [date.fromisoformat(payment["starts_on"]) if payment["starts_on"] else "не указан",
                              date.fromisoformat(payment["ends_on"]) if payment["ends_on"] else "не указан",
                              payment["amount_kopeks"] / 100 if payment["amount_kopeks"] is not None else "не указано",
                              "Оплачено бонусами" if payment["paid_with_bonus"] else ""],
                       money_columns=(3,))
        append_row(sheet, ["Всего учтено, ₽", server["total_paid_kopeks"] / 100], money_columns=(2,))
        append_row(sheet, ["Периодов без суммы", server["unknown_payments"]])
        if server["unknown_payments"]:
            append_row(sheet, ["Итог неполный: учтены только известные суммы."])
        for column, width in zip("ABCD", (32, 45, 25, 28)):
            sheet.column_dimensions[column].width = width
    if not book.worksheets:
        append_row(book.create_sheet("Статистика"), ["Нет серверов"])
    result = BytesIO()
    book.save(result)
    return result.getvalue()

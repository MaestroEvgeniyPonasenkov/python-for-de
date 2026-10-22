"""Учёт расчётных и сберегательных счетов.

История операций общая для обоих типов. Отличаются только правила
снятия и набор допустимых операций. Номера выдаёт один счётчик.
"""

import math
import re
from datetime import datetime
from itertools import count
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

SUCCESS = "success"
FAIL = "fail"

_KNOWN_OPERATIONS = frozenset({"deposit", "withdraw", "interest"})
_REQUIRED_COLUMNS = (
    "account_number",
    "account_type",
    "date",
    "operation",
    "amount",
    "balance_after",
    "status",
)
_HISTORY_COLUMNS = (
    "операция",
    "сумма",
    "дата",
    "баланс после операции",
    "статус",
    "перевод",
)
_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
)
_TYPE_LABELS = {
    "checking": "расчётный",
    "savings": "сберегательный",
}
# «Имя Фамилия»: оба слова с заглавной и в одной письменности.
_OWNER_PATTERN = re.compile(
    r"^(?:"
    r"[А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+"
    r"|"
    r"[A-Z][a-z]+ [A-Z][a-z]+"
    r")$"
)
_OWNER_ERROR = (
    "Имя владельца должно быть в формате «Имя Фамилия»: "
    "два слова с заглавной буквы, кириллица или латиница"
)
_AMOUNT_TYPE_ERROR = "Сумма операции должна быть положительным числом"
_AMOUNT_SIGN_ERROR = "Сумма операции должна быть положительной"

_transfer_ids = count(1)


def validate_owner(name):
    """Проверить и нормализовать имя владельца."""
    if not isinstance(name, str):
        raise ValueError(_OWNER_ERROR)
    cleaned = " ".join(name.split())
    if _OWNER_PATTERN.fullmatch(cleaned) is None:
        raise ValueError(_OWNER_ERROR)
    return cleaned


def _is_real(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _to_cents(value):
    return int(round(float(value) * 100))


def _require_amount_cents(amount):
    if not _is_real(amount) or not math.isfinite(float(amount)):
        raise ValueError(_AMOUNT_TYPE_ERROR)
    cents = _to_cents(float(amount))
    if cents <= 0:
        raise ValueError(_AMOUNT_SIGN_ERROR)
    return cents


def _require_balance_cents(balance):
    if not _is_real(balance) or not math.isfinite(float(balance)):
        raise ValueError("Начальный баланс должен быть неотрицательным числом")
    if float(balance) < 0:
        raise ValueError("Начальный баланс не может быть отрицательным")
    return _to_cents(float(balance))


def _moment(when):
    if when is None:
        return datetime.now()
    if not isinstance(when, datetime):
        raise ValueError("Дата операции должна быть datetime")
    return when


def _next_transfer_id():
    return f"TR-{next(_transfer_ids):04d}"


class Operation:
    """Одна запись истории операций."""

    def __init__(
        self,
        operation,
        amount,
        timestamp,
        balance_after,
        status,
        transfer_id=None,
    ):
        self.operation = operation
        self.amount = amount
        self.timestamp = timestamp
        self.balance_after = balance_after
        self.status = status
        self.transfer_id = transfer_id


class Account:
    """Общие правила учёта: баланс, история, проверка суммы и имени.

    Номер вида ACC-XXXX выдаётся с 1000 и больше не повторяется.
    Счётчик один на все типы счетов.
    """

    account_type = ""
    allowed_operations = frozenset({"deposit", "withdraw"})
    _next_number = 1000

    def __init__(self, owner, balance=0):
        if type(self) is Account:
            raise TypeError("Создайте CheckingAccount или SavingsAccount")
        self.owner = validate_owner(owner)
        self._balance_cents = _require_balance_cents(balance)
        self.number = self._allocate_number()
        self._history = []

    @classmethod
    def _allocate_number(cls):
        number = f"ACC-{Account._next_number:04d}"
        Account._next_number += 1
        return number

    @property
    def balance(self):
        return self._balance_cents / 100

    @property
    def type_label(self):
        return _TYPE_LABELS[self.account_type]

    def deposit(self, amount, when=None):
        """Пополнить счёт. Некорректная сумма вызывает ValueError."""
        cents = _require_amount_cents(amount)
        moment = _moment(when)
        self._balance_cents += cents
        return self._record("deposit", cents, moment, SUCCESS, None)

    def withdraw(self, amount, when=None):
        """Снять деньги. Отказ записывается в историю со статусом fail."""
        cents = _require_amount_cents(amount)
        moment = _moment(when)
        if self._withdraw_block_reason(cents) is not None:
            return self._record("withdraw", cents, moment, FAIL, None)
        self._balance_cents -= cents
        return self._record("withdraw", cents, moment, SUCCESS, None)

    def _withdraw_block_reason(self, amount_cents):
        if amount_cents > self._balance_cents:
            return "недостаточно средств"
        return None

    def _record(self, operation, amount_cents, when, status, transfer_id):
        stored = Operation(
            operation=operation,
            amount=amount_cents / 100,
            timestamp=when,
            balance_after=self.balance,
            status=status,
            transfer_id=transfer_id,
        )
        self._history.append(stored)
        return stored

    def _apply_loaded(self, parsed):
        """Провести строку файла или вернуть причину отбраковки.

        Колонка balance_after из файла не используется: остаток
        пересчитывается по принятым операциям. Снятие, которое
        нарушает правила счёта, в историю не пишется.
        """
        if parsed.status == SUCCESS and parsed.operation == "withdraw":
            reason = self._withdraw_block_reason(parsed.amount_cents)
            if reason is not None:
                return reason
        if parsed.status == SUCCESS:
            if parsed.operation == "withdraw":
                self._balance_cents -= parsed.amount_cents
            else:
                self._balance_cents += parsed.amount_cents
        self._record(
            parsed.operation,
            parsed.amount_cents,
            parsed.timestamp,
            parsed.status,
            None,
        )
        return None

    def to_dataframe(self):
        """История операций таблицей pandas."""
        rows = [
            {
                "операция": operation.operation,
                "сумма": operation.amount,
                "дата": operation.timestamp,
                "баланс после операции": operation.balance_after,
                "статус": operation.status,
                "перевод": operation.transfer_id or "",
            }
            for operation in self._history
        ]
        return pd.DataFrame(rows, columns=list(_HISTORY_COLUMNS))

    def large_operations(self, count, threshold):
        """Последние N успешных операций с суммой не ниже порога."""
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("N должно быть положительным целым числом")
        if (
            isinstance(threshold, bool)
            or not _is_real(threshold)
            or not math.isfinite(float(threshold))
            or float(threshold) < 0
        ):
            raise ValueError(
                "Порог крупности должен быть неотрицательным числом"
            )
        frame = self.to_dataframe()
        large = frame["сумма"] >= float(threshold)
        successful = frame["статус"] == SUCCESS
        selected = frame[successful & large]
        return selected.tail(count).reset_index(drop=True)

    def plot_balance(self):
        """Линейный график баланса. Точки берутся из истории через pandas."""
        frame = self.to_dataframe().sort_values("дата", kind="stable")
        figure, axes = plt.subplots(figsize=(9, 4))
        if not frame.empty:
            axes.plot(
                frame["дата"],
                frame["баланс после операции"],
                marker="o",
            )
        axes.set_title(f"Изменение баланса счёта {self.number}")
        axes.set_xlabel("Время операции")
        axes.set_ylabel("Баланс после операции")
        axes.grid(True, linestyle="--", alpha=0.4)
        figure.autofmt_xdate()
        figure.tight_layout()
        return figure

    def load_transactions(self, path, source_account_number):
        """Загрузить операции номера source_account_number из CSV или JSON."""
        return TransactionLoader().load(self, path, source_account_number)

    def transfer_to(self, recipient, amount, when=None):
        """Перевести сумму на другой счёт.

        При успехе у обоих счетов появляется запись с общим номером.
        Если списание отклонено, баланс получателя не меняется,
        а отказ остаётся только в истории отправителя.
        """
        if not isinstance(recipient, Account):
            raise TypeError("Перевод возможен только между счетами")
        if recipient is self:
            raise ValueError("Нельзя перевести средства на тот же счёт")
        cents = _require_amount_cents(amount)
        moment = _moment(when)
        transfer_id = _next_transfer_id()
        if self._withdraw_block_reason(cents) is not None:
            return self._record(
                "transfer_out",
                cents,
                moment,
                FAIL,
                transfer_id,
            )
        self._balance_cents -= cents
        outgoing = self._record(
            "transfer_out",
            cents,
            moment,
            SUCCESS,
            transfer_id,
        )
        recipient._receive_transfer(cents, moment, transfer_id)
        return outgoing

    def _receive_transfer(self, amount_cents, when, transfer_id):
        self._balance_cents += amount_cents
        return self._record(
            "transfer_in",
            amount_cents,
            when,
            SUCCESS,
            transfer_id,
        )

    def format_history(self):
        """Текст счёта и истории для чтения в консоли или ноутбуке."""
        header = (
            f"Счёт {self.number} ({self.type_label}), "
            f"владелец: {self.owner}, баланс: {self.balance:.2f}"
        )
        if not self._history:
            return header + "\nИстория пуста."
        lines = [header, "История:"]
        lines.extend(self._format_operation(item) for item in self._history)
        return "\n".join(lines)

    def _format_operation(self, operation):
        transfer = ""
        if operation.transfer_id:
            transfer = f"  {operation.transfer_id}"
        return (
            f"  {operation.timestamp:%Y-%m-%d %H:%M:%S}"
            f"  {operation.operation:<12}"
            f"  {operation.amount:10.2f}"
            f"  {operation.status:<7}"
            f"  баланс {operation.balance_after:10.2f}"
            f"{transfer}"
        )

    def __str__(self):
        return self.format_history()

    def __repr__(self):
        return (
            f"{type(self).__name__}(number={self.number!r}, "
            f"owner={self.owner!r}, balance={self.balance:.2f})"
        )


class CheckingAccount(Account):
    """Расчётный счёт: снятие ограничено только остатком."""

    account_type = "checking"


class SavingsAccount(Account):
    """Сберегательный счёт: проценты и снятие не больше 50% остатка."""

    account_type = "savings"
    allowed_operations = frozenset({"deposit", "withdraw", "interest"})

    def _withdraw_block_reason(self, amount_cents):
        insufficient = super()._withdraw_block_reason(amount_cents)
        if insufficient is not None:
            return insufficient
        if amount_cents * 2 > self._balance_cents:
            return "превышен лимит снятия 50%"
        return None

    def apply_interest(self, rate, when=None):
        """Начислить проценты на остаток. rate — доля: 0.07 это 7%."""
        if not _is_real(rate) or not math.isfinite(float(rate)) or rate <= 0:
            raise ValueError("Ставка должна быть положительным числом")
        moment = _moment(when)
        interest_cents = int(round(self._balance_cents * float(rate)))
        if interest_cents <= 0:
            raise ValueError("Сумма начисления должна быть положительной")
        self._balance_cents += interest_cents
        return self._record("interest", interest_cents, moment, SUCCESS, None)


class ParsedOperation:
    """Строка файла, которая прошла проверку полей."""

    def __init__(self, operation, amount_cents, timestamp, status):
        self.operation = operation
        self.amount_cents = amount_cents
        self.timestamp = timestamp
        self.status = status


class RejectedRow:
    """Строка файла, не попавшая в историю."""

    def __init__(
        self,
        row,
        reason,
        operation,
        amount,
        date,
        status,
        account_type,
    ):
        self.row = row
        self.reason = reason
        self.operation = operation
        self.amount = amount
        self.date = date
        self.status = status
        self.account_type = account_type


class LoadReport:
    """Сколько строк принято и почему остальные отклонены."""

    def __init__(
        self,
        path,
        account_number,
        source_account_number,
        total_rows,
        selected_rows,
        accepted_count,
        rejected_rows,
    ):
        self.path = path
        self.account_number = account_number
        self.source_account_number = source_account_number
        self.total_rows = total_rows
        self.selected_rows = selected_rows
        self.accepted_count = accepted_count
        self.rejected_rows = rejected_rows

    @property
    def rejected_count(self):
        return len(self.rejected_rows)

    def reason_counts(self):
        counts = {}
        for item in self.rejected_rows:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    def summary(self):
        """Сводка отбраковки: причина и число строк."""
        rows = [
            {"причина": reason, "количество": count}
            for reason, count in self.reason_counts()
        ]
        return pd.DataFrame(rows, columns=["причина", "количество"])

    def details(self):
        """Каждая отклонённая строка и причина."""
        rows = [
            {
                "строка": item.row,
                "причина": item.reason,
                "операция": item.operation,
                "сумма": item.amount,
                "дата": item.date,
                "статус": item.status,
                "тип счёта": item.account_type,
            }
            for item in self.rejected_rows
        ]
        return pd.DataFrame(
            rows,
            columns=[
                "строка",
                "причина",
                "операция",
                "сумма",
                "дата",
                "статус",
                "тип счёта",
            ],
        )

    def __str__(self):
        lines = [
            f"Файл: {self.path}",
            f"Счёт: {self.account_number}",
            f"Номер в файле: {self.source_account_number}",
            f"Строк в файле: {self.total_rows}",
            f"Отобрано по номеру: {self.selected_rows}",
            f"Принято: {self.accepted_count}",
            f"Отклонено: {self.rejected_count}",
        ]
        counts = self.reason_counts()
        if not counts:
            lines.append("Отклонённых строк нет.")
            return "\n".join(lines)
        lines.append("Причины отклонения:")
        for reason, total in counts:
            lines.append(f"  {reason}: {total}")
        return "\n".join(lines)


class TransactionLoader:
    """Чтение CSV и JSON с фильтрацией и отчётом об отбраковке."""

    def load(self, account, path, source_account_number):
        source = Path(path)
        suffix = source.suffix.lower()
        if suffix not in {".csv", ".json"}:
            raise ValueError("Поддерживаются только файлы CSV и JSON")
        if (
            not isinstance(source_account_number, str)
            or not source_account_number.strip()
        ):
            raise ValueError("Укажите номер счёта в файле")
        wanted = source_account_number.strip()
        rows = _read_rows(source)
        chosen = [
            (index, row)
            for index, row in enumerate(rows, start=1)
            if row["account_number"] == wanted
        ]
        rejected = []
        accepted = 0
        for index, row in chosen:
            parsed, reason = _inspect(account, row)
            if parsed is None:
                rejected.append(
                    _rejected(index, row, reason or "некорректная строка")
                )
                continue
            policy_reason = account._apply_loaded(parsed)
            if policy_reason is not None:
                rejected.append(_rejected(index, row, policy_reason))
                continue
            accepted += 1
        return LoadReport(
            path=source.name,
            account_number=account.number,
            source_account_number=wanted,
            total_rows=len(rows),
            selected_rows=len(chosen),
            accepted_count=accepted,
            rejected_rows=tuple(rejected),
        )


def _rejected(index, row, reason):
    return RejectedRow(
        row=index,
        reason=reason,
        operation=row["operation"],
        amount=row["amount"],
        date=row["date"],
        status=row["status"],
        account_type=row["account_type"],
    )


def _read_rows(path):
    frame = _read_frame(path)
    frame.columns = [str(name).strip() for name in frame.columns]
    missing = [name for name in _REQUIRED_COLUMNS if name not in frame.columns]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"В файле нет колонок: {joined}")
    records = frame.loc[:, list(_REQUIRED_COLUMNS)].to_dict(orient="records")
    return [
        {key: _cell(record[key]) for key in _REQUIRED_COLUMNS}
        for record in records
    ]


def _read_frame(path):
    if path.suffix.lower() == ".csv":
        return pd.read_csv(
            path,
            dtype=str,
            keep_default_na=False,
            encoding="utf-8",
        )
    return pd.read_json(path, convert_dates=False)


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return str(value).strip()


def _inspect(account, row):
    row_type = row["account_type"].strip().lower()
    if not row_type:
        return None, "пустой тип счёта"
    if row_type != account.account_type:
        return None, "тип счёта не совпадает"

    operation, operation_issue = _parse_operation(
        row["operation"],
        account.allowed_operations,
    )
    if operation_issue is not None:
        return None, operation_issue

    amount_cents, amount_issue = _parse_amount(row["amount"])
    if amount_issue is not None:
        return None, amount_issue

    timestamp, date_issue = _parse_timestamp(row["date"])
    if date_issue is not None:
        return None, date_issue

    status, status_issue = _parse_status(row["status"])
    if status_issue is not None:
        return None, status_issue
    if (
        operation is None
        or amount_cents is None
        or timestamp is None
        or status is None
    ):
        return None, "некорректная строка"
    return (
        ParsedOperation(
            operation=operation,
            amount_cents=amount_cents,
            timestamp=timestamp,
            status=status,
        ),
        None,
    )


def _parse_operation(raw, allowed):
    text = raw.strip().lower()
    if not text:
        return None, "пустой тип операции"
    if text not in _KNOWN_OPERATIONS:
        shown = raw.strip()
        return None, f"опечатка в типе операции: {shown}"
    if text not in allowed:
        return None, f"недопустимый тип операции для счёта: {text}"
    return text, None


def _parse_amount(raw):
    text = raw.strip()
    if not text:
        return None, "пустая сумма"
    try:
        value = float(text)
    except ValueError:
        return None, "некорректная сумма"
    if not math.isfinite(value):
        return None, "некорректная сумма"
    if value < 0:
        return None, "отрицательная сумма"
    cents = _to_cents(value)
    if cents <= 0:
        return None, "неположительная сумма"
    return cents, None


def _parse_timestamp(raw):
    text = raw.strip()
    if not text:
        return None, "пустая дата"
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt), None
        except ValueError:
            continue
    return None, "некорректная дата"


def _parse_status(raw):
    text = raw.strip().lower()
    if not text:
        return None, "пустой статус"
    if text not in {SUCCESS, FAIL}:
        shown = raw.strip()
        return None, f"некорректный статус: {shown}"
    return text, None

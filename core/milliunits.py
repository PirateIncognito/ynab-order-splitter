"""Conversions between dollars and YNAB's "milliunits" (thousandths of a
currency unit -- $123.93 is 123930 milliunits, per the YNAB API docs).
"""


def dollars_to_milliunits(dollars: float) -> int:
    return round(dollars * 1000)


def milliunits_to_dollars(milliunits: int) -> float:
    return milliunits / 1000

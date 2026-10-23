"""Public holidays (no internet needed). The sample list below is Georgia's; edit FIXED for your country.

Source: Labour Code of Georgia, article 20 (non-working holidays): the fixed dates below plus the Orthodox Easter days
(Great Friday, Great Saturday, Easter Sunday and the Monday after Easter). The law does not move a holiday that falls
on a weekend, so neither does this list. Extra days off that the government announces are added by the owner in the
admin panel (table holiday_overrides), and an automatic day can be switched off there too.
"""
from datetime import date, timedelta

FIXED = {
    (1, 1): "New Year's Day",
    (1, 2): "New Year holiday",
    (1, 7): "Orthodox Christmas",
    (1, 19): "Epiphany",
    (3, 3): "Mother's Day",
    (3, 8): "International Women's Day",
    (4, 9): "National Unity Day",
    (5, 9): "Victory Day",
    (5, 12): "St. Andrew's Day",
    (5, 26): "Independence Day",
    (8, 28): "Dormition of the Virgin",
    (10, 14): "Svetitskhovloba",
    (11, 23): "St. George's Day",
}


def orthodox_easter(year):
    """Orthodox Easter Sunday as a Gregorian date (Julian computus plus the 13 day difference, valid 1900-2099)."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month, day = divmod(d + e + 114, 31)
    return date(year, month, day + 1) + timedelta(days=13)


def auto_holidays(year):
    """date -> name for one year: the fixed dates and the four Easter days."""
    out = {date(year, m, d): name for (m, d), name in FIXED.items()}
    easter = orthodox_easter(year)
    out[easter - timedelta(days=2)] = "Good Friday"
    out[easter - timedelta(days=1)] = "Holy Saturday"
    out[easter] = "Orthodox Easter"
    out[easter + timedelta(days=1)] = "Easter Monday"
    return out

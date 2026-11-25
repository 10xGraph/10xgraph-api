"""What a dynamic schedule may be: cron TaskIQ's scheduler can match, and bounds for ``every``.

Schedules run in UTC only. TaskIQ matches cron with pycron, which accepts input it never matches
(``61 * * * *``, ``0 abc * * *``) and raises at match time on other input (``*/0``), so every
field is checked here against what pycron understands: ``*``, ``*/n``, ``a``, ``a-b``,
``a-b/n`` and comma lists of those; day names (``mon``) only in the day-of-week field and
without a step.
"""

# Label on schedules created through POST /v1/tasks: they stop when the task loses http=True.
HTTP_SCHEDULE_LABEL = "tenxgraph_http_schedule"
MIN_EVERY = 10  # seconds: the scheduler re-reads its sources every 10 s
MAX_EVERY = 365 * 86400  # one year; larger values overflow the scheduler's timedelta

_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 6),
)
_DAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_FIELD_COUNT = len(_FIELDS)


def _number(text: str, name: str, low: int, high: int, *, names: bool) -> int:
    if names and text.lower() in _DAY_NAMES:
        return _DAY_NAMES.index(text.lower())
    if not (text.isascii() and text.isdigit()):
        raise ValueError(f"cron {name}: {text!r} is not a number")
    number = int(text)
    if not low <= number <= high:
        raise ValueError(f"cron {name}: {number} is outside {low}-{high}")
    return number


def _check_item(item: str, name: str, low: int, high: int) -> None:
    base, slash, step = item.partition("/")
    if slash and not (step.isascii() and step.isdigit() and 1 <= int(step) <= high):
        raise ValueError(f"cron {name}: step {step!r} must be a number from 1 to {high}")
    if base == "*":
        return
    names = name == "day of week" and not slash  # pycron reads names only without a step
    start, dash, end = base.partition("-")
    first = _number(start, name, low, high, names=names)
    if dash:
        if _number(end, name, low, high, names=names) < first:
            raise ValueError(f"cron {name}: range {base!r} goes backwards")
    elif slash:
        raise ValueError(f"cron {name}: use '*/{step}' or 'a-b/{step}', not {item!r}")


def check_cron(expression: str) -> str:
    """The expression with single spaces (pycron splits on one space); ValueError naming the
    bad field otherwise."""
    fields = expression.split()
    if len(fields) != _FIELD_COUNT:
        raise ValueError("cron needs 5 fields: minute hour day-of-month month day-of-week (UTC)")
    for text, (name, low, high) in zip(fields, _FIELDS, strict=True):
        for item in text.split(","):
            _check_item(item, name, low, high)
    return " ".join(fields)


def check_every(seconds: float) -> None:
    """ValueError when ``every`` is outside ``MIN_EVERY``..``MAX_EVERY`` seconds."""
    if not MIN_EVERY <= seconds <= MAX_EVERY:
        raise ValueError(f"every must be from {MIN_EVERY} to {MAX_EVERY} seconds")

import datetime


def format_relative_time(dt: datetime.datetime) -> str:
    """Formats datetime to 'DD.MM.YYYY HH:MM (X ago)' in Russian."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    
    now = datetime.datetime.utcnow()
    diff = now - dt

    seconds = int(diff.total_seconds())
    if seconds < 0:
        seconds = 0

    if seconds < 60:
        time_ago = "только что"
    elif seconds < 3600:
        minutes = seconds // 60
        time_ago = f"{minutes} мин. назад"
    elif seconds < 86400:
        hours = seconds // 3600
        time_ago = f"{hours} ч. назад"
    else:
        days = seconds // 86400
        time_ago = f"{days} дн. назад"

    date_str = dt.strftime("%d.%m.%Y %H:%M")
    return f"{date_str} ({time_ago})"

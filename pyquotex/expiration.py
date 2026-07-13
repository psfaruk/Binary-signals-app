import calendar
import time

# NOTE: The following expiration helpers were removed as dead code
# (no internal caller in this codebase). Only get_timestamp() remains,
# which is used by pyquotex/_api/history.py (get_candles).
#   - date_to_timestamp
#   - timestamp_to_date
#   - get_timestamp_days_ago
#   - get_expiration_time_quotex
#   - get_next_timeframe
#   - get_expiration_time
#   - get_period_time
#   - get_remaning_time
#   - get_server_timer


def get_timestamp() -> int:
    return calendar.timegm(time.gmtime())

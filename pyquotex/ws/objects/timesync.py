import time

from pyquotex.ws.objects.base import Base

# NOTE: The following TimeSync properties were removed as dead code
# (no external accessor anywhere in the codebase):
#   - server_datetime
#   - expiration_time (getter + setter)
#   - expiration_datetime
#   - expiration_timestamp
# Only ``server_timestamp`` (getter + setter) remains — it is used by
# pyquotex/api.py to sync the server clock.


class TimeSync(Base):
    """Class to manage time synchronization for Quotex WebSocket."""

    def __init__(self) -> None:
        super().__init__()
        self.__name = "timeSync"
        self.__server_timestamp: float = time.time()

    @property
    def server_timestamp(self) -> float:
        """Get the server timestamp.

        :returns: The server timestamp.
        """
        return self.__server_timestamp

    @server_timestamp.setter
    def server_timestamp(self, timestamp: float | int) -> None:
        """Set the server timestamp.

        :param timestamp: New timestamp to set.
        """
        if not isinstance(timestamp, (int, float)):
            raise ValueError("The timestamp must be a number.")
        self.__server_timestamp = float(timestamp)

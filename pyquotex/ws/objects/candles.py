from typing import Any

from pyquotex.ws.objects.base import Base

# NOTE: The standalone ``Candle`` class was removed as dead code — it was
# never imported externally. The ``first_candle`` / ``second_candle`` /
# ``current_candle`` properties of ``Candles`` were also removed because
# they depended on ``Candle`` and were never accessed externally.
# Only ``Candles.candles_data`` (getter + setter) is used in production
# (by pyquotex/_api/history.py).


class Candles(Base):
    """Class for Quotex Candles websocket object."""

    def __init__(self) -> None:
        super(Candles, self).__init__()
        self.__name = "candles"
        self.__candles_data: list[Any] | None = None

    @property
    def candles_data(self) -> list[Any] | None:
        """Property to get candles data.

        :returns: The list of candles data.
        """
        return self.__candles_data

    @candles_data.setter
    def candles_data(self, candles_data: list[Any]) -> None:
        """Method to set candles data."""
        self.__candles_data = candles_data

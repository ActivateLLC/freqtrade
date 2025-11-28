from enum import Enum


class OptionType(str, Enum):
    """
    Enum to distinguish between CALL and PUT options
    """

    CALL = "call"
    PUT = "put"

    def __str__(self):
        return f"{self.name.lower()}"

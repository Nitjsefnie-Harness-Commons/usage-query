"""The switcher's state file."""
import json


def swap_pending(path) -> bool:
    try:
        with open(path, encoding="utf-8") as stream:
            state = json.load(stream)
    except FileNotFoundError:
        return False
    except (OSError, ValueError, RecursionError):
        return True
    return isinstance(state, dict) and "pending" in state

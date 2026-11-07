"""Fail closed before constructing requests or importing upstream code."""

from dataclasses import dataclass
from pathlib import Path
import tomllib


class SafetyError(RuntimeError):
    """A local policy rejected an operation; never include request data."""


@dataclass(frozen=True)
class SafetyPolicy:
    mode: str = "offline"
    live_writes_enabled: bool = False
    automatic_retry: bool = False
    browser_fallback: bool = False
    schedule_enabled: bool = False
    daemon_enabled: bool = False

    @classmethod
    def load(cls, path: Path) -> "SafetyPolicy":
        try:
            with path.open("rb") as stream:
                value = tomllib.load(stream)
        except (OSError, ValueError):
            raise SafetyError("CONFIG_INVALID: cannot read safety configuration") from None
        expected = {
            "safety": {"mode": "offline", "live_writes_enabled": False,
                       "automatic_retry": False, "browser_fallback": False},
            "schedule": {"enabled": False},
            "daemon": {"enabled": False},
        }
        # Exact types reject strings such as 'false' and integers such as 0.
        for section, fields in expected.items():
            actual = value.get(section)
            if not isinstance(actual, dict):
                raise SafetyError("CONFIG_UNSAFE: required safety section missing")
            for key, required in fields.items():
                item = actual.get(key)
                if type(item) is not type(required) or item != required:
                    raise SafetyError("CONFIG_UNSAFE: offline defaults and disabled automation are required")
        if set(value) != set(expected) or any(
            set(value[section]) != set(fields) for section, fields in expected.items()
        ):
            raise SafetyError("CONFIG_UNSAFE: unknown configuration option")
        return cls()

    def require_write(self, *, live: bool, confirm: bool, interactive_confirmed: bool = False) -> None:
        if not (live and confirm):
            raise SafetyError("WRITE_DENIED: both --live and --confirm are required")
        if not interactive_confirmed:
            raise SafetyError("INTERACTIVE_CONFIRMATION_REQUIRED: confirm the exact book and budget locally")
        from paperread.wereadgear_adapter import require_live_flags
        require_live_flags(live=live, confirm=confirm)

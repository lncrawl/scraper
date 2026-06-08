from typing import Any


class RequestHeaders(dict):
    """Case-insensitive dict for HTTP request headers (keys normalised to Title-Case)."""

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key.title(), value)

    def __getitem__(self, key: str) -> Any:
        return super().__getitem__(key.title())

    def __contains__(self, key: object) -> bool:
        return super().__contains__(key.title() if isinstance(key, str) else key)

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        return super().get(key.title(), default)

    def setdefault(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        return super().setdefault(key.title(), default)

    def pop(self, key: str, *args: Any) -> Any:  # type: ignore[override]
        return super().pop(key.title(), *args)

    def update(self, other: Any = None, **kwargs: Any) -> None:  # type: ignore[override]
        if other:
            for k, v in other.items() if hasattr(other, "items") else other:
                self[k] = v
        for k, v in kwargs.items():
            self[k] = v

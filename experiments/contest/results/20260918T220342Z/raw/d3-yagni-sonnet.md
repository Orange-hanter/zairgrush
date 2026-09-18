```python
def notify(user: dict, message: str) -> str:
    channel = user["channel"]
    address = user["address"]
    if channel == "email":
        return f"email:{address}:{message}"
    elif channel == "sms":
        return f"sms:{address}:{message}"
    else:
        raise ValueError(f"Unknown channel: {channel}")


def notify_all(users: list[dict], message: str) -> list[str]:
    return [notify(user, message) for user in users]
```
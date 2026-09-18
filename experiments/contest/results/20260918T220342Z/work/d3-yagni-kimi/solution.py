def notify(user: dict, message: str) -> str:
    channel = user.get("channel")
    address = user.get("address")
    if channel == "email":
        return f"email:{address}:{message}"
    if channel == "sms":
        return f"sms:{address}:{message}"
    raise ValueError(f"Unknown channel: {channel}")


def notify_all(users: list[dict], message: str) -> list[str]:
    return [notify(user, message) for user in users]

# D2 — Распознавание паттернов и антипаттернов

Ниже три фрагмента кода. Для каждого назови применённые паттерны и антипаттерны.

## Словарь (используй ТОЛЬКО эти метки)

- patterns: `singleton`, `factory`, `observer`, `adapter`, `decorator`, `strategy`, `object-pool`, `registry`, `builder`, `facade`
- antipatterns: `god-object`, `singleton-abuse`, `stringly-typed`, `copy-paste`, `leaky-abstraction`, `premature-optimization`, `magic-numbers`, `tight-coupling`

Метку ставь, только если она действительно применена в коде. В коде есть ловушки: то, что ПОХОЖЕ на паттерн, им не является.

## Snippet s1

```python
class EventBus:
    def __init__(self):
        self._subs = {}

    def on(self, event, handler):
        self._subs.setdefault(event, []).append(handler)

    def emit(self, event, *args):
        for h in self._subs.get(event, []):
            h(*args)

class App:
    """Центральный объект приложения."""
    def __init__(self):
        self.bus = EventBus()
        self.users = {}
        self.orders = {}
        self.payments = {}
        self.sessions = {}

    def create_user(self, name): ...
    def ban_user(self, uid): ...
    def place_order(self, uid, items): ...
    def cancel_order(self, oid): ...
    def charge(self, oid): ...
    def refund(self, oid): ...
    def login(self, uid): ...
    def logout(self, uid): ...
    def render_admin_page(self): ...
    def send_digest(self): ...
```

## Snippet s2

```python
def discount_for(customer_type, price):
    if customer_type == "vip":
        return price * 0.8
    elif customer_type == "regular":
        return price * 0.95
    elif customer_type == "guest":
        return price
    return price

def shipping_for(customer_type, weight):
    if customer_type == "vip":
        return 0.0
    elif customer_type == "regular":
        return weight * 1.5
    elif customer_type == "guest":
        return weight * 2.0
    return weight * 2.0

def notify_template(customer_type):
    if customer_type == "vip":
        return "Dear VIP, ..."
    elif customer_type == "regular":
        return "Hello, ..."
    elif customer_type == "guest":
        return "Hi guest, ..."
    return "Hi, ..."
```

## Snippet s3

```python
import threading

class ConnectionPool:
    def __init__(self, factory, size=4):
        self._factory = factory
        self._free = [factory() for _ in range(size)]
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            return self._free.pop() if self._free else self._factory()

    def release(self, conn):
        with self._lock:
            self._free.append(conn)

class ReportService:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def daily_report(self):
        conn = self._pool.acquire()
        try:
            cur = conn.cursor()
            cur.execute("SELECT ...")
            rows = cur.fetchall()
            return self._format(rows)
        finally:
            self._pool.release(conn)

    def _format(self, rows):
        return "\n".join(str(r) for r in rows)
```

## Формат ответа

Ровно один блок ```json:

```json
{"s1": {"patterns": [...], "antipatterns": [...]},
 "s2": {"patterns": [...], "antipatterns": [...]},
 "s3": {"patterns": [...], "antipatterns": [...]}}
```

Пустой список допустим. Без пояснений до и после.

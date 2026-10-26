"""集中 import 所有 ORM models，確保 Base.metadata 完整。

alembic env 與測試 conftest 只引用本模組；新增資料表時在此補一行。
"""
from uep.context import models as _context  # noqa: F401
from uep.events import models as _events  # noqa: F401
from uep.output import models as _output  # noqa: F401
from uep.presence import models as _presence  # noqa: F401
from uep.state import models as _state  # noqa: F401
from uep.stop import models as _stop  # noqa: F401
from uep.tasks import models as _tasks  # noqa: F401

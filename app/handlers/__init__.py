from aiogram import Router

public_router = Router(name="public")
admin_router = Router(name="admin")

from . import admin as _admin  # noqa: F401
from . import subscriptions as _subscriptions  # noqa: F401
from . import participants as _participants  # noqa: F401
from . import reminders as _reminders  # noqa: F401
from . import members as _members  # noqa: F401
from . import topups as _topups  # noqa: F401

__all__ = ["public_router", "admin_router"]

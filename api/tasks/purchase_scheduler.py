"""Compatibility shim: the executor was split on 2026-09-22 (audit Batch B3).

Everything that used to live here is re-exported from
api/services/exchange_gateway.py, api/services/purchase_executor.py and
api/tasks/scheduler.py. Existing imports keep working, and so do monkeypatches:
setting an attribute on this module forwards the assignment to every module
that binds that name, so a patch of e.g. ``_client_for`` or
``execute_due_purchases`` reaches the code that calls it.
"""
import sys
import types

from api.services import exchange_gateway as _gateway
from api.services import purchase_executor as _executor
from api.tasks import scheduler as _scheduler

_MODULES = (_gateway, _executor, _scheduler)

for _m in _MODULES:
    globals().update({_k: _v for _k, _v in vars(_m).items() if not _k.startswith("__")})


class _ForwardingModule(types.ModuleType):
    """Attribute writes propagate to the owning module(s) so patches take effect."""

    def __setattr__(self, name, value):
        for m in _MODULES:
            if name in vars(m):
                setattr(m, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        for m in _MODULES:
            if name in vars(m):
                delattr(m, name)
        super().__delattr__(name)


sys.modules[__name__].__class__ = _ForwardingModule

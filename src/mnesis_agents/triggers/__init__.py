"""Trigger interfaces + reference implementations.

Event triggers (a.k.a. source connectors) emit normalized ``InboundEvent``s; the
``SourceConnector`` base is THE pattern every inbound source implements (see
``connector.py``). Schedule triggers fire on a cadence.
"""
from .connector import ConnectorError, ProcessedStore, SourceConnector
from .events import EventTrigger, InboundEvent, InMemoryEventTrigger
from .schedule import AsyncIntervalScheduler, Schedule, ScheduleTrigger

__all__ = [
    "InboundEvent",
    "EventTrigger",
    "InMemoryEventTrigger",
    "SourceConnector",
    "ProcessedStore",
    "ConnectorError",
    "Schedule",
    "ScheduleTrigger",
    "AsyncIntervalScheduler",
]

"""Concrete source connectors (instances of the W1 SourceConnector pattern).

Each connector turns an external feed into normalized ``InboundEvent``s and does
*only* detection + normalization — never a Mnesis or LLM call (that is the
WritingAgent downstream). The notes inbox is the first, cleanest instance.
"""
from .notes import NotesInboxConnector

__all__ = ["NotesInboxConnector"]

"""Agent Task Watcher (ATW) V1.0.

A zero-LLM local supervisor for AI-handoff compute tasks.  ATW only observes,
persists and notifies; it never starts, stops or retries a compute process.
"""

__version__ = "1.0.0"
PROTOCOL_TASK = "atw.task.v1"
PROTOCOL_RECEIPT = "atw.receipt.v1"

"""Outcome-first agent loop controller.

Talks to the outside world (the simulator, or production) only over HTTP via
`client.SimClient`. Never imports `sim`. All state lives in one SQLite file
(`store.Store`) so the process can be killed at any point and resumed.
"""
__version__ = "1.0.0"

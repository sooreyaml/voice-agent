"""Third-party integration framework and the first calendar connector.

An organization connects one provider per integration slot (currently only a
calendar). Provider credentials are encrypted at rest; the call runtime asks
``service.load_calendar_provider`` for a ready-to-use client and exposes the
matching voice tools only when a connection exists.
"""

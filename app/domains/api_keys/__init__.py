"""Scoped API keys for the public REST API.

Keys authenticate the same ``/api/v1`` surface as the dashboard cookie, but
carry an explicit scope set instead of a membership role. ``dependencies`` holds
``require_org_access`` which accepts either a bearer key or a session cookie.
"""

"""Admin services: seed/import logic invoked from app/admin/seed_view.py.

These modules replace the previous CLI seed scripts in `scripts/` and contain
only the parsing/DB-write logic — no argparse, no __main__ entrypoint. The
admin UI is the sole caller.
"""

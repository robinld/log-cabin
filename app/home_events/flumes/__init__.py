"""Flumes: one module per home system, each carrying its messages into the log table.

A flume connects to one system (listening, holding a connection, or polling),
and appends every message it gets to `log`, as received, with a few columns
copied out for querying. Interpretation belongs in the views in db.py, not here.
Each runs as its own service: python -m home_events.flumes.<name>
"""

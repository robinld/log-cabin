"""Flumes: one module per home system, each carrying its messages into the log table.

A flume connects to one system (listening, holding a connection, or polling),
and appends every message it gets to `log`, as received, with a few columns
copied out for querying. Its parse() says what one message means, stored
alongside it as `parsed` (see home_events/parsing.py); anything that compares
messages over time (a zone opening, a state changing) belongs in the views in
db.py or the page.
Each runs as its own service: python -m home_events.flumes.<name>
"""

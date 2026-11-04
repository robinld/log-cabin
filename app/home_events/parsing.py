"""Each flume's parse(): what one stored message means, as a dict.

A flume module may define

    parse(event_type, event_name, body) -> dict or None
    PARSE_VERSION = 1

where body is the message as stored (text: JSON, or a raw line). Rows are
parsed as they are stored, into log.parsed, with the version in log.parser.
Raise PARSE_VERSION whenever parse() changes: install.sh runs
home_events.reparse on every deploy, which recomputes rows parsed by an older
version, so history picks up the change.

parse() sees one message. Anything that compares it with earlier ones (a zone
opening, a plug switching off) belongs in a view or the page.

Field names are snake_case, with units where there are any (temperature_c,
signal_dbm), and only what's worth reading or querying, not a copy of body.
"""

import importlib
import json

_parsers = {}


def parser(source):
    """(parse, version) for a source, or (None, None) if its flume has no parse()."""
    if source not in _parsers:
        name = f"home_events.flumes.{source}"
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name != name:
                raise  # the flume exists but something it needs doesn't
            module = None
        fn = getattr(module, "parse", None)
        _parsers[source] = (fn, getattr(module, "PARSE_VERSION", 1)) if fn else (None, None)
    return _parsers[source]


def parse(source, event_type, event_name, body):
    """-> (parsed as JSON text or None, the parser's version or None). Raises
    whatever parse() raises."""
    fn, version = parser(source)
    if fn is None:
        return None, None
    out = fn(event_type, event_name, body)
    # allow_nan=False: SQLite's JSON functions reject NaN.
    return (None if out is None else json.dumps(out, separators=(",", ":"), allow_nan=False)), version

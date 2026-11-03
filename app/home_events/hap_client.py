"""A minimal HomeKit (HAP) controller over IP: read and subscribe, never set.

It opens a session with a pairing that already exists (pair-verify), then
reads the accessory database and subscribes to events, as a Home hub does.
There is no pair-setup and no write: the only PUT it sends turns events on.

Protocol: Apple's HomeKit Accessory Protocol Specification (non-commercial
version), pair verify (5.7) and the encrypted session after it (6.5), checked
against HAP-NodeJS and HAP-python.
"""

import asyncio
import json
import struct
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# TLV8 tags and the pair-verify states.
T_IDENTIFIER, T_PUBLIC_KEY, T_ENCRYPTED, T_STATE, T_ERROR, T_SIGNATURE = 1, 3, 5, 6, 7, 10
BLOCK = 1024  # the most plaintext in one encrypted frame


class HapError(Exception):
    pass


@dataclass
class Pairing:
    controller_id: str  # our identifier, as the accessory has it in its pairings
    controller_key: ed25519.Ed25519PrivateKey
    accessory_id: str  # the accessory's identifier, e.g. "0E:12:34:56:78:9A"
    accessory_ltpk: bytes  # its long-term public key


@dataclass
class Message:
    event: bool  # an EVENT/1.0 notification, not a response
    status: int
    body: bytes


def tlv_encode(*items):
    """(tag, value) pairs -> TLV8; values over 255 bytes are split into fragments."""
    out = bytearray()
    for tag, value in items:
        for i in range(0, max(len(value), 1), 255):
            chunk = value[i:i + 255]
            out += bytes([tag, len(chunk)]) + chunk
    return bytes(out)


def tlv_decode(data):
    out, i = {}, 0
    while i + 2 <= len(data):
        tag, n = data[i], data[i + 1]
        out[tag] = out.get(tag, b"") + data[i + 2:i + 2 + n]
        i += 2 + n
    return out


def hkdf(key, salt, info):
    return HKDF(algorithm=hashes.SHA512(), length=32, salt=salt, info=info).derive(key)


def raw(public_key):
    return public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def nonce(n):
    """A ChaCha20-Poly1305 nonce: 4 zero bytes, then a counter or label of 8."""
    return b"\0\0\0\0" + (n if isinstance(n, bytes) else struct.pack("<Q", n))


def parse_http(buf):
    """One whole HTTP-style message from the front of buf -> (Message, rest), or
    None if it hasn't all arrived. HAP sends a Content-Length or chunks."""
    end = buf.find(b"\r\n\r\n")
    if end < 0:
        return None
    lines = bytes(buf[:end]).decode("latin-1").split("\r\n")
    proto, status = lines[0].split(" ", 2)[:2]
    headers = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
    rest = bytes(buf[end + 4:])
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body, i = b"", 0
        while True:
            j = rest.find(b"\r\n", i)
            if j < 0:
                return None
            size = int(rest[i:j].split(b";")[0], 16)
            if len(rest) < j + 2 + size + 2:
                return None
            body += rest[j + 2:j + 2 + size]
            i = j + 2 + size + 2
            if size == 0:
                return Message(proto.startswith("EVENT"), int(status), body), rest[i:]
    n = int(headers.get("content-length", 0))
    if len(rest) < n:
        return None
    return Message(proto.startswith("EVENT"), int(status), rest[:n]), rest[n:]


async def _plain_post(reader, writer, host, path, body):
    writer.write(f"POST {path} HTTP/1.1\r\nHost: {host}\r\n"
                 f"Content-Type: application/pairing+tlv8\r\nContent-Length: {len(body)}\r\n\r\n"
                 .encode() + body)
    await writer.drain()
    buf = b""
    while (m := parse_http(buf)) is None:
        data = await reader.read(4096)
        if not data:
            raise HapError(f"connection closed during {path}")
        buf += data
    if m[0].status != 200:
        raise HapError(f"{path}: HTTP {m[0].status}")
    return m[0].body


async def pair_verify(reader, writer, host, pairing):
    """Run pair verify (M1-M4) and return the session's shared secret."""
    ours = x25519.X25519PrivateKey.generate()
    our_pub = raw(ours.public_key())
    m2 = tlv_decode(await _plain_post(reader, writer, host, "/pair-verify",
                                      tlv_encode((T_STATE, b"\x01"), (T_PUBLIC_KEY, our_pub))))
    if T_ERROR in m2:
        raise HapError(f"pair verify refused at M2 (error {m2[T_ERROR][0]})")
    their_pub = m2[T_PUBLIC_KEY]
    shared = ours.exchange(x25519.X25519PublicKey.from_public_bytes(their_pub))
    aead = ChaCha20Poly1305(hkdf(shared, b"Pair-Verify-Encrypt-Salt", b"Pair-Verify-Encrypt-Info"))
    try:
        sub = tlv_decode(aead.decrypt(nonce(b"PV-Msg02"), m2[T_ENCRYPTED], None))
    except InvalidTag:
        raise HapError("pair verify: couldn't decrypt the accessory's proof") from None
    their_id = sub.get(T_IDENTIFIER, b"")
    if their_id.decode(errors="replace") != pairing.accessory_id:
        raise HapError(f"pair verify: accessory is {their_id!r}, expected {pairing.accessory_id}")
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(pairing.accessory_ltpk).verify(
            sub.get(T_SIGNATURE, b""), their_pub + their_id + our_pub)
    except InvalidSignature:
        raise HapError("pair verify: the accessory's signature doesn't match its key") from None
    our_id = pairing.controller_id.encode()
    proof = pairing.controller_key.sign(our_pub + our_id + their_pub)
    m3 = aead.encrypt(nonce(b"PV-Msg03"), tlv_encode((T_IDENTIFIER, our_id), (T_SIGNATURE, proof)), None)
    m4 = tlv_decode(await _plain_post(reader, writer, host, "/pair-verify",
                                      tlv_encode((T_STATE, b"\x03"), (T_ENCRYPTED, m3))))
    if T_ERROR in m4:
        raise HapError(f"pair verify refused at M4 (error {m4[T_ERROR][0]}): is this controller paired?")
    return shared


class Session:
    """An encrypted session. One reader task splits what arrives into responses
    and events; requests go one at a time."""

    def __init__(self, reader, writer, host, shared):
        self.reader, self.writer, self.host = reader, writer, host
        self._read = ChaCha20Poly1305(hkdf(shared, b"Control-Salt", b"Control-Read-Encryption-Key"))
        self._write = ChaCha20Poly1305(hkdf(shared, b"Control-Salt", b"Control-Write-Encryption-Key"))
        self._in = self._out = 0
        self._responses, self._events = asyncio.Queue(), asyncio.Queue()
        self._lock = asyncio.Lock()
        self._task = asyncio.create_task(self._read_loop())

    @classmethod
    async def open(cls, host, port, pairing, timeout=15):
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        try:
            shared = await asyncio.wait_for(pair_verify(reader, writer, f"{host}:{port}", pairing), timeout)
        except BaseException:
            writer.close()
            raise
        return cls(reader, writer, f"{host}:{port}", shared)

    async def _read_loop(self):
        buf = b""
        try:
            while True:
                head = await self.reader.readexactly(2)
                frame = await self.reader.readexactly(struct.unpack("<H", head)[0] + 16)
                try:
                    buf += self._read.decrypt(nonce(self._in), frame, head)
                except InvalidTag:
                    raise HapError("couldn't decrypt a frame from the accessory") from None
                self._in += 1
                while (m := parse_http(buf)) is not None:
                    msg, buf = m
                    (self._events if msg.event else self._responses).put_nowait(msg)
        except (OSError, asyncio.IncompleteReadError, HapError, ValueError) as e:
            closed = e if isinstance(e, HapError) else HapError(f"connection closed: {e or type(e).__name__}")
            self._responses.put_nowait(closed)
            self._events.put_nowait(closed)

    def _send(self, data):
        for i in range(0, len(data), BLOCK):
            block = data[i:i + BLOCK]
            head = struct.pack("<H", len(block))
            self.writer.write(head + self._write.encrypt(nonce(self._out), block, head))
            self._out += 1

    async def request(self, method, path, body=None, timeout=30):
        """Send a request and return its JSON body (None if empty)."""
        data = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
        head = f"{method} {path} HTTP/1.1\r\nHost: {self.host}\r\n"
        if body is not None:
            head += "Content-Type: application/hap+json\r\n"
        async with self._lock:
            self._send(f"{head}Content-Length: {len(data)}\r\n\r\n".encode() + data)
            await self.writer.drain()
            msg = await asyncio.wait_for(self._responses.get(), timeout)
        if isinstance(msg, HapError):
            raise msg
        if msg.status >= 300 and msg.status != 207:  # 207: some of a batch failed; the body says which
            raise HapError(f"{method} {path}: HTTP {msg.status} {msg.body[:200]!r}")
        return json.loads(msg.body) if msg.body else None

    async def accessories(self):
        return await self.request("GET", "/accessories")

    async def subscribe(self, ids):
        """Turn on events for (aid, iid) pairs. Returns the per-characteristic
        statuses if any failed, else None."""
        return await self.request("PUT", "/characteristics", {
            "characteristics": [{"aid": a, "iid": i, "ev": True} for a, i in ids]})

    async def next_event(self, timeout=None):
        """The next event's JSON body, or TimeoutError after timeout seconds."""
        msg = await asyncio.wait_for(self._events.get(), timeout)
        if isinstance(msg, HapError):
            self._events.put_nowait(msg)  # every later call fails the same way
            raise msg
        return json.loads(msg.body)

    async def close(self):
        self._task.cancel()
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except OSError:
            pass

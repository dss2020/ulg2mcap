"""Minimal ULog file writer for building synthetic test fixtures.

Implements just enough of the ULog spec (https://docs.px4.io/main/en/dev_log/ulog_file_format.html)
for pyulog to parse: header, flag bits, formats, info/parameters, subscriptions, data and
logged-string messages. Data messages are written in the order they were added, so callers
can deliberately produce out-of-order timestamps.
"""

import struct
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple, Union

_HEADER_MAGIC = b"ULog\x01\x12\x35"
_TYPE_CODES = {
    "int8_t": "b",
    "uint8_t": "B",
    "int16_t": "h",
    "uint16_t": "H",
    "int32_t": "i",
    "uint32_t": "I",
    "int64_t": "q",
    "uint64_t": "Q",
    "float": "f",
    "double": "d",
    "bool": "?",
}


def _message(msg_type: bytes, payload: bytes) -> bytes:
    return struct.pack("<HB", len(payload), msg_type[0]) + payload


def _split_type(field_type: str) -> Tuple[str, int]:
    """'float[3]' -> ('float', 3); 'uint64_t' -> ('uint64_t', 1)."""
    if field_type.endswith("]"):
        base, count = field_type[:-1].split("[")
        return base, int(count)
    return field_type, 1


class ULogBuilder:
    def __init__(self, start_timestamp_us: int = 0):
        self.start_timestamp_us = start_timestamp_us
        self._formats: Dict[str, List[Tuple[str, str]]] = {}
        self._info: List[Tuple[str, Union[str, int]]] = []
        self._params: List[Tuple[str, Union[int, float]]] = []
        self._subscriptions: List[Tuple[str, int]] = []
        self._body: List[Tuple[str, Any]] = []

    def add_format(self, name: str, fields: Sequence[Tuple[str, str]]) -> "ULogBuilder":
        """fields: [(type, name)], e.g. [('uint64_t', 'timestamp'), ('float[3]', 'q')]."""
        self._formats[name] = list(fields)
        return self

    def add_info(self, key: str, value: Union[str, int]) -> "ULogBuilder":
        self._info.append((key, value))
        return self

    def add_param(self, key: str, value: Union[int, float]) -> "ULogBuilder":
        self._params.append((key, value))
        return self

    def subscribe(self, name: str, multi_id: int = 0) -> int:
        self._subscriptions.append((name, multi_id))
        return len(self._subscriptions) - 1

    def add_data(self, msg_id: int, values: Dict[str, Any]) -> "ULogBuilder":
        self._body.append(("data", (msg_id, values)))
        return self

    def add_log(self, timestamp_us: int, message: str, level: int = 6) -> "ULogBuilder":
        self._body.append(("log", (timestamp_us, message, level)))
        return self

    def _encode_data(self, msg_id: int, values: Dict[str, Any]) -> bytes:
        name, _ = self._subscriptions[msg_id]
        out = bytearray(struct.pack("<H", msg_id))
        for field_type, field_name in self._formats[name]:
            base, count = _split_type(field_type)
            code = _TYPE_CODES[base]
            value = values[field_name]
            items = value if count > 1 else [value]
            out += struct.pack(f"<{count}{code}", *items)
        return bytes(out)

    def build(self) -> bytes:
        out = bytearray(_HEADER_MAGIC + b"\x01" + struct.pack("<Q", self.start_timestamp_us))
        out += _message(b"B", bytes(16) + struct.pack("<3Q", 0, 0, 0))

        for name, fields in self._formats.items():
            spec = name + ":" + "".join(f"{t} {n};" for t, n in fields)
            out += _message(b"F", spec.encode())

        for key, value in self._info:
            if isinstance(value, str):
                encoded = value.encode()
                full_key = f"char[{len(encoded)}] {key}".encode()
            else:
                encoded = struct.pack("<i", value)
                full_key = f"int32_t {key}".encode()
            out += _message(b"I", bytes([len(full_key)]) + full_key + encoded)

        for key, value in self._params:
            if isinstance(value, float):
                full_key, encoded = f"float {key}".encode(), struct.pack("<f", value)
            else:
                full_key, encoded = f"int32_t {key}".encode(), struct.pack("<i", value)
            out += _message(b"P", bytes([len(full_key)]) + full_key + encoded)

        for msg_id, (name, multi_id) in enumerate(self._subscriptions):
            out += _message(b"A", struct.pack("<BH", multi_id, msg_id) + name.encode())

        for kind, item in self._body:
            if kind == "data":
                out += _message(b"D", self._encode_data(*item))
            else:
                timestamp_us, text, level = item
                out += _message(b"L", struct.pack("<BQ", level, timestamp_us) + text.encode())

        return bytes(out)

    def write(self, path: Union[str, Path]) -> Path:
        path = Path(path)
        path.write_bytes(self.build())
        return path

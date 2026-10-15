"""ULog to MCAP conversion module."""

import heapq
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Set, Tuple, Union
import numpy as np

from mcap.writer import CompressionType, Writer
from pyulog import ULog

from ulg2mcap.schemas import LOG_TOPIC_SCHEMA, generate_json_schema, parse_grouped_fields

# (timestamp_ns, channel_id, payload_bytes)
_Message = Tuple[int, int, bytes]

# Metadata record names the converter writes itself.
RESERVED_METADATA_NAMES = frozenset({"ulog_header", "ulog_params"})


class ULog2MCAPConverter:
    """Converter class for transforming ULog flight log files into MCAP container files."""

    COMPRESSION_MAP: Dict[str, CompressionType] = {
        "zstd": CompressionType.ZSTD,
        "lz4": CompressionType.LZ4,
        "none": CompressionType.NONE,
    }

    def __init__(
        self,
        include_topics: Optional[List[str]] = None,
        exclude_topics: Optional[List[str]] = None,
        compression: str = "zstd",
        time_offset_ns: int = 0,
        start_us: Optional[int] = None,
        end_us: Optional[int] = None,
        carry_state: bool = False,
    ):
        """Initialize converter options.

        Args:
            include_topics: List of topic names to include (if set, only these are converted).
            exclude_topics: List of topic names to exclude.
            compression: Compression algorithm ('zstd', 'lz4', or 'none').
            time_offset_ns: Offset added to every MCAP log_time/publish_time, e.g. to shift
                ULog boot-relative time onto the UTC epoch. The `timestamp` fields inside
                message payloads are left untouched (boot-relative).
            start_us: Only write messages logged at or after this ULog timestamp (us,
                before time_offset_ns). None: from the start of the log.
            end_us: Only write messages logged at or before this ULog timestamp (us).
                None: to the end of the log.
            carry_state: With start_us, also write each topic's latest message from before
                start_us, stamped at start_us, so topics logged only on change still have
                their current value at the start of the range. The payload (including its
                own `timestamp` field) is unchanged. Text log messages are never carried.
        """
        self.include_topics: Optional[Set[str]] = (
            set(include_topics) if include_topics else None
        )
        self.exclude_topics: Set[str] = set(exclude_topics) if exclude_topics else set()

        compression_lower = compression.lower()
        if compression_lower not in self.COMPRESSION_MAP:
            raise ValueError(
                f"Unsupported compression type '{compression}'. Choose from: {list(self.COMPRESSION_MAP.keys())}"
            )
        self.compression_type = self.COMPRESSION_MAP[compression_lower]
        self.time_offset_ns = int(time_offset_ns)
        if start_us is not None and end_us is not None and start_us > end_us:
            raise ValueError(f"start_us ({start_us}) is after end_us ({end_us})")
        self.start_us = None if start_us is None else int(start_us)
        self.end_us = None if end_us is None else int(end_us)
        self.carry_state = bool(carry_state)

    def _in_range(self, timestamp_us: int) -> bool:
        return (self.start_us is None or timestamp_us >= self.start_us) and (
            self.end_us is None or timestamp_us <= self.end_us
        )

    def _selected(self, timestamps: np.ndarray) -> List[Tuple[int, int]]:
        """(index, write timestamp in us) of one topic's messages to write, in time order
        (stable for ties): those in range, preceded by the carried state if enabled."""
        order = np.argsort(timestamps, kind="stable")
        ordered = timestamps[order].astype(np.int64)
        lo = 0 if self.start_us is None else int(np.searchsorted(ordered, self.start_us, "left"))
        hi = len(ordered) if self.end_us is None else int(np.searchsorted(ordered, self.end_us, "right"))
        selected = [(int(order[k]), int(ordered[k])) for k in range(lo, hi)]
        if self.carry_state and self.start_us is not None and lo > 0:
            selected.insert(0, (int(order[lo - 1]), self.start_us))
        return selected

    def _should_include_topic(self, topic_name: str) -> bool:
        """Check if a topic should be included based on filter settings."""
        if self.exclude_topics and topic_name in self.exclude_topics:
            return False
        if self.include_topics is not None:
            return topic_name in self.include_topics
        return True

    @staticmethod
    def _format_value(val: Any) -> Any:
        """Format scalar numpy value for JSON serialization."""
        if isinstance(val, (np.floating, float)):
            return None if (math.isnan(val) or math.isinf(val)) else float(val)
        if isinstance(val, (np.integer, int)):
            return int(val)
        if isinstance(val, (np.bool_, bool)):
            return bool(val)
        return str(val)

    def _dataset_messages(self, d: Any, channel_id: int) -> Iterator[_Message]:
        """Lazily yield one topic's messages in timestamp order (stable for ties)."""
        _, access_list = parse_grouped_fields(d.data)

        for i, timestamp_us in self._selected(d.data["timestamp"]):
            timestamp_ns = timestamp_us * 1000  # us -> ns

            sample_dict = {}
            for field_name, is_array, arr_target in access_list:
                if is_array:
                    sample_dict[field_name] = [self._format_value(arr[i]) for arr in arr_target]
                else:
                    sample_dict[field_name] = self._format_value(arr_target[i])

            yield timestamp_ns, channel_id, json.dumps(sample_dict).encode("utf-8")

    def _logged_text_messages(self, ulog: ULog, channel_id: int) -> Iterator[_Message]:
        """Lazily yield ULog text messages in range, in timestamp order (stable for ties)."""
        for msg in sorted(ulog.logged_messages, key=lambda m: int(m.timestamp)):
            if not self._in_range(int(msg.timestamp)):
                continue
            timestamp_ns = int(msg.timestamp) * 1000
            level_str = msg.log_level_str() if callable(msg.log_level_str) else str(msg.log_level_str)
            log_dict = {
                "timestamp": timestamp_ns,
                "level": level_str,
                "log_level": int(msg.log_level),
                "message": msg.message,
            }
            yield timestamp_ns, channel_id, json.dumps(log_dict).encode("utf-8")

    @staticmethod
    def _check_metadata(metadata: Mapping[str, Mapping[str, str]]) -> None:
        """Reject extra metadata MCAP can't store as given, before any output is written."""
        for name, data in metadata.items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"metadata record name must be a non-empty string: {name!r}")
            if name in RESERVED_METADATA_NAMES:
                raise ValueError(f"metadata record name {name!r} is reserved by ulg2mcap")
            if not isinstance(data, Mapping):
                raise ValueError(f"metadata record {name!r} must be a mapping of strings")
            for key, value in data.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise ValueError(
                        f"metadata record {name!r} keys and values must be strings: "
                        f"{key!r}: {value!r}"
                    )

    def convert(
        self,
        input_path: Union[str, Path],
        output_path: Union[str, Path],
        *,
        metadata: Optional[Mapping[str, Mapping[str, str]]] = None,
    ) -> Path:
        """Convert a ULog file to MCAP format.

        Messages are streamed into the writer via a k-way merge of the (individually
        time-ordered) topics, so payloads are never all held in memory at once.

        Args:
            input_path: Path to input .ulg file.
            output_path: Path to output .mcap file.
            metadata: Extra MCAP metadata records to write, as {record name: {key: value}}
                with string keys and values, e.g. to describe where a log came from. Names
                must not clash with the records ulg2mcap writes ('ulog_header', 'ulog_params').

        Returns:
            Path to written MCAP file.
        """
        input_file = Path(input_path).resolve()
        output_file = Path(output_path).resolve()
        metadata = metadata or {}
        self._check_metadata(metadata)

        if not input_file.exists():
            raise FileNotFoundError(f"Input file not found: {input_file}")

        ulog = ULog(str(input_file))

        start_us = self._earliest_timestamp_us(ulog)
        if start_us is not None and start_us * 1000 + self.time_offset_ns < 0:
            raise ValueError(
                f"time_offset_ns={self.time_offset_ns} would make log times negative "
                f"(earliest message at {start_us} us)"
            )

        # Ensure output directory exists
        output_file.parent.mkdir(parents=True, exist_ok=True)

        with open(output_file, "wb") as f_out:
            writer = Writer(f_out, compression=self.compression_type)
            writer.start()

            # 1. Add ULog metadata
            if ulog.msg_info_dict:
                writer.add_metadata(
                    name="ulog_header",
                    data={str(k): str(v) for k, v in ulog.msg_info_dict.items()},
                )

            if ulog.initial_parameters:
                writer.add_metadata(
                    name="ulog_params",
                    data={str(k): str(v) for k, v in ulog.initial_parameters.items()},
                )

            for name, data in metadata.items():
                writer.add_metadata(name=name, data=dict(data))

            # One lazily-evaluated, time-ordered stream per topic. Order matters: on
            # timestamp ties, heapq.merge yields from earlier streams first.
            streams: List[Iterator[_Message]] = []

            # 2. Register dataset topics
            for d in ulog.data_list:
                topic_name = d.name if d.multi_id == 0 else f"{d.name}_{d.multi_id}"
                if not self._should_include_topic(topic_name):
                    continue

                if "timestamp" not in d.data or len(d.data["timestamp"]) == 0:
                    continue

                # Build schema definition with array field grouping
                schema_dict = generate_json_schema(topic_name, d.data)
                schema_json = json.dumps(schema_dict).encode("utf-8")

                schema_id = writer.register_schema(
                    name=topic_name,
                    encoding="jsonschema",
                    data=schema_json,
                )

                channel_id = writer.register_channel(
                    topic=topic_name,
                    message_encoding="json",
                    schema_id=schema_id,
                )
                streams.append(self._dataset_messages(d, channel_id))

            # 3. Register logged text messages
            if ulog.logged_messages and self._should_include_topic("log"):
                log_schema_json = json.dumps(LOG_TOPIC_SCHEMA).encode("utf-8")
                log_schema_id = writer.register_schema(
                    name="log",
                    encoding="jsonschema",
                    data=log_schema_json,
                )
                log_channel_id = writer.register_channel(
                    topic="log",
                    message_encoding="json",
                    schema_id=log_schema_id,
                )
                streams.append(self._logged_text_messages(ulog, log_channel_id))

            # 4. Merge all topics chronologically and stream into the MCAP container
            for ts_ns, ch_id, payload in heapq.merge(*streams, key=lambda m: m[0]):
                log_time = ts_ns + self.time_offset_ns
                writer.add_message(
                    channel_id=ch_id,
                    log_time=log_time,
                    publish_time=log_time,
                    data=payload,
                )

            writer.finish()

        return output_file

    def _earliest_timestamp_us(self, ulog: ULog) -> Optional[int]:
        """Earliest timestamp (us) among the messages that will be written, if any."""
        candidates = [
            selected[0][1]
            for d in ulog.data_list
            if self._should_include_topic(d.name if d.multi_id == 0 else f"{d.name}_{d.multi_id}")
            and "timestamp" in d.data
            and len(d.data["timestamp"]) > 0
            for selected in [self._selected(d.data["timestamp"])]
            if selected
        ]
        if self._should_include_topic("log"):
            candidates.extend(
                int(m.timestamp) for m in ulog.logged_messages if self._in_range(int(m.timestamp))
            )
        return min(candidates) if candidates else None

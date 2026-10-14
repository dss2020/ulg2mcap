"""ULog to MCAP conversion module."""

import heapq
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Union
import numpy as np

from mcap.writer import CompressionType, Writer
from pyulog import ULog

from ulg2mcap.schemas import LOG_TOPIC_SCHEMA, generate_json_schema, parse_grouped_fields

# (timestamp_ns, channel_id, payload_bytes)
_Message = Tuple[int, int, bytes]


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
    ):
        """Initialize converter options.

        Args:
            include_topics: List of topic names to include (if set, only these are converted).
            exclude_topics: List of topic names to exclude.
            compression: Compression algorithm ('zstd', 'lz4', or 'none').
            time_offset_ns: Offset added to every MCAP log_time/publish_time, e.g. to shift
                ULog boot-relative time onto the UTC epoch. The `timestamp` fields inside
                message payloads are left untouched (boot-relative).
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
        timestamps = d.data["timestamp"]
        _, access_list = parse_grouped_fields(d.data)

        if np.all(timestamps[1:] >= timestamps[:-1]):
            order: Any = range(len(timestamps))
        else:
            order = np.argsort(timestamps, kind="stable")

        for i in order:
            timestamp_ns = int(timestamps[i]) * 1000  # us -> ns

            sample_dict = {}
            for field_name, is_array, arr_target in access_list:
                if is_array:
                    sample_dict[field_name] = [self._format_value(arr[i]) for arr in arr_target]
                else:
                    sample_dict[field_name] = self._format_value(arr_target[i])

            yield timestamp_ns, channel_id, json.dumps(sample_dict).encode("utf-8")

    @staticmethod
    def _logged_text_messages(ulog: ULog, channel_id: int) -> Iterator[_Message]:
        """Lazily yield ULog text messages in timestamp order (stable for ties)."""
        for msg in sorted(ulog.logged_messages, key=lambda m: int(m.timestamp)):
            timestamp_ns = int(msg.timestamp) * 1000
            level_str = msg.log_level_str() if callable(msg.log_level_str) else str(msg.log_level_str)
            log_dict = {
                "timestamp": timestamp_ns,
                "level": level_str,
                "log_level": int(msg.log_level),
                "message": msg.message,
            }
            yield timestamp_ns, channel_id, json.dumps(log_dict).encode("utf-8")

    def convert(self, input_path: Union[str, Path], output_path: Union[str, Path]) -> Path:
        """Convert a ULog file to MCAP format.

        Messages are streamed into the writer via a k-way merge of the (individually
        time-ordered) topics, so payloads are never all held in memory at once.

        Args:
            input_path: Path to input .ulg file.
            output_path: Path to output .mcap file.

        Returns:
            Path to written MCAP file.
        """
        input_file = Path(input_path).resolve()
        output_file = Path(output_path).resolve()

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
            int(np.min(d.data["timestamp"]))
            for d in ulog.data_list
            if self._should_include_topic(d.name if d.multi_id == 0 else f"{d.name}_{d.multi_id}")
            and "timestamp" in d.data
            and len(d.data["timestamp"]) > 0
        ]
        if self._should_include_topic("log"):
            candidates.extend(int(m.timestamp) for m in ulog.logged_messages)
        return min(candidates) if candidates else None

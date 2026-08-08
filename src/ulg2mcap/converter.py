"""ULog to MCAP conversion module."""

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union
import numpy as np

from mcap.writer import CompressionType, Writer
from pyulog import ULog

from ulg2mcap.schemas import LOG_TOPIC_SCHEMA, generate_json_schema, parse_grouped_fields


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
    ):
        """Initialize converter options.

        Args:
            include_topics: List of topic names to include (if set, only these are converted).
            exclude_topics: List of topic names to exclude.
            compression: Compression algorithm ('zstd', 'lz4', or 'none').
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

    def convert(self, input_path: Union[str, Path], output_path: Union[str, Path]) -> Path:
        """Convert a ULog file to MCAP format.

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

        # Ensure output directory exists
        output_file.parent.mkdir(parents=True, exist_ok=True)

        ulog = ULog(str(input_file))

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

            messages_to_write: List[tuple] = []  # (timestamp_ns, channel_id, payload_bytes)

            # 2. Process dataset topics
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

                timestamps = d.data["timestamp"]
                num_samples = len(timestamps)

                # Parse grouped fields for efficient sample payload construction
                _, access_list = parse_grouped_fields(d.data)

                for i in range(num_samples):
                    timestamp_ns = int(timestamps[i]) * 1000  # us -> ns

                    sample_dict = {}
                    for field_name, is_array, arr_target in access_list:
                        if is_array:
                            sample_dict[field_name] = [
                                self._format_value(arr[i]) for arr in arr_target
                            ]
                        else:
                            sample_dict[field_name] = self._format_value(arr_target[i])

                    payload = json.dumps(sample_dict).encode("utf-8")
                    messages_to_write.append((timestamp_ns, channel_id, payload))

            # 3. Process logged text messages
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

                for msg in ulog.logged_messages:
                    timestamp_ns = int(msg.timestamp) * 1000
                    level_str = msg.log_level_str() if callable(msg.log_level_str) else str(msg.log_level_str)
                    log_dict = {
                        "timestamp": timestamp_ns,
                        "level": level_str,
                        "log_level": int(msg.log_level),
                        "message": msg.message,
                    }
                    payload = json.dumps(log_dict).encode("utf-8")
                    messages_to_write.append((timestamp_ns, log_channel_id, payload))

            # 4. Sort all messages chronologically by timestamp
            messages_to_write.sort(key=lambda item: item[0])

            # 5. Write messages to MCAP container
            for ts_ns, ch_id, payload in messages_to_write:
                writer.add_message(
                    channel_id=ch_id,
                    log_time=ts_ns,
                    publish_time=ts_ns,
                    data=payload,
                )

            writer.finish()

        return output_file

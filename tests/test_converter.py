import json
import tempfile
from pathlib import Path
import numpy as np
import pytest

from mcap.reader import make_reader
from ulg2mcap.converter import ULog2MCAPConverter
from ulg2mcap.schemas import generate_json_schema, parse_grouped_fields

SAMPLE_ULG_PATH = (
    Path(__file__).resolve().parents[2] / "ekf_playback" / "ulog_files" / "log_sample.ulg"
)


def test_schema_generation():
    raw_fields = {
        "timestamp": np.dtype("uint64"),
        "q[0]": np.dtype("float32"),
        "q[1]": np.dtype("float32"),
        "q[2]": np.dtype("float32"),
        "flag": np.dtype("bool"),
    }
    schema = generate_json_schema("test_topic", raw_fields)
    assert schema["title"] == "test_topic"
    assert schema["type"] == "object"
    assert schema["properties"]["timestamp"]["type"] == "integer"
    assert schema["properties"]["q"]["type"] == "array"
    assert schema["properties"]["q"]["items"]["type"] == "number"
    assert schema["properties"]["flag"]["type"] == "boolean"


@pytest.mark.skipif(not SAMPLE_ULG_PATH.exists(), reason="Sample ULog file not found")
def test_full_conversion_and_reading():
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_mcap = Path(tmp_dir) / "output.mcap"
        converter = ULog2MCAPConverter(compression="zstd")
        result_path = converter.convert(SAMPLE_ULG_PATH, out_mcap)

        assert result_path.exists()
        assert result_path.stat().st_size > 0

        # Read back and validate using mcap library
        with open(result_path, "rb") as f:
            reader = make_reader(f)
            summary = reader.get_summary()

            assert summary is not None
            assert len(summary.schemas) > 0
            assert len(summary.channels) > 0
            assert summary.statistics.message_count > 0

            # Verify topic naming convention (bare names)
            channel_topics = [ch.topic for ch in summary.channels.values()]
            for topic in channel_topics:
                assert not topic.startswith("/ulog/")

            # Verify metadata
            metadata_records = list(reader.iter_metadata())
            assert any(m.name == "ulog_header" for m in metadata_records)

            # Verify array field grouping in messages
            for schema, channel, message in reader.iter_messages():
                if channel.topic == "estimator/ekf_state":
                    payload = json.loads(message.data.decode("utf-8"))
                    assert "q_wxyz" in payload
                    assert isinstance(payload["q_wxyz"], list)
                    assert len(payload["q_wxyz"]) == 4
                    assert "position" in payload
                    assert isinstance(payload["position"], list)
                    assert len(payload["position"]) == 3
                    break


@pytest.mark.skipif(not SAMPLE_ULG_PATH.exists(), reason="Sample ULog file not found")
def test_include_exclude_topics():
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_mcap = Path(tmp_dir) / "filtered.mcap"
        target_topic = "estimator/ekf_state"

        converter = ULog2MCAPConverter(include_topics=[target_topic], compression="none")
        converter.convert(SAMPLE_ULG_PATH, out_mcap)

        with open(out_mcap, "rb") as f:
            reader = make_reader(f)
            summary = reader.get_summary()
            assert summary is not None
            channel_topics = [ch.topic for ch in summary.channels.values()]
            assert channel_topics == [target_topic]

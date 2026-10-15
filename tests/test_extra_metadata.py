"""Tests for caller-supplied MCAP metadata records."""

from pathlib import Path

import pytest
from mcap.reader import make_reader

from tests.ulog_builder import ULogBuilder
from ulg2mcap.converter import ULog2MCAPConverter


@pytest.fixture
def ulg(tmp_path: Path) -> Path:
    b = ULogBuilder(start_timestamp_us=1000)
    b.add_info("sys_name", "synthetic")
    b.add_format("alpha", [("uint64_t", "timestamp"), ("uint32_t", "seq")])
    alpha = b.subscribe("alpha")
    b.add_data(alpha, {"timestamp": 1000, "seq": 0})
    return b.write(tmp_path / "in.ulg")


def metadata_records(mcap_path: Path) -> dict:
    with open(mcap_path, "rb") as f:
        return {m.name: m.metadata for m in make_reader(f).iter_metadata()}


def test_extra_metadata_written_beside_ulog_records(ulg, tmp_path):
    extra = {"vehicle": {"serial": "22829", "site": ""}, "batch": {"id": "7"}}

    out = ULog2MCAPConverter().convert(ulg, tmp_path / "o.mcap", metadata=extra)

    records = metadata_records(out)
    assert records["vehicle"] == {"serial": "22829", "site": ""}
    assert records["batch"] == {"id": "7"}
    assert records["ulog_header"]["sys_name"] == "synthetic"


def test_no_extra_metadata_by_default(ulg, tmp_path):
    out = ULog2MCAPConverter().convert(ulg, tmp_path / "o.mcap")

    assert set(metadata_records(out)) == {"ulog_header"}


@pytest.mark.parametrize(
    "metadata, message",
    [
        ({"ulog_header": {"a": "b"}}, "reserved"),
        ({"ulog_params": {"a": "b"}}, "reserved"),
        ({"": {"a": "b"}}, "non-empty"),
        ({"vehicle": {"serial": 22829}}, "strings"),
        ({"vehicle": {1: "x"}}, "strings"),
        ({"vehicle": ["serial"]}, "mapping"),
    ],
)
def test_invalid_metadata_rejected_before_writing(ulg, tmp_path, metadata, message):
    out = tmp_path / "o.mcap"

    with pytest.raises(ValueError, match=message):
        ULog2MCAPConverter().convert(ulg, out, metadata=metadata)
    assert not out.exists()

"""Tests for time_offset_ns and the streaming (k-way merge) writer."""

import json
import os
import sys
from pathlib import Path
from typing import List, Tuple

import pytest
from mcap.reader import make_reader
from pyulog import ULog

from tests.ulog_builder import ULogBuilder
from ulg2mcap import cli
from ulg2mcap.converter import ULog2MCAPConverter

SAMPLE_ULG_PATH = Path(os.environ.get("ULG2MCAP_SAMPLE_ULG", ""))
HAVE_SAMPLE_ULG = SAMPLE_ULG_PATH.is_file()

# 2026-09-25T09:57:47Z expressed in ns: a realistic boot->UTC offset.
UTC_OFFSET_NS = 1_790_330_267_000_000_000


@pytest.fixture
def synthetic_ulg(tmp_path: Path) -> Path:
    """Two topics plus text logs, with timestamp ties across topics and an
    out-of-order sample inside 'alpha'."""
    b = ULogBuilder(start_timestamp_us=1000)
    b.add_info("sys_name", "synthetic")
    b.add_format("alpha", [("uint64_t", "timestamp"), ("uint32_t", "seq"), ("float[3]", "vec")])
    b.add_format("beta", [("uint64_t", "timestamp"), ("uint32_t", "seq")])
    alpha = b.subscribe("alpha")
    beta = b.subscribe("beta")

    b.add_data(alpha, {"timestamp": 1000, "seq": 0, "vec": [1.0, 2.0, 3.0]})
    b.add_data(beta, {"timestamp": 2000, "seq": 100})
    b.add_log(1500, "armed")
    b.add_data(alpha, {"timestamp": 3000, "seq": 1, "vec": [0.0, 0.0, 0.0]})
    b.add_data(alpha, {"timestamp": 2000, "seq": 2, "vec": [0.0, 0.0, 0.0]})  # out of order
    b.add_data(alpha, {"timestamp": 2000, "seq": 3, "vec": [0.0, 0.0, 0.0]})  # tie within topic
    b.add_log(2000, "tie with data")
    b.add_data(beta, {"timestamp": 2500, "seq": 101})
    return b.write(tmp_path / "synthetic.ulg")


def legacy_order(ulg_path: Path) -> List[Tuple[int, str, object]]:
    """Message order produced by the original collect-everything-then-stable-sort writer."""
    ulog = ULog(str(ulg_path))
    items: List[Tuple[int, str, object]] = []
    for d in ulog.data_list:
        topic = d.name if d.multi_id == 0 else f"{d.name}_{d.multi_id}"
        for ts, seq in zip(d.data["timestamp"], d.data["seq"]):
            items.append((int(ts) * 1000, topic, int(seq)))
    for m in ulog.logged_messages:
        items.append((int(m.timestamp) * 1000, "log", m.message))
    items.sort(key=lambda item: item[0])
    return items


def read_back(mcap_path: Path):
    """Messages in on-disk (write) order, plus the summary."""
    with open(mcap_path, "rb") as f:
        reader = make_reader(f)
        messages = [
            (channel.topic, message, json.loads(message.data))
            for _, channel, message in reader.iter_messages(log_time_order=False)
        ]
        return messages, reader.get_summary()


def identity(topic: str, payload: dict) -> object:
    return payload["message"] if topic == "log" else payload["seq"]


def test_default_order_matches_legacy_stable_sort(synthetic_ulg, tmp_path):
    out = ULog2MCAPConverter(compression="none").convert(synthetic_ulg, tmp_path / "o.mcap")
    messages, _ = read_back(out)

    actual = [(m.log_time, topic, identity(topic, p)) for topic, m, p in messages]
    assert actual == legacy_order(synthetic_ulg)


def test_offset_shifts_log_and_publish_time_but_not_payload(synthetic_ulg, tmp_path):
    converter = ULog2MCAPConverter(compression="none", time_offset_ns=UTC_OFFSET_NS)
    messages, summary = read_back(converter.convert(synthetic_ulg, tmp_path / "o.mcap"))

    expected = legacy_order(synthetic_ulg)
    assert [(m.log_time - UTC_OFFSET_NS, t, identity(t, p)) for t, m, p in messages] == expected

    for topic, message, payload in messages:
        assert message.publish_time == message.log_time
        boot_ns = message.log_time - UTC_OFFSET_NS
        if topic == "log":
            assert payload["timestamp"] == boot_ns  # log payloads carry boot-relative ns
        else:
            assert payload["timestamp"] * 1000 == boot_ns  # raw ULog microseconds

    assert summary.statistics.message_start_time == 1_000_000 + UTC_OFFSET_NS
    assert summary.statistics.message_end_time == 3_000_000 + UTC_OFFSET_NS


def test_arrays_still_grouped(synthetic_ulg, tmp_path):
    out = ULog2MCAPConverter(compression="none").convert(synthetic_ulg, tmp_path / "o.mcap")
    messages, _ = read_back(out)

    first_alpha = next(p for t, _, p in messages if t == "alpha")
    assert first_alpha["vec"] == [1.0, 2.0, 3.0]


@pytest.mark.parametrize("offset_ns", [-1_000_000, 0, UTC_OFFSET_NS])
def test_non_negative_results_accepted(synthetic_ulg, tmp_path, offset_ns):
    converter = ULog2MCAPConverter(compression="none", time_offset_ns=offset_ns)
    _, summary = read_back(converter.convert(synthetic_ulg, tmp_path / "o.mcap"))

    assert summary.statistics.message_start_time == 1_000_000 + offset_ns


def test_offset_making_times_negative_is_rejected_without_writing(synthetic_ulg, tmp_path):
    out = tmp_path / "o.mcap"
    converter = ULog2MCAPConverter(time_offset_ns=-1_000_001)

    with pytest.raises(ValueError, match="negative"):
        converter.convert(synthetic_ulg, out)
    assert not out.exists()


def test_negative_check_only_considers_included_topics(synthetic_ulg, tmp_path):
    # 'alpha' holds the earliest sample (1000 us); without it and the text log, beta starts at 2000 us.
    converter = ULog2MCAPConverter(
        exclude_topics=["alpha", "log"], compression="none", time_offset_ns=-1_500_000
    )
    messages, _ = read_back(converter.convert(synthetic_ulg, tmp_path / "o.mcap"))

    assert [m.log_time for _, m, _ in messages] == [500_000, 1_000_000]


def test_cli_time_offset_flag(synthetic_ulg, tmp_path, monkeypatch):
    out = tmp_path / "cli.mcap"
    monkeypatch.setattr(
        sys,
        "argv",
        ["ulg2mcap", str(synthetic_ulg), "-o", str(out), "--time-offset-ns", str(UTC_OFFSET_NS)],
    )

    assert cli.main() == 0
    _, summary = read_back(out)
    assert summary.statistics.message_start_time == 1_000_000 + UTC_OFFSET_NS


@pytest.mark.skipif(not HAVE_SAMPLE_ULG, reason="ULG2MCAP_SAMPLE_ULG not set to a .ulg file")
def test_sample_offset_and_ordering(tmp_path):
    plain = ULog2MCAPConverter().convert(SAMPLE_ULG_PATH, tmp_path / "plain.mcap")
    shifted = ULog2MCAPConverter(time_offset_ns=UTC_OFFSET_NS).convert(
        SAMPLE_ULG_PATH, tmp_path / "shifted.mcap"
    )

    with open(plain, "rb") as fp, open(shifted, "rb") as fs:
        plain_reader, shifted_reader = make_reader(fp), make_reader(fs)
        plain_stats = plain_reader.get_summary().statistics
        shifted_stats = shifted_reader.get_summary().statistics

        assert shifted_stats.message_count == plain_stats.message_count
        assert shifted_stats.message_start_time == plain_stats.message_start_time + UTC_OFFSET_NS
        assert shifted_stats.message_end_time == plain_stats.message_end_time + UTC_OFFSET_NS

        previous = -1
        for _, _, message in shifted_reader.iter_messages(log_time_order=False):
            assert message.log_time >= previous
            previous = message.log_time

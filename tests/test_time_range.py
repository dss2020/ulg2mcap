"""Tests for start_us/end_us (time range) and carry_state."""

import sys

import pytest

from tests.test_time_offset_streaming import UTC_OFFSET_NS, read_back, synthetic_ulg  # noqa: F401
from ulg2mcap import cli
from ulg2mcap.converter import ULog2MCAPConverter

# synthetic_ulg (ULog µs): alpha seq0@1000, seq2@2000, seq3@2000, seq1@3000;
# beta seq100@2000, seq101@2500; text logs "armed"@1500, "tie with data"@2000.


def written(ulg, tmp_path, **options):
    """[(log_time in µs, topic, seq or message, payload timestamp)] in write order."""
    converter = ULog2MCAPConverter(compression="none", **options)
    messages, _ = read_back(converter.convert(ulg, tmp_path / "o.mcap"))
    return [
        (
            m.log_time // 1000,
            topic,
            p["message"] if topic == "log" else p["seq"],
            p["timestamp"],
        )
        for topic, m, p in messages
    ]


def test_strict_range_is_inclusive(synthetic_ulg, tmp_path):
    assert written(synthetic_ulg, tmp_path, start_us=2000, end_us=2500) == [
        (2000, "alpha", 2, 2000),
        (2000, "alpha", 3, 2000),
        (2000, "beta", 100, 2000),
        (2000, "log", "tie with data", 2_000_000),
        (2500, "beta", 101, 2500),
    ]


def test_open_ended_ranges(synthetic_ulg, tmp_path):
    assert [m[:3] for m in written(synthetic_ulg, tmp_path, end_us=1500)] == [
        (1000, "alpha", 0),
        (1500, "log", "armed"),
    ]
    assert [m[:3] for m in written(synthetic_ulg, tmp_path, start_us=2500)] == [
        (2500, "beta", 101),
        (3000, "alpha", 1),
    ]


def test_carry_state_stamps_latest_earlier_message_at_start(synthetic_ulg, tmp_path):
    assert written(synthetic_ulg, tmp_path, start_us=2500, end_us=3000, carry_state=True) == [
        (2500, "alpha", 3, 2000),  # latest alpha before 2500 (last of the 2000 tie)
        (2500, "beta", 100, 2000),
        (2500, "beta", 101, 2500),
        (3000, "alpha", 1, 3000),
    ]  # text logs are events, never carried


def test_carry_state_for_a_topic_that_ended_before_the_range(synthetic_ulg, tmp_path):
    got = written(synthetic_ulg, tmp_path, start_us=2600, carry_state=True)
    assert got == [
        (2600, "alpha", 3, 2000),
        (2600, "beta", 101, 2500),
        (3000, "alpha", 1, 3000),
    ]


def test_carry_state_needs_a_start(synthetic_ulg, tmp_path):
    assert written(synthetic_ulg, tmp_path, carry_state=True) == written(
        synthetic_ulg, tmp_path
    )
    # Nothing before the start: nothing to carry.
    assert written(synthetic_ulg, tmp_path, start_us=1000, carry_state=True) == written(
        synthetic_ulg, tmp_path, start_us=1000
    )


def test_range_with_offset(synthetic_ulg, tmp_path):
    converter = ULog2MCAPConverter(
        compression="none", time_offset_ns=UTC_OFFSET_NS, start_us=2500, carry_state=True
    )
    _, summary = read_back(converter.convert(synthetic_ulg, tmp_path / "o.mcap"))
    assert summary.statistics.message_start_time == 2_500_000 + UTC_OFFSET_NS
    assert summary.statistics.message_end_time == 3_000_000 + UTC_OFFSET_NS


def test_negative_check_uses_the_range(synthetic_ulg, tmp_path):
    # Earliest written message is at 2500 µs, so an offset of -2.4 ms is still valid.
    converter = ULog2MCAPConverter(compression="none", time_offset_ns=-2_400_000, start_us=2500)
    converter.convert(synthetic_ulg, tmp_path / "o.mcap")
    with pytest.raises(ValueError, match="negative"):
        ULog2MCAPConverter(time_offset_ns=-2_400_000).convert(synthetic_ulg, tmp_path / "x.mcap")


def test_start_after_end_rejected():
    with pytest.raises(ValueError, match="after end_us"):
        ULog2MCAPConverter(start_us=2, end_us=1)


def test_cli_range_flags(synthetic_ulg, tmp_path, monkeypatch):
    out = tmp_path / "cli.mcap"
    argv = ["ulg2mcap", str(synthetic_ulg), "-o", str(out), "--compress", "none",
            "--start-us", "2500", "--end-us", "3000", "--carry-state"]  # fmt: skip
    monkeypatch.setattr(sys, "argv", argv)
    assert cli.main() == 0
    messages, _ = read_back(out)
    assert [(t, p["seq"]) for t, _, p in messages] == [
        ("alpha", 3),
        ("beta", 100),
        ("beta", 101),
        ("alpha", 1),
    ]

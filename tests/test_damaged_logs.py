"""Files that are not logs, and logs with parts that are not.

Found by mutating real logs: 510 mutants, none ended the process, and the
reader and the report each had ways to fail that this file holds shut.
"""

from __future__ import annotations

import os
import struct

import pytest

import binlog
from lib import log_reader as log_reader_module
from lib.i18n import t
from lib.log_reader import LogReader, UnreadableLog, not_log_bytes
from lib.pipeline import NotAFlightLog, run_analysis

LONGEST_MESSAGE = 255


@pytest.fixture(scope="module")
def flight(tmp_path_factory) -> str:
    folder = tmp_path_factory.mktemp("damaged")
    return binlog.flight_log().save(folder / "flight.bin")


def _with_run(source: str, path, run: bytes, at: float = 0.5) -> tuple[str, int]:
    """A copy of the log with run put in that share of the way through.
    Returns the copy and the offset the run starts at."""
    data = open(source, "rb").read()
    cut = int(len(data) * at)
    path.write_bytes(data[:cut] + run + data[cut:])
    return str(path), cut


class TestCounting:
    def test_whole_log_has_none(self, flight):
        assert not_log_bytes(flight) == (0, 0)

    def test_zeros_after_the_last_message(self, flight, tmp_path):
        size = os.path.getsize(flight)
        path, _cut = _with_run(flight, tmp_path / "tail.bin", bytes(10_000),
                               at=1.0)

        count, first = not_log_bytes(path)

        # The last message may be as long as a message can be, so up to
        # 255 bytes after its header are given to it.
        assert 10_000 - LONGEST_MESSAGE <= count <= 10_000
        assert size <= first <= size + LONGEST_MESSAGE

    def test_hole_in_the_middle_is_placed(self, flight, tmp_path):
        path, cut = _with_run(flight, tmp_path / "hole.bin", bytes(10_000))

        count, first = not_log_bytes(path)

        assert abs(count - 10_000) <= LONGEST_MESSAGE
        assert cut < first <= cut + LONGEST_MESSAGE

    def test_file_with_no_header_is_all_of_it(self, tmp_path):
        path = tmp_path / "text.bin"
        path.write_bytes(b"hover test, third flight\n" * 400)

        assert not_log_bytes(str(path)) == (10_000, 0)

    def test_header_split_across_two_blocks_is_found(self, tmp_path,
                                                     monkeypatch):
        """Headers 200 bytes apart, read 201 at a time: the second header
        has one byte in each block."""
        path = tmp_path / "split.bin"
        path.write_bytes((b"\xa3\x95" + bytes(198)) * 10)
        monkeypatch.setattr(log_reader_module, "_SCAN_BLOCK", 201)

        assert not_log_bytes(str(path)) == (0, 0)

    def test_header_byte_alone_is_not_a_header(self, tmp_path):
        path = tmp_path / "half.bin"
        path.write_bytes(b"\xa3\x95" + (b"\xa3" + bytes(99)) * 20)

        count, first = not_log_bytes(str(path))

        assert (count, first) == (2002 - LONGEST_MESSAGE, LONGEST_MESSAGE)


@pytest.fixture(params=[
    ("zeros.bin", bytes(300_000)),
    ("text.BIN", b"2026-11-10 12:00:00 hover test\n" * 10_000),
    ("pattern.bin", bytes(range(256)) * 1200),
], ids=lambda case: case[0])
def junk(request, tmp_path) -> str:
    name, content = request.param
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)


class TestNotALogAtAll:
    """5 MB of zeros named .BIN took 30 s to refuse, and put 10 million
    lines on the console: pymavlink walks what it cannot read a byte at a
    time. 500 MB, the upload limit of the day, came to most of an hour."""

    def test_refused_before_pymavlink_sees_it(self, junk, monkeypatch):
        def never(*_args, **_kwargs):
            raise AssertionError("pymavlink was given the file")
        monkeypatch.setattr(log_reader_module.mavutil, "mavlink_connection",
                            never)

        with pytest.raises(UnreadableLog) as refusal:
            LogReader(junk)

        assert refusal.value.not_log_bytes * 2 > refusal.value.size
        assert refusal.value.first_offset == 0

    def test_pipeline_says_it_is_no_flight_log(self, junk):
        with pytest.raises(NotAFlightLog) as refusal:
            run_analysis(junk)

        assert str(refusal.value) == t("pipeline.not_a_flight_log",
                                       name=os.path.basename(junk))


class TestHoles:
    def test_short_hole_is_read_through_in_silence(self, flight, tmp_path,
                                                   capfd):
        """pymavlink printed a line for every byte of it: 262,000 lines for
        64 KB, with the file read four times over."""
        path, _cut = _with_run(flight, tmp_path / "hole.bin", bytes(8192))
        assert not_log_bytes(path)[0] > 7000  # precondition: the hole is there
        whole = LogReader(flight)
        capfd.readouterr()

        holed = LogReader(path)

        assert holed.params == whole.params
        assert holed.has_message("ATT")
        assert capfd.readouterr().err == ""

    def test_long_hole_is_refused_with_its_size_and_place(
            self, flight, tmp_path, monkeypatch):
        monkeypatch.setattr(log_reader_module, "_MAX_NOT_LOG_BYTES", 20_000)
        path, cut = _with_run(flight, tmp_path / "holed.bin", bytes(65_536))
        assert 65_536 * 2 < os.path.getsize(path)  # precondition: mostly log

        with pytest.raises(UnreadableLog) as refusal:
            LogReader(path)

        assert abs(refusal.value.not_log_bytes - 65_536) <= LONGEST_MESSAGE
        assert cut < refusal.value.first_offset <= cut + LONGEST_MESSAGE

    def test_pipeline_tells_the_user_how_much_and_where(
            self, flight, tmp_path, monkeypatch):
        monkeypatch.setattr(log_reader_module, "_MAX_NOT_LOG_BYTES", 20_000)
        path, _cut = _with_run(flight, tmp_path / "holed.bin", bytes(65_536))
        count, first = not_log_bytes(path)

        with pytest.raises(NotAFlightLog) as refusal:
            run_analysis(path)

        assert str(refusal.value) == t(
            "pipeline.damaged_not_log", name="holed.bin",
            mb=f"{count / 1e6:.1f}", offset=first)

    def test_log_with_more_zeros_than_log_is_called_damaged(self, flight,
                                                            tmp_path):
        """A copy that ran into a bad card: what it holds is a log."""
        size = os.path.getsize(flight)
        path, _cut = _with_run(flight, tmp_path / "padded.bin",
                               bytes(2 * size), at=1.0)
        count, first = not_log_bytes(path)
        assert count * 2 > os.path.getsize(path)  # precondition: mostly zeros
        assert first >= size

        with pytest.raises(NotAFlightLog) as refusal:
            run_analysis(path)

        assert str(refusal.value) == t(
            "pipeline.damaged_not_log", name="padded.bin",
            mb=f"{count / 1e6:.1f}", offset=first)

    def test_same_hole_under_the_limit_is_read(self, flight, tmp_path):
        path, _cut = _with_run(flight, tmp_path / "holed.bin", bytes(65_536))

        assert LogReader(path).has_message("ATT")


def _log_with_bad_format_record(path) -> str:
    """Enough whole records for a file of some size, then one FMT whose
    format string holds a character no format has."""
    log = binlog.BinLog()
    for i in range(40):
        log.declare(f"X{i:03d}", "Q", "TimeUS")
    log.data += b"\xa3\x95\x80" + struct.pack(
        "<BB4s16s64s", 200, 12, b"BAD\0", b"\\", b"TimeUS")
    for i in range(40, 80):
        log.declare(f"X{i:03d}", "Q", "TimeUS")
    return log.save(path)


class TestFormatRecords:
    """pymavlink gives up on a damaged format record in a dozen ways. The
    user was told "smp_cnt", "GMS", or "unpack requires a buffer of 87
    bytes"."""

    def test_reader_says_what_could_not_be_read(self, tmp_path):
        path = _log_with_bad_format_record(tmp_path / "bad.bin")
        assert not_log_bytes(path) == (0, 0)  # precondition: frames are whole

        with pytest.raises(UnreadableLog) as refusal:
            LogReader(path)

        assert "format records cannot be read" in str(refusal.value)
        assert refusal.value.reason.startswith(
            "Exception: Unsupported format char")
        assert refusal.value.__cause__ is not None

    def test_pipeline_names_both_causes(self, tmp_path):
        path = _log_with_bad_format_record(tmp_path / "bad.bin")

        with pytest.raises(NotAFlightLog) as refusal:
            run_analysis(path)

        assert str(refusal.value).startswith(t(
            "pipeline.damaged_format", name="bad.bin", reason="@")
            .split("@")[0])
        assert "Unsupported format char" in str(refusal.value)

    def test_missing_file_is_still_a_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            LogReader(str(tmp_path / "none.bin"))


def _log_with_batches(header_columns: str, data_format: str) -> binlog.BinLog:
    log = binlog.BinLog()
    log.declare("PARM")
    log.declare("ISBH", "QHBBHHQf", header_columns)
    log.declare("ISBD", data_format, "TimeUS,N,seqno,x,y,z")
    log.write("PARM", 1_000_000, "BEFORE", 1.0, 1.0)
    log.write("ISBH", 2_000_000, 0, 1, 0, 1, 32, 2_000_000, 1000.0)
    samples = list(range(32))
    if data_format == "QHHaaa":
        log.write("ISBD", 2_000_000, 0, 0, samples, samples, samples)
    else:
        log.write("ISBD", 2_000_000, 0, 0, 1.0, 2.0, 3.0)
    log.write("PARM", 3_000_000, "AFTER", 2.0, 2.0)
    return log


class TestBatchRowsOfAnotherLayout:
    """The batch sampler rows were read by attribute, with no fallback: a
    header without smp_cnt ended the read of the whole log."""

    STANDARD = "TimeUS,N,type,instance,mul,smp_cnt,SampleUS,smp_rate"

    def test_standard_layout_is_collected(self, tmp_path):
        path = _log_with_batches(self.STANDARD, "QHHaaa").save(
            tmp_path / "batches.bin")

        reader = LogReader(path)

        assert list(reader.isb_batches) == [0]
        assert len(reader.isb_batches[0]["x"]) == 32

    def test_header_without_a_field_costs_the_batch_not_the_log(self,
                                                                tmp_path):
        columns = self.STANDARD.replace("smp_cnt", "count")
        path = _log_with_batches(columns, "QHHaaa").save(
            tmp_path / "batches.bin")

        reader = LogReader(path)

        assert reader.isb_batches == {}
        assert reader.params == {"BEFORE": 1.0, "AFTER": 2.0}

    def test_samples_that_are_not_arrays_drop_the_batch(self, tmp_path):
        path = _log_with_batches(self.STANDARD, "QHHfff").save(
            tmp_path / "batches.bin")

        reader = LogReader(path)

        assert reader.isb_batches == {}
        assert reader.params == {"BEFORE": 1.0, "AFTER": 2.0}

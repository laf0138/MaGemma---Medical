"""
Tests for RingBuffer (services/specter_rx_ring_buffer.py).

The class docstring documents a previously-shipped bug: `filled` was only
set when a write happened to land exactly on pos==0, rather than whenever
a write causes the position to wrap past the end of the buffer. The
practical effect was that a buffer that had just been completely filled by
an exact-boundary write could report itself as not-yet-filled and
`read_all()` would silently return an empty/truncated capture right at
the moment a trigger fired.

These tests pin the documented fix so it can't regress silently.
"""
import queue

import numpy as np
import pytest

import services.specter_rx_ring_buffer as rx_mod
from services.specter_rx_ring_buffer import RingBuffer, RXBufferService


def make_buffer(maxlen: int) -> RingBuffer:
    # sample_rate=maxlen, seconds=1, channels=1 -> buffer.maxlen == maxlen
    return RingBuffer(sample_rate=maxlen, seconds=1, channels=1)


class TestRingBufferBelowCapacity:
    def test_partial_write_is_not_filled(self):
        rb = make_buffer(10)
        rb.write(np.array([1, 2, 3, 4], dtype=np.int16))
        assert rb.filled is False
        assert rb.pos == 4

    def test_read_all_before_filled_returns_only_written_samples(self):
        rb = make_buffer(10)
        rb.write(np.array([1, 2, 3, 4], dtype=np.int16))
        np.testing.assert_array_equal(rb.read_all(), np.array([1, 2, 3, 4], dtype=np.int16))

    def test_empty_write_is_a_noop(self):
        rb = make_buffer(10)
        rb.write(np.array([], dtype=np.int16))
        assert rb.pos == 0
        assert rb.filled is False


class TestRingBufferExactBoundary:
    """The specific scenario the documented bug fix addresses."""

    def test_incremental_writes_landing_exactly_on_boundary_set_filled(self):
        rb = make_buffer(10)
        rb.write(np.array([1, 2, 3, 4, 5, 6], dtype=np.int16))   # pos -> 6, not filled
        assert rb.filled is False
        rb.write(np.array([7, 8, 9, 10], dtype=np.int16))         # end == maxlen exactly
        assert rb.filled is True
        assert rb.pos == 0

    def test_read_all_after_exact_boundary_fill_returns_full_buffer_in_order(self):
        rb = make_buffer(10)
        rb.write(np.array([1, 2, 3, 4, 5, 6], dtype=np.int16))
        rb.write(np.array([7, 8, 9, 10], dtype=np.int16))
        # This is the regression the bug fix guards: without it, a buffer
        # that just became exactly full could report read_all() as empty.
        np.testing.assert_array_equal(
            rb.read_all(), np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=np.int16)
        )


class TestRingBufferTrueWraparound:
    def test_wraparound_write_marks_filled(self):
        rb = make_buffer(10)
        rb.write(np.array([1, 2, 3, 4, 5, 6, 7, 8], dtype=np.int16))   # pos -> 8
        rb.write(np.array([9, 10, 11, 12, 13], dtype=np.int16))        # wraps past end
        assert rb.filled is True
        assert rb.pos == 3

    def test_wraparound_preserves_chronological_order_on_read(self):
        rb = make_buffer(10)
        rb.write(np.array([1, 2, 3, 4, 5, 6, 7, 8], dtype=np.int16))
        rb.write(np.array([9, 10, 11, 12, 13], dtype=np.int16))
        # Last 10 of [1..13] is [4..13], oldest first.
        np.testing.assert_array_equal(
            rb.read_all(), np.array([4, 5, 6, 7, 8, 9, 10, 11, 12, 13], dtype=np.int16)
        )

    def test_multiple_wraps_still_hold_only_most_recent_window(self):
        rb = make_buffer(5)
        for chunk_start in range(1, 22, 3):
            rb.write(np.array(range(chunk_start, chunk_start + 3), dtype=np.int16))
        # 21 samples [1..21] written in chunks of 3 into a 5-slot buffer.
        np.testing.assert_array_equal(
            rb.read_all(), np.array([17, 18, 19, 20, 21], dtype=np.int16)
        )


class TestRingBufferOversizedWrite:
    def test_single_write_larger_than_buffer_keeps_most_recent_tail(self):
        rb = make_buffer(5)
        rb.write(np.array([1, 2, 3, 4, 5, 6, 7, 8], dtype=np.int16))
        assert rb.filled is True
        assert rb.pos == 0
        np.testing.assert_array_equal(rb.read_all(), np.array([4, 5, 6, 7, 8], dtype=np.int16))

    def test_write_exactly_equal_to_buffer_length_in_one_call(self):
        rb = make_buffer(5)
        rb.write(np.array([1, 2, 3, 4, 5], dtype=np.int16))
        assert rb.filled is True
        np.testing.assert_array_equal(rb.read_all(), np.array([1, 2, 3, 4, 5], dtype=np.int16))


class TestRXBufferService:
    @pytest.fixture
    def service(self, tmp_path):
        return RXBufferService(
            sample_rate=10, channels=1, buffer_seconds=1, post_seconds=1,
            max_record_sec=3, queue_depth=2, output_dir=str(tmp_path),
            trigger_file=str(tmp_path / "trigger"), mqtt_enabled=False,
        )

    def test_audio_level_handles_empty_and_full_scale(self):
        assert RXBufferService._audio_level(np.array([], dtype=np.int16)) == 0.0
        assert RXBufferService._audio_level(np.array([32767, -32768], dtype=np.int16)) == pytest.approx(1, rel=0.001)

    def test_trigger_starts_recording_with_prebuffer(self, service, monkeypatch):
        monkeypatch.setattr(rx_mod.time, "time", lambda: 100.0)
        service.ring.write(np.array([1, 2, 3], dtype=np.int16))
        service.trigger("manual-test")
        assert service._recording is True
        assert service._post_deadline == 101.0
        assert service._last_trigger_reason == "manual-test"
        np.testing.assert_array_equal(service._rec_frames[0], np.array([1, 2, 3], dtype=np.int16))

    def test_trigger_file_is_consumed(self, service, monkeypatch):
        reasons = []
        monkeypatch.setattr(service, "trigger", reasons.append)
        service.trigger_file.touch()
        service._check_trigger_file()
        assert reasons == ["trigger-file"]
        assert service.trigger_file.exists() is False

    def test_empty_queue_timeout_finalizes_without_lock_deadlock(self, service, monkeypatch):
        service._recording = True
        service._post_deadline = 1.0
        service._rec_frames = [np.array([1], dtype=np.int16)]
        calls = []

        class StopAfterOneEmpty:
            def __init__(self):
                self.calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls > 1

        service._stop_event = StopAfterOneEmpty()
        monkeypatch.setattr(rx_mod.time, "time", lambda: 2.0)
        monkeypatch.setattr(service._audio_q, "get", lambda timeout: (_ for _ in ()).throw(queue.Empty()))
        def finalize(reason="post-timeout"):
            acquired = service._record_lock.acquire(blocking=False)
            assert acquired, "process loop called finalize while holding the record lock"
            service._record_lock.release()
            service._recording = False
            calls.append(reason)

        monkeypatch.setattr(service, "_finalize_recording", finalize)
        service._process_loop()
        assert calls == ["post-timeout"]

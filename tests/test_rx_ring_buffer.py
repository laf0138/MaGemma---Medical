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
import numpy as np
import pytest

from services.specter_rx_ring_buffer import RingBuffer


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

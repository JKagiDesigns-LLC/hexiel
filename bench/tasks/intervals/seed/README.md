# intervals

Small helpers for closed numeric intervals, each written `(start, end)` with `start <= end`.

* `merge_intervals(intervals)` — returns the union as a list of `(start, end)` **tuples**, sorted by start.
  Intervals that overlap **or merely touch** (`(1, 2)` and `(2, 3)`) are merged into one. Accepts any iterable of
  2-item lists or tuples. The input is never modified. An empty input gives `[]`. If any interval has `start > end`
  raise `ValueError`.
* `total_covered(intervals)` — the total length covered by the union of the intervals (overlaps are counted once).
  `total_covered([(1, 3), (2, 5)])` is `4`; an empty input is `0`.
* `gaps(intervals, lo, hi)` — the parts of the range `[lo, hi]` that are **not** covered, as a sorted list of
  `(start, end)` tuples. Intervals are clipped to the range; intervals completely outside it are ignored.
  `gaps([(2, 4), (6, 7)], 0, 10)` is `[(0, 2), (4, 6), (7, 10)]`. A range fully covered gives `[]`.
  Raise `ValueError` if `lo > hi`.

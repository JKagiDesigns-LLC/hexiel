"""Helpers for closed numeric intervals. See README.md."""


def merge_intervals(intervals):
    intervals.sort()
    merged = []
    for start, end in intervals:
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def total_covered(intervals):
    return sum(end - start for start, end in intervals)


def gaps(intervals, lo, hi):
    result = []
    cursor = lo
    for start, end in merge_intervals(intervals):
        if start > cursor:
            result.append((cursor, start))
        cursor = end
    if cursor < hi:
        result.append((cursor, hi))
    return result

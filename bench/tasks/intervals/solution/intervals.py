"""Helpers for closed numeric intervals. See README.md."""


def merge_intervals(intervals):
    items = [tuple(i) for i in intervals]
    for start, end in items:
        if start > end:
            raise ValueError(f"bad interval {(start, end)}")
    items.sort()
    merged = []
    for start, end in items:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def total_covered(intervals):
    return sum(end - start for start, end in merge_intervals(intervals))


def gaps(intervals, lo, hi):
    if lo > hi:
        raise ValueError("lo > hi")
    result = []
    cursor = lo
    for start, end in merge_intervals(intervals):
        if end < lo or start > hi:
            continue
        start, end = max(start, lo), min(end, hi)
        if start > cursor:
            result.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < hi:
        result.append((cursor, hi))
    return result

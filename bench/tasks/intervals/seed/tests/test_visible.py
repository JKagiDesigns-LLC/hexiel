from intervals import merge_intervals, total_covered, gaps


def test_merge_overlapping():
    assert merge_intervals([(1, 3), (2, 5)]) == [(1, 5)]


def test_total():
    assert total_covered([(0, 2)]) == 2


def test_gaps_basic():
    assert gaps([(2, 4)], 0, 10) == [(0, 2), (4, 10)]

import copy
import pytest
from intervals import merge_intervals, total_covered, gaps


def test_touching_intervals_merge():
    assert merge_intervals([(1, 2), (2, 3)]) == [(1, 3)]


def test_unsorted_input_and_tuples_out():
    out = merge_intervals([[5, 6], [1, 2], [2, 4]])
    assert out == [(1, 4), (5, 6)] and all(isinstance(x, tuple) for x in out)


def test_input_is_not_modified():
    data = [[3, 4], [1, 2], [2, 3]]
    before = copy.deepcopy(data)
    merge_intervals(data)
    assert data == before


def test_generator_input_and_empty():
    assert merge_intervals(iter([(1, 2)])) == [(1, 2)]
    assert merge_intervals([]) == []
    assert total_covered([]) == 0
    assert gaps([], 0, 5) == [(0, 5)]


def test_nested_and_duplicates():
    assert merge_intervals([(1, 10), (2, 3), (2, 3), (4, 5)]) == [(1, 10)]


def test_float_values():
    assert merge_intervals([(0.5, 1.5), (1.5, 2.0)]) == [(0.5, 2.0)]
    assert total_covered([(0.5, 1.5), (1.0, 2.0)]) == pytest.approx(1.5)


def test_bad_interval_raises():
    with pytest.raises(ValueError):
        merge_intervals([(3, 1)])
    with pytest.raises(ValueError):
        total_covered([(5, 2)])


def test_total_covered_counts_overlap_once():
    assert total_covered([(1, 3), (2, 5)]) == 4
    assert total_covered([(1, 2), (2, 3), (10, 12)]) == 4


def test_gaps_example_from_readme():
    assert gaps([(2, 4), (6, 7)], 0, 10) == [(0, 2), (4, 6), (7, 10)]


def test_gaps_clip_to_range():
    assert gaps([(-5, 3), (8, 20)], 0, 10) == [(3, 8)]


def test_gaps_ignore_outside_and_full_cover():
    assert gaps([(20, 30), (-9, -1)], 0, 10) == [(0, 10)]
    assert gaps([(0, 10)], 0, 10) == []
    assert gaps([(-1, 11)], 0, 10) == []


def test_gaps_touching_neighbours_leave_no_gap():
    assert gaps([(0, 3), (3, 6)], 0, 6) == []


def test_gaps_bad_range():
    with pytest.raises(ValueError):
        gaps([(1, 2)], 5, 1)


def test_gaps_input_not_modified_and_unsorted_ok():
    data = [[6, 7], [2, 4]]
    before = copy.deepcopy(data)
    assert gaps(data, 0, 10) == [(0, 2), (4, 6), (7, 10)]
    assert data == before

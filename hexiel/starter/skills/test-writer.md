---
name: test-writer
description: Write focused tests for a function, module or bug — matching the project's test framework and style.
---

# Test writer

1. Find the existing tests (`glob` for test_*.py, *.test.ts, *_test.go, *Test.java …) and read two of them: framework, fixtures, naming, how they isolate files/network/time.
2. Read the code under test. List its behaviours: normal cases, boundaries (empty, one, many, max), error paths, and anything that touches the outside world.
3. Write one test per behaviour with a name that states it. Prefer real objects over mocks; mock only the network, the clock and randomness. For a bug report, write the failing test first and show it failing.
4. Run only the new tests (never the whole suite unless asked) and fix until they pass for the right reason. Report what is covered and what is still not.

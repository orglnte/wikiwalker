"""Present so pytest puts the repository root on `sys.path`.

pytest inserts the directory of the topmost `conftest.py`, so this otherwise
empty file is what lets `tests/` import `walker`, `link_store` and `wikifetcher`.
"""

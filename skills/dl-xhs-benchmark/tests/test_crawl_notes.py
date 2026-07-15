import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts import crawl_notes


def test_build_batch_id_formats_date_and_count():
    result = crawl_notes.build_batch_id(5, today=date(2026, 7, 15))
    assert result == "20260715_5notes"


def test_build_batch_id_uses_today_by_default():
    result = crawl_notes.build_batch_id(3)
    assert result.endswith("_3notes")
    assert len(result.split("_")[0]) == 8

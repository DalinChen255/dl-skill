import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts import scan_blogger


def test_sort_by_likes_orders_descending():
    notes = [
        {"note_id": "1", "liked_count": 50},
        {"note_id": "2", "liked_count": 300},
        {"note_id": "3", "liked_count": 120},
    ]
    result = scan_blogger.sort_by_likes(notes)
    assert [n["note_id"] for n in result] == ["2", "3", "1"]


def test_sort_by_likes_handles_missing_liked_count():
    notes = [{"note_id": "1"}, {"note_id": "2", "liked_count": 10}]
    result = scan_blogger.sort_by_likes(notes)
    assert [n["note_id"] for n in result] == ["2", "1"]

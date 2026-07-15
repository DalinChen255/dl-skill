import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts import crawl_notes


def test_build_batch_id_formats_date_count_and_link_hash():
    links = ["https://a", "https://b", "https://c", "https://d", "https://e"]
    result = crawl_notes.build_batch_id(links, today=date(2026, 7, 15))
    assert result.startswith("20260715_5notes_")


def test_build_batch_id_uses_today_by_default():
    result = crawl_notes.build_batch_id(["https://a", "https://b", "https://c"])
    assert "_3notes_" in result
    assert len(result.split("_")[0]) == 8


def test_build_batch_id_same_links_produce_same_id_regardless_of_order():
    links_a = ["https://a", "https://b", "https://c"]
    links_b = ["https://c", "https://a", "https://b"]
    assert crawl_notes.build_batch_id(links_a) == crawl_notes.build_batch_id(links_b)


def test_build_batch_id_different_links_produce_different_id():
    links_a = ["https://a", "https://b", "https://c"]
    links_b = ["https://x", "https://y", "https://c"]
    assert crawl_notes.build_batch_id(links_a) != crawl_notes.build_batch_id(links_b)


def test_dedupe_links_removes_duplicates_and_preserves_order():
    links = ["https://a", "https://b", "https://a", "https://c"]
    assert crawl_notes.dedupe_links(links) == ["https://a", "https://b", "https://c"]

from __future__ import annotations

import unittest

from pal.shared.text_search import compile_jieba_fts_queries, jieba_fts_text, jieba_search_terms


class JiebaTextSearchTests(unittest.TestCase):
    def test_cjk_text_is_segmented_for_fts(self) -> None:
        self.assertEqual(jieba_search_terms("简洁中文回复"), ("简洁", "中文", "回复"))
        self.assertEqual(jieba_fts_text("简洁中文回复"), "简洁 中文 回复")

    def test_fts_queries_use_segmented_terms(self) -> None:
        queries = [query for query, _weight in compile_jieba_fts_queries("简洁中文回复")]

        self.assertIn('"简洁 中文 回复"', queries)
        self.assertIn('"简洁" OR "中文" OR "回复"', queries)


if __name__ == "__main__":
    unittest.main()

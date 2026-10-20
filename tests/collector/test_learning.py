"""v3.6 learning: 少推这类 (vote -1) = 0.5 gated negative; 已收起 never counts; like/收藏 +0.08 each, cap 0.30; why() for the taste chip."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import learning  # noqa: E402

NOW = 1_800_000_000_000


def fb(**kw):
    return learning.Feedback(kw, NOW)


class LearningV356(unittest.TestCase):
    def test_constants(self):
        self.assertEqual((learning.BOOST_PER, learning.BOOST_CAP, learning.PENALTY_FLOOR), (0.08, 0.30, 0.4))

    def test_low_ratings_penalise_specific_keys_light(self):
        one = fb(disliked=[{"id": "a", "at": NOW, "keys": ["repo:a/b"], "weight": 0.5}])
        two = fb(disliked=[{"id": "a", "at": NOW, "keys": ["repo:a/b"], "weight": 0.3}])
        self.assertEqual(one.factor(["repo:a/b"]), 0.875)   # 1 − 0.25·0.5
        self.assertEqual(two.factor(["repo:a/b"]), 0.925)   # 1 − 0.25·0.3
        self.assertEqual(one.factor(["repo:other/x"]), 1.0)

    def test_low_ratings_gated_like_deletes(self):
        d = fb(disliked=[{"id": "a", "at": NOW, "keys": ["kw:scion", "kw:claude", "feed:openai-news", "x:@q"], "weight": 0.5}])
        self.assertEqual(d.factor(["kw:scion"]), 1.0)          # kw needs ≥2 distinct items
        self.assertEqual(d.factor(["feed:openai-news"]), 1.0)  # first-party lab channel gated
        self.assertEqual(d.factor(["kw:claude"]), 1.0)         # brands exempt
        self.assertLess(d.factor(["x:@q"]), 1.0)
        d2 = fb(disliked=[{"id": i, "at": NOW, "keys": ["kw:scion", "kw:claude"], "weight": 0.5} for i in "ab"])
        self.assertLess(d2.factor(["kw:scion"]), 1.0)
        self.assertEqual(d2.factor(["kw:claude"]), 1.0)

    def test_tucks_never_count(self):
        t = fb(tucked=[{"id": "a", "at": NOW, "keys": ["x:@q", "kw:scion"], "weight": 0.2}])  # legacy list in an old feedback file
        self.assertEqual(t.factor(["x:@q"]), 1.0)
        self.assertEqual(learning.DISLIKE_WEIGHT, 0.5)

    def test_negatives_share_floor(self):
        many = fb(trashed=[{"id": f"t{i}", "at": NOW, "keys": ["x:@q", "sub:y", "repo:a/b"]} for i in range(5)],
                  disliked=[{"id": "d", "at": NOW, "keys": ["x:@q"], "weight": 0.5}])
        self.assertEqual(many.factor(["x:@q", "sub:y", "repo:a/b"]), 0.4)

    def test_boost_cap_030(self):
        b = fb(liked=[{"id": f"l{i}", "at": NOW, "keys": ["x:@good", "repo:g/h"], "weight": 1.5} for i in range(4)])
        self.assertEqual(b.factor(["x:@good", "repo:g/h"]), 1.3)
        self.assertEqual(fb(liked=[{"id": "l", "at": NOW, "keys": ["x:@good"]}]).factor(["x:@good"]), 1.08)

    def test_why_names_boost_keys(self):
        b = fb(liked=[{"id": "l", "at": NOW, "keys": ["x:@good"], "weight": 1.5}, {"id": "m", "at": NOW, "keys": ["kw:scion"]}],
               trashed=[{"id": "t", "at": NOW, "keys": ["repo:a/b"]}])
        self.assertEqual(b.why(["repo:a/b", "kw:scion", "x:@good"]), ["x:@good", "kw:scion"])
        self.assertEqual(b.why(["repo:a/b"]), ["repo:a/b"])
        self.assertEqual(b.why(["nothing:x"]), [])


if __name__ == "__main__":
    unittest.main()

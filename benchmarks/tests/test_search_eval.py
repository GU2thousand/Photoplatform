import unittest
from benchmarks.search_eval import metrics


class RetrievalMetricsTest(unittest.TestCase):
    def test_perfect_top_ten(self):
        result=metrics(list(range(10)),list(range(10)))
        self.assertEqual(result,{'recall@5':.5,'recall@10':1,'ndcg@10':1})

    def test_order_matters_for_discounted_gain(self):
        early=metrics([1,2,3],[1])
        late=metrics([2,3,1],[1])
        self.assertEqual(early['recall@5'],late['recall@5'])
        self.assertGreater(early['ndcg@10'],late['ndcg@10'])

    def test_missing_and_empty_labels(self):
        self.assertEqual(metrics([2,3],[1])['ndcg@10'],0)
        with self.assertRaises(ValueError): metrics([1],[])


if __name__=='__main__': unittest.main()

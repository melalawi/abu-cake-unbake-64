"""The address index goes to pool workers in a shared pickle, so it must pickle."""

import pickle
import unittest

from unbake.typemap import solver


class AddressIndexTests(unittest.TestCase):
    def test_the_index_round_trips_through_pickle(self) -> None:
        facts = {"globals": {"a": {"versions": {"us": {"address": 4}}}, "b": {"versions": {"us": {"address": 4}}}}}
        loaded = pickle.loads(pickle.dumps(solver._addresses(facts)))
        self.assertEqual(loaded["us"][4], ["a", "b"])
        self.assertEqual(loaded["eu"], {})

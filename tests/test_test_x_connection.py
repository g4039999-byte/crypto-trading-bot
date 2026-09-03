"""scripts/test_x_connection.py: only its no-token fast path is safe to
exercise in the automated suite (this script exists specifically to
make one *real* network call otherwise -- see its own docstring). This
just locks in that it never attempts a network call when unconfigured.
"""

import unittest
from unittest import mock

import scripts.test_x_connection as test_x_connection


class TestNoTokenFastPath(unittest.TestCase):
    def test_returns_zero_and_never_touches_the_network_without_a_token(self):
        with mock.patch.object(test_x_connection, "X_BEARER_TOKEN", ""), \
                mock.patch("src.x_client.search_recent") as mock_search:
            exit_code = test_x_connection.main()

        self.assertEqual(exit_code, 0)
        mock_search.assert_not_called()


if __name__ == "__main__":
    unittest.main()

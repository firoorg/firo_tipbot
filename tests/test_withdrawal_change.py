import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import tipbot
from test_safety import MemoryCollection, ready_bot


class WithdrawalChangeSolvencyTests(unittest.TestCase):
    @staticmethod
    def bot_with_spend(confirmations, chainlock, available):
        bot = ready_bot()
        bot.col_users = MemoryCollection([{
            "_id": 1, "Balance": 9.0, "Locked": 1.0,
        }])
        bot.col_senders = MemoryCollection([{
            "_id": "withdraw:1", "schemaVersion": 2,
            "status": "pending", "user_id": 1,
            "locked_amount": 1.0, "txId": "spend-tx",
        }])
        bot.col_state = MemoryCollection([{
            "_id": "admin_funding_address", "address": "admin-spark",
        }])
        bot.wallet_api = SimpleNamespace(
            get_spark_balance=Mock(return_value={"availableBalance": available}),
            list_confirmed_unspent=Mock(return_value=[]),
            get_txs_list=Mock(return_value={"result": [{
                "txid": "spend-tx", "category": "spend",
                "confirmations": confirmations, "chainlock": chainlock,
                "abandoned": False,
            }], "error": None}),
            listsparkmints=Mock(return_value={"result": [{
                "txid": "spend-tx", "isUsed": False,
                "nHeight": confirmations, "amount": 9.0,
            }], "error": None}),
            list_spark_addresses=Mock(return_value={"admin-spark"}),
        )
        bot.send_to_logs = Mock(return_value=True)
        return bot

    def test_zero_confirmation_spend_gap_is_provisional(self):
        bot = self.bot_with_spend(0, False, 0)

        with self.assertRaises(tipbot.FundingShortfall) as raised:
            bot.verify_solvency()

        message = str(raised.exception)
        self.assertIn("A wallet spend is unconfirmed", message)
        self.assertIn("provisional coverage gap: 9.00000000 FIRO", message)
        self.assertNotIn("shortfall: 9.00000000 FIRO", message)
        self.assertIn("Recheck after finality before topping up", message)
        self.assertNotIn("Send at least", message)
        bot.wallet_api.listsparkmints.assert_not_called()

    def test_nonfinal_spend_change_is_excluded_from_available_balance(self):
        for confirmations, chainlock in ((1, False), (2, False)):
            with self.subTest(confirmations=confirmations, chainlock=chainlock):
                bot = self.bot_with_spend(confirmations, chainlock, 900_000_000)
                # Firo can return one spend entry per external output.
                entry = bot.wallet_api.get_txs_list.return_value["result"][0]
                bot.wallet_api.get_txs_list.return_value["result"].append(dict(entry))

                with self.assertRaises(tipbot.FundingShortfall) as raised:
                    bot.verify_solvency()

                message = str(raised.exception)
                self.assertIn("provisional coverage gap: 9.00000000 FIRO", message)
                self.assertIn("9.00000000 FIRO of wallet-owned Spark outputs", message)
                self.assertNotIn("Send at least", message)
                bot.wallet_api.listsparkmints.assert_called_once_with()

    def test_spent_parent_change_is_not_subtracted_twice(self):
        bot = self.bot_with_spend(2, False, 1_800_000_000)
        bot.col_users.documents[1]["BalanceGroth"] = 1_800_000_000
        bot.wallet_api.get_txs_list.return_value["result"].append({
            "txid": "child-spend", "category": "spend",
            "confirmations": 1, "chainlock": False,
        })
        bot.wallet_api.listsparkmints.return_value = {"result": [
            {"txid": "spend-tx", "isUsed": True,
             "nHeight": 2, "amount": 9.0},
            {"txid": "child-spend", "isUsed": False,
             "nHeight": 1, "amount": 8.0},
        ], "error": None}

        with self.assertRaises(tipbot.FundingShortfall):
            bot.verify_solvency()

        self.assertEqual(
            bot.col_state.documents["funding_shortfall_alert"]["pending_groth"],
            800_000_000,
        )

    def test_spent_nonfinal_receive_is_not_subtracted_from_other_assets(self):
        bot = self.bot_with_spend(2, True, 900_000_000)
        bot.wallet_api.get_txs_list.return_value["result"] = [{
            "txid": "deposit-tx", "category": "receive",
            "confirmations": 1, "chainlock": False,
        }]
        bot.wallet_api.listsparkmints.return_value = {"result": [{
            "txid": "deposit-tx", "isUsed": True,
            "nHeight": 1, "amount": 9.0,
        }], "error": None}

        bot.verify_solvency()
        bot.wallet_api.listsparkmints.assert_called_once_with()

    def test_final_spend_change_covers_balance_but_real_deficit_requests_topup(self):
        bot = self.bot_with_spend(2, True, 900_000_000)
        bot.verify_solvency()
        bot.wallet_api.listsparkmints.assert_not_called()

        bot.wallet_api.get_spark_balance.return_value = {"availableBalance": 800_000_000}
        with self.assertRaises(tipbot.FundingShortfall) as raised:
            bot.verify_solvency()
        self.assertIn("Send at least 1.00000000 FIRO", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

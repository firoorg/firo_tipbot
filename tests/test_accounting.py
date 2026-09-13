import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import tipbot
from api.firo_wallet_api import FiroTransportError
from test_safety import MemoryCollection, transaction_runner


class AccountingTests(unittest.TestCase):
    @staticmethod
    def deposit_bot():
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.col_users = MemoryCollection([{"_id": 1, "Balance": 10.0}])
        bot.col_txs = MemoryCollection(
            [
                {
                    "_id": "deposit:tx:address",
                    "txId": "tx",
                    "type": "deposit",
                    "eventVersion": 2,
                    "status": "confirmed",
                    "address": "address",
                    "user_id": 1,
                    "amount": 1.0,
                }
            ]
        )
        bot.run_transaction = transaction_runner(bot.col_users, bot.col_txs)
        bot.wallet_api = SimpleNamespace(get_tx_status=Mock())
        return bot

    def test_missing_conflicted_deposit_reverses_and_recovers_once(self):
        bot = self.deposit_bot()
        bot.wallet_api.get_tx_status.return_value = {
            "result": {"confirmations": -1, "chainlock": False},
            "error": None,
        }

        for _ in range(2):
            bot.reconcile_deposit_confirmations({})

        self.assertEqual(bot.col_users.documents[1]["BalanceGroth"], 900_000_000)
        self.assertEqual(bot.col_users.documents[1]["Balance"], 9.0)
        self.assertEqual(
            bot.col_txs.documents["deposit:tx:address"]["status"], "reversed"
        )
        bot.wallet_api.get_tx_status.assert_called_with("tx")

        recovered = {"tx": [{"confirmations": 2, "chainlock": True}]}
        for _ in range(2):
            bot.reconcile_deposit_confirmations(recovered)

        self.assertEqual(bot.col_users.documents[1]["BalanceGroth"], 1_000_000_000)
        self.assertEqual(bot.col_users.documents[1]["Balance"], 10.0)
        self.assertEqual(
            bot.col_txs.documents["deposit:tx:address"]["status"], "confirmed"
        )

    def test_missing_deposit_rpc_failure_preserves_credit(self):
        for transport_failure in (False, True):
            with self.subTest(transport_failure=transport_failure):
                bot = self.deposit_bot()
                if transport_failure:
                    bot.wallet_api.get_tx_status.side_effect = FiroTransportError(
                        "RPC unavailable"
                    )
                else:
                    bot.wallet_api.get_tx_status.return_value = {
                        "result": None,
                        "error": {"code": -5, "message": "Transaction not found"},
                    }

                with self.assertRaises(RuntimeError):
                    bot.reconcile_deposit_confirmations({})

                self.assertEqual(
                    bot.col_users.documents[1]["BalanceGroth"], 1_000_000_000
                )
                self.assertEqual(bot.col_users.documents[1]["Balance"], 10.0)
                self.assertEqual(
                    bot.col_txs.documents["deposit:tx:address"]["status"],
                    "confirmed",
                )

    def test_completed_money_migration_does_not_access_account_collections(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        state = {"_id": "money_schema", "version": 1, "status": "complete"}
        bot.col_state = MemoryCollection([state])

        bot.migrate_money_schema()

        self.assertEqual(bot.col_state.documents["money_schema"], state)

    def test_legacy_money_migration_creates_missing_integer_fields(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.col_users = MemoryCollection(
            [{"_id": 1, "Balance": 0.30000000000000004, "Locked": 0.0}]
        )
        # The shared fake normally populates integer fields automatically.
        del bot.col_users.documents[1]["BalanceGroth"]
        del bot.col_users.documents[1]["LockedGroth"]
        bot.col_senders = MemoryCollection()
        bot.col_envelopes = MemoryCollection()
        bot.col_txs = MemoryCollection()
        bot.col_state = MemoryCollection()
        bot.wallet_api = SimpleNamespace(get_tx_status=Mock())
        bot.run_transaction = transaction_runner(
            bot.col_users, bot.col_senders, bot.col_envelopes, bot.col_txs
        )

        bot.migrate_money_schema()

        user = bot.col_users.documents[1]
        self.assertEqual((user["BalanceGroth"], user["Balance"]), (30_000_000, 0.3))
        self.assertEqual((user["LockedGroth"], user["Locked"]), (0, 0.0))
        self.assertEqual(user["moneySchemaVersion"], 1)
        self.assertFalse(user["IsWithdraw"])
        self.assertEqual(bot.col_state.documents["money_schema"]["status"], "complete")


if __name__ == "__main__":
    unittest.main()

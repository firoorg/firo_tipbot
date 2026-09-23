import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import tipbot
from api.firo_wallet_api import FiroTransportError
from test_safety import MemoryCollection, ready_bot, transaction_runner


class AccountingTests(unittest.TestCase):
    @staticmethod
    def deposit_bot():
        bot = ready_bot()
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
        bot.send_to_logs = Mock()
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

    def test_reorg_deficit_pauses_recipient_withdrawals(self):
        bot = self.deposit_bot()
        bot.col_users.documents[1].update(Balance=0.0, BalanceGroth=0)
        bot.col_users.insert_one({"_id": 2, "Balance": 1.0, "BalanceGroth": 100_000_000})
        bot.wallet_api.get_tx_status.return_value = {
            "result": {"confirmations": -1, "chainlock": False}, "error": None,
        }

        bot.reconcile_deposit_confirmations({})
        self.assertEqual(bot.col_users.documents[1]["BalanceGroth"], -100_000_000)
        self.assertTrue(bot.outgoing_paused())

        bot.user_id = 2
        bot.send_message = Mock()
        bot.wallet_api.validate_address = Mock()
        bot.wallet_api.spendspark = Mock()
        bot.withdraw_coins("external", "0.5")
        bot.wallet_api.validate_address.assert_not_called()
        bot.wallet_api.spendspark.assert_not_called()

    def test_missing_deposit_rpc_failure_preserves_credit_and_continues(self):
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

                bot.wallet_api.get_txs_list = Mock(
                    return_value={"result": [], "error": None}
                )
                bot.reconcile_withdrawals = Mock()
                bot.update_balance()

                self.assertEqual(
                    bot.col_users.documents[1]["BalanceGroth"], 1_000_000_000
                )
                self.assertEqual(bot.col_users.documents[1]["Balance"], 10.0)
                self.assertEqual(
                    bot.col_txs.documents["deposit:tx:address"]["status"],
                    "confirmed",
                )
                self.assertTrue(bot.outgoing_paused())
                event = bot.col_txs.documents["deposit:tx:address"]
                self.assertTrue(event["review_required"])
                self.assertIn(
                    "RPC unavailable" if transport_failure else "Transaction not found",
                    event["review_reason"],
                )
                bot.reconcile_withdrawals.assert_called_once_with([])

                other = dict(event, _id="other-deposit", txId="other-tx")
                bot.col_txs.insert_one(other)
                transactions = [{"txid": "other-tx", "confirmations": -1}]
                bot.wallet_api.get_txs_list.return_value["result"] = transactions
                for _ in range(2):
                    bot.update_balance()
                self.assertEqual(bot.col_users.documents[1]["BalanceGroth"], 900_000_000)
                self.assertEqual(bot.col_txs.documents["other-deposit"]["status"], "reversed")
                self.assertEqual(bot.reconcile_withdrawals.call_count, 3)
                bot.send_to_logs.assert_called_once()

                bot.wallet_api.get_tx_status.side_effect = None
                bot.wallet_api.get_tx_status.return_value = {
                    "result": {"confirmations": -1}, "error": None
                }
                for _ in range(2):
                    bot.update_balance()
                self.assertEqual(bot.col_users.documents[1]["BalanceGroth"], 800_000_000)
                self.assertEqual(event["status"], "reversed")
                self.assertFalse(bot.outgoing_paused())
                self.assertNotIn("review_required", event)
                self.assertNotIn("review_reason", event)
                self.assertNotIn("reviewReportedAt", event)

    def test_empty_spark_output_is_retried_before_marking_scan_complete(self):
        bot = ready_bot()
        outputs = []
        bot.wallet_api = SimpleNamespace(get_spark_coin_address=lambda txid: outputs)
        bot.col_users = MemoryCollection(
            [{"_id": 1, "Address": ["address"], "Balance": 0.0}]
        )
        bot.col_txs = MemoryCollection()
        bot.col_state = MemoryCollection()
        bot.run_transaction = transaction_runner(bot.col_users, bot.col_txs)
        bot.create_receive_tips_image = Mock()
        transaction = {"txid": "tx", "category": "receive", "confirmations": 2}

        bot.apply_deposits(transaction)
        self.assertNotIn("deposit-scan:tx", bot.col_txs.documents)
        outputs.append({"address": "address", "amount": 0})
        bot.apply_deposits(transaction)
        self.assertNotIn("deposit-scan:tx", bot.col_txs.documents)
        outputs.append({"address": "address", "amount": 1.0})
        bot.apply_deposits(transaction)
        self.assertEqual(bot.col_users.documents[1]["BalanceGroth"], 100_000_000)
        self.assertIn("deposit-scan:tx", bot.col_txs.documents)

    def test_failed_reconciliation_pauses_outgoing_funds(self):
        bot = ready_bot()
        bot.reconciliation_ok = True
        bot.col_txs = MemoryCollection()
        bot.wallet_api = SimpleNamespace(
            get_txs_list=Mock(side_effect=FiroTransportError("wallet offline"))
        )

        with self.assertRaises(FiroTransportError):
            bot.update_balance()
        self.assertTrue(bot.outgoing_paused())

    def test_completed_money_migration_does_not_access_account_collections(self):
        bot = ready_bot()
        state = {"_id": "money_schema", "version": 1, "status": "complete"}
        bot.col_state = MemoryCollection([state])

        bot.migrate_money_schema()

        self.assertEqual(bot.col_state.documents["money_schema"], state)

    def test_legacy_money_migration_creates_missing_integer_fields(self):
        bot = ready_bot()
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

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from api.firo_wallet_api import FiroTransportError, FiroWalletAPI
from test_safety import MemoryCollection, ready_bot


class AdminFundingTests(unittest.TestCase):
    def test_wallet_address_listing_rejects_unknown_rpc_shape(self):
        api = FiroWalletAPI("http://unused")
        api._result = Mock(return_value={"0": "admin-spark", "1": "user-spark"})
        self.assertEqual(api.list_spark_addresses(), {"admin-spark", "user-spark"})
        api._result.return_value = ["admin-spark"]
        with self.assertRaises(FiroTransportError):
            api.list_spark_addresses()
        api._result.return_value = []
        api.list_confirmed_unspent()
        api._result.assert_called_with("listunspent", [2, 9999999, [], False])

    def test_admin_address_is_created_once_and_reused_after_restart(self):
        bot = ready_bot()
        bot.col_users = MemoryCollection([{"_id": 1, "Address": ["user-spark"]}])
        bot.wallet_api = SimpleNamespace(
            create_user_wallet=Mock(return_value="admin-spark"),
            get_default_address=Mock(return_value=["shared-spark"]),
            list_spark_addresses=Mock(return_value={"admin-spark", "user-spark"}),
        )

        self.assertEqual(bot.ensure_admin_funding_address(), "admin-spark")
        self.assertEqual(
            bot.col_state.documents["admin_funding_address"]["address"],
            "admin-spark",
        )

        restarted = ready_bot()
        restarted.col_state = bot.col_state
        restarted.col_users = bot.col_users
        restarted.wallet_api = bot.wallet_api
        self.assertEqual(restarted.ensure_admin_funding_address(), "admin-spark")
        bot.wallet_api.create_user_wallet.assert_called_once_with()

    def test_admin_address_rejects_user_default_and_retired_addresses(self):
        for generated in ("user-spark", "shared-spark", "retired-spark"):
            with self.subTest(generated=generated):
                bot = ready_bot()
                bot.col_users = MemoryCollection([{"_id": 1, "Address": ["user-spark"]}])
                bot.col_state = MemoryCollection([{
                    "_id": "retired_deposit_addresses", "addresses": ["retired-spark"],
                }])
                bot.wallet_api = SimpleNamespace(
                    create_user_wallet=Mock(return_value=generated),
                    get_default_address=Mock(return_value=["shared-spark"]),
                    list_spark_addresses=Mock(return_value={generated, "user-spark"}),
                )

                with self.assertRaises(RuntimeError):
                    bot.ensure_admin_funding_address()
                self.assertNotIn("admin_funding_address", bot.col_state.documents)

    def test_existing_user_address_must_belong_to_active_wallet(self):
        bot = ready_bot()
        bot.col_users = MemoryCollection([{"_id": 1, "Address": ["old-wallet-address"]}])
        bot.wallet_api = SimpleNamespace(
            create_user_wallet=Mock(),
            get_default_address=Mock(return_value=["new-default"]),
            list_spark_addresses=Mock(return_value={"new-default"}),
        )

        with self.assertRaisesRegex(RuntimeError, "existing user deposit address"):
            bot.ensure_admin_funding_address()

        bot.wallet_api.create_user_wallet.assert_not_called()

    def test_saved_admin_address_missing_from_wallet_is_not_announced(self):
        bot = ready_bot()
        bot.col_state = MemoryCollection([{
            "_id": "admin_funding_address", "address": "admin-spark",
        }])
        bot.wallet_api = SimpleNamespace(
            create_user_wallet=Mock(),
            get_default_address=Mock(return_value=["shared-spark"]),
            list_spark_addresses=Mock(return_value={"other-spark"}),
        )
        bot.send_to_logs = Mock(return_value=True)

        with self.assertRaisesRegex(RuntimeError, "not owned by the active wallet"):
            bot.ensure_admin_funding_address()

        bot.wallet_api.create_user_wallet.assert_not_called()
        bot.send_to_logs.assert_not_called()
        self.assertNotIn("announced_at", bot.col_state.documents["admin_funding_address"])

    def test_failed_initial_announcement_is_retried_after_restart(self):
        bot = ready_bot()
        bot.wallet_api = SimpleNamespace(
            create_user_wallet=Mock(return_value="admin-spark"),
            get_default_address=Mock(return_value=["shared-spark"]),
            list_spark_addresses=Mock(return_value={"admin-spark"}),
        )
        bot.send_to_logs = Mock(return_value=False)

        self.assertEqual(bot.ensure_admin_funding_address(), "admin-spark")
        self.assertNotIn("announced_at", bot.col_state.documents["admin_funding_address"])

        restarted = ready_bot()
        restarted.col_state = bot.col_state
        restarted.wallet_api = bot.wallet_api
        restarted.send_to_logs = Mock(return_value=True)
        self.assertEqual(restarted.ensure_admin_funding_address(), "admin-spark")

        bot.wallet_api.create_user_wallet.assert_called_once_with()
        bot.send_to_logs.assert_called_once_with("Admin-only Spark funding address: admin-spark")
        restarted.send_to_logs.assert_called_once_with(
            "Admin-only Spark funding address: admin-spark"
        )
        self.assertIn("announced_at", bot.col_state.documents["admin_funding_address"])

    def test_solvency_error_gives_exact_shortfall_and_admin_address(self):
        bot = ready_bot()
        bot.col_users = MemoryCollection([
            {"_id": 1, "Balance": 2.25, "Locked": 0.5},
            {"_id": 2, "Balance": -0.25, "Locked": 0.0},
        ])
        bot.col_envelopes = MemoryCollection([{"_id": "envelope", "remains": 0.5}])
        bot.col_senders = MemoryCollection([
            {"_id": "reserved", "schemaVersion": 2, "status": "reserved", "user_id": 1, "locked_amount": 0.35},
            {"_id": "rejected", "schemaVersion": 2, "status": "rejected", "user_id": 1, "locked_amount": 0.15},
        ])
        bot.col_state = MemoryCollection([{
            "_id": "admin_funding_address", "address": "admin-spark",
        }])
        bot.wallet_api = SimpleNamespace(
            get_spark_balance=Mock(return_value={"availableBalance": 100_000_000}),
            get_txs_list=Mock(return_value={"result": [], "error": None}),
            list_confirmed_unspent=Mock(return_value=[
                {"spendable": True, "amount": 0.75, "txid": "transparent-tx"},
                {"spendable": False, "amount": 2.0},
            ]),
            get_tx_status=Mock(return_value={
                "result": {"confirmations": 2, "chainlock": True},
                "error": None,
            }),
            get_default_address=Mock(return_value=["shared-spark"]),
            list_spark_addresses=Mock(return_value={"admin-spark"}),
        )

        with self.assertRaises(RuntimeError) as error:
            bot.verify_solvency()

        self.assertIn("1.50000000 FIRO", str(error.exception))
        self.assertIn("admin-spark", str(error.exception))

    def test_shortfall_does_not_recommend_address_lost_from_wallet(self):
        bot = ready_bot()
        bot.col_users = MemoryCollection([{"_id": 1, "Balance": 1.0, "Locked": 0.0}])
        bot.col_state = MemoryCollection([{
            "_id": "admin_funding_address", "address": "old-admin-spark",
        }])
        bot.wallet_api = SimpleNamespace(
            get_spark_balance=Mock(return_value={"availableBalance": 0}),
            get_txs_list=Mock(return_value={"result": [], "error": None}),
            list_confirmed_unspent=Mock(return_value=[]),
            list_spark_addresses=Mock(return_value=set()),
        )

        with self.assertRaises(RuntimeError) as error:
            bot.verify_solvency()

        self.assertIn("1.00000000 FIRO", str(error.exception))
        self.assertIn("do not send funds", str(error.exception))
        self.assertNotIn("old-admin-spark", str(error.exception))

    def test_unresolved_withdrawal_and_orphan_remain_liabilities(self):
        bot = ready_bot()
        bot.col_users = MemoryCollection([{
            "_id": 1, "Balance": 9.0, "Locked": 1.0,
        }])
        bot.col_senders = MemoryCollection([{
            "_id": "uncertain", "user_id": 1, "schemaVersion": 2,
            "status": "unknown", "locked_amount": 1.0,
        }])
        bot.col_txs = MemoryCollection([{
            "_id": "deposit-orphan:tx:unknown", "type": "deposit-orphan",
            "status": "review_required", "amount": 1.0,
        }])
        bot.wallet_api = SimpleNamespace(
            get_spark_balance=Mock(return_value={"availableBalance": 900_000_000}),
            list_confirmed_unspent=Mock(return_value=[]),
            get_txs_list=Mock(return_value={"result": [], "error": None}),
        )

        with self.assertRaisesRegex(RuntimeError, "2.00000000 FIRO"):
            bot.verify_solvency()
        self.assertTrue(bot.outgoing_paused())

    def test_pending_user_spark_receipt_cannot_hide_funding_gap(self):
        bot = ready_bot()
        bot.col_users = MemoryCollection([{
            "_id": 1, "Address": ["user-spark"], "Balance": 1.0,
            "Locked": 0.0,
        }])
        bot.wallet_api = SimpleNamespace(
            get_spark_balance=Mock(return_value={"availableBalance": 100_000_000}),
            list_confirmed_unspent=Mock(return_value=[]),
            get_txs_list=Mock(return_value={"result": [{
                "txid": "pending-user", "category": "receive",
                "confirmations": 1, "chainlock": False,
            }], "error": None}),
            list_spark_addresses=Mock(return_value={"user-spark"}),
            listsparkmints=Mock(return_value={"result": [{
                "txid": "pending-user", "isUsed": False,
                "nHeight": 1, "amount": 0.8,
            }], "error": None}),
        )

        with self.assertRaisesRegex(RuntimeError, "0.80000000 FIRO"):
            bot.verify_solvency()

    def test_transparent_output_without_chainlock_cannot_cover_gap(self):
        bot = ready_bot()
        bot.col_users = MemoryCollection([{
            "_id": 1, "Balance": 1.0, "Locked": 0.0,
        }])
        bot.wallet_api = SimpleNamespace(
            get_spark_balance=Mock(return_value={"availableBalance": 0}),
            list_confirmed_unspent=Mock(return_value=[{
                "spendable": True, "amount": 1.0, "txid": "transparent-tx",
            }]),
            get_tx_status=Mock(return_value={
                "result": {"confirmations": 2, "chainlock": False},
                "error": None,
            }),
            get_txs_list=Mock(return_value={"result": [], "error": None}),
        )

        with self.assertRaisesRegex(RuntimeError, "1.00000000 FIRO"):
            bot.verify_solvency()

        bot.wallet_api.get_tx_status.return_value = {
            "result": {"confirmations": 2, "chainlock": True},
            "error": None,
        }
        bot.verify_solvency()

    def test_admin_topup_is_scanned_once_without_user_credit_or_orphan(self):
        bot = ready_bot()
        bot.col_users = MemoryCollection([{
            "_id": 1, "Address": ["user-spark"], "Balance": 0.0,
        }])
        bot.col_state = MemoryCollection([{
            "_id": "admin_funding_address", "address": "admin-spark",
        }])
        bot.wallet_api = SimpleNamespace(
            get_spark_coin_address=Mock(return_value=[
                {"address": "admin-spark", "amount": 1.5},
            ]),
            get_default_address=Mock(return_value=["shared-spark"]),
        )
        bot.send_to_logs = Mock()
        bot.create_receive_tips_image = Mock()
        transaction = {
            "txid": "fund-tx", "category": "receive", "confirmations": 2,
            "chainlock": True,
        }

        bot.apply_deposits(transaction)
        bot.apply_deposits(transaction)

        self.assertEqual(bot.col_users.documents[1]["BalanceGroth"], 0)
        self.assertNotIn("deposit-orphan:fund-tx:admin-spark", bot.col_txs.documents)
        scan = bot.col_txs.documents["deposit-scan:fund-tx"]
        self.assertEqual(scan["status"], "complete")
        self.assertFalse(scan["orphaned"])
        bot.wallet_api.get_spark_coin_address.assert_called_once_with("fund-tx")
        bot.create_receive_tips_image.assert_not_called()
        self.assertFalse(any(
            "ownership review" in str(call) for call in bot.send_to_logs.call_args_list
        ))

    def test_failed_admin_topup_notice_is_retried_from_scan_record(self):
        bot = ready_bot()
        bot.col_state = MemoryCollection([{
            "_id": "admin_funding_address", "address": "admin-spark",
        }])
        bot.wallet_api = SimpleNamespace(
            get_spark_coin_address=Mock(return_value=[
                {"address": "admin-spark", "amount": 1.5},
            ]),
        )
        bot.send_to_logs = Mock(side_effect=[False, True])
        transaction = {"txid": "fund-tx"}

        bot.apply_deposits(transaction)
        scan = bot.col_txs.documents["deposit-scan:fund-tx"]
        self.assertNotIn("admin_funding_announced_at", scan)

        bot.apply_deposits(transaction)
        self.assertIn("admin_funding_announced_at", scan)
        bot.apply_deposits(transaction)

        self.assertEqual(bot.send_to_logs.call_count, 2)
        bot.wallet_api.get_spark_coin_address.assert_called_once_with("fund-tx")

    def test_confirmed_admin_topup_resumes_reconciliation(self):
        bot = ready_bot()
        bot.col_users = MemoryCollection([{
            "_id": 1, "Address": ["user-spark"], "Balance": 1.0, "Locked": 0.0,
        }])
        bot.col_state = MemoryCollection([{
            "_id": "admin_funding_address", "address": "admin-spark",
        }])
        bot.wallet_api = SimpleNamespace(
            get_txs_list=Mock(return_value={"result": [], "error": None}),
            list_spark_spends=Mock(return_value=[]),
            get_spark_balance=Mock(return_value={"availableBalance": 20_000_000}),
            list_confirmed_unspent=Mock(return_value=[]),
            get_spark_coin_address=Mock(return_value=[
                {"address": "admin-spark", "amount": 0.8},
            ]),
            listsparkmints=Mock(return_value={"result": [{
                "txid": "fund-tx", "isUsed": False,
                "nHeight": 1, "amount": 0.8,
            }], "error": None}),
            list_spark_addresses=Mock(return_value={"admin-spark"}),
        )
        bot.send_to_logs = Mock(return_value=True)

        with self.assertRaisesRegex(RuntimeError, "0.80000000 FIRO"):
            bot.update_balance()
        self.assertTrue(bot.outgoing_paused())

        bot.wallet_api.get_txs_list.return_value = {
            "result": [{
                "txid": "fund-tx", "category": "receive", "confirmations": 1,
                "chainlock": False,
            }],
            "error": None,
        }
        bot.wallet_api.get_spark_balance.return_value = {
            "availableBalance": 100_000_000,
        }
        with self.assertRaisesRegex(RuntimeError, "pending two confirmations"):
            bot.update_balance()
        self.assertTrue(bot.outgoing_paused())
        self.assertNotIn("deposit-scan:fund-tx", bot.col_txs.documents)

        bot.wallet_api.get_txs_list.return_value = {
            "result": [{
                "txid": "fund-tx", "category": "receive", "confirmations": 2,
                "chainlock": True,
            }],
            "error": None,
        }
        bot.update_balance()

        self.assertFalse(bot.outgoing_paused())
        self.assertEqual(bot.col_users.documents[1]["BalanceGroth"], 100_000_000)
        self.assertNotIn("deposit-orphan:fund-tx:admin-spark", bot.col_txs.documents)
        self.assertEqual(bot.col_txs.documents["deposit-scan:fund-tx"]["status"], "complete")
        self.assertEqual(
            bot.col_state.documents["funding_shortfall_alert"]["shortfall_groth"], 0,
        )


if __name__ == "__main__":
    unittest.main()

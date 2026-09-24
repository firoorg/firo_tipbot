import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import tipbot
from api.firo_wallet_api import FiroTransportError, FiroWalletAPI
from test_safety import MemoryCollection, ready_bot, transaction_runner


class WithdrawalAccountingTests(unittest.TestCase):
    @staticmethod
    def withdrawal_bot(error=None):
        bot = ready_bot()
        bot.user_id = 1
        bot.new_message = SimpleNamespace(update_id=50)
        bot.col_users = MemoryCollection([
            {"_id": 1, "Balance": 10.0, "Locked": 0.0, "IsWithdraw": False}
        ])
        bot.col_senders = MemoryCollection()
        bot.run_transaction = transaction_runner(bot.col_users, bot.col_senders)
        bot.send_message = Mock()
        bot.send_to_logs = Mock()
        bot.withdraw_image = Mock()
        bot.wallet_api = SimpleNamespace(
            validate_address=Mock(return_value={
                "result": {"isvalidSpark": True, "ismine": False}, "error": None,
            }),
            spendspark=Mock(return_value={"result": "tx50", "error": error}),
        )
        return bot

    def test_generic_rpc_errors_keep_funds_reserved_and_never_retry(self):
        for code in (-4, -1, -32700):
            with self.subTest(code=code):
                bot = self.withdrawal_bot({"code": code, "message": "Wallet error"})
                bot.withdraw_coins("external", "1")
                bot.withdraw_coins("external", "1")

                user = bot.col_users.documents[1]
                self.assertEqual(user["BalanceGroth"], 900_000_000)
                self.assertEqual(user["LockedGroth"], 100_000_000)
                self.assertTrue(user["IsWithdraw"])
                self.assertEqual(bot.col_senders.documents["withdraw:50"]["status"], "unknown")
                self.assertEqual(bot.wallet_api.spendspark.call_count, 1)
                bot.withdraw_image.assert_not_called()

    def test_recorded_rejection_survives_interrupted_refund(self):
        bot = self.withdrawal_bot({"code": -13, "message": "Wallet locked"})
        with patch.object(bot, "refund_withdrawal", side_effect=RuntimeError("DB interrupted")):
            with self.assertRaisesRegex(RuntimeError, "DB interrupted"):
                bot.withdraw_coins("external", "1")

        self.assertEqual(bot.col_senders.documents["withdraw:50"]["status"], "rejected")
        bot.reconcile_withdrawals([])
        bot.reconcile_withdrawals([])
        user = bot.col_users.documents[1]
        self.assertEqual((user["BalanceGroth"], user["LockedGroth"]), (1_000_000_000, 0))
        self.assertFalse(user["IsWithdraw"])
        self.assertEqual(bot.col_senders.documents["withdraw:50"]["status"], "failed")
        self.assertEqual(bot.wallet_api.spendspark.call_count, 1)

    def test_withdrawal_wire_amount_keeps_every_groth(self):
        bot = self.withdrawal_bot()
        bot.col_users.documents[1].update(Balance=100_000_000.0, BalanceGroth=10_000_000_000_000_000)
        bot.withdraw_coins("external", "99999999.00200001")
        bot.wallet_api.spendspark.assert_called_once_with(
            "external", "99999999.00000001", "", subtract_fee=True,
        )
        self.assertEqual(bot.col_users.documents[1]["BalanceGroth"], 99_799_999)
        self.assertEqual(bot.col_users.documents[1]["LockedGroth"], 9_999_999_900_200_001)

    def test_known_withdrawal_id_resumes_without_another_broadcast(self):
        bot = self.withdrawal_bot()
        bot.withdraw_coins("external", "1")
        sender = bot.col_senders.documents["withdraw:50"]
        sender["status"] = "unknown"
        bot.recover_ambiguous_withdrawal(dict(sender), [])
        self.assertEqual(sender["status"], "pending")
        self.assertEqual(sender["txId"], "tx50")
        self.assertEqual(bot.wallet_api.spendspark.call_count, 1)

    def test_ambiguous_withdrawal_reports_changed_candidates_once(self):
        bot = self.withdrawal_bot({"code": -4, "message": "Wallet error"})
        bot.withdraw_coins("external", "1")
        bot.send_to_logs.reset_mock()
        sender = bot.col_senders.documents["withdraw:50"]
        started_at = tipbot.calendar.timegm(sender["broadcast_started_at"].utctimetuple())
        candidate = {
            "txid": "candidate1", "category": "spend", "address": "external",
            "amount": "-0.9975", "fee": "-0.0005", "time": started_at,
        }
        histories = [[], [candidate], [candidate, dict(candidate, txid="candidate2")]]
        reasons = [
            "no matching wallet transaction",
            "wallet candidates require manual verification: candidate1",
            "wallet candidates require manual verification: candidate1, candidate2",
        ]
        with patch.object(tipbot.time, "time", return_value=started_at + 600):
            for count, (history, reason) in enumerate(zip(histories, reasons), 1):
                bot.reconcile_withdrawals(history)
                self.assertEqual(sender["review_reason"], reason)
                reported_at = sender["reviewReportedAt"]
                bot.reconcile_withdrawals(list(reversed(history)))
                self.assertEqual(sender["reviewReportedAt"], reported_at)
                self.assertEqual(bot.send_to_logs.call_count, count)
                bot.send_to_logs.assert_called_with(
                    "Withdrawal withdraw:50 needs review: " + reason
                )
        self.assertTrue(sender["review_required"])
        self.assertEqual(sender["status"], "unknown")
        self.assertNotIn("txId", sender)
        self.assertEqual(bot.col_users.documents[1]["BalanceGroth"], 900_000_000)
        self.assertEqual(bot.col_users.documents[1]["LockedGroth"], 100_000_000)
        self.assertEqual(bot.wallet_api.spendspark.call_count, 1)

    def test_transaction_rpc_requires_a_valid_confirmation_count(self):
        api = FiroWalletAPI("http://unused")
        for result in (None, {}, {"confirmations": False}, {"confirmations": "2"}):
            with self.subTest(result=result), patch.object(api, "_rpc", return_value={
                "result": result, "error": None,
            }), self.assertRaises(FiroTransportError):
                api.get_tx_status("tx")

    def test_balance_reads_preserve_integer_precision(self):
        bot = self.withdrawal_bot()
        amount = 9_999_999_900_000_001
        bot.col_users.documents[1].update(Address=["deposit"], BalanceGroth=amount)
        bot.update_address_and_balance = Mock(return_value=False)
        _addresses, balance, _locked, _withdrawing = bot.get_user_data()
        self.assertEqual(tipbot.firo_to_groth(balance), amount)


class ProcessOwnershipTests(unittest.TestCase):
    @staticmethod
    def owner_bot(state=None):
        bot = ready_bot()
        bot.col_state = state if state is not None else MemoryCollection()
        bot.stop_jobs = threading.Event()
        bot.scheduler_thread = None
        return bot

    def test_second_owner_cannot_replace_first(self):
        first = self.owner_bot()
        second = self.owner_bot(first.col_state)
        with patch.object(tipbot.atexit, "register"):
            first.claim_process_ownership()
            with self.assertRaisesRegex(RuntimeError, "another bot owns"):
                second.claim_process_ownership()
        self.assertEqual(first.col_state.documents["bot_owner"]["owner_id"], first.owner_id)
        self.assertNotEqual(first.owner_id, second.owner_id)

    def test_startup_preserves_missing_id_withdrawals_for_quarantine(self):
        senders = MemoryCollection([
            {"_id": "legacy1", "txId": None, "status": "pending", "user_id": 1},
            {"_id": "legacy2", "txId": None, "status": "pending", "user_id": 2},
            {"_id": "valid", "txId": "tx", "status": "pending", "user_id": 3},
        ])

        def update_many(query, update):
            for document in senders.find(query):
                senders.update_one({"_id": document["_id"]}, update)

        def create_index(*args, **kwargs):
            # Sparse unique indexes reject repeated explicit nulls as well as IDs.
            ids = [doc["txId"] for doc in senders.documents.values() if "txId" in doc]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(senders.documents["valid"]["txId"], "tx")
            for legacy_id in ("legacy1", "legacy2"):
                self.assertNotIn("txId", senders.documents[legacy_id])
                self.assertEqual(senders.documents[legacy_id]["status"], "pending")
            raise RuntimeError("index checked")

        senders.update_many = update_many
        senders.create_index = create_index
        database = {name: Mock() for name in (
            "captcha", "commands_history", "users", "tip_logs", "envelopes", "txs", "state",
        )}
        database["senders"] = senders
        client = Mock()
        client.admin.command.return_value = {"setName": "rs0"}
        client.get_default_database.return_value = database
        with patch.object(tipbot, "SyncBot"), patch.object(tipbot, "MongoClient", return_value=client), \
                patch.object(tipbot.TipBot, "claim_process_ownership"), \
                patch.object(tipbot.TipBot, "ensure_admin_funding_address"), \
                patch.object(tipbot.TipBot, "require_offline_migration_confirmation"), \
                self.assertRaisesRegex(RuntimeError, "index checked"):
            tipbot.TipBot(Mock())

    def test_release_waits_for_worker_and_deletes_only_own_token(self):
        bot = self.owner_bot(Mock())
        bot.owner_id = "owner-token"
        bot.scheduler_thread = Mock()
        bot.scheduler_thread.is_alive.return_value = True
        bot.release_process_ownership()
        self.assertTrue(bot.stop_jobs.is_set())
        bot.col_state.delete_one.assert_not_called()

        bot.scheduler_thread.is_alive.return_value = False
        bot.release_process_ownership()
        bot.scheduler_thread.join.assert_called_with(timeout=5)
        bot.col_state.delete_one.assert_called_once_with({
            "_id": "bot_owner", "owner_id": "owner-token",
        })


if __name__ == "__main__":
    unittest.main()

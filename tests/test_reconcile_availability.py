import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from api.firo_wallet_api import FiroTransportError
from test_safety import MemoryCollection, ready_bot


class ReconcileAvailabilityTests(unittest.TestCase):
    def test_completed_withdrawals_use_current_wallet_history_without_more_rpcs(self):
        bot = ready_bot()
        bot.col_senders = MemoryCollection([
            {"_id": i, "schemaVersion": 2, "status": "completed", "txId": f"tx{i}"}
            for i in range(20)
        ])
        bot.wallet_api = SimpleNamespace(get_tx_status=Mock(side_effect=AssertionError("extra RPC")))
        bot.col_senders.update_one = Mock(side_effect=AssertionError("extra DB write"))
        history = [
            {"txid": f"tx{i}", "category": "spend", "confirmations": 5, "chainlock": True}
            for i in range(20)
        ]

        bot.reconcile_withdrawals(history)

        bot.wallet_api.get_tx_status.assert_not_called()
        bot.col_senders.update_one.assert_not_called()

    def test_completed_withdrawal_reverses_when_history_loses_finality(self):
        bot = ready_bot()
        sender = {"_id": "withdraw:1", "schemaVersion": 2, "status": "completed", "txId": "tx1"}
        bot.col_senders = MemoryCollection([sender])
        bot.wallet_api = SimpleNamespace(get_tx_status=Mock(side_effect=AssertionError("extra RPC")))
        bot.reverse_completed_withdrawal = Mock()
        bot.reconcile_withdrawals([
            {"txid": "tx1", "category": "spend", "confirmations": 1, "chainlock": False}
        ])

        bot.reverse_completed_withdrawal.assert_called_once_with(sender, 1)

    def test_missing_completed_history_falls_back_and_fails_closed(self):
        bot = ready_bot()
        bot.col_senders = MemoryCollection([
            {"_id": "withdraw:1", "schemaVersion": 2, "status": "completed", "txId": "tx1"}
        ])
        bot.wallet_api = SimpleNamespace(get_tx_status=Mock(side_effect=FiroTransportError("offline")))
        bot.report_withdrawal_once = Mock()

        with self.assertRaisesRegex(FiroTransportError, "finality could not be verified"):
            bot.reconcile_withdrawals([])
        bot.wallet_api.get_tx_status.assert_called_once_with("tx1")
        bot.report_withdrawal_once.assert_called_once()

    def test_release_waits_for_accounting_worker_before_removing_owner(self):
        bot = ready_bot()
        bot.owner_id = "owner-token"
        bot.stop_jobs = threading.Event()
        bot.col_state = Mock()
        finished = threading.Event()

        def accounting_job():
            bot.stop_jobs.wait()
            time.sleep(0.02)
            finished.set()

        bot.scheduler_thread = threading.Thread(target=accounting_job)
        bot.scheduler_thread.start()
        bot.col_state.delete_one.side_effect = lambda *_: self.assertTrue(finished.is_set())
        bot.release_process_ownership()

        self.assertTrue(finished.is_set())
        self.assertFalse(bot.scheduler_thread.is_alive())
        bot.col_state.delete_one.assert_called_once_with({
            "_id": "bot_owner", "owner_id": bot.owner_id,
        })


if __name__ == "__main__":
    unittest.main()

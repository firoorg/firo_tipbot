import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import tipbot
from test_safety import MemoryCollection, Result, transaction_runner


class EnvelopeAccountingTests(unittest.TestCase):
    def make_bot(self, status=None):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.user_id = 1
        bot.first_name = "Alice"
        bot.group_id = -100
        bot.group_username = None
        bot.message = SimpleNamespace(chat=SimpleNamespace(type="group"))
        bot.new_message = SimpleNamespace(update_id=42)
        bot.col_users = MemoryCollection(
            [{"_id": 1, "Balance": 0.0, "IsVerified": True}]
        )
        envelopes = [] if status is None else [{
            "_id": "envelope:42",
            "schemaVersion": 2,
            "status": status,
            "creator_id": 1,
            "group_id": -100,
            "group_username": None,
            "msg_id": None,
            "amount": 0.003,
            "remains": 0.003,
            "takers": [],
            "error": "Telegram send failed",
        }]
        bot.col_envelopes = MemoryCollection(envelopes)
        bot.col_tip_logs = MemoryCollection()
        bot.run_transaction = transaction_runner(
            bot.col_users, bot.col_envelopes, bot.col_tip_logs
        )
        for name in (
            "send_message", "insufficient_balance_image", "answer_call_back",
            "red_envelope_catched", "delete_tg_message", "create_send_tips_image",
            "create_receive_tips_image",
        ):
            setattr(bot, name, Mock())
        bot.red_envelope_created = Mock(return_value=77)
        return bot

    @staticmethod
    def set_claimant(bot, user_id):
        bot.user_id = user_id
        bot.new_message = SimpleNamespace(callback_query=SimpleNamespace(
            id="query:%s" % user_id,
            from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-100), message_id=77
            ),
        ))

    def test_zero_balance_replay_recovers_reserved_envelope_once(self):
        for status in ("creating", "sending", "rejected"):
            with self.subTest(status=status):
                bot = self.make_bot(status)

                bot.create_red_envelope("0.003")
                bot.create_red_envelope("0.003")

                refunded = status == "rejected"
                envelope = bot.col_envelopes.documents["envelope:42"]
                self.assertEqual(envelope["status"], "failed" if refunded else "active")
                self.assertEqual(envelope["remains_groth"], 0 if refunded else 300_000)
                self.assertEqual(
                    bot.col_users.documents[1]["BalanceGroth"],
                    300_000 if refunded else 0,
                )
                self.assertEqual(bot.red_envelope_created.call_count, 0 if refunded else 1)
                bot.insufficient_balance_image.assert_not_called()
                self.assertEqual(len(bot.col_envelopes.documents), 1)

    def test_claimant_failure_rolls_back_envelope_debit(self):
        bot = self.make_bot("active")
        bot.col_envelopes.documents["envelope:42"]["msg_id"] = 77
        self.set_claimant(bot, 2)
        before = copy.deepcopy(bot.col_envelopes.documents)

        with self.assertRaisesRegex(RuntimeError, "claimant disappeared"):
            bot.catch_envelope("envelope:42")

        self.assertEqual(bot.col_envelopes.documents, before)
        self.assertEqual(bot.col_users.documents[1]["BalanceGroth"], 0)
        self.assertEqual(bot.col_tip_logs.documents, {})
        bot.red_envelope_catched.assert_not_called()

    def test_tip_recipient_failure_rolls_back_sender_debit(self):
        bot = self.make_bot()
        bot.col_users.documents[1].update(Balance=1.0, BalanceGroth=100_000_000)
        bot.col_users.insert_one({
            "_id": 2, "Balance": 0.0, "BalanceGroth": 0,
            "IsVerified": True, "first_name": "Bob",
        })
        before = copy.deepcopy(bot.col_users.documents)
        update_one = bot.col_users.update_one

        def fail_recipient(query, update, session=None):
            if query["_id"] == 2:
                return Result(0)
            return update_one(query, update, session=session)

        with patch.object(bot.col_users, "update_one", side_effect=fail_recipient):
            with self.assertRaisesRegex(RuntimeError, "recipient disappeared"):
                bot.send_tip(2, "0.25", None, "")

        self.assertEqual(bot.col_users.documents, before)
        self.assertEqual(bot.col_tip_logs.documents, {})
        bot.create_send_tips_image.assert_not_called()
        bot.create_receive_tips_image.assert_not_called()

    def test_tip_confirmations_handle_missing_recipient_name(self):
        for profile, expected_name in (
            ({}, "2"), ({"first_name": None}, "2"),
            ({"first_name": ""}, "2"), ({"first_name": "Bob"}, "Bob"),
        ):
            with self.subTest(profile=profile):
                bot = self.make_bot()
                bot.col_users.documents[1].update(Balance=1.0, BalanceGroth=100_000_000)
                bot.col_users.insert_one({
                    "_id": 2, "Balance": 0.0, "BalanceGroth": 0,
                    "IsVerified": True, **profile,
                })

                bot.send_tip(2, "0.25", None, "thanks")
                bot.send_tip(2, "0.25", None, "thanks")

                self.assertEqual(bot.col_users.documents[1]["BalanceGroth"], 75_000_000)
                self.assertEqual(bot.col_users.documents[2]["BalanceGroth"], 25_000_000)
                self.assertEqual(len(bot.col_tip_logs.documents), 1)
                bot.create_send_tips_image.assert_called_once_with(
                    1, "0.25000000", expected_name, "thanks",
                )
                bot.create_receive_tips_image.assert_called_once_with(
                    2, "0.25000000", "Alice", "thanks",
                )

    def test_full_depletion_conserves_groth_and_claim_replay_is_noop(self):
        bot = self.make_bot()
        bot.col_users.documents[1].update(Balance=0.003, BalanceGroth=300_000)
        bot.create_red_envelope("0.003")

        for user_id in (2, 3, 4):
            bot.col_users.insert_one({
                "_id": user_id, "Balance": 0.0, "BalanceGroth": 0,
                "IsVerified": True,
            })
            self.set_claimant(bot, user_id)
            with patch.object(tipbot.secrets, "randbelow", return_value=0):
                bot.catch_envelope("envelope:42")
            snapshot = copy.deepcopy((
                bot.col_users.documents, bot.col_envelopes.documents,
                bot.col_tip_logs.documents,
            ))
            bot.catch_envelope("envelope:42")
            self.assertEqual((
                bot.col_users.documents, bot.col_envelopes.documents,
                bot.col_tip_logs.documents,
            ), snapshot)
            envelope = bot.col_envelopes.documents["envelope:42"]
            balances = sum(user["BalanceGroth"] for user in bot.col_users.documents.values())
            self.assertEqual(balances + envelope["remains_groth"], 300_000)

        self.assertEqual(envelope["status"], "ended")
        self.assertEqual(envelope["remains_groth"], 0)
        self.assertEqual(len(bot.col_tip_logs.documents), 3)
        self.assertEqual(
            sum(log["amount_groth"] for log in bot.col_tip_logs.documents.values()),
            300_000,
        )
        bot.delete_tg_message.assert_called_once_with(-100, 77)


if __name__ == "__main__":
    unittest.main()

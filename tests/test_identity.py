import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from telegram import Chat, Message, Update, User
from telegram.constants import ChatID

import tipbot
from test_safety import MemoryCollection, ready_bot


class IdentityTests(unittest.TestCase):
    @staticmethod
    def message_update(user_id, text, sender_chat=None):
        chat = Chat(id=-100, type="supergroup")
        message = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=User(id=user_id, first_name="Sender", is_bot=False),
            sender_chat=sender_chat,
            text=text,
        )
        return Update(update_id=42, message=message)

    @staticmethod
    def prepared_bot(user_id):
        bot = ready_bot()
        bot.col_users = MemoryCollection([{
            "_id": user_id, "Balance": 1.0, "IsVerified": True,
        }])
        bot.get_user_data = Mock(return_value=(["deposit"], 1.0, 0.0, False))
        bot.check_username_on_change = Mock()
        bot.action_processing = Mock()
        return bot

    def test_chat_sender_and_fake_ids_cannot_act_as_user_accounts(self):
        chat = Chat(id=-100, type="supergroup")
        for user_id, sender_chat in (
            (ChatID.ANONYMOUS_ADMIN, chat),
            (ChatID.ANONYMOUS_ADMIN, None),
            (1, chat),
        ):
            with self.subTest(user_id=user_id, sender_chat=sender_chat):
                bot = self.prepared_bot(user_id)
                update = self.message_update(user_id, "/withdraw secret 1", sender_chat)
                self.assertEqual(update.effective_user.id, user_id)

                with patch.object(tipbot.time, "sleep"):
                    self.assertTrue(bot.processing_messages([update]))

                bot.get_user_data.assert_not_called()
                bot.action_processing.assert_not_called()
                self.assertEqual(bot.col_users.documents[user_id]["BalanceGroth"], 100_000_000)

    def test_reply_to_anonymous_admin_cannot_credit_fake_id(self):
        for recipient_id in (ChatID.ANONYMOUS_ADMIN, 2):
            with self.subTest(recipient_id=recipient_id):
                bot = self.prepared_bot(1)
                bot.user_id = 1
                bot.send_message = Mock()
                bot.send_tip = Mock()
                bot.message = SimpleNamespace(reply_to_message=SimpleNamespace(
                    from_user=User(id=recipient_id, first_name="Group", is_bot=False),
                    sender_chat=Chat(id=-100, type="supergroup"),
                ))

                bot.tip_in_the_chat("0.1")

                bot.send_tip.assert_not_called()
                self.assertIn("anonymous admin", bot.send_message.call_args.args[1])

    def test_registered_fake_id_cannot_receive_tip_directly(self):
        bot = self.prepared_bot(1)
        bot.col_users.insert_one({
            "_id": ChatID.FAKE_CHANNEL, "Balance": 0.0, "IsVerified": True,
        })
        bot.user_id = 1
        bot.send_message = Mock()
        bot.run_transaction = Mock()

        bot.send_tip(ChatID.FAKE_CHANNEL, "0.1", None, "")

        bot.run_transaction.assert_not_called()
        self.assertIn("Telegram user", bot.send_message.call_args.args[1])

    def test_message_handler_does_not_log_private_text_even_on_error(self):
        bot = self.prepared_bot(1)
        bot.action_processing.side_effect = RuntimeError("secret-withdrawal-address")
        update = self.message_update(1, "/withdraw secret-withdrawal-address 1")

        with patch.object(tipbot.time, "sleep"), patch("builtins.print") as printed, \
                patch.object(tipbot.logger, "error") as logged:
            self.assertFalse(bot.processing_messages([update]))

        printed.assert_not_called()
        self.assertNotIn("secret-withdrawal-address", str(logged.call_args))
        self.assertIn("42", str(logged.call_args))

    def test_reply_tip_error_does_not_print_private_text(self):
        bot = self.prepared_bot(1)
        bot.message = SimpleNamespace(reply_to_message=SimpleNamespace(
            from_user=User(id=2, first_name="Recipient", is_bot=False),
            sender_chat=None,
        ))
        bot.send_tip = Mock(side_effect=RuntimeError("private-tip-comment"))

        with patch("builtins.print") as printed, self.assertRaises(RuntimeError):
            bot.tip_in_the_chat("0.1")

        printed.assert_not_called()


if __name__ == "__main__":
    unittest.main()

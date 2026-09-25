import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from test_safety import MemoryCollection, ready_bot


class AddressNoticeTests(unittest.TestCase):
    def test_address_replacement_does_not_send_during_migration(self):
        bot = ready_bot()
        bot.col_users = MemoryCollection([
            {"_id": user_id, "Address": ["shared"]}
            for user_id in range(1, 4)
        ])
        bot.wallet_api = SimpleNamespace(
            get_default_address=Mock(return_value=["shared"]),
            create_user_wallet=Mock(side_effect=[
                "replacement-1", "replacement-2", "replacement-3"
            ]),
        )
        bot.send_to_logs = Mock()
        bot.send_message = Mock(return_value=object())

        bot.migrate_deposit_addresses()

        bot.send_message.assert_not_called()
        bot.send_to_logs.assert_called_once()
        for user_id in range(1, 4):
            self.assertEqual(
                bot.col_users.documents[user_id]["Address"],
                [f"replacement-{user_id}"],
            )
            self.assertTrue(
                bot.col_users.documents[user_id]["AddressMigrationNoticePending"]
            )

    def test_failed_notices_retry_without_starving_other_users(self):
        bot = ready_bot()
        bot.col_users = MemoryCollection([
            {"_id": user_id, "Address": [f"address-{user_id}"],
             "AddressMigrationNoticePending": True}
            for user_id in range(1, 7)
        ])
        bot.send_message = Mock(return_value=None)

        bot.send_address_migration_notices()
        self.assertEqual(bot.send_message.call_count, 5)
        self.assertNotIn("AddressMigrationNoticeAttemptAt", bot.col_users.documents[6])
        self.assertTrue(all(
            bot.col_users.documents[user_id]["AddressMigrationNoticePending"]
            for user_id in range(1, 7)
        ))

        bot.send_message.return_value = object()
        bot.send_address_migration_notices()
        self.assertEqual(bot.send_message.call_count, 6)
        self.assertNotIn("AddressMigrationNoticePending", bot.col_users.documents[6])

        bot.col_users.documents[1]["AddressMigrationNoticeAttemptAt"] = (
            datetime.datetime.utcnow() - datetime.timedelta(minutes=6)
        )
        bot.send_address_migration_notices()
        self.assertEqual(bot.send_message.call_count, 7)
        self.assertNotIn("AddressMigrationNoticePending", bot.col_users.documents[1])
        self.assertNotIn("AddressMigrationNoticeAttemptAt", bot.col_users.documents[1])


if __name__ == "__main__":
    unittest.main()

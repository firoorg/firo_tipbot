import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from pymongo.errors import DuplicateKeyError
from telegram import Update

import tipbot
from api.firo_wallet_api import (
    FiroRPCError,
    FiroTransportError,
    FiroWalletAPI,
)


class Result:
    def __init__(self, modified_count=1):
        self.modified_count = modified_count


class MemoryCollection:
    def __init__(self, documents=()):
        self.documents = {}
        for document in documents:
            document = copy.deepcopy(document)
            for firo_field, groth_field in (
                ("Balance", "BalanceGroth"),
                ("Locked", "LockedGroth"),
                ("amount", "amount_groth"),
                ("send_amount", "send_amount_groth"),
                ("locked_amount", "locked_amount_groth"),
                ("fee", "fee_groth"),
                ("remains", "remains_groth"),
            ):
                if firo_field in document and groth_field not in document:
                    document[groth_field] = tipbot.legacy_firo_to_groth(
                        document[firo_field]
                    )
            self.documents[document["_id"]] = document

    @staticmethod
    def _value(document, key):
        values = [document]
        for part in key.split("."):
            next_values = []
            for value in values:
                if isinstance(value, list):
                    next_values.extend(
                        item.get(part)
                        for item in value
                        if isinstance(item, dict) and part in item
                    )
                elif isinstance(value, dict) and part in value:
                    next_values.append(value[part])
            values = next_values
        if not values:
            return None
        return values if len(values) > 1 else values[0]

    @staticmethod
    def _matches(document, query):
        for key, expected in query.items():
            actual = MemoryCollection._value(document, key)
            if key == "Address":
                values = actual if isinstance(actual, list) else [actual]
                if expected not in values:
                    return False
            elif isinstance(expected, dict) and "$exists" in expected:
                if (actual is not None) is not expected["$exists"]:
                    return False
            elif isinstance(expected, dict) and "$gte" in expected:
                if actual is None or actual < expected["$gte"]:
                    return False
            elif isinstance(expected, dict) and "$ne" in expected:
                values = actual if isinstance(actual, list) else [actual]
                if expected["$ne"] in values:
                    return False
            elif isinstance(expected, dict) and "$in" in expected:
                values = actual if isinstance(actual, list) else [actual]
                if not any(value in expected["$in"] for value in values):
                    return False
            elif actual != expected:
                return False
        return True

    def find_one(self, query, projection=None, session=None):
        for document in self.documents.values():
            if self._matches(document, query):
                return dict(document)
        return None

    def find(self, query):
        return [
            dict(document)
            for document in self.documents.values()
            if self._matches(document, query)
        ]

    def insert_one(self, document, session=None):
        if document["_id"] in self.documents:
            raise DuplicateKeyError("duplicate")
        self.documents[document["_id"]] = dict(document)
        return Result()

    def update_one(self, query, update, session=None, upsert=False):
        for key, document in self.documents.items():
            if not self._matches(document, query):
                continue
            for field, amount in update.get("$inc", {}).items():
                document[field] = document.get(field, 0) + amount
            document.update(update.get("$set", {}))
            for field, value in update.get("$push", {}).items():
                document.setdefault(field, []).append(copy.deepcopy(value))
            for field in update.get("$unset", {}):
                document.pop(field, None)
            self.documents[key] = document
            return Result()
        if upsert:
            document = {"_id": query["_id"]}
            document.update(update.get("$setOnInsert", {}))
            document.update(update.get("$set", {}))
            self.documents[document["_id"]] = document
            return Result()
        return Result(0)


def transaction_runner(*collections):
    def run(callback):
        snapshots = [copy.deepcopy(collection.documents) for collection in collections]
        try:
            return callback(None)
        except Exception:
            for collection, snapshot in zip(collections, snapshots):
                collection.documents = snapshot
            raise

    return run


class FakeResponse:
    def __init__(self, payload, status_error=False):
        self.payload = payload
        self.status_error = status_error

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_error:
            raise requests.HTTPError("HTTP 500")


class SafetyTests(unittest.TestCase):
    def test_amounts_are_finite_positive_and_exact(self):
        self.assertEqual(tipbot.parse_amount("1.00000001"), tipbot.Decimal("1.00000001"))
        for value in ("nan", "inf", "0", "0.000000001", "100000001"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tipbot.parse_amount(value)

    def test_rpc_paginates_and_never_omits_timeout(self):
        api = FiroWalletAPI("http://node", timeout=(3, 9))
        calls = []
        pages = [
            [{"txid": "3"}, {"txid": "2"}],
            [{"txid": "1"}],
        ]

        def post(url, json, timeout):
            calls.append((json, timeout))
            return FakeResponse({"result": pages.pop(0), "error": None})

        api.session = SimpleNamespace(post=post)
        response = api.get_txs_list(page_size=2)

        self.assertEqual([tx["txid"] for tx in response["result"]], ["3", "2", "1"])
        self.assertEqual([call[0]["params"][2] for call in calls], [0, 2])
        self.assertTrue(all(call[1] == (3, 9) for call in calls))

    def test_rpc_rejects_invalid_page_sizes_before_requesting(self):
        api = FiroWalletAPI("http://unused")
        api.session.post = Mock(side_effect=AssertionError("RPC must not be called"))
        for page_size in (0, -1, True, 1.5, "2", None):
            with self.subTest(page_size=page_size), self.assertRaisesRegex(
                ValueError, "page_size must be a positive integer"
            ):
                api.get_txs_list(page_size=page_size)
        api.session.post.assert_not_called()

    def test_rpc_error_body_survives_http_500(self):
        api = FiroWalletAPI("http://node")
        error = {"code": -4, "message": "Spark spend creation failed."}
        api.session = SimpleNamespace(
            post=lambda *args, **kwargs: FakeResponse(
                {"result": None, "error": error},
                status_error=True,
            )
        )
        self.assertEqual(api.spendspark("address", 1)["error"], error)

    def test_automint_rpc_error_is_not_silently_ignored(self):
        api = FiroWalletAPI("http://node")
        api.session = SimpleNamespace(
            post=lambda *args, **kwargs: FakeResponse(
                {
                    "result": None,
                    "error": {"code": -4, "message": "wallet locked"},
                }
            )
        )

        with self.assertRaises(FiroRPCError):
            api.automintunspent()

    def test_deposit_output_order_is_irrelevant_and_replay_safe(self):
        def run(outputs):
            bot = tipbot.TipBot.__new__(tipbot.TipBot)
            bot.wallet_api = SimpleNamespace(
                get_spark_coin_address=lambda txid: outputs
            )
            bot.col_users = MemoryCollection(
                [
                    {
                        "_id": 1,
                        "Address": ["user-address"],
                        "Balance": 9.0,
                    }
                ]
            )
            bot.col_txs = MemoryCollection()
            bot.col_state = MemoryCollection()
            bot.run_transaction = transaction_runner(bot.col_users, bot.col_txs)
            bot.create_receive_tips_image = lambda *args: None
            bot.send_to_logs = lambda *args: None

            transaction = {
                "txid": "tx1",
                "category": "receive",
                "confirmations": 2,
            }
            bot.apply_deposits(transaction)
            bot.apply_deposits(transaction)
            deposit_events = [
                event for event in bot.col_txs.documents.values()
                if event["type"] == "deposit"
            ]
            return bot.col_users.documents[1]["Balance"], len(deposit_events)

        user_output = {"address": "user-address", "amount": 0.9979}
        change_output = {"address": "wallet-change", "amount": 4}
        self.assertEqual(run([change_output, user_output]), (9.9979, 1))
        self.assertEqual(run([user_output, change_output]), (9.9979, 1))

    def test_retired_shared_address_deposit_is_recorded_for_review(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.wallet_api = SimpleNamespace(
            get_spark_coin_address=lambda txid: [
                {"address": "retired", "amount": 1.0}
            ]
        )
        bot.col_users = MemoryCollection()
        bot.col_txs = MemoryCollection()
        bot.col_state = MemoryCollection(
            [
                {
                    "_id": "retired_deposit_addresses",
                    "addresses": ["retired"],
                }
            ]
        )
        bot.send_to_logs = Mock()

        bot.apply_deposits(
            {"txid": "orphan", "category": "receive", "confirmations": 2}
        )

        orphan = bot.col_txs.documents["deposit-orphan:orphan:retired"]
        self.assertEqual(orphan["amount_groth"], 100_000_000)
        self.assertEqual(orphan["status"], "review_required")
        self.assertTrue(bot.col_txs.documents["deposit-scan:orphan"]["orphaned"])
        bot.send_to_logs.assert_called_once()

    def test_invalid_address_never_calls_spendspark(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.user_id = 1
        bot.new_message = SimpleNamespace(update_id=10)
        bot.wallet_api = SimpleNamespace(
            validate_address=lambda address: {
                "result": {"isvalid": False},
                "error": None,
            },
            spendspark=Mock(),
        )
        bot.send_message = Mock()

        bot.withdraw_coins("invalid", "1")

        bot.wallet_api.spendspark.assert_not_called()
        self.assertIn("incorrect address", bot.send_message.call_args.args[1])

    def test_tipbot_deposit_address_never_calls_spendspark(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.user_id = 1
        bot.new_message = SimpleNamespace(update_id=10)
        bot.col_users = MemoryCollection(
            [{"_id": 2, "Address": ["tipbot-deposit"]}]
        )
        bot.wallet_api = SimpleNamespace(
            validate_address=lambda address: {
                "result": {"isvalidSpark": True, "ismine": False},
                "error": None,
            },
            spendspark=Mock(),
        )
        bot.send_message = Mock()

        bot.withdraw_coins("tipbot-deposit", "1")

        bot.wallet_api.spendspark.assert_not_called()
        self.assertIn("belongs to the tipbot", bot.send_message.call_args.args[1])

    def test_withdrawal_is_reserved_before_broadcast_and_fee_is_not_subtracted_twice(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.user_id = 1
        bot.new_message = SimpleNamespace(update_id=11)
        bot.col_users = MemoryCollection(
            [
                {
                    "_id": 1,
                    "Address": ["deposit"],
                    "Balance": 10.0,
                    "Locked": 0.0,
                    "IsWithdraw": False,
                }
            ]
        )
        bot.col_senders = MemoryCollection()
        bot.run_transaction = lambda callback: callback(None)
        bot.send_message = Mock()
        bot.send_to_logs = Mock()
        bot.withdraw_image = Mock()

        def spendspark(address, amount, comment, subtract_fee):
            user = bot.col_users.documents[1]
            intent = bot.col_senders.documents["withdraw:11"]
            self.assertEqual((user["Balance"], user["Locked"]), (9.0, 1.0))
            self.assertEqual(intent["status"], "broadcasting")
            self.assertEqual(amount, "0.99800000")
            self.assertFalse(subtract_fee)
            return {"result": "txid", "error": None}

        bot.wallet_api = SimpleNamespace(
            validate_address=lambda address: {
                "result": {"isvalidSpark": True, "ismine": False},
                "error": None,
            },
            spendspark=spendspark,
        )

        bot.withdraw_coins("external", "1")

        self.assertEqual(bot.col_senders.documents["withdraw:11"]["status"], "pending")
        self.assertEqual(bot.col_senders.documents["withdraw:11"]["txId"], "txid")

    def test_start_never_resets_existing_money(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.user_id = 1
        bot.first_name = "Alice"
        bot.username = "Alice"
        bot.firo_address = ["deposit"]
        bot.col_users = MemoryCollection(
            [
                {
                    "_id": 1,
                    "Address": ["deposit"],
                    "Balance": 12.5,
                    "Locked": 0.4,
                    "IsWithdraw": True,
                    "IsVerified": True,
                }
            ]
        )
        bot.wallet_api = SimpleNamespace(create_user_wallet=Mock())
        bot.send_message = Mock()
        bot.create_wallet_image = Mock()

        bot.auth_user()

        user = bot.col_users.documents[1]
        self.assertEqual((user["Balance"], user["Locked"]), (12.5, 0.4))
        self.assertEqual(user["Address"], ["deposit"])
        bot.wallet_api.create_user_wallet.assert_not_called()

    def test_transport_failure_stays_locked_and_cannot_rebroadcast(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.user_id = 1
        bot.new_message = SimpleNamespace(update_id=12)
        bot.col_users = MemoryCollection(
            [
                {
                    "_id": 1,
                    "Address": ["deposit"],
                    "Balance": 10.0,
                    "Locked": 0.0,
                    "IsWithdraw": False,
                }
            ]
        )
        bot.col_senders = MemoryCollection()
        bot.run_transaction = transaction_runner(bot.col_users, bot.col_senders)
        bot.send_message = Mock()
        bot.send_to_logs = Mock()
        bot.withdraw_image = Mock()
        spendspark = Mock(side_effect=FiroTransportError("timeout"))
        bot.wallet_api = SimpleNamespace(
            validate_address=lambda address: {
                "result": {"isvalidSpark": True, "ismine": False},
                "error": None,
            },
            spendspark=spendspark,
        )

        bot.withdraw_coins("external", "1")
        bot.withdraw_coins("external", "1")

        user = bot.col_users.documents[1]
        self.assertEqual((user["Balance"], user["Locked"]), (9.0, 1.0))
        self.assertTrue(user["IsWithdraw"])
        self.assertEqual(bot.col_senders.documents["withdraw:12"]["status"], "unknown")
        self.assertEqual(spendspark.call_count, 1)

    def test_channel_reply_tip_is_rejected_without_stalling(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.user_id = 1
        bot.message = SimpleNamespace(
            reply_to_message=SimpleNamespace(from_user=None)
        )
        bot.send_message = Mock()

        bot.tip_in_the_chat("1")

        self.assertIn("Telegram user", bot.send_message.call_args.args[1])

    def test_explicit_rpc_rejection_refunds_once(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.user_id = 1
        bot.new_message = SimpleNamespace(update_id=13)
        bot.col_users = MemoryCollection(
            [
                {
                    "_id": 1,
                    "Address": ["deposit"],
                    "Balance": 10.0,
                    "Locked": 0.0,
                    "IsWithdraw": False,
                }
            ]
        )
        bot.col_senders = MemoryCollection()
        bot.run_transaction = transaction_runner(bot.col_users, bot.col_senders)
        bot.send_message = Mock()
        bot.send_to_logs = Mock()
        bot.withdraw_image = Mock()
        bot.wallet_api = SimpleNamespace(
            validate_address=lambda address: {
                "result": {"isvalidSpark": True, "ismine": False},
                "error": None,
            },
            spendspark=lambda *args, **kwargs: {
                "result": None,
                "error": {"code": -13, "message": "Wallet is locked."},
            },
        )

        bot.withdraw_coins("external", "1")
        bot.refund_withdrawal(
            "withdraw:13",
            "second refund",
            allowed_statuses=["broadcasting"],
        )

        user = bot.col_users.documents[1]
        self.assertEqual((user["Balance"], user["Locked"]), (10.0, 0.0))
        self.assertFalse(user["IsWithdraw"])
        self.assertEqual(bot.col_senders.documents["withdraw:13"]["status"], "failed")

    def test_replaying_tip_update_does_not_transfer_twice(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.user_id = 1
        bot.first_name = "Alice"
        bot.new_message = SimpleNamespace(update_id=14)
        bot.col_users = MemoryCollection(
            [
                {
                    "_id": 1,
                    "Balance": 10.0,
                    "IsVerified": True,
                    "first_name": "Alice",
                },
                {
                    "_id": 2,
                    "Balance": 0.0,
                    "IsVerified": True,
                    "first_name": "Bob",
                },
            ]
        )
        bot.col_tip_logs = MemoryCollection()
        bot.run_transaction = transaction_runner(bot.col_users, bot.col_tip_logs)
        bot.send_message = Mock()
        bot.insufficient_balance_image = Mock()
        bot.incorrect_parametrs_image = Mock()
        bot.create_send_tips_image = Mock()
        bot.create_receive_tips_image = Mock()

        bot.send_tip(2, 7, None, "")
        bot.send_tip(2, 7, None, "")

        self.assertEqual(bot.col_users.documents[1]["Balance"], 3.0)
        self.assertEqual(bot.col_users.documents[2]["Balance"], 7.0)
        self.assertEqual(len(bot.col_tip_logs.documents), 1)

    def test_repeated_decimal_tips_use_exact_groth_guards(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.user_id = 1
        bot.first_name = "Alice"
        bot.col_users = MemoryCollection(
            [
                {
                    "_id": 1,
                    "Balance": 0.3,
                    "IsVerified": True,
                    "first_name": "Alice",
                },
                {
                    "_id": 2,
                    "Balance": 0.0,
                    "IsVerified": True,
                    "first_name": "Bob",
                },
            ]
        )
        bot.col_tip_logs = MemoryCollection()
        bot.run_transaction = transaction_runner(
            bot.col_users, bot.col_tip_logs
        )
        bot.send_message = Mock()
        bot.insufficient_balance_image = Mock()
        bot.incorrect_parametrs_image = Mock()
        bot.create_send_tips_image = Mock()
        bot.create_receive_tips_image = Mock()

        for update_id in (30, 31, 32):
            bot.new_message = SimpleNamespace(update_id=update_id)
            bot.send_tip(2, "0.1", None, "")

        self.assertEqual(bot.col_users.documents[1]["BalanceGroth"], 0)
        self.assertEqual(bot.col_users.documents[2]["BalanceGroth"], 30_000_000)
        self.assertEqual(len(bot.col_tip_logs.documents), 3)

    def test_plain_text_cannot_claim_envelope(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.user_id = 2
        bot.new_message = SimpleNamespace(callback_query=None)
        bot.col_envelopes = MemoryCollection(
            [
                {
                    "_id": "envelope:1",
                    "schemaVersion": 2,
                    "status": "active",
                    "group_id": -100,
                    "msg_id": 50,
                    "remains": 1.0,
                }
            ]
        )
        bot.col_users = MemoryCollection(
            [{"_id": 2, "Balance": 0.0, "IsVerified": True}]
        )

        bot.catch_envelope("envelope:1")

        self.assertEqual(bot.col_users.documents[2]["Balance"], 0.0)
        self.assertEqual(bot.col_envelopes.documents["envelope:1"]["remains"], 1.0)

    def test_replayed_sending_envelope_does_not_debit_twice(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.user_id = 1
        bot.first_name = "Alice"
        bot.group_id = -100
        bot.group_username = "groupname"
        bot.message = SimpleNamespace(chat=SimpleNamespace(type="group"))
        bot.new_message = SimpleNamespace(update_id=15)
        bot.col_users = MemoryCollection(
            [{"_id": 1, "Balance": 9.0, "IsVerified": True}]
        )
        bot.col_envelopes = MemoryCollection(
            [
                {
                    "_id": "envelope:15",
                    "schemaVersion": 2,
                    "amount": 1.0,
                    "remains": 1.0,
                    "group_id": -100,
                    "group_username": "groupname",
                    "group_type": "group",
                    "creator_id": 1,
                    "msg_id": None,
                    "takers": [],
                    "status": "sending",
                }
            ]
        )
        bot.run_transaction = transaction_runner(bot.col_users, bot.col_envelopes)
        bot.red_envelope_created = Mock(return_value=77)
        bot.send_message = Mock()
        bot.insufficient_balance_image = Mock()

        bot.create_red_envelope("1")

        self.assertEqual(bot.col_users.documents[1]["Balance"], 9.0)
        envelope = bot.col_envelopes.documents["envelope:15"]
        self.assertEqual((envelope["status"], envelope["msg_id"]), ("active", 77))

    def test_withdrawal_confirmation_clears_exact_reserved_amount(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.col_users = MemoryCollection(
            [
                {
                    "_id": 1,
                    "Address": ["deposit"],
                    "Balance": 9.0,
                    "Locked": 1.0,
                    "IsWithdraw": True,
                }
            ]
        )
        sender = {
            "_id": "withdraw:16",
            "schemaVersion": 2,
            "user_id": 1,
            "address": "external",
            "amount": 1.0,
            "send_amount": 0.998,
            "locked_amount": 1.0,
            "txId": "tx16",
            "status": "pending",
        }
        bot.col_senders = MemoryCollection([sender])
        bot.col_txs = MemoryCollection()
        bot.run_transaction = transaction_runner(
            bot.col_users, bot.col_senders, bot.col_txs
        )
        bot.create_send_tips_image = Mock()
        sender = bot.col_senders.find_one({"_id": "withdraw:16"})

        bot.complete_withdrawal(sender, {"confirmations": 2, "fee": -0.0001})

        self.assertEqual(bot.col_users.documents[1]["Locked"], 0.0)
        self.assertFalse(bot.col_users.documents[1]["IsWithdraw"])
        self.assertEqual(
            bot.col_senders.documents["withdraw:16"]["status"],
            "completed",
        )

    def test_reserved_withdrawal_replay_does_not_debit_or_broadcast_twice(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.user_id = 1
        bot.new_message = SimpleNamespace(update_id=17)
        bot.col_users = MemoryCollection(
            [
                {
                    "_id": 1,
                    "Address": ["deposit"],
                    "Balance": 9.0,
                    "Locked": 1.0,
                    "IsWithdraw": True,
                }
            ]
        )
        bot.col_senders = MemoryCollection(
            [
                {
                    "_id": "withdraw:17",
                    "schemaVersion": 2,
                    "user_id": 1,
                    "address": "external",
                    "amount": 1.0,
                    "send_amount": 0.998,
                    "locked_amount": 1.0,
                    "comment": "",
                    "status": "reserved",
                }
            ]
        )
        spendspark = Mock(return_value={"result": "tx17", "error": None})
        bot.wallet_api = SimpleNamespace(
            validate_address=lambda address: {
                "result": {"isvalidSpark": True, "ismine": False},
                "error": None,
            },
            spendspark=spendspark,
        )
        bot.send_message = Mock()
        bot.send_to_logs = Mock()
        bot.withdraw_image = Mock()
        bot.insufficient_balance_image = Mock()

        bot.withdraw_coins("external", "1")
        bot.withdraw_coins("external", "1")

        user = bot.col_users.documents[1]
        self.assertEqual((user["Balance"], user["Locked"]), (9.0, 1.0))
        self.assertTrue(user["IsWithdraw"])
        self.assertEqual(
            bot.col_senders.documents["withdraw:17"]["status"],
            "pending",
        )
        self.assertEqual(spendspark.call_count, 1)

    def test_processing_messages_returns_false_when_handler_fails(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.col_users = MemoryCollection(
            [{"_id": 1, "Balance": 1.0, "IsVerified": True}]
        )
        bot.get_action = Mock(return_value=("/tip 1", False))
        bot.get_user_data = Mock(return_value=(["deposit"], 1.0, 0.0, False))
        bot.check_username_on_change = Mock()
        bot.action_processing = Mock(side_effect=RuntimeError("handler failed"))
        update = SimpleNamespace(
            update_id=18,
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-100, username=None),
            ),
            callback_query=None,
            effective_user=SimpleNamespace(
                first_name="Alice",
                username=None,
                id=1,
            ),
        )

        with patch.object(tipbot.time, "sleep"), patch.object(
            tipbot.traceback, "print_exc"
        ), patch("builtins.print"):
            result = bot.processing_messages([update])

        self.assertFalse(result)
        bot.action_processing.assert_called_once_with("/tip", ["1"])

    def test_confirmed_deposit_reverses_below_two_confirmations(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.col_users = MemoryCollection(
            [
                {
                    "_id": 1,
                    "Address": ["deposit"],
                    "Balance": 10.0,
                }
            ]
        )
        bot.col_txs = MemoryCollection(
            [
                {
                    "_id": "deposit:tx18:deposit",
                    "txId": "tx18",
                    "type": "deposit",
                    "eventVersion": 2,
                    "status": "confirmed",
                    "address": "deposit",
                    "user_id": 1,
                    "amount": 1.0,
                }
            ]
        )
        bot.run_transaction = transaction_runner(bot.col_users, bot.col_txs)
        transactions = {"tx18": [{"confirmations": 1}]}

        bot.reconcile_deposit_confirmations(transactions)
        bot.reconcile_deposit_confirmations(transactions)

        self.assertEqual(bot.col_users.documents[1]["Balance"], 9.0)
        self.assertEqual(
            bot.col_txs.documents["deposit:tx18:deposit"]["status"],
            "reversed",
        )

    def test_missing_chainlock_does_not_reverse_a_deep_confirmed_deposit(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.col_users = MemoryCollection(
            [{"_id": 1, "Address": ["deposit"], "Balance": 10.0}]
        )
        bot.col_txs = MemoryCollection(
            [
                {
                    "_id": "deposit:tx19:deposit",
                    "txId": "tx19",
                    "type": "deposit",
                    "eventVersion": 2,
                    "status": "confirmed",
                    "address": "deposit",
                    "user_id": 1,
                    "amount": 1.0,
                }
            ]
        )
        bot.run_transaction = transaction_runner(bot.col_users, bot.col_txs)

        bot.reconcile_deposit_confirmations(
            {"tx19": [{"confirmations": 100, "chainlock": False}]}
        )

        self.assertEqual(bot.col_users.documents[1]["BalanceGroth"], 1_000_000_000)
        self.assertEqual(
            bot.col_txs.documents["deposit:tx19:deposit"]["status"],
            "confirmed",
        )

    def test_completed_withdrawal_reorg_relocks_and_recompletion_clears(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.col_users = MemoryCollection(
            [
                {
                    "_id": 1,
                    "Address": ["deposit"],
                    "Balance": 9.0,
                    "Locked": 1.0,
                    "IsWithdraw": True,
                }
            ]
        )
        bot.col_senders = MemoryCollection(
            [
                {
                    "_id": "withdraw:20",
                    "schemaVersion": 2,
                    "user_id": 1,
                    "address": "external",
                    "amount": 1.0,
                    "send_amount": 0.998,
                    "locked_amount": 1.0,
                    "fee": 0.002,
                    "txId": "tx20",
                    "status": "pending",
                }
            ]
        )
        bot.col_txs = MemoryCollection()
        bot.run_transaction = transaction_runner(
            bot.col_users, bot.col_senders, bot.col_txs
        )
        bot.create_send_tips_image = Mock()
        bot.send_to_logs = Mock()
        current = {
            "result": {"confirmations": 2, "chainlock": True},
            "error": None,
        }
        bot.wallet_api = SimpleNamespace(
            get_tx_status=lambda txid: copy.deepcopy(current)
        )

        bot.reconcile_withdrawals([])
        self.assertEqual(bot.col_users.documents[1]["LockedGroth"], 0)
        self.assertEqual(
            bot.col_senders.documents["withdraw:20"]["status"], "completed"
        )

        current["result"] = {"confirmations": 1, "chainlock": False}
        bot.reconcile_withdrawals([])
        self.assertEqual(bot.col_users.documents[1]["LockedGroth"], 100_000_000)
        self.assertEqual(
            bot.col_senders.documents["withdraw:20"]["status"], "reorged"
        )
        self.assertEqual(
            bot.col_txs.documents["withdraw:tx20"]["status"], "reversed"
        )

        current["result"] = {"confirmations": 2, "chainlock": True}
        bot.reconcile_withdrawals([])
        self.assertEqual(bot.col_users.documents[1]["LockedGroth"], 0)
        self.assertFalse(bot.col_users.documents[1]["IsWithdraw"])
        self.assertEqual(
            bot.col_senders.documents["withdraw:20"]["status"], "completed"
        )

    def test_unknown_withdrawal_requires_review_even_with_one_wallet_match(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        started = tipbot.datetime.datetime.utcnow()
        started_at = tipbot.calendar.timegm(started.utctimetuple())
        bot.col_users = MemoryCollection(
            [
                {
                    "_id": 1,
                    "Balance": 9.0,
                    "Locked": 1.0,
                    "IsWithdraw": True,
                }
            ]
        )
        bot.col_senders = MemoryCollection(
            [
                {
                    "_id": "withdraw:21",
                    "schemaVersion": 2,
                    "user_id": 1,
                    "address": "external",
                    "amount": 1.0,
                    "send_amount": 0.998,
                    "locked_amount": 1.0,
                    "fee": 0.002,
                    "status": "unknown",
                    "broadcast_started_at": started,
                }
            ]
        )
        bot.col_txs = MemoryCollection()
        bot.run_transaction = transaction_runner(
            bot.col_users, bot.col_senders, bot.col_txs
        )
        bot.send_to_logs = Mock()
        bot.create_send_tips_image = Mock()
        bot.wallet_api = SimpleNamespace(
            get_tx_status=lambda txid: {
                "result": {"confirmations": 0, "chainlock": False},
                "error": None,
            }
        )
        history = [
            {
                "txid": "tx21",
                "category": "spend",
                "address": "external",
                "amount": -0.998,
                "time": started_at,
            }
        ]

        bot.reconcile_withdrawals(history)

        sender = bot.col_senders.documents["withdraw:21"]
        self.assertEqual(sender["status"], "unknown")
        self.assertNotIn("txId", sender)
        self.assertTrue(sender["review_required"])
        self.assertIn("tx21", sender["review_reason"])
        self.assertEqual(bot.col_users.documents[1]["LockedGroth"], 100_000_000)

    def test_legacy_lock_dust_is_removed_without_active_withdrawals(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.col_users = MemoryCollection(
            [
                {
                    "_id": 1,
                    "Address": ["deposit"],
                    "Balance": 9.0,
                    "Locked": 0.0001,
                    "IsWithdraw": False,
                }
            ]
        )
        bot.col_senders = MemoryCollection()
        bot.col_envelopes = MemoryCollection()
        bot.col_txs = MemoryCollection()
        bot.col_state = MemoryCollection()
        bot.wallet_api = SimpleNamespace(get_tx_status=Mock())
        bot.send_to_logs = Mock()
        bot.run_transaction = transaction_runner(
            bot.col_users,
            bot.col_senders,
            bot.col_envelopes,
            bot.col_txs,
        )

        bot.migrate_money_schema()

        user = bot.col_users.documents[1]
        self.assertEqual((user["LockedGroth"], user["Locked"]), (0, 0.0))
        self.assertFalse(user["IsWithdraw"])

    def test_existing_database_requires_offline_migration_confirmation(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.col_users = MemoryCollection([{"_id": 1, "Balance": 1.0}])
        bot.col_senders = MemoryCollection()
        bot.col_envelopes = MemoryCollection()
        bot.col_txs = MemoryCollection()
        bot.col_state = MemoryCollection()

        with patch.dict(
            tipbot.conf["mongo"], {"migrationConfirmedOffline": False}
        ), self.assertRaises(RuntimeError):
            bot.require_offline_migration_confirmation()

        with patch.dict(
            tipbot.conf["mongo"], {"migrationConfirmedOffline": True}
        ):
            bot.require_offline_migration_confirmation()

    def test_legacy_envelope_refund_reads_remainder_inside_transaction(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.col_users = MemoryCollection([{"_id": 1, "Balance": 0.0}])
        bot.col_envelopes = MemoryCollection(
            [
                {
                    "_id": "legacy-envelope",
                    "creator_id": 1,
                    "amount": 1.0,
                    "remains": 1.0,
                }
            ]
        )
        bot.send_to_logs = Mock()

        def run(callback):
            bot.col_envelopes.update_one(
                {"_id": "legacy-envelope"},
                {"$set": {"remains": 0.4}},
            )
            return callback(None)

        bot.run_transaction = run
        bot.refund_legacy_envelopes()

        self.assertEqual(bot.col_users.documents[1]["BalanceGroth"], 40_000_000)
        envelope = bot.col_envelopes.documents["legacy-envelope"]
        self.assertEqual(envelope["legacy_refunded_groth"], 40_000_000)
        self.assertEqual(envelope["status"], "legacy_refunded")

    def test_unresolved_legacy_withdrawal_quarantines_zero_lock_user(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.col_users = MemoryCollection(
            [
                {
                    "_id": 1,
                    "Address": ["deposit"],
                    "Balance": 9.0,
                    "Locked": 0.0,
                    "IsWithdraw": False,
                }
            ]
        )
        bot.col_senders = MemoryCollection(
            [{"_id": "legacy", "user_id": 1, "txId": "tx22", "status": "pending"}]
        )
        bot.col_envelopes = MemoryCollection()
        bot.col_txs = MemoryCollection()
        bot.col_state = MemoryCollection()
        bot.wallet_api = SimpleNamespace(
            get_tx_status=lambda txid: {
                "result": None,
                "error": {"code": -5, "message": "not found"},
            }
        )
        bot.send_to_logs = Mock()
        bot.run_transaction = transaction_runner(
            bot.col_users,
            bot.col_senders,
            bot.col_envelopes,
            bot.col_txs,
        )

        bot.migrate_money_schema()

        self.assertTrue(bot.col_users.documents[1]["IsWithdraw"])
        self.assertTrue(bot.col_senders.documents["legacy"]["review_required"])
        self.assertEqual(
            bot.col_state.documents["money_schema"]["quarantined_withdrawals"],
            1,
        )

    def test_lock_release_preserves_legacy_withdrawal_quarantine(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.col_users = MemoryCollection(
            [
                {
                    "_id": 1,
                    "Balance": 9.0,
                    "Locked": 1.0,
                    "IsWithdraw": True,
                    "WithdrawalQuarantined": True,
                }
            ]
        )

        bot.release_user_lock(None, 1, 100_000_000)

        user = bot.col_users.documents[1]
        self.assertEqual(user["LockedGroth"], 0)
        self.assertTrue(user["IsWithdraw"])

    def test_quarantined_user_cannot_move_balance_through_tips(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.user_id = 1
        bot.first_name = "Alice"
        bot.new_message = SimpleNamespace(update_id=33)
        bot.col_users = MemoryCollection(
            [
                {
                    "_id": 1,
                    "Balance": 10.0,
                    "IsVerified": True,
                    "WithdrawalQuarantined": True,
                },
                {
                    "_id": 2,
                    "Balance": 0.0,
                    "IsVerified": True,
                    "first_name": "Bob",
                },
            ]
        )
        bot.col_tip_logs = MemoryCollection()
        bot.run_transaction = transaction_runner(
            bot.col_users, bot.col_tip_logs
        )
        bot.send_message = Mock()
        bot.insufficient_balance_image = Mock()
        bot.incorrect_parametrs_image = Mock()

        bot.send_tip(2, "10", None, "")

        self.assertEqual(bot.col_users.documents[1]["BalanceGroth"], 1_000_000_000)
        self.assertEqual(bot.col_users.documents[2]["BalanceGroth"], 0)
        self.assertIn("frozen", bot.send_message.call_args.args[1])

    def test_shared_default_addresses_are_replaced_before_deposits(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.col_users = MemoryCollection(
            [
                {"_id": 1, "Address": ["shared"], "Balance": 0.0},
                {"_id": 2, "Address": "shared", "Balance": 0.0},
            ]
        )
        bot.col_state = MemoryCollection()
        replacements = iter([["address-1"], ["address-2"]])
        bot.wallet_api = SimpleNamespace(
            get_default_address=lambda: ["shared"],
            create_user_wallet=lambda: next(replacements),
        )
        bot.send_to_logs = Mock()
        bot.send_message = Mock()

        bot.migrate_deposit_addresses()

        self.assertEqual(bot.col_users.documents[1]["Address"], ["address-1"])
        self.assertEqual(bot.col_users.documents[2]["Address"], ["address-2"])

    def test_envelope_claim_requires_exact_message_and_is_replay_safe(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.user_id = 2
        bot.first_name = "Bob"
        query = SimpleNamespace(
            id="query",
            from_user=SimpleNamespace(id=2),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-999),
                message_id=50,
            ),
        )
        bot.new_message = SimpleNamespace(callback_query=query)
        bot.col_envelopes = MemoryCollection(
            [
                {
                    "_id": "envelope:23",
                    "schemaVersion": 2,
                    "status": "active",
                    "group_id": -100,
                    "group_username": None,
                    "msg_id": 50,
                    "creator_id": 1,
                    "amount": 0.001,
                    "remains": 0.001,
                    "takers": [],
                }
            ]
        )
        bot.col_users = MemoryCollection(
            [{"_id": 2, "Balance": 0.0, "IsVerified": True}]
        )
        bot.col_tip_logs = MemoryCollection()
        bot.run_transaction = transaction_runner(
            bot.col_users, bot.col_envelopes, bot.col_tip_logs
        )
        bot.answer_call_back = Mock()
        bot.send_message = Mock()
        bot.red_envelope_catched = Mock()
        bot.delete_tg_message = Mock()

        bot.catch_envelope("envelope:23")
        self.assertEqual(bot.col_users.documents[2]["BalanceGroth"], 0)

        query.message.chat.id = -100
        bot.catch_envelope("envelope:23")
        bot.catch_envelope("envelope:23")

        self.assertEqual(bot.col_users.documents[2]["BalanceGroth"], 100_000)
        self.assertEqual(
            bot.col_envelopes.documents["envelope:23"]["status"], "ended"
        )
        self.assertEqual(len(bot.col_tip_logs.documents), 1)

    def test_ptb22_update_offset_uses_object_attribute(self):
        bot = tipbot.TipBot.__new__(tipbot.TipBot)
        bot.col_state = MemoryCollection()

        bot.acknowledge_updates([Update(update_id=24)])

        self.assertEqual(bot.update_offset, 25)
        self.assertEqual(
            bot.col_state.documents["telegram_offset"]["value"], 25
        )

    def test_ptb22_adapter_waits_for_async_calls(self):
        class AsyncBot:
            async def initialize(self):
                return None

            async def send_message(self, chat_id, text):
                return (chat_id, text)

        with patch.object(tipbot, "Bot", return_value=AsyncBot()):
            bot = tipbot.SyncBot("token")
            self.assertEqual(bot.send_message(1, "hello"), (1, "hello"))
            bot._loop.call_soon_threadsafe(bot._loop.stop)
            bot._thread.join(timeout=1)
            bot._loop.close()

    def test_html_is_escaped(self):
        self.assertEqual(
            tipbot.TipBot.cleanhtml('<a href="bad">&'),
            "&lt;a href=&quot;bad&quot;&gt;&amp;",
        )


if __name__ == "__main__":
    unittest.main()

"""
    Developed by @vsnation(t.me/vsnation)
    Email: vsnation.v@gmail.com
    If you'll need the support use the contacts ^(above)!
"""
import asyncio
import atexit
import calendar
import datetime
import html
import io
import json
import logging
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import traceback
import uuid
from collections import defaultdict
from decimal import Decimal, InvalidOperation

import pyqrcode
import schedule
from PIL import Image, ImageDraw, ImageFont
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from api.firo_wallet_api import FiroRPCError, FiroTransportError, FiroWalletAPI

logger = logging.getLogger()
logger.setLevel(logging.ERROR)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

GROTH_PER_FIRO = 100_000_000
MAX_FIRO = Decimal("100000000")
MAX_GROTH = 10_000_000_000_000_000
FIRO_QUANTUM = Decimal("0.00000001")
WITHDRAW_FEE = Decimal("0.002")
MIN_ENVELOPE = Decimal("0.001")
WITHDRAW_FEE_GROTH = 200_000
MIN_ENVELOPE_GROTH = 100_000
MAX_TIP_COMMENT_UTF16 = 1000  # Photo captions allow 1024 units including "Comment: ".

with open('services.json') as conf_file:
    conf = json.load(conf_file)
    connectionString = conf['mongo']['connectionString']
    bot_token = conf['telegram_bot']['bot_token']
    httpprovider = conf['httpprovider']
    dictionary = conf['dictionary']
    LOG_CHANNEL = conf['log_ch']

wallet_api = FiroWalletAPI(httpprovider)

point_to_pixels = 1.33
bold = ImageFont.truetype(font="fonts/ProximaNova-Bold.ttf", size=int(18 * point_to_pixels))
regular = ImageFont.truetype(font="fonts/ProximaNova-Regular.ttf", size=int(18 * point_to_pixels))
bold_high = ImageFont.truetype(font="fonts/ProximaNova-Bold.ttf", size=int(26 * point_to_pixels))
# Fits exact eight-decimal amounts in the 522px receipt templates.
money_bold = ImageFont.truetype(font="fonts/ProximaNova-Bold.ttf", size=20)

WELCOME_MESSAGE = """
<b>Welcome to the Firo telegram tip bot!</b> 
"""


def parse_amount(value, minimum=FIRO_QUANTUM):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid amount") from exc

    if not amount.is_finite() or amount < minimum or amount > MAX_FIRO:
        raise ValueError("invalid amount")
    try:
        quantized = amount.quantize(FIRO_QUANTUM)
    except InvalidOperation as exc:
        raise ValueError("invalid amount") from exc
    if amount != quantized:
        raise ValueError("amount has more than eight decimal places")
    return amount


def firo_to_groth(value):
    try:
        scaled = Decimal(str(value)) * GROTH_PER_FIRO
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid FIRO amount") from exc
    if (
        not scaled.is_finite()
        or scaled != scaled.to_integral_value()
        or abs(scaled) > MAX_GROTH
    ):
        raise ValueError("FIRO amount is not an exact number of groth")
    return int(scaled)


def legacy_firo_to_groth(value):
    try:
        scaled = Decimal(str(value)) * GROTH_PER_FIRO
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid legacy FIRO amount") from exc
    rounded = scaled.to_integral_value()
    if (
        not scaled.is_finite()
        or abs(scaled - rounded) > Decimal("0.5")
        or abs(rounded) > MAX_GROTH
    ):
        raise ValueError("legacy FIRO amount cannot be normalized to groth")
    return int(rounded)


def groth_to_decimal(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or abs(value) > MAX_GROTH
    ):
        raise ValueError("groth amount must be an integer")
    return Decimal(value) / GROTH_PER_FIRO


def groth_to_float(value):
    return float(groth_to_decimal(value))


def format_groth(value):
    return "{0:.8f}".format(groth_to_decimal(value))


def is_final_transaction(transaction):
    return (
        transaction.get("confirmations", 0) >= 2
        and transaction.get("chainlock") is True
    )


def normalize_addresses(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [address for address in value if isinstance(address, str) and address]
    if isinstance(value, str) and value:
        return [value]
    raise ValueError("invalid address schema")


class SyncBot:
    def __init__(self, token):
        self._bot = Bot(token)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._call(self._bot.initialize())

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coroutine):
        return asyncio.run_coroutine_threadsafe(
            coroutine, self._loop
        ).result()

    def __getattr__(self, name):
        value = getattr(self._bot, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            return self._call(value(*args, **kwargs))

        return call


class TipBot:
    # ponytail: one process-wide lock; shard only if reconciliation throughput requires it.
    accounting_lock = threading.RLock()

    def __init__(self, wallet_api):
        # INIT
        self.bot = SyncBot(bot_token)
        self.bot_username = self.bot.username.casefold()
        self.wallet_api = wallet_api
        # firo Butler Initialization
        self.client = MongoClient(connectionString, w="majority")
        hello = self.client.admin.command("hello")
        if not hello.get("setName") and hello.get("msg") != "isdbgrid":
            raise RuntimeError(
                "MongoDB must run as a replica set because tipbot money updates use transactions"
            )
        db = self.client.get_default_database()
        self.col_captcha = db['captcha']
        self.col_commands_history = db['commands_history']
        self.col_users = db['users']
        self.col_senders = db['senders']
        self.col_tip_logs = db['tip_logs']
        self.col_envelopes = db['envelopes']
        self.col_txs = db['txs']
        self.col_state = db['state']
        self.reconciliation_ok = False
        self.stop_jobs = threading.Event()
        self.scheduler_thread = None
        self.claim_process_ownership()
        self.require_offline_migration_confirmation()
        # Legacy failed RPCs stored explicit null IDs, which sparse indexes include.
        self.col_senders.update_many({"txId": None}, {"$unset": {"txId": ""}})
        self.col_senders.create_index("txId", unique=True, sparse=True)
        self.migrate_deposit_addresses()
        self.col_users.create_index(
            "Address",
            unique=True,
            sparse=True,
            name="unique_deposit_address",
        )
        self.migrate_money_schema()
        self.col_users.create_index("BalanceGroth", name="negative_balance_guard")
        self.col_txs.create_index(
            [("type", 1), ("status", 1), ("review_required", 1)],
            name="outgoing_hold",
        )
        self.recover_incomplete_envelopes()

        self.message, self.text, self._is_video, self.message_text, \
            self.first_name, self.username, self.user_id, self.firo_address, \
            self.balance_in_firo, self.locked_in_firo, self.is_withdraw, self.balance_in_groth, \
            self._is_verified, self.group_id, self.group_username = \
            None, None, None, None, None, None, None, None, None, None, None, None, None, None, None

        self.new_message = None
        offset = self.col_state.find_one({"_id": "telegram_offset"})
        self.update_offset = offset.get("value") if offset else None

        self.safe_job("wallet balance", self.get_wallet_balance)
        self.safe_job("balance reconciliation", self.update_balance)
        self.safe_job("automint", self.wallet_api.automintunspent)
        schedule.every(60).seconds.do(
            self.safe_job, "balance reconciliation", self.update_balance
        )
        schedule.every(300).seconds.do(
            self.safe_job, "automint", self.wallet_api.automintunspent
        )
        self.scheduler_thread = threading.Thread(target=self.pending_tasks, daemon=True)
        self.scheduler_thread.start()

        while True:
            try:
                self._is_user_in_db = None
                # get chat updates
                new_messages = self.wait_new_message()
                if new_messages and self.processing_messages(new_messages):
                    self.acknowledge_updates(new_messages)
            except Exception as exc:
                print(exc)
                traceback.print_exc()

    def claim_process_ownership(self):
        self.owner_id = uuid.uuid4().hex
        atexit.register(self.release_process_ownership)
        try:
            self.col_state.insert_one(
                {
                    "_id": "bot_owner",
                    "owner_id": self.owner_id,
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                    "started_at": datetime.datetime.utcnow(),
                }
            )
        except DuplicateKeyError as exc:
            raise RuntimeError(
                "another bot owns this database; after verifying every bot process "
                "has stopped, remove a stale state.bot_owner record before restarting"
            ) from exc

    def release_process_ownership(self):
        self.stop_jobs.set()
        if self.scheduler_thread is not None:
            self.scheduler_thread.join(timeout=5)
            if self.scheduler_thread.is_alive():
                return  # Leave ownership in place while accounting work can still run.
        try:
            self.col_state.delete_one({"_id": "bot_owner", "owner_id": self.owner_id})
        except Exception:
            logger.exception("could not release bot database ownership")

    def safe_job(self, name, job):
        try:
            return job()
        except Exception as exc:
            logger.exception("%s failed", name)
            self.send_to_logs("%s failed: %s" % (name, exc))
            return None

    def acknowledge_updates(self, updates):
        self.update_offset = updates[-1].update_id + 1
        self.col_state.update_one(
            {"_id": "telegram_offset"},
            {"$set": {"value": self.update_offset}},
            upsert=True,
        )

    def command_matches(self, command, name):
        return command == name or command == "%s@%s" % (
            name,
            self.bot_username,
        )

    def run_transaction(self, callback):
        with self.accounting_lock, self.client.start_session() as session:
            return session.with_transaction(callback)

    def outgoing_paused(self, session=None):
        return (
            not self.reconciliation_ok
            or self.col_txs.find_one(
                {"type": "deposit", "status": "confirmed", "review_required": True},
                session=session,
            ) is not None
            or self.col_users.find_one({"BalanceGroth": {"$lt": 0}}, session=session)
            is not None
        )

    def command_id(self, prefix):
        update_id = getattr(self.new_message, "update_id", None)
        return "%s:%s" % (prefix, update_id if update_id is not None else uuid.uuid4().hex)

    def require_offline_migration_confirmation(self):
        for event in self.col_txs.find({"type": "deposit"}):
            txid = event.get("txId")
            address = event.get("address")
            if (
                event.get("eventVersion") != 2
                or not isinstance(txid, str) or not txid
                or not isinstance(address, str) or not address
                or event.get("_id") != "deposit:%s:%s" % (txid, address)
                or event.get("user_id") is None
                or type(event.get("amount_groth")) is not int
                or not 0 < event["amount_groth"] <= MAX_GROTH
                or event.get("status") not in ("confirmed", "reversed")
            ):
                raise RuntimeError(
                    "legacy deposits lack canonical output-level records; "
                    "reconcile and convert them offline before starting this bot"
                )
        state = self.col_state.find_one({"_id": "money_schema"})
        if state and state.get("status") == "complete":
            if state.get("version") != 1:
                raise RuntimeError("unsupported completed money schema version")
            return
        has_existing_data = any(
            collection.find_one({}) is not None
            for collection in (
                self.col_users,
                self.col_senders,
                self.col_envelopes,
                self.col_txs,
            )
        )
        if (
            has_existing_data
            and conf["mongo"].get("migrationConfirmedOffline") is not True
        ):
            raise RuntimeError(
                "legacy data migration requires every old tipbot process to be stopped; "
                "after taking a backup, set mongo.migrationConfirmedOffline to true"
            )

    def create_safe_deposit_addresses(self):
        addresses = normalize_addresses(self.wallet_api.create_user_wallet())
        retired = self.col_state.find_one({"_id": "retired_deposit_addresses"})
        if not addresses or (retired and set(addresses).intersection(retired["addresses"])):
            raise RuntimeError("wallet returned no deposit address or a retired address")
        return list(dict.fromkeys(addresses))

    def migrate_deposit_addresses(self):
        default_addresses = set(
            normalize_addresses(self.wallet_api.get_default_address())
        )
        if not default_addresses:
            raise RuntimeError("wallet returned no default Spark address")
        retired = self.col_state.find_one({"_id": "retired_deposit_addresses"})
        retired_addresses = set(retired.get("addresses", [])) if retired else set()
        retired_addresses.update(default_addresses)
        self.col_state.update_one(
            {"_id": "retired_deposit_addresses"},
            {"$set": {"addresses": sorted(retired_addresses)}},
            upsert=True,
        )
        for user in self.col_users.find({}):
            addresses = list(dict.fromkeys(normalize_addresses(user.get("Address"))))
            if default_addresses.intersection(addresses):
                addresses = [
                    address for address in addresses
                    if address not in default_addresses
                ]
                if not addresses:
                    addresses = self.create_safe_deposit_addresses()
                self.col_users.update_one(
                    {"_id": user["_id"]},
                    {"$set": {"Address": addresses}},
                )
                self.send_to_logs(
                    "Replaced shared default deposit address for user %s"
                    % user["_id"]
                )
                self.send_message(
                    user["_id"],
                    "<b>Your old shared deposit address was retired. Use /deposit to get your current address before sending funds.</b>",
                    parse_mode="HTML",
                )
            elif addresses != user.get("Address"):
                self.col_users.update_one(
                    {"_id": user["_id"]},
                    {"$set": {"Address": addresses}},
                )
            if not addresses:
                raise RuntimeError("user %s has no deposit address" % user["_id"])

        owners = {}
        for user in self.col_users.find({}):
            for address in normalize_addresses(user.get("Address")):
                if address in owners and owners[address] != user["_id"]:
                    raise RuntimeError(
                        "deposit address %s belongs to multiple users" % address
                    )
                owners[address] = user["_id"]

    def migrate_money_schema(self):
        state = self.col_state.find_one({"_id": "money_schema"})
        if state and state.get("status") == "complete":
            if state.get("version") != 1:
                raise RuntimeError("unsupported completed money schema version")
            return
        for user in self.col_users.find({}):
            balance_groth = user.get("BalanceGroth")
            locked_groth = user.get("LockedGroth")
            if balance_groth is None:
                balance_groth = legacy_firo_to_groth(user.get("Balance", 0))
            if locked_groth is None:
                locked_groth = legacy_firo_to_groth(user.get("Locked", 0))
            groth_to_decimal(balance_groth)
            groth_to_decimal(locked_groth)
            if locked_groth < 0:
                raise RuntimeError("user %s has a negative locked balance" % user["_id"])
            self.col_users.update_one(
                {"_id": user["_id"]},
                {
                    "$set": {
                        "BalanceGroth": balance_groth,
                        "LockedGroth": locked_groth,
                        "Balance": groth_to_float(balance_groth),
                        "Locked": groth_to_float(locked_groth),
                        "moneySchemaVersion": 1,
                    }
                },
            )

        self.refund_legacy_envelopes()

        for sender in self.col_senders.find({"schemaVersion": 2}):
            values = {}
            for groth_field, firo_field in (
                ("amount_groth", "amount"),
                ("send_amount_groth", "send_amount"),
                ("locked_amount_groth", "locked_amount"),
                ("fee_groth", "fee"),
            ):
                value = sender.get(groth_field)
                if value is None and firo_field in sender:
                    value = legacy_firo_to_groth(sender[firo_field])
                if value is None:
                    raise RuntimeError(
                        "withdrawal %s is missing %s" % (sender["_id"], groth_field)
                    )
                groth_to_decimal(value)
                values[groth_field] = value
                values[firo_field] = groth_to_float(value)
            values["moneySchemaVersion"] = 1
            self.col_senders.update_one(
                {"_id": sender["_id"], "schemaVersion": 2},
                {"$set": values},
            )

        self.migrate_legacy_pending_withdrawals()

        for envelope in self.col_envelopes.find({"schemaVersion": 2}):
            amount_groth = envelope.get("amount_groth")
            remains_groth = envelope.get("remains_groth")
            if amount_groth is None:
                amount_groth = legacy_firo_to_groth(envelope["amount"])
            if remains_groth is None:
                remains_groth = legacy_firo_to_groth(envelope["remains"])
            if not 0 <= remains_groth <= amount_groth:
                raise RuntimeError(
                    "envelope %s has an invalid remainder" % envelope["_id"]
                )
            self.col_envelopes.update_one(
                {"_id": envelope["_id"], "schemaVersion": 2},
                {
                    "$set": {
                        "amount_groth": amount_groth,
                        "remains_groth": remains_groth,
                        "amount": groth_to_float(amount_groth),
                        "remains": groth_to_float(remains_groth),
                        "moneySchemaVersion": 1,
                    }
                },
            )

        for event in self.col_txs.find({"eventVersion": 2}):
            if "amount" not in event:
                continue
            amount_groth = event.get("amount_groth")
            if amount_groth is None:
                amount_groth = legacy_firo_to_groth(event["amount"])
            user_id = event.get("user_id")
            if event.get("type") == "deposit" and user_id is None:
                recipients = list(
                    self.col_users.find({"Address": event["address"]})
                )
                if len(recipients) != 1:
                    raise RuntimeError(
                        "deposit event %s has no unique recipient" % event["_id"]
                    )
                user_id = recipients[0]["_id"]
            self.col_txs.update_one(
                {"_id": event["_id"], "eventVersion": 2},
                {
                    "$set": {
                        "amount_groth": amount_groth,
                        "amount": groth_to_float(amount_groth),
                        "user_id": user_id,
                        "moneySchemaVersion": 1,
                    }
                },
            )

        active_locks = defaultdict(int)
        for sender in self.col_senders.find(
            {
                "schemaVersion": 2,
                "status": {
                    "$in": [
                        "reserved",
                        "broadcasting",
                        "unknown",
                        "pending",
                        "reorged",
                        "conflicted",
                        "rejected",
                    ]
                },
            }
        ):
            active_locks[sender["user_id"]] += sender["locked_amount_groth"]
        quarantined_users = {
            sender["user_id"]
            for sender in self.col_senders.find(
                {"status": "pending", "schemaVersion": {"$ne": 2}}
            )
        }
        for user in self.col_users.find({}):
            user_id = user["_id"]
            locked_groth = active_locks.get(user_id, 0)
            if user["LockedGroth"] < locked_groth:
                raise RuntimeError(
                    "user %s lock does not cover active withdrawals" % user_id
                )
            if user_id in quarantined_users:
                self.col_users.update_one(
                    {"_id": user_id},
                    {
                        "$set": {
                            "IsWithdraw": True,
                            "WithdrawalQuarantined": True,
                        }
                    },
                )
                continue
            self.col_users.update_one(
                {"_id": user_id},
                {
                    "$set": {
                        "LockedGroth": locked_groth,
                        "Locked": groth_to_float(locked_groth),
                        "IsWithdraw": locked_groth > 0,
                        "WithdrawalQuarantined": False,
                    }
                },
            )

        self.col_state.update_one(
            {"_id": "money_schema"},
            {
                "$set": {
                    "version": 1,
                    "status": "complete",
                    "quarantined_withdrawals": len(quarantined_users),
                    "updated_at": datetime.datetime.utcnow(),
                }
            },
            upsert=True,
        )

    def refund_legacy_envelopes(self):
        for envelope in self.col_envelopes.find({"schemaVersion": {"$ne": 2}}):
            def refund(
                session,
                envelope_id=envelope["_id"],
            ):
                current = self.col_envelopes.find_one(
                    {"_id": envelope_id, "schemaVersion": {"$ne": 2}},
                    session=session,
                )
                if current is None:
                    return None
                amount_groth = legacy_firo_to_groth(current.get("amount", 0))
                remains_groth = legacy_firo_to_groth(current.get("remains", 0))
                if not 0 <= remains_groth <= amount_groth:
                    raise RuntimeError(
                        "legacy envelope %s has an invalid remainder" % envelope_id
                    )
                if remains_groth:
                    credited = self.col_users.update_one(
                        {"_id": current.get("creator_id")},
                        {
                            "$inc": {
                                "BalanceGroth": remains_groth,
                                "Balance": groth_to_float(remains_groth),
                            }
                        },
                        session=session,
                    )
                    if credited.modified_count != 1:
                        raise RuntimeError("legacy envelope creator disappeared")
                changed = self.col_envelopes.update_one(
                    {"_id": envelope_id, "schemaVersion": {"$ne": 2}},
                    {
                        "$set": {
                            "schemaVersion": 2,
                            "moneySchemaVersion": 1,
                            "amount_groth": amount_groth,
                            "remains_groth": 0,
                            "amount": groth_to_float(amount_groth),
                            "remains": 0.0,
                            "status": "legacy_refunded",
                            "legacy_refunded_groth": remains_groth,
                            "completed_at": datetime.datetime.utcnow(),
                        }
                    },
                    session=session,
                )
                if changed.modified_count != 1:
                    raise RuntimeError("legacy envelope changed during refund")
                return remains_groth

            refunded_groth = self.run_transaction(refund)
            if refunded_groth is not None:
                self.send_to_logs(
                    "Refunded %s FIRO remaining in legacy envelope %s"
                    % (format_groth(refunded_groth), envelope["_id"])
                )

    def report_legacy_withdrawal(self, sender, reason):
        self.col_users.update_one(
            {"_id": sender.get("user_id")},
            {
                "$set": {
                    "IsWithdraw": True,
                    "WithdrawalQuarantined": True,
                }
            },
        )
        reported = self.col_senders.update_one(
            {
                "_id": sender["_id"],
                "legacyReportedAt": {"$exists": False},
            },
            {
                "$set": {
                    "review_required": True,
                    "legacy_error": str(reason),
                    "legacyReportedAt": datetime.datetime.utcnow(),
                }
            },
        )
        if reported.modified_count == 1:
            self.send_to_logs(
                "Legacy withdrawal %s needs manual review: %s"
                % (sender.get("txId", sender["_id"]), reason)
            )

    def migrate_legacy_pending_withdrawals(self):
        plans = defaultdict(list)
        for sender in self.col_senders.find(
            {"status": "pending", "schemaVersion": {"$ne": 2}}
        ):
            txid = sender.get("txId")
            if not txid:
                self.report_legacy_withdrawal(sender, "missing transaction id")
                continue
            if self.col_txs.find_one({"txId": txid, "type": "withdraw"}):
                self.report_legacy_withdrawal(
                    sender, "an old completion record already exists"
                )
                continue
            try:
                response = self.wallet_api.get_tx_status(txid)
            except Exception as exc:
                self.report_legacy_withdrawal(sender, exc)
                continue
            if response.get("error"):
                self.report_legacy_withdrawal(sender, response["error"])
                continue
            transaction = response["result"]
            spends = [
                detail for detail in transaction.get("details", [])
                if detail.get("category") == "spend"
                and detail.get("address")
                and detail.get("amount") is not None
            ]
            if len(spends) != 1:
                self.report_legacy_withdrawal(
                    sender, "expected exactly one outgoing transaction detail"
                )
                continue
            try:
                send_amount_groth = abs(firo_to_groth(spends[0]["amount"]))
                network_fee_groth = abs(
                    firo_to_groth(transaction.get("fee", 0))
                )
            except ValueError as exc:
                self.report_legacy_withdrawal(sender, exc)
                continue
            locked_amount_groth = send_amount_groth + network_fee_groth
            plans[sender["user_id"]].append(
                (
                    sender,
                    spends[0]["address"],
                    send_amount_groth,
                    locked_amount_groth,
                )
            )

        for user_id, user_plans in plans.items():
            user = self.col_users.find_one({"_id": user_id})
            total_locked = sum(plan[3] for plan in user_plans)
            existing_locked = sum(
                active.get("locked_amount_groth", 0)
                for active in self.col_senders.find(
                    {
                        "user_id": user_id,
                        "schemaVersion": 2,
                        "status": {
                            "$in": [
                                "reserved",
                                "broadcasting",
                                "unknown",
                                "pending",
                                "reorged",
                                "conflicted",
                                "rejected",
                            ]
                        },
                    }
                )
            )
            if (
                user is None
                or user.get("LockedGroth", -1)
                < total_locked + existing_locked
            ):
                for sender, _address, _send, _locked in user_plans:
                    self.report_legacy_withdrawal(
                        sender, "stored lock does not cover pending transactions"
                    )
                continue
            for sender, address, send_groth, locked_groth in user_plans:
                amount_groth = locked_groth + WITHDRAW_FEE_GROTH
                self.col_senders.update_one(
                    {
                        "_id": sender["_id"],
                        "status": "pending",
                        "schemaVersion": {"$ne": 2},
                    },
                    {
                        "$set": {
                            "schemaVersion": 2,
                            "moneySchemaVersion": 1,
                            "legacy": True,
                            "address": address,
                            "amount_groth": amount_groth,
                            "send_amount_groth": send_groth,
                            "locked_amount_groth": locked_groth,
                            "fee_groth": WITHDRAW_FEE_GROTH,
                            "amount": groth_to_float(amount_groth),
                            "send_amount": groth_to_float(send_groth),
                            "locked_amount": groth_to_float(locked_groth),
                            "fee": groth_to_float(WITHDRAW_FEE_GROTH),
                            "migrated_at": datetime.datetime.utcnow(),
                        }
                    },
                )
                self.col_users.update_one(
                    {"_id": user_id},
                    {"$set": {"IsWithdraw": True}},
                )

    def recover_incomplete_envelopes(self):
        for envelope in self.col_envelopes.find(
            {"schemaVersion": 2, "status": {"$in": ["creating", "sending"]}}
        ):
            self.col_envelopes.update_one(
                {
                    "_id": envelope["_id"],
                    "status": {"$in": ["creating", "sending"]},
                },
                {
                    "$set": {
                        "status": "rejected",
                        "error": "Bot stopped before envelope activation",
                    }
                },
            )
        for envelope in self.col_envelopes.find(
            {"schemaVersion": 2, "status": "rejected"}
        ):
            self.refund_envelope(
                envelope["_id"],
                envelope.get("error", "Envelope creation failed"),
            )

    @staticmethod
    def image_buffer(image):
        output = io.BytesIO()
        image.save(output, format="PNG")
        output.seek(0)
        return output

    @staticmethod
    def mention(user_id, name):
        return '<a href="tg://user?id=%s">%s</a>' % (
            int(user_id),
            html.escape(str(name), quote=True),
        )

    def pending_tasks(self):
        while not self.stop_jobs.wait(5):
            try:
                schedule.run_pending()
            except Exception:
                logger.exception("scheduled task dispatcher failed")

    def processing_messages(self, new_messages):
        for self.new_message in new_messages:
            query = getattr(self.new_message, "callback_query", None)
            message = self.new_message.message or (query.message if query else None)
            if message is None or self.new_message.effective_user is None:
                continue
            try:
                time.sleep(0.5)
                self.message = message
                self.text, self._is_video = self.get_action(self.new_message)
                self.message_text = str(self.text).lower()
                # init user data
                self.first_name = self.new_message.effective_user.first_name
                self.username = self.new_message.effective_user.username
                self.user_id = int(self.new_message.effective_user.id)

                self.firo_address, self.balance_in_firo, self.locked_in_firo, self.is_withdraw = self.get_user_data()
                self.balance_in_groth = (
                    firo_to_groth(self.balance_in_firo)
                    if self.balance_in_firo is not None else 0
                )

                user = self.col_users.find_one({"_id": self.user_id})
                self._is_verified = bool(user and user.get('IsVerified'))
                self._is_user_in_db = self._is_verified
                #
                print(self.username)
                print(self.user_id)
                print(self.first_name)
                print(self.message_text, '\n')
                self.group_id = self.message.chat.id
                self.group_username = self.get_group_username()

                split = self.text.split(' ')
                if len(split) > 1:
                    args = split[1:]
                else:
                    args = None

                # Check if user changed his username
                self.check_username_on_change()
                self.action_processing(str(split[0]).lower(), args)
                # self.check_group_msg()
            except Exception as exc:
                print(exc)
                traceback.print_exc()
                return False
        return True

    def send_to_logs(self, text):
        try:
            self.bot.send_message(
                LOG_CHANNEL,
                text
            )
        except Exception as exc:
            print(exc)

    def get_group_username(self):
        """
            Get group username
        """
        username = getattr(self.message.chat, "username", None)
        return username if username and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username) else None

    def get_user_username(self):
        """
                Get User username
        """
        try:
            return str(self.message.from_user.username)
        except Exception:
            return None

    def wait_new_message(self):
        return self.bot.get_updates(
            offset=self.update_offset,
            timeout=30,
            limit=1,
            allowed_updates=["message", "callback_query"],
        )

    @staticmethod
    def get_action(message):
        _is_document = False
        menu_option = None

        if message.message is not None:
            menu_option = message.message.text
            _is_document = message.message.document is not None
            if 'mp4' in str(message.message.document):
                _is_document = False

        elif message.callback_query is not None:
            menu_option = message.callback_query.data

        return str(menu_option), _is_document

    def action_processing(self, cmd, args):
        """
            Check each user actions
        """
        # ***** Tip bot section begin *****
        is_tip = self.command_matches(cmd, "/tip")
        is_atip = self.command_matches(cmd, "/atip")
        if is_tip or is_atip:
            if not self._is_user_in_db:
                self.send_message(self.group_id,
                                  '%s, <a href="https://t.me/firo_tipbot?start=1">start the bot</a> to receive tips!'
                                  % self.mention(self.user_id, self.first_name),
                                  parse_mode='HTML')
                return
            if args is not None and len(args) >= 1:
                _type = "anonymous" if is_atip else None

                if self.message.reply_to_message is not None:
                    comment = " ".join(args[1:]) if len(args) > 1 else ""
                    self.tip_in_the_chat(args[0], comment=comment, _type=_type)
                elif len(args) >= 2:
                    comment = " ".join(args[2:]) if len(args) > 2 else ""
                    self.tip_user(args[0], args[1], comment, _type=_type)
                else:
                    self.incorrect_parametrs_image()
                    self.send_message(
                        self.user_id,
                        dictionary['tip_help'],
                        parse_mode='HTML'
                    )
            else:
                self.incorrect_parametrs_image()
                self.send_message(
                    self.user_id,
                    dictionary['tip_help'],
                    parse_mode='HTML'
                )


        elif self.command_matches(cmd, "/envelope"):
            try:
                self.bot.delete_message(self.group_id, self.message.message_id)
            except Exception as exc:
                logger.debug("could not delete envelope command: %s", exc)

            if self.message.chat.type == 'private':
                self.send_message(
                    self.user_id,
                    "<b>You can use this cmd only in the group</b>",
                    parse_mode="HTML"
                )
                return

            if not self._is_user_in_db:
                self.send_message(self.group_id,
                                  '%s, <a href="https://t.me/firo_tipbot?start=1">start the bot</a> to receive tips!'
                                  % self.mention(self.user_id, self.first_name),
                                  parse_mode="HTML", disable_web_page_preview=True)
                return

            if args is not None and len(args) == 1:
                self.create_red_envelope(*args)
            else:
                self.incorrect_parametrs_image()


        elif cmd.startswith("catch_envelope|"):
            if not self._is_user_in_db:
                self.send_message(self.group_id,
                                  '%s, <a href="https://t.me/firo_tipbot?start=1">start the bot</a> to receive tips!'
                                  % self.mention(self.user_id, self.first_name),
                                  parse_mode="HTML", disable_web_page_preview=True)
                return

            envelope_id = cmd.split("|", 1)[1]
            self.catch_envelope(envelope_id)



        elif self.command_matches(cmd, "/balance"):
            if not self._is_user_in_db:
                self.send_message(self.group_id,
                                  '%s, <a href="https://t.me/firo_tipbot?start=1">start the bot</a> to receive tips!'
                                  % self.mention(self.user_id, self.first_name),
                                  parse_mode="HTML", disable_web_page_preview=True)
                return
            self.send_message(
                self.user_id,
                dictionary['balance'] % format_groth(self.balance_in_groth),
                parse_mode='HTML'
            )

        elif self.command_matches(cmd, "/withdraw"):
            if not self._is_user_in_db:
                self.send_message(self.group_id,
                                  '%s, <a href="https://t.me/firo_tipbot?start=1">start the bot</a> to receive tips!'
                                  % self.mention(self.user_id, self.first_name),
                                  parse_mode="HTML", disable_web_page_preview=True)
                return
            if args is not None and len(args) == 2:
                self.withdraw_coins(*args)
            else:
                self.incorrect_parametrs_image()

        elif self.command_matches(cmd, "/deposit"):
            if not self._is_user_in_db:
                self.send_message(self.group_id,
                                  '%s, <a href="https://t.me/firo_tipbot?start=1">start the bot</a> to receive tips!'
                                  % self.mention(self.user_id, self.first_name),
                                  parse_mode="HTML", disable_web_page_preview=True)
                return
            try:
                self.firo_address = self.update_address_and_balance(
                    self.col_users.find_one({"_id": self.user_id})
                )
            except Exception:
                logger.exception("deposit address could not be refreshed")
                self.send_message(
                    self.user_id,
                    "Deposit address is unavailable. Please try again later.",
                )
                return
            self.send_message(
                self.user_id,
                dictionary['deposit'] % self.firo_address[-1],
                parse_mode='HTML'
            )
            self.create_qr_code()

        elif self.command_matches(cmd, "/help"):
            self.send_message(
                self.user_id,
                dictionary['help'],
                parse_mode='HTML',
                disable_web_page_preview=True
            )

        # ***** Tip bot section end *****
        # ***** Verification section begin *****
        elif self.command_matches(cmd, "/start"):
            self.auth_user()

    def check_username_on_change(self):
        """
            Check username on change in the bot
        """
        if not self._is_user_in_db:
            return

        update = {"$set": {"first_name": self.first_name}}
        if self.username:
            username_norm = self.username.casefold()
            self.col_users.update_many(
                {
                    "_id": {"$ne": self.user_id},
                    "$or": [
                        {"username_norm": username_norm},
                        {
                            "username": {
                                "$regex": "^%s$" % re.escape(self.username),
                                "$options": "i",
                            }
                        },
                    ],
                },
                {"$unset": {"username": "", "username_norm": ""}},
            )
            update["$set"].update(
                {"username": self.username, "username_norm": username_norm}
            )
        else:
            update["$unset"] = {"username": "", "username_norm": ""}

        self.col_users.update_one(
            {"_id": self.user_id},
            update,
        )

    def get_wallet_balance(self):
        response = self.wallet_api.listsparkmints()
        if response.get("error"):
            raise RuntimeError(response["error"])
        result = sum(
            mint['amount'] for mint in response['result'] if not mint['isUsed']
        )
        print("Current Balance", result)

    def update_balance(self):
        with self.accounting_lock:
            return self._update_balance()

    def _update_balance(self):
        """
            Update user's balance using transactions history
        """
        print("Handle TXs")
        self.reconciliation_ok = False
        response = self.wallet_api.get_txs_list()
        if response.get("error"):
            raise RuntimeError(response["error"])

        transactions = response["result"]
        by_txid = {}
        for transaction in transactions:
            txid = transaction.get("txid")
            if txid:
                by_txid.setdefault(txid, []).append(transaction)

        for txid, entries in by_txid.items():
            receive = next(
                (
                    entry for entry in entries
                    if entry.get("category") == "receive"
                    and is_final_transaction(entry)
                ),
                None,
            )
            if receive is not None:
                self.apply_deposits(receive)

        self.reconcile_deposit_confirmations(by_txid)
        self.reconcile_withdrawals(transactions)
        self.reconciliation_ok = True

    def flag_deposit_for_review(self, event, reason):
        reason = str(reason)

        def hold_deposit(session):
            current = self.col_txs.find_one({"_id": event["_id"]}, session=session)
            if current is None:
                return False
            if current.get("review_required") and current.get("review_reason") == reason:
                return False
            self.col_txs.update_one(
                {"_id": current["_id"]},
                {"$set": {
                    "review_required": True,
                    "review_reason": reason,
                    "reviewReportedAt": datetime.datetime.utcnow(),
                }},
                session=session,
            )
            return True

        if self.run_transaction(hold_deposit):
            self.send_to_logs(
                "Deposit %s needs review: %s" % (event["txId"], reason)
            )

    def reconcile_deposit_confirmations(self, transactions):
        for event in self.col_txs.find(
            {
                "type": "deposit",
                "eventVersion": 2,
                "status": {"$in": ["confirmed", "reversed"]},
            }
        ):
            entries = transactions.get(event["txId"])
            if not entries:
                # Conflicted incoming transactions can disappear from listtransactions.
                try:
                    response = self.wallet_api.get_tx_status(event["txId"])
                    if response.get("error"):
                        raise FiroRPCError(response["error"])
                except (FiroRPCError, FiroTransportError) as exc:
                    self.flag_deposit_for_review(event, exc)
                    continue
                entries = [response["result"]]
                transactions[event["txId"]] = entries
            confirmations = max(entry.get("confirmations", 0) for entry in entries)
            final = any(is_final_transaction(entry) for entry in entries)
            if event["status"] == "confirmed" and confirmations >= 2 and not final:
                self.flag_deposit_for_review(event, "transaction lost chainlock")
                continue
            amount_groth = event["amount_groth"]
            if event["status"] == "confirmed" and confirmations < 2:
                expected, new_status, delta_groth = (
                    "confirmed", "reversed", -amount_groth
                )
            elif event["status"] == "reversed" and final:
                expected, new_status, delta_groth = (
                    "reversed", "confirmed", amount_groth
                )
            else:
                expected = new_status = event["status"]
                delta_groth = 0
                if not event.get("review_required"):
                    continue

            def apply_confirmation_change(
                session,
                event_id=event["_id"],
                user_id=event["user_id"],
                expected=expected,
                new_status=new_status,
                delta_groth=delta_groth,
            ):
                current = self.col_txs.find_one(
                    {"_id": event_id, "status": expected}, session=session
                )
                if current is None or (delta_groth == 0 and not current.get("review_required")):
                    return False
                update = {"$set": {"status": new_status}}
                if current.get("review_required"):
                    update["$unset"] = {
                        "review_required": "",
                        "review_reason": "",
                        "reviewReportedAt": "",
                    }
                changed = self.col_txs.update_one(
                    {"_id": event_id, "status": expected},
                    update,
                    session=session,
                )
                if changed.modified_count != 1:
                    return False
                if delta_groth:
                    user = self.col_users.update_one(
                        {"_id": user_id},
                        {"$inc": {
                            "BalanceGroth": delta_groth,
                            "Balance": groth_to_float(delta_groth),
                        }},
                        session=session,
                    )
                    if user.modified_count != 1:
                        raise RuntimeError("reorg deposit recipient disappeared")
                return True

            self.run_transaction(apply_confirmation_change)

    def apply_deposits(self, transaction):
        txid = transaction["txid"]
        scan_id = "deposit-scan:%s" % txid
        if self.col_txs.find_one({"_id": scan_id}):
            return

        legacy = self.col_txs.find_one(
            {
                "txId": txid,
                "type": "deposit",
                "address": {"$exists": False},
            }
        )
        if legacy:
            reported = self.col_txs.update_one(
                {
                    "_id": legacy["_id"],
                    "legacyReportedAt": {"$exists": False},
                },
                {
                    "$set": {
                        "legacyReportedAt": datetime.datetime.utcnow(),
                        "review_required": True,
                    }
                },
            )
            if reported.modified_count == 1:
                self.send_to_logs(
                    "Legacy deposit %s needs manual review before output-level migration"
                    % txid
                )
            return

        totals = defaultdict(int)
        for output in self.wallet_api.get_spark_coin_address(txid):
            try:
                address = output["address"]
                amount_groth = firo_to_groth(output["amount"])
            except (KeyError, TypeError, ValueError) as exc:
                raise FiroTransportError("invalid Spark output for %s" % txid) from exc
            if not isinstance(address, str) or not address or amount_groth < 0:
                raise FiroTransportError("invalid Spark output for %s" % txid)
            if amount_groth == 0:
                continue
            totals[address] += amount_groth
        if not totals:
            # ponytail: retry empty results; classify non-Spark receives only if repeated RPCs become costly.
            return

        retired = self.col_state.find_one({"_id": "retired_deposit_addresses"})
        retired_addresses = set(retired.get("addresses", [])) if retired else set()
        orphaned = False
        for address, amount_groth in totals.items():
            user = self.col_users.find_one({"Address": address})
            if user is None:
                orphaned = True
                try:
                    self.col_txs.insert_one(
                        {
                            "_id": "deposit-orphan:%s:%s" % (txid, address),
                            "txId": txid,
                            "address": address,
                            "amount": groth_to_float(amount_groth),
                            "amount_groth": amount_groth,
                            "type": "deposit-orphan",
                            "eventVersion": 2,
                            "moneySchemaVersion": 1,
                            "status": "review_required",
                            "timestamp": datetime.datetime.utcnow(),
                        }
                    )
                except DuplicateKeyError:
                    pass
                else:
                    address_kind = (
                        "a retired shared address"
                        if address in retired_addresses else "an unassigned wallet address"
                    )
                    self.send_to_logs(
                        "Deposit %s sent %s FIRO to %s and needs manual ownership review"
                        % (txid, format_groth(amount_groth), address_kind)
                    )
                continue

            amount = groth_to_float(amount_groth)
            event_id = "deposit:%s:%s" % (txid, address)
            event = {
                    "_id": event_id,
                    "txId": txid,
                    "address": address,
                    "user_id": user["_id"],
                    "amount": amount,
                    "amount_groth": amount_groth,
                    "type": "deposit",
                    "eventVersion": 2,
                    "moneySchemaVersion": 1,
                    "status": "confirmed",
                    "timestamp": datetime.datetime.utcnow(),
            }

            def credit(
                session,
                event=event,
                user_id=user["_id"],
                amount=amount,
                amount_groth=amount_groth,
            ):
                self.col_txs.insert_one(event, session=session)
                result = self.col_users.update_one(
                    {"_id": user_id},
                    {
                        "$inc": {
                            "BalanceGroth": amount_groth,
                            "Balance": amount,
                        }
                    },
                    session=session,
                )
                if result.modified_count != 1:
                    raise RuntimeError("deposit recipient disappeared")

            try:
                self.run_transaction(credit)
            except DuplicateKeyError:
                continue

            self.create_receive_tips_image(
                user["_id"], format_groth(amount_groth), "Deposit", is_deposit=True
            )
            print(
                "*Deposit Success*\nBalance of address %s increased by %s FIRO."
                % (address, amount)
            )

        self.col_txs.update_one(
            {"_id": scan_id},
            {
                "$setOnInsert": {
                    "_id": scan_id,
                    "txId": txid,
                    "type": "deposit-scan",
                    "eventVersion": 2,
                    "status": "review_required" if orphaned else "complete",
                    "orphaned": orphaned,
                    "timestamp": datetime.datetime.utcnow(),
                }
            },
            upsert=True,
        )

    def report_withdrawal_once(self, sender, reason):
        reported = self.col_senders.update_one(
            {
                "_id": sender["_id"],
                "review_reason": {"$ne": str(reason)},
            },
            {
                "$set": {
                    "review_required": True,
                    "review_reason": str(reason),
                    "reviewReportedAt": datetime.datetime.utcnow(),
                }
            },
        )
        if reported.modified_count == 1:
            self.send_to_logs(
                "Withdrawal %s needs review: %s"
                % (sender.get("txId", sender["_id"]), reason)
            )

    def recover_ambiguous_withdrawal(self, sender, transactions):
        if sender.get("txId"):
            self.col_senders.update_one(
                {
                    "_id": sender["_id"],
                    "status": {"$in": ["broadcasting", "unknown"]},
                },
                {
                    "$set": {"status": "pending"},
                    "$unset": {
                        "review_required": "",
                        "review_reason": "",
                        "reviewReportedAt": "",
                    },
                },
            )
            return

        started = sender.get("broadcast_started_at")
        if not isinstance(started, datetime.datetime):
            self.report_withdrawal_once(sender, "missing broadcast timestamp")
            return
        started_at = calendar.timegm(started.utctimetuple())
        candidates = set()
        for transaction in transactions:
            txid = transaction.get("txid")
            tx_time = transaction.get("time", transaction.get("timereceived"))
            if (
                not txid
                or transaction.get("category") != "spend"
                or transaction.get("address") != sender["address"]
                or not isinstance(tx_time, (int, float))
                or tx_time < started_at - 300
                or tx_time > started_at + 300
            ):
                continue
            try:
                amount_groth = (
                    abs(firo_to_groth(transaction["amount"]))
                    + abs(firo_to_groth(transaction.get("fee", 0)))
                )
            except (KeyError, ValueError):
                continue
            if amount_groth != sender["send_amount_groth"]:
                continue
            assigned = self.col_senders.find_one({"txId": txid})
            if assigned is None or assigned["_id"] == sender["_id"]:
                candidates.add(txid)

        # Address, amount and time cannot prove which command created a transaction.
        if candidates or time.time() - started_at >= 300:
            reason = (
                "no matching wallet transaction"
                if not candidates
                else "wallet candidates require manual verification: %s"
                % ", ".join(sorted(candidates))
            )
            self.report_withdrawal_once(sender, reason)

    def reconcile_withdrawals(self, transactions):
        now = datetime.datetime.utcnow()
        completed_check_failed = False
        for sender in self.col_senders.find(
            {"schemaVersion": 2, "status": {"$in": ["reserved", "rejected"]}}
        ):
            created_at = sender.get("created_at")
            if (
                sender["status"] == "reserved"
                and isinstance(created_at, datetime.datetime)
                and (now - created_at).total_seconds() < 300
            ):
                continue
            try:
                if self.refund_withdrawal(
                    sender["_id"],
                    sender.get("error", "Interrupted before broadcast"),
                    allowed_statuses=[sender["status"]],
                ):
                    self.send_to_logs(
                        "Refunded unbroadcast withdrawal %s" % sender["_id"]
                    )
            except Exception as exc:
                self.report_withdrawal_once(sender, exc)

        for sender in self.col_senders.find(
            {
                "schemaVersion": 2,
                "status": {"$in": ["broadcasting", "unknown"]},
            }
        ):
            try:
                self.recover_ambiguous_withdrawal(sender, transactions)
            except Exception as exc:
                self.report_withdrawal_once(sender, exc)

        for sender in self.col_senders.find(
            {
                "schemaVersion": 2,
                "status": {
                    "$in": [
                        "pending",
                        "completed",
                        "reorged",
                        "conflicted",
                    ]
                },
            }
        ):
            txid = sender.get("txId")
            if not txid:
                self.report_withdrawal_once(sender, "missing transaction id")
                if sender["status"] == "completed":
                    completed_check_failed = True
                continue
            try:
                response = self.wallet_api.get_tx_status(txid)
                if response.get("error"):
                    self.report_withdrawal_once(sender, response["error"])
                    if sender["status"] == "completed":
                        completed_check_failed = True
                    continue
                transaction = response["result"]
                confirmations = transaction.get("confirmations", 0)
                final = is_final_transaction(transaction)
                status = sender["status"]
                if status in ("pending", "reorged", "conflicted"):
                    if final:
                        self.complete_withdrawal(sender, transaction)
                    elif confirmations < 0 and status != "conflicted":
                        self.mark_withdrawal_conflicted(sender)
                elif status == "completed":
                    if not final:
                        self.reverse_completed_withdrawal(sender, confirmations)
                    else:
                        self.col_senders.update_one(
                            {"_id": sender["_id"], "status": "completed"},
                            {
                                "$unset": {
                                    "review_required": "",
                                    "review_reason": "",
                                    "reviewReportedAt": "",
                                }
                            },
                        )
            except Exception as exc:
                self.report_withdrawal_once(sender, exc)
                if sender["status"] == "completed":
                    completed_check_failed = True
        if completed_check_failed:
            raise FiroTransportError("completed withdrawal finality could not be verified")

    def release_user_lock(self, session, user_id, amount_groth, refund=False):
        user = self.col_users.find_one({"_id": user_id}, session=session)
        if user is None or user.get("LockedGroth", -1) < amount_groth:
            raise RuntimeError("withdrawal lock invariant failed")
        remaining = user["LockedGroth"] - amount_groth
        increments = {
            "LockedGroth": -amount_groth,
            "Locked": -groth_to_float(amount_groth),
        }
        if refund:
            increments.update(
                {
                    "BalanceGroth": amount_groth,
                    "Balance": groth_to_float(amount_groth),
                }
            )
        changed = self.col_users.update_one(
            {
                "_id": user_id,
                "LockedGroth": user["LockedGroth"],
            },
            {
                "$inc": increments,
                "$set": {
                    "IsWithdraw": (
                        remaining > 0
                        or user.get("WithdrawalQuarantined") is True
                    )
                },
            },
            session=session,
        )
        if changed.modified_count != 1:
            raise RuntimeError("withdrawal lock changed concurrently")

    def mark_withdrawal_conflicted(self, sender):
        changed = self.col_senders.update_one(
            {
                "_id": sender["_id"],
                "status": {"$in": ["pending", "reorged"]},
            },
            {
                "$set": {
                    "status": "conflicted",
                    "error": "Transaction has negative confirmations",
                    "review_required": True,
                    "updated_at": datetime.datetime.utcnow(),
                }
            },
        )
        if changed.modified_count == 1:
            self.report_withdrawal_once(
                sender, "transaction has negative confirmations and remains locked"
            )

    def reverse_completed_withdrawal(self, sender, confirmations):
        amount_groth = sender["locked_amount_groth"]
        new_status = "conflicted" if confirmations < 0 else "reorged"

        def reverse(session):
            event = self.col_txs.update_one(
                {"_id": "withdraw:%s" % sender["txId"], "status": "confirmed"},
                {
                    "$set": {
                        "status": "reversed",
                        "reversed_at": datetime.datetime.utcnow(),
                    }
                },
                session=session,
            )
            if event.modified_count != 1:
                raise RuntimeError("confirmed withdrawal event is missing")
            changed = self.col_senders.update_one(
                {"_id": sender["_id"], "status": "completed"},
                {
                    "$set": {
                        "status": new_status,
                        "review_required": True,
                        "updated_at": datetime.datetime.utcnow(),
                    }
                },
                session=session,
            )
            if changed.modified_count != 1:
                raise RuntimeError("withdrawal intent changed during reorg")
            user = self.col_users.update_one(
                {"_id": sender["user_id"]},
                {
                    "$inc": {
                        "LockedGroth": amount_groth,
                        "Locked": groth_to_float(amount_groth),
                    },
                    "$set": {"IsWithdraw": True},
                },
                session=session,
            )
            if user.modified_count != 1:
                raise RuntimeError("withdrawal owner disappeared during reorg")

        self.run_transaction(reverse)
        self.report_withdrawal_once(
            sender, "confirmed transaction lost finality and was re-locked"
        )

    def complete_withdrawal(self, sender, transaction):
        txid = sender["txId"]
        amount_groth = sender["locked_amount_groth"]
        event_id = "withdraw:%s" % txid
        event = {
                "_id": event_id,
                "schemaVersion": 2,
                "eventVersion": 2,
                "moneySchemaVersion": 1,
                "sender_id": sender["_id"],
                "user_id": sender["user_id"],
                "txId": txid,
                "type": "withdraw",
                "status": "confirmed",
                "amount": sender["amount"],
                "amount_groth": sender["amount_groth"],
                "send_amount": sender["send_amount"],
                "send_amount_groth": sender["send_amount_groth"],
                "locked_amount": sender["locked_amount"],
                "locked_amount_groth": amount_groth,
                "timestamp": datetime.datetime.utcnow(),
        }

        def complete(session):
            existing = self.col_txs.find_one({"_id": event_id}, session=session)
            if existing is None:
                self.col_txs.insert_one(event, session=session)
            elif existing.get("status") == "reversed":
                restored = self.col_txs.update_one(
                    {"_id": event_id, "status": "reversed"},
                    {
                        "$set": {
                            "status": "confirmed",
                            "reconfirmed_at": datetime.datetime.utcnow(),
                        }
                    },
                    session=session,
                )
                if restored.modified_count != 1:
                    raise RuntimeError("withdrawal event changed during completion")
            elif existing.get("status") == "confirmed":
                return False
            else:
                raise RuntimeError("withdrawal event has an invalid status")

            self.release_user_lock(
                session,
                sender["user_id"],
                amount_groth,
            )
            pending = self.col_senders.update_one(
                {
                    "_id": sender["_id"],
                    "status": {
                        "$in": ["pending", "reorged", "conflicted"]
                    },
                },
                {
                    "$set": {
                        "status": "completed",
                        "completed_at": datetime.datetime.utcnow(),
                    },
                    "$unset": {
                        "review_required": "",
                        "review_reason": "",
                        "reviewReportedAt": "",
                    },
                },
                session=session,
            )
            if pending.modified_count != 1:
                raise RuntimeError("withdrawal intent changed during completion")
            return True

        try:
            completed = self.run_transaction(complete)
        except DuplicateKeyError:
            completed = False
        if not completed:
            return

        self.send_message(
            sender["user_id"],
            "Withdrawal %s confirmed. The recipient received up to %s FIRO "
            "after the network fee was deducted from the output."
            % (txid, format_groth(sender["send_amount_groth"])),
        )
        print(
            "*Withdrawal Success*\nUser %s withdrew %s FIRO."
            % (sender["user_id"], format_groth(sender["send_amount_groth"]))
        )

    def get_user_data(self):
        """
            Get user data
        """
        user = self.col_users.find_one({"_id": self.user_id})
        if user is None:
            return None, None, None, None
        return (
            normalize_addresses(user.get('Address')),
            groth_to_decimal(user['BalanceGroth']),
            groth_to_decimal(user['LockedGroth']),
            user['IsWithdraw'],
        )

    def update_address_and_balance(self, user):
        # Check if User has a Spark address
        addresses = normalize_addresses(user.get('Address'))
        if not addresses:
            raise RuntimeError("existing user has no deposit address")

        response = self.wallet_api.validate_address(addresses[-1])
        if response.get("error"):
            raise RuntimeError(response["error"])
        valid = response["result"]
        changed = user.get("Address") != addresses

        if valid.get("isvalidSpark") is not True:
            new_addresses = self.create_safe_deposit_addresses()
            for address in new_addresses:
                if address not in addresses:
                    addresses.append(address)
                    changed = True

        if changed:
            self.col_users.update_one(
                {"_id": user["_id"]},
                {"$set": {"Address": addresses}},
            )
        return addresses

    def withdraw_coins(self, address, amount, comment=""):
        with self.accounting_lock:
            return self._withdraw_coins(address, amount, comment)

    def _withdraw_coins(self, address, amount, comment=""):
        """
            Withdraw coins to address with params:
            address
            amount
        """
        try:
            amount_decimal = parse_amount(amount, WITHDRAW_FEE + FIRO_QUANTUM)
        except ValueError:
            self.send_message(
                self.user_id,
                dictionary['incorrect_amount'],
                parse_mode='HTML',
            )
            return

        if self.outgoing_paused():
            self.send_message(
                self.user_id,
                "<b>Transfers are paused until wallet reconciliation completes.</b>",
                parse_mode='HTML',
            )
            return

        validation_response = self.wallet_api.validate_address(address)
        if validation_response.get("error"):
            self.send_message(
                self.user_id,
                "<b>Address validation failed. No balance was charged.</b>",
                parse_mode='HTML',
            )
            return
        validation = validation_response["result"]
        valid = (
            validation.get("isvalid") is True
            or validation.get("isvalidSpark") is True
        )
        if not valid:
            self.send_message(
                self.user_id,
                "<b>You specified an incorrect address.</b>",
                parse_mode='HTML',
            )
            return

        if validation.get("ismine") is True or self.col_users.find_one(
            {"Address": address}, {"_id": 1}
        ):
            self.send_message(
                self.user_id,
                "<b>That address belongs to the tipbot. Use a reply-to /tip instead.</b>",
                parse_mode='HTML',
            )
            return

        amount_groth = firo_to_groth(amount_decimal)
        send_amount_groth = amount_groth - WITHDRAW_FEE_GROTH
        amount_float = groth_to_float(amount_groth)
        send_amount = groth_to_float(send_amount_groth)
        intent_id = self.command_id("withdraw")
        intent = {
            "_id": intent_id,
            "schemaVersion": 2,
            "moneySchemaVersion": 1,
            "user_id": self.user_id,
            "address": address,
            "amount": amount_float,
            "amount_groth": amount_groth,
            "send_amount": send_amount,
            "send_amount_groth": send_amount_groth,
            "locked_amount": amount_float,
            "locked_amount_groth": amount_groth,
            "fee": groth_to_float(WITHDRAW_FEE_GROTH),
            "fee_groth": WITHDRAW_FEE_GROTH,
            "comment": comment,
            "status": "reserved",
            "created_at": datetime.datetime.utcnow(),
        }
        existing = self.col_senders.find_one({"_id": intent_id})

        def reserve(session):
            if self.outgoing_paused(session):
                return False
            user = self.col_users.update_one(
                {
                    "_id": self.user_id,
                    "IsWithdraw": {"$ne": True},
                    "WithdrawalQuarantined": {"$ne": True},
                    "BalanceGroth": {"$gte": amount_groth},
                },
                {
                    "$inc": {
                        "BalanceGroth": -amount_groth,
                        "LockedGroth": amount_groth,
                        "Balance": -amount_float,
                        "Locked": amount_float,
                    },
                    "$set": {"IsWithdraw": True},
                },
                session=session,
            )
            if user.modified_count != 1:
                return False
            self.col_senders.insert_one(intent, session=session)
            return True

        if existing is not None:
            reserved = existing.get("status") == "reserved"
        else:
            try:
                reserved = self.run_transaction(reserve)
            except DuplicateKeyError:
                existing = self.col_senders.find_one({"_id": intent_id})
                reserved = bool(
                    existing and existing.get("status") == "reserved"
                )

        if not reserved:
            existing = self.col_senders.find_one({"_id": intent_id})
            if existing is not None:
                self.send_message(
                    self.user_id,
                    "<b>This withdrawal is already being processed. Do not retry it.</b>",
                    parse_mode='HTML',
                )
                return
            user = self.col_users.find_one({"_id": self.user_id})
            if self.outgoing_paused():
                self.send_message(
                    self.user_id,
                    "<b>Transfers are paused until wallet reconciliation completes.</b>",
                    parse_mode='HTML',
                )
            elif user and user.get("IsWithdraw"):
                self.send_message(
                    self.user_id,
                    dictionary['withdrawal_busy'],
                    parse_mode='HTML',
                )
            else:
                self.insufficient_balance_image()
            return

        current = self.col_senders.find_one({"_id": intent_id})
        address = current["address"]
        send_amount_groth = current["send_amount_groth"]
        comment = current.get("comment", "")
        broadcasting = self.col_senders.update_one(
            {"_id": intent_id, "status": "reserved"},
            {
                "$set": {
                    "status": "broadcasting",
                    "broadcast_started_at": datetime.datetime.utcnow(),
                }
            },
        )
        if broadcasting.modified_count != 1:
            self.send_message(
                self.user_id,
                "<b>This withdrawal is already being processed. Do not retry it.</b>",
                parse_mode='HTML',
            )
            return

        try:
            response = self.wallet_api.spendspark(
                address,
                format_groth(send_amount_groth),
                comment,
                subtract_fee=True,
            )
            error = response.get("error")
            # Core can persist a spend before reporting a generic wallet error.
            # Only these validation/unlock/dispatch errors prove no spend was made.
            if error and (
                not isinstance(error, dict)
                or error.get("code") not in {-3, -5, -8, -13, -32601}
            ):
                raise FiroRPCError(error)
        except (FiroTransportError, FiroRPCError) as exc:
            self.col_senders.update_one(
                {"_id": intent_id, "status": "broadcasting"},
                {
                    "$set": {
                        "status": "unknown",
                        "error": str(exc),
                        "updated_at": datetime.datetime.utcnow(),
                    }
                },
            )
            self.send_message(
                self.user_id,
                "<b>The withdrawal outcome is uncertain. Your funds remain locked for review. Do not retry.</b>",
                parse_mode='HTML',
            )
            self.send_to_logs("Ambiguous withdrawal %s: %s" % (intent_id, exc))
            return

        if response.get('error'):
            rejected = self.col_senders.update_one(
                {"_id": intent_id, "status": "broadcasting"},
                {"$set": {"status": "rejected", "error": response["error"]}},
            )
            if rejected.modified_count != 1:
                raise RuntimeError("withdrawal rejection could not be recorded")
            refunded = self.refund_withdrawal(
                intent_id,
                response["error"],
                allowed_statuses=["rejected"],
            )
            if not refunded and not self.col_senders.find_one(
                {"_id": intent_id, "status": "failed"}
            ):
                raise RuntimeError("withdrawal rejection could not be refunded")
            self.send_message(
                self.user_id,
                self.withdrawal_error_message(response["error"]),
                parse_mode='HTML',
            )
            self.send_to_logs("Rejected withdrawal %s: %s" % (intent_id, response))
            return

        txid = response["result"]
        if not isinstance(txid, str) or not txid:
            self.col_senders.update_one(
                {"_id": intent_id, "status": "broadcasting"},
                {
                    "$set": {
                        "status": "unknown",
                        "error": "Firo RPC returned no transaction id",
                        "updated_at": datetime.datetime.utcnow(),
                    }
                },
            )
            self.send_message(
                self.user_id,
                "<b>The node returned an invalid transaction response. Your funds remain locked while it is checked. Do not retry.</b>",
                parse_mode="HTML",
            )
            self.send_to_logs(
                "Ambiguous withdrawal %s returned no transaction id" % intent_id
            )
            return
        updated = self.col_senders.update_one(
            {"_id": intent_id, "status": "broadcasting"},
            {
                "$set": {
                    "txId": txid,
                    "status": "pending",
                    "broadcast_at": datetime.datetime.utcnow(),
                }
            },
        )
        if updated.modified_count != 1:
            raise RuntimeError(
                "withdrawal broadcast succeeded but its intent could not record the txid"
            )

        self.withdraw_image(
            self.user_id,
            format_groth(send_amount_groth),
            address,
            msg=("Your txId %s. The 0.002 FIRO bot fee is included; the "
                 "network fee is deducted from the displayed recipient amount.") % txid,
        )

    @staticmethod
    def withdrawal_error_message(error):
        message = str(error.get("message", error)).lower() if isinstance(error, dict) else str(error).lower()
        if "address" in message or "amount" in message or "parameter" in message:
            return "<b>The node rejected the address or amount. No balance was charged.</b>"
        if "input" in message or "insufficient" in message or "select" in message:
            return "<b>The wallet does not currently have enough confirmed Spark inputs. No balance was charged.</b>"
        return "<b>The node rejected the withdrawal. No balance was charged.</b>"

    def refund_withdrawal(self, intent_id, error, allowed_statuses):
        def refund(session):
            sender = self.col_senders.find_one(
                {"_id": intent_id, "status": {"$in": allowed_statuses}},
                session=session,
            )
            if sender is None:
                return False
            self.release_user_lock(
                session,
                sender["user_id"],
                sender["locked_amount_groth"],
                refund=True,
            )
            changed = self.col_senders.update_one(
                {"_id": intent_id, "status": {"$in": allowed_statuses}},
                {
                    "$set": {
                        "status": "failed",
                        "error": error,
                        "completed_at": datetime.datetime.utcnow(),
                    }
                },
                session=session,
            )
            if changed.modified_count != 1:
                raise RuntimeError("withdrawal intent changed during refund")
            return True

        return self.run_transaction(refund)

    def tip_user(self, username, amount, comment, _type=None):
        """
            Tip user with params:
            username
            amount
        """
        self.send_message(
            self.user_id,
            "<b>Username tips are disabled because Telegram usernames can change. Reply to the recipient's message with /tip instead.</b>",
            parse_mode='HTML',
        )

    def tip_in_the_chat(self, amount, comment="", _type=None):
        """
            Send a tip to user in the chat
        """
        try:
            reply = getattr(self.message, "reply_to_message", None)
            recipient = getattr(reply, "from_user", None)
            if recipient is None:
                self.send_message(
                    self.user_id,
                    "<b>Reply to a message sent by a Telegram user, not a channel.</b>",
                    parse_mode="HTML",
                )
                return
            try:
                amount = parse_amount(amount)
            except ValueError as exc:
                self.incorrect_parametrs_image()
                print(exc)
                return

            self.send_tip(
                recipient.id,
                amount,
                _type,
                comment
            )

        except Exception as exc:
            print(exc)
            traceback.print_exc()
            raise

    def send_tip(self, user_id, amount, _type, comment):
        """
            Send tip to user with params
            user_id - user identificator
            addrees - user address
            amount - amount of a tip
        """
        if self.user_id == user_id:
            self.send_message(
                self.user_id,
                "<b>You can't send tips to yourself!</b>",
                parse_mode='HTML'
            )
            return

        try:
            amount_groth = firo_to_groth(parse_amount(amount))
        except ValueError:
            self.incorrect_parametrs_image()
            return
        if len(comment.encode("utf-16-le")) // 2 > MAX_TIP_COMMENT_UTF16:
            self.send_message(self.user_id, "Tip comment is too long; please shorten it.")
            return
        amount = groth_to_float(amount_groth)

        receiver = self.col_users.find_one(
            {"_id": user_id, "IsVerified": True}
        )
        if receiver is None:
            self.send_message(
                self.user_id,
                dictionary['username_error'],
                parse_mode='HTML',
            )
            return

        transfer_id = self.command_id("tip")
        tip_type = "atip" if _type == "anonymous" else "tip"

        def transfer(session):
            if self.outgoing_paused(session):
                return False
            debited = self.col_users.update_one(
                {
                    "_id": self.user_id,
                    "WithdrawalQuarantined": {"$ne": True},
                    "BalanceGroth": {"$gte": amount_groth},
                },
                {
                    "$inc": {
                        "BalanceGroth": -amount_groth,
                        "Balance": -amount,
                    }
                },
                session=session,
            )
            if debited.modified_count != 1:
                return False

            credited = self.col_users.update_one(
                {"_id": user_id, "IsVerified": True},
                {
                    "$inc": {
                        "BalanceGroth": amount_groth,
                        "Balance": amount,
                    }
                },
                session=session,
            )
            if credited.modified_count != 1:
                raise RuntimeError("tip recipient disappeared")

            self.col_tip_logs.insert_one(
                {
                    "_id": transfer_id,
                    "type": tip_type,
                    "from_user_id": self.user_id,
                    "to_user_id": user_id,
                    "amount": amount,
                    "amount_groth": amount_groth,
                    "moneySchemaVersion": 1,
                    "timestamp": datetime.datetime.utcnow(),
                },
                session=session,
            )
            return True

        try:
            transferred = self.run_transaction(transfer)
        except DuplicateKeyError:
            return

        if not transferred:
            sender = self.col_users.find_one({"_id": self.user_id})
            if self.outgoing_paused() or (sender and sender.get("WithdrawalQuarantined") is True):
                self.send_message(
                    self.user_id,
                    "<b>Transfers are paused pending review or wallet reconciliation.</b>",
                    parse_mode="HTML",
                )
            else:
                self.insufficient_balance_image()
            return

        sender_name = "Anonymous" if _type == 'anonymous' else self.first_name
        self.create_send_tips_image(
            self.user_id,
            format_groth(amount_groth),
            receiver.get('first_name') or str(user_id),
            comment,
        )
        self.create_receive_tips_image(
            receiver['_id'],
            format_groth(amount_groth),
            sender_name,
            comment,
        )

    def create_receive_tips_image(self, user_id, amount, first_name, comment="", is_deposit=False):
        try:
            im = Image.open("images/receive_template.png")
            d = ImageDraw.Draw(im)

            location_f = (266, 21)
            location_s = (266, 45)
            location_t = (266, 67)
            if is_deposit:
                d.text(location_f, "%s" % first_name, font=bold, fill='#000000')
                d.text(location_s, "has recharged", font=regular, fill='#000000')

            else:
                d.text(location_f, "%s" % first_name, font=bold, fill='#000000')
                d.text(location_s, "sent you a tip of", font=regular, fill='#000000')
            d.text(location_t, "%s Firo" % amount.rstrip("0").rstrip("."),
                   font=money_bold, fill='#000000')

            receive_img = self.image_buffer(im)
            if comment == "":
                self.bot.send_photo(
                    user_id,
                    receive_img,
                )
            else:
                self.bot.send_photo(
                    user_id,
                    receive_img,
                    caption="<b>Comment:</b> <i>%s</i>" % self.cleanhtml(comment),
                    parse_mode='HTML'
                )


        except Exception as exc:
            try:
                print(exc)
                if 'blocked' in str(exc):
                    self.send_message(self.group_id,
                                      "<a href='tg://user?id=%s'>User</a> <b>needs to unblock the bot in order to check their balance!</b>" % user_id,
                                      parse_mode='HTML')
                traceback.print_exc()
            except Exception as exc:
                print(exc)

    def create_send_tips_image(self, user_id, amount, first_name, comment=""):
        try:
            im = Image.open("images/send_template.png")

            d = ImageDraw.Draw(im)
            location_f = (276, 21)
            location_s = (276, 45)
            location_t = (276, 67)
            d.text(location_f, "%s Firo" % amount.rstrip("0").rstrip("."),
                   font=money_bold, fill='#000001')
            d.text(location_s, "tip was sent to", font=regular, fill='#000000')
            d.text(location_t, "%s" % first_name, font=bold, fill='#000000')
            send_img = self.image_buffer(im)
            if comment == "":
                self.bot.send_photo(
                    user_id,
                    send_img)
            else:
                self.bot.send_photo(
                    user_id,
                    send_img,
                    caption="<b>Comment:</b> <i>%s</i>" % self.cleanhtml(comment),
                    parse_mode='HTML'
                )

        except Exception as exc:
            try:
                print(exc)
                if 'blocked' in str(exc):
                    self.send_message(self.group_id,
                                      "<a href='tg://user?id=%s'>User</a> <b>needs to unblock the bot in order to check their balance!</b>" % user_id,
                                      parse_mode='HTML')
                traceback.print_exc()
            except Exception as exc:
                print(exc)
                traceback.print_exc()

    def withdraw_image(self, user_id, amount, address, msg=None):
        try:
            im = Image.open("images/withdraw_template.png")

            d = ImageDraw.Draw(im)
            location_transfer = (256, 21)
            location_amount = (276, 45)
            location_addess = (256, 65)

            d.text(location_transfer, "Transaction transfer", font=regular,
                   fill='#000000')
            d.text(location_amount, "%s Firo" % amount.rstrip("0").rstrip("."),
                   font=money_bold, fill='#000001')
            d.text(location_addess, "to %s..." % address[:8], font=bold,
                   fill='#000000')
            self.bot.send_photo(
                user_id,
                self.image_buffer(im),
                caption=str(msg),
            )
        except Exception as exc:
            print(exc)
            traceback.print_exc()

    def create_wallet_image(self, public_address):
        try:
            im = Image.open("images/create_wallet_template.png")

            d = ImageDraw.Draw(im)
            location_transfer = (258, 32)

            d.text(location_transfer, "Wallet created", font=bold,
                   fill='#000000')
            address = normalize_addresses(public_address)[-1]
            self.bot.send_photo(
                self.user_id,
                self.image_buffer(im),
                caption=dictionary['welcome'] % html.escape(address, quote=True),
                parse_mode='HTML',
                read_timeout=200
            )
        except Exception as exc:
            print(exc)
            traceback.print_exc()

    def withdraw_failed_image(self, user_id):
        try:
            im = Image.open("images/withdraw_failed_template.png")

            d = ImageDraw.Draw(im)
            location_text = (230, 52)

            d.text(location_text, "Withdraw failed", font=bold, fill='#000000')

            self.bot.send_photo(
                user_id,
                self.image_buffer(im),
                dictionary['withdrawal_failed'],
                parse_mode='HTML'
            )
        except Exception as exc:
            print(exc)
            traceback.print_exc()

    def insufficient_balance_image(self):
        try:
            im = Image.open("images/insufficient_balance_template.png")

            d = ImageDraw.Draw(im)
            location_text = (230, 62)

            d.text(location_text, "Insufficient Balance", font=bold, fill='#000000')

            im = im.convert("RGB")
            try:
                user = self.col_users.find_one(
                    {"_id": self.user_id}, {"BalanceGroth": 1}
                )
                balance = (
                    groth_to_float(user.get("BalanceGroth", 0))
                    if user else 0
                )
                self.bot.send_photo(
                    self.user_id,
                    self.image_buffer(im),
                    caption=dictionary['incorrect_balance'] % "{0:.8f}".format(
                        float(balance)),
                    parse_mode='HTML'
                )
            except Exception as exc:
                print(exc)
        except Exception as exc:
            print(exc)
            traceback.print_exc()

    def red_envelope_catched(self, amount):
        try:
            im = Image.open("images/red_envelope_catched.png")

            d = ImageDraw.Draw(im)
            location_transfer = (236, 35)
            location_amount = (256, 65)
            location_addess = (205, 95)

            d.text(location_transfer, "You caught", font=bold, fill='#000000')
            d.text(location_amount, "%s Firo" % amount.rstrip("0").rstrip("."),
                   font=money_bold, fill='#f72c56')
            d.text(location_addess, "FROM A RED ENVELOPE", font=regular, fill='#000000')
            try:
                self.bot.send_photo(
                    self.user_id,
                    self.image_buffer(im),
                )
            except Exception as exc:
                print(exc)
        except Exception as exc:
            print(exc)
            traceback.print_exc()

    def red_envelope_created(self, first_name, envelope_id):
        try:
            im = Image.open("images/red_envelope_created.png")
            d = ImageDraw.Draw(im)
            location_who = (230, 35)
            location_note = (256, 70)
            d.text(location_who, "%s CREATED" % first_name, font=bold, fill='#000000')
            d.text(location_note, "A RED ENVELOPE", font=bold, fill='#f72c56')
            response = self.bot.send_photo(
                self.group_id,
                self.image_buffer(im),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(
                        text='Catch Firo✋',
                        callback_data='catch_envelope|%s' % envelope_id
                    )]]
                )
            )
            return response.message_id
        except Exception as exc:
            print(exc)
            return 0

    def red_envelope_ended(self):
        im = Image.open("images/red_envelope_ended.png")

        d = ImageDraw.Draw(im)
        location_who = (256, 41)
        location_note = (306, 75)

        d.text(location_who, "RED ENVELOPE", font=bold, fill='#000000')
        d.text(location_note, "ENDED", font=bold, fill='#f72c56')
        try:
            self.bot.send_photo(
                self.user_id,
                self.image_buffer(im),
            )
        except Exception as exc:
            print(exc)

    def incorrect_parametrs_image(self):
        try:
            im = Image.open("images/incorrect_parametrs_template.png")

            d = ImageDraw.Draw(im)
            location_text = (230, 62)

            d.text(location_text, "Incorrect parameters", font=bold,
                   fill='#000000')

            im = im.convert("RGB")
            self.bot.send_photo(
                self.user_id,
                self.image_buffer(im),
                caption=dictionary['incorrect_parametrs'],
                parse_mode='HTML'
            )
        except Exception as exc:
            print(exc)
            traceback.print_exc()

    def create_red_envelope(self, amount):
        try:
            amount_groth = firo_to_groth(
                parse_amount(amount, MIN_ENVELOPE)
            )
        except ValueError:
            self.incorrect_parametrs_image()
            return
        amount_float = groth_to_float(amount_groth)

        envelope_id = self.command_id("envelope")
        envelope = {
            "_id": envelope_id,
            "schemaVersion": 2,
            "moneySchemaVersion": 1,
            "amount": amount_float,
            "amount_groth": amount_groth,
            "remains": amount_float,
            "remains_groth": amount_groth,
            "group_id": self.group_id,
            "group_username": self.group_username,
            "group_type": self.message.chat.type,
            "creator_id": self.user_id,
            "msg_id": None,
            "takers": [],
            "status": "creating",
            "created_at": datetime.datetime.utcnow(),
        }

        def reserve(session):
            if self.col_envelopes.find_one({"_id": envelope_id}, session=session):
                return True
            if self.outgoing_paused(session):
                return False
            debited = self.col_users.update_one(
                {
                    "_id": self.user_id,
                    "WithdrawalQuarantined": {"$ne": True},
                    "BalanceGroth": {"$gte": amount_groth},
                },
                {
                    "$inc": {
                        "BalanceGroth": -amount_groth,
                        "Balance": -amount_float,
                    }
                },
                session=session,
            )
            if debited.modified_count != 1:
                return False
            self.col_envelopes.insert_one(envelope, session=session)
            return True

        try:
            reserved = self.run_transaction(reserve)
        except DuplicateKeyError:
            reserved = True

        if reserved:
            existing = self.col_envelopes.find_one({"_id": envelope_id})
            if existing and existing.get("status") == "rejected":
                self.refund_envelope(envelope_id, existing.get("error"))
                return
            if existing and existing.get("status") == "sending":
                # A replay can safely send a replacement button because claims
                # require the one message id that is eventually activated.
                self.col_envelopes.update_one(
                    {"_id": envelope_id, "status": "sending"},
                    {"$set": {"status": "creating"}},
                )

        if not reserved:
            sender = self.col_users.find_one({"_id": self.user_id})
            if self.outgoing_paused() or (sender and sender.get("WithdrawalQuarantined") is True):
                self.send_message(
                    self.user_id,
                    "<b>Transfers are paused pending review or wallet reconciliation.</b>",
                    parse_mode="HTML",
                )
            else:
                self.insufficient_balance_image()
            return

        sending = self.col_envelopes.update_one(
            {"_id": envelope_id, "status": "creating"},
            {"$set": {"status": "sending"}},
        )
        if sending.modified_count != 1:
            self.send_message(
                self.user_id,
                "<b>This envelope is already being processed.</b>",
                parse_mode="HTML",
            )
            return

        msg_id = self.red_envelope_created(self.first_name[:8], envelope_id)
        if not msg_id:
            rejected = self.col_envelopes.update_one(
                {"_id": envelope_id, "status": "sending"},
                {
                    "$set": {
                        "status": "rejected",
                        "error": "Telegram failed to create the envelope",
                    }
                },
            )
            if rejected.modified_count != 1:
                raise RuntimeError("envelope send rejection could not be recorded")
            self.refund_envelope(envelope_id, "Telegram failed to create the envelope")
            return

        activated = self.col_envelopes.update_one(
            {"_id": envelope_id, "status": "sending"},
            {
                "$set": {
                    "status": "active",
                    "msg_id": msg_id,
                    "activated_at": datetime.datetime.utcnow(),
                }
            },
        )
        if activated.modified_count != 1:
            raise RuntimeError("envelope was sent but could not be activated")

    def catch_envelope(self, envelope_id):
        query = getattr(self.new_message, "callback_query", None)
        envelope = self.col_envelopes.find_one(
            {"_id": envelope_id, "schemaVersion": 2}
        )
        if (
            query is None
            or query.message is None
            or envelope is None
            or envelope.get("status") not in ("active", "ended")
            or query.from_user.id != self.user_id
            or query.message.chat.id != envelope["group_id"]
            or query.message.message_id != envelope["msg_id"]
        ):
            if query is not None:
                self.answer_call_back(
                    text="This envelope is not available from this message.",
                    query_id=query.id,
                )
            return

        if envelope["status"] == "ended" or envelope["remains_groth"] <= 0:
            self.answer_call_back(
                text="RED ENVELOPE ENDED",
                query_id=query.id,
            )
            return

        claim_id = "claim:%s:%s" % (envelope_id, self.user_id)
        result = {}

        def claim(session):
            if self.outgoing_paused(session):
                return False
            current = self.col_envelopes.find_one(
                {
                    "_id": envelope_id,
                    "status": "active",
                    "takers.user_id": {"$ne": self.user_id},
                },
                session=session,
            )
            if current is None or current["remains_groth"] <= 0:
                return False
            if self.col_users.find_one(
                {"_id": current["creator_id"], "WithdrawalQuarantined": True},
                session=session,
            ):
                return False

            remaining_groth = current["remains_groth"]
            if remaining_groth <= MIN_ENVELOPE_GROTH:
                caught_groth = remaining_groth
            else:
                upper = (
                    max(MIN_ENVELOPE_GROTH, remaining_groth // 2)
                    if len(current["takers"]) < 5
                    else remaining_groth
                )
                caught_groth = MIN_ENVELOPE_GROTH + secrets.randbelow(
                    upper - MIN_ENVELOPE_GROTH + 1
                )

            catch_amount = groth_to_float(caught_groth)
            new_remains_groth = remaining_groth - caught_groth
            new_remains = groth_to_float(new_remains_groth)
            status = "ended" if new_remains_groth == 0 else "active"

            changed = self.col_envelopes.update_one(
                {
                    "_id": envelope_id,
                    "status": "active",
                    "remains_groth": remaining_groth,
                    "takers.user_id": {"$ne": self.user_id},
                },
                {
                    "$set": {
                        "remains_groth": new_remains_groth,
                        "remains": new_remains,
                        "status": status,
                    },
                    "$push": {
                        "takers": {
                            "user_id": self.user_id,
                            "amount": catch_amount,
                            "amount_groth": caught_groth,
                            "claim_id": claim_id,
                        }
                    },
                },
                session=session,
            )
            if changed.modified_count != 1:
                raise RuntimeError("envelope changed during claim")

            credited = self.col_users.update_one(
                {"_id": self.user_id, "IsVerified": True},
                {
                    "$inc": {
                        "BalanceGroth": caught_groth,
                        "Balance": catch_amount,
                    }
                },
                session=session,
            )
            if credited.modified_count != 1:
                raise RuntimeError("envelope claimant disappeared")

            self.col_tip_logs.insert_one(
                {
                    "_id": claim_id,
                    "type": "envelope",
                    "from_user_id": current["creator_id"],
                    "to_user_id": self.user_id,
                    "amount": catch_amount,
                    "amount_groth": caught_groth,
                    "moneySchemaVersion": 1,
                    "timestamp": datetime.datetime.utcnow(),
                },
                session=session,
            )
            result.update(
                {
                    "amount": catch_amount,
                    "amount_groth": caught_groth,
                    "remains_groth": new_remains_groth,
                    "group_id": current["group_id"],
                    "group_username": current.get("group_username"),
                    "msg_id": current["msg_id"],
                }
            )
            return True

        try:
            claimed = self.run_transaction(claim)
        except DuplicateKeyError:
            claimed = False

        if not claimed:
            self.answer_call_back(
                text=("Claims are paused pending deposit review."
                      if self.outgoing_paused()
                      else "You already claimed this envelope, or it has ended."),
                query_id=query.id,
            )
            return

        caught_text = format_groth(result["amount_groth"])
        if result["group_username"]:
            msg_text = (
                '<i>%s caught %s FIRO from a '
                '<a href="https://t.me/%s/%s">RED ENVELOPE</a></i>'
                % (
                    self.mention(self.user_id, self.first_name),
                    caught_text,
                    result["group_username"],
                    result["msg_id"],
                )
            )
        else:
            msg_text = '<i>%s caught %s FIRO from a RED ENVELOPE</i>' % (
                self.mention(self.user_id, self.first_name),
                caught_text,
            )
        self.send_message(
            result["group_id"],
            text=msg_text,
            disable_web_page_preview=True,
            parse_mode='HTML',
        )
        self.answer_call_back(
            text="YOU CAUGHT %s FIRO" % caught_text,
            query_id=query.id,
        )
        self.red_envelope_catched(caught_text)
        if result["remains_groth"] == 0:
            self.delete_tg_message(result["group_id"], result["msg_id"])

    def refund_envelope(self, envelope_id, error):
        def refund(session):
            envelope = self.col_envelopes.find_one(
                {
                    "_id": envelope_id,
                    "status": "rejected",
                },
                session=session,
            )
            if envelope is None:
                return False
            amount_groth = envelope["amount_groth"]
            credited = self.col_users.update_one(
                {"_id": envelope["creator_id"]},
                {
                    "$inc": {
                        "BalanceGroth": amount_groth,
                        "Balance": groth_to_float(amount_groth),
                    }
                },
                session=session,
            )
            if credited.modified_count != 1:
                raise RuntimeError("envelope refund recipient disappeared")
            changed = self.col_envelopes.update_one(
                {
                    "_id": envelope_id,
                    "status": "rejected",
                },
                {
                    "$set": {
                        "status": "failed",
                        "remains_groth": 0,
                        "remains": 0.0,
                        "error": error,
                        "completed_at": datetime.datetime.utcnow(),
                    }
                },
                session=session,
            )
            if changed.modified_count != 1:
                raise RuntimeError("envelope changed during refund")
            return True

        return self.run_transaction(refund)

    def delete_tg_message(self, user_id, message_id):
        try:
            self.bot.delete_message(user_id, message_id=message_id)
        except Exception as exc:
            logger.debug("could not delete Telegram message: %s", exc)

    def answer_call_back(self, text, query_id):
        try:
            self.bot.answer_callback_query(
                query_id,
                text=text,
                show_alert=True
            )
        except Exception as exc:
            print(exc)

    def auth_user(self):
        try:
            user = self.col_users.find_one({"_id": self.user_id})
            if user is None:
                public_address = self.create_safe_deposit_addresses()
                profile = {
                    "first_name": self.first_name,
                    "IsVerified": True,
                }
                if self.username:
                    profile.update(
                        {
                            "username": self.username,
                            "username_norm": self.username.casefold(),
                        }
                    )
                self.col_users.update_one(
                    {"_id": self.user_id},
                    {
                        "$set": profile,
                        "$setOnInsert": {
                            "JoinDate": datetime.datetime.utcnow(),
                            "Address": public_address,
                            "Balance": 0.0,
                            "Locked": 0.0,
                            "BalanceGroth": 0,
                            "LockedGroth": 0,
                            "moneySchemaVersion": 1,
                            "IsWithdraw": False,
                            "WithdrawalQuarantined": False,
                        },
                    },
                    upsert=True,
                )
                stored = self.col_users.find_one({"_id": self.user_id})
                public_address = normalize_addresses(stored["Address"])
                self.send_message(
                    self.user_id,
                    WELCOME_MESSAGE,
                    parse_mode='html',
                )
                self.create_wallet_image(public_address)
            else:
                required = {
                    "Address",
                    "BalanceGroth",
                    "LockedGroth",
                    "IsWithdraw",
                }
                missing = required.difference(user)
                if missing:
                    raise RuntimeError(
                        "existing user record is missing fields: %s"
                        % ", ".join(sorted(missing))
                    )
                profile = {
                    "first_name": self.first_name,
                    "IsVerified": True,
                }
                if self.username:
                    profile.update(
                        {
                            "username": self.username,
                            "username_norm": self.username.casefold(),
                        }
                    )
                self.col_users.update_one(
                    {"_id": self.user_id},
                    {"$set": profile},
                )
                self.send_message(
                    self.user_id,
                    WELCOME_MESSAGE,
                    parse_mode='html',
                )
        except Exception as exc:
            print(exc)
            traceback.print_exc()
            raise

    def create_qr_code(self):
        try:
            url = pyqrcode.create(self.firo_address[-1])
            output = io.BytesIO()
            url.png(
                output,
                scale=6,
                module_color="#000000",
                background="#d8e4ee",
            )
            output.seek(0)
            self.bot.send_photo(
                self.user_id,
                output,
                parse_mode='HTML'
            )
        except Exception as exc:
            print(exc)

    @staticmethod
    def cleanhtml(string_html):
        return html.escape(str(string_html), quote=True)

    def send_message(self, user_id, text, parse_mode=None, disable_web_page_preview=None, reply_markup=None):
        try:
            response = self.bot.send_message(
                user_id,
                text,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
                reply_markup=reply_markup
            )
            return response
        except Exception as exc:
            print(exc)


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        TipBot(wallet_api)

    except Exception as e:
        print(e)
        traceback.print_exc()
        raise


if __name__ == '__main__':
    main()

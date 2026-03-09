"""
    Developed by @vsnation(t.me/vsnation)
    Email: vsnation.v@gmail.com
    If you'll need the support use the contacts ^(above)!
"""
import json
import logging
import threading
import random
import pyqrcode
import schedule
import re
from PIL import Image, ImageFont, ImageDraw
import matplotlib.pyplot as plt
import datetime
import time
from decimal import Decimal, ROUND_DOWN
from bson.decimal128 import Decimal128
from bson.codec_options import CodecOptions, TypeCodec, TypeRegistry
from pymongo import MongoClient, ReturnDocument
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
import uuid
from api.firo_wallet_api import FiroWalletAPI


class DecimalCodec(TypeCodec):
    """Codec to transparently convert between Python Decimal and BSON Decimal128."""
    python_type = Decimal
    bson_type = Decimal128

    def transform_python(self, value):
        return Decimal128(value)

    def transform_bson(self, value):
        return value.to_decimal()


decimal_codec = DecimalCodec()
type_registry = TypeRegistry([decimal_codec])

plt.style.use('seaborn-whitegrid')

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler = logging.StreamHandler()
handler.setFormatter(formatter)
logger.addHandler(handler)

AV_FEE = Decimal('0.002')
ENVELOPE_EXPIRY_SECONDS = 86400  # 24 hours


def to_decimal(value):
    """Convert a value to Decimal with 8 decimal places for FIRO precision."""
    return Decimal(str(value)).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)


def decimal_to_store(value):
    """Quantize a value to 8 decimal places for MongoDB storage as Decimal128."""
    return to_decimal(value)

with open('services.json') as conf_file:
    conf = json.load(conf_file)
    connectionString = conf['mongo']['connectionString']
    bot_token = conf['telegram_bot']['bot_token']
    httpprovider = conf['httpprovider']
    dictionary = conf['dictionary']
    LOG_CHANNEL = conf['log_ch']

SATS_IN_BTC = Decimal('1e8')

wallet_api = FiroWalletAPI(httpprovider)

point_to_pixels = 1.33
bold = ImageFont.truetype(font="fonts/ProximaNova-Bold.ttf", size=int(18 * point_to_pixels))
regular = ImageFont.truetype(font="fonts/ProximaNova-Regular.ttf", size=int(18 * point_to_pixels))
bold_high = ImageFont.truetype(font="fonts/ProximaNova-Bold.ttf", size=int(26 * point_to_pixels))

WELCOME_MESSAGE = """
<b>Welcome to the Firo telegram tip bot!</b> 
"""


class TipBot:
    def __init__(self, wallet_api):
        # INIT
        self.bot = Bot(bot_token)
        self.wallet_api = wallet_api
        # Firo Butler Initialization
        self.client = MongoClient(connectionString)
        codec_options = CodecOptions(type_registry=type_registry)
        db = self.client.get_default_database(codec_options=codec_options)
        self.col_captcha = db['captcha']
        self.col_commands_history = db['commands_history']
        self.col_users = db['users']
        self.col_senders = db['senders']
        self.col_tip_logs = db['tip_logs']
        self.col_envelopes = db['envelopes']
        self.col_txs = db['txs']
        self.get_wallet_balance()
        self.update_balance()

        self.message, self.text, self._is_video, self.message_text, \
            self.first_name, self.username, self.user_id, self.firo_address, \
            self.balance_in_firo, self.locked_in_firo, self.is_withdraw, self.balance_in_groth, \
            self._is_verified, self.group_id, self.group_username = \
            None, None, None, None, None, None, None, None, None, None, None, None, None, None, None

        self.wallet_api.automintunspent()
        self.expire_envelopes()  # clean up envelopes that expired while bot was offline
        schedule.every(60).seconds.do(self.update_balance)
        schedule.every(60).seconds.do(self.expire_envelopes)
        schedule.every(300).seconds.do(self.wallet_api.automintunspent)
        threading.Thread(target=self.pending_tasks).start()

        self.new_message = None

        while True:
            try:
                self._is_user_in_db = None
                # get chat updates
                new_messages = self.wait_new_message()
                self.processing_messages(new_messages)
            except Exception as exc:
                logger.error(exc, exc_info=True)

    def pending_tasks(self):
        while True:
            schedule.run_pending()
            time.sleep(5)

    def processing_messages(self, new_messages):
        for self.new_message in new_messages:
            try:
                time.sleep(0.5)
                self.message = self.new_message.message \
                    if self.new_message.message is not None \
                    else self.new_message.callback_query.message
                self.text, self._is_video = self.get_action(self.new_message)
                self.message_text = str(self.text).lower()
                # init user data
                self.first_name = self.new_message.effective_user.first_name
                self.username = self.new_message.effective_user.username
                self.user_id = int(self.new_message.effective_user.id)

                self.firo_address, self.balance_in_firo, self.locked_in_firo, self.is_withdraw = self.get_user_data()
                self.balance_in_groth = to_decimal(self.balance_in_firo) * SATS_IN_BTC if self.balance_in_firo is not None else Decimal('0')

                try:
                    self._is_verified = self.col_users.find_one({"_id": self.user_id})['IsVerified']
                    self._is_user_in_db = self._is_verified
                except Exception as exc:
                    logger.error(exc, exc_info=True)
                    self._is_verified = True
                    self._is_user_in_db = False

                logger.info("User: %s (%s) - %s", self.username, self.user_id, self.message_text)
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
                logger.error(exc, exc_info=True)

    def send_to_logs(self, text):
        try:
            self.bot.send_message(
                LOG_CHANNEL,
                text,
                parse_mode='HTML'
            )
        except Exception as exc:
            logger.error(exc, exc_info=True)

    def get_group_username(self):
        """
            Get group username
        """
        try:
            return str(self.message.chat.username)
        except Exception:
            return str(self.message.chat.id)

    def get_user_username(self):
        """
                Get User username
        """
        try:
            return str(self.message.from_user.username)
        except Exception:
            return None

    def wait_new_message(self):
        while True:
            updates = self.bot.get_updates(allowed_updates=["message", "callback_query"])
            if len(updates) > 0:
                break
        update = updates[-1]
        self.bot.get_updates(offset=update["update_id"] + 1, allowed_updates=["message", "callback_query"])
        return updates

    @staticmethod
    def get_action(message):
        _is_document = False
        menu_option = None

        if message['message'] is not None:
            menu_option = message['message']['text']
            _is_document = message['message']['document'] is not None
            if 'mp4' in str(message['message']['document']):
                _is_document = False

        elif message["callback_query"] != 0:
            menu_option = message["callback_query"]["data"]

        return str(menu_option), _is_document

    def action_processing(self, cmd, args):
        """
            Check each user actions
        """
        # ***** Tip bot section begin *****
        if cmd.startswith("/tip") or cmd.startswith("/atip"):
            if not self._is_user_in_db:
                self.send_message(self.group_id,
                                  f'<a href="tg://user?id={self.user_id}">{self.first_name}</a>, <a href="https://t.me/firo_tipbot?start=1"><a href="https://t.me/firo_tipbot?start=1">start the bot</a></a>to receive tips!',
                                  parse_mode='HTML')
                return
            try:
                if args is not None and len(args) >= 1:
                    if cmd.startswith("/atip"):
                        _type = "anonymous"
                    else:
                        _type = None

                    if self.message.reply_to_message is not None:
                        comment = " ".join(args[1:]) if len(args) > 1 else ""
                        args = args[0:1]
                        self.tip_in_the_chat(_type=_type, comment=comment, *args)
                    else:
                        comment = " ".join(args[2:]) if len(args) > 2 else ""
                        args = args[0:2]
                        self.tip_user(_type=_type, comment=comment, *args)
                else:
                    self.incorrect_parametrs_image()
                    self.send_message(
                        self.user_id,
                        dictionary['tip_help'],
                        parse_mode='HTML'
                    )
            except Exception as exc:
                logger.error(exc, exc_info=True)
                self.incorrect_parametrs_image()
                self.send_message(
                    self.user_id,
                    dictionary['tip_help'],
                    parse_mode='HTML'
                )


        elif cmd.startswith("/envelope"):
            try:
                self.bot.delete_message(self.group_id, self.message.message_id)
            except Exception:
                pass

            if self.message.chat['type'] == 'private':
                self.send_message(
                    self.user_id,
                    "<b>You can use this cmd only in the group</b>",
                    parse_mode="HTML"
                )
                return

            if not self._is_user_in_db:
                self.send_message(self.group_id,
                                  f'<a href="tg://user?id={self.user_id}">{self.first_name}</a>, <a href="https://t.me/firo_tipbot?start=1">start the bot</a> to receive tips!',
                                  parse_mode="HTML", disable_web_page_preview=True)
                return

            try:
                if args is not None and len(args) == 1:
                    self.create_red_envelope(*args)
                else:
                    self.incorrect_parametrs_image()
            except Exception as exc:
                logger.error(exc, exc_info=True)
                self.incorrect_parametrs_image()


        elif cmd.startswith("catch_envelope|"):
            if not self._is_user_in_db:
                self.send_message(self.group_id,
                                  f'<a href="tg://user?id={self.user_id}">{self.first_name}</a>, <a href="https://t.me/firo_tipbot?start=1">start the bot</a> to receive tips!',
                                  parse_mode="HTML", disable_web_page_preview=True)
                return

            try:
                envelope_id = cmd.split("|")[1]
                self.catch_envelope(envelope_id)
            except Exception as exc:
                logger.error(exc, exc_info=True)
                self.incorrect_parametrs_image()



        elif cmd.startswith("/balance"):
            if not self._is_user_in_db:
                self.send_message(self.group_id,
                                  f'<a href="tg://user?id={self.user_id}">{self.first_name}</a>, <a href="https://t.me/firo_tipbot?start=1">start the bot</a> to receive tips!',
                                  parse_mode="HTML", disable_web_page_preview=True)
                return
            self.send_message(
                self.user_id,
                dictionary['balance'] % "{0:.8f}".format(float(self.balance_in_firo)),
                parse_mode='HTML'
            )

        elif cmd.startswith("/withdraw"):
            try:
                if not self._is_user_in_db:
                    self.send_message(self.group_id,
                                      f'<a href="tg://user?id={self.user_id}">{self.first_name}</a>, <a href="https://t.me/firo_tipbot?start=1">start the bot</a> to receive tips!',
                                      parse_mode="HTML", disable_web_page_preview=True)
                    return
                if args is not None and len(args) == 2:
                    self.withdraw_coins(*args)
                else:
                    self.incorrect_parametrs_image()
            except Exception as exc:
                logger.error(exc, exc_info=True)

        elif cmd.startswith("/deposit"):
            if not self._is_user_in_db:
                self.send_message(self.group_id,
                                  f'<a href="tg://user?id={self.user_id}">{self.first_name}</a>, <a href="https://t.me/firo_tipbot?start=1">start the bot</a> to receive tips!',
                                  parse_mode="HTML", disable_web_page_preview=True)
                return
            self.send_message(
                self.user_id,
                dictionary['deposit'] % self.firo_address[0],
                parse_mode='HTML'
            )
            self.create_qr_code()

        elif cmd.startswith("/help"):
            bot_msg = self.send_message(
                self.user_id,
                dictionary['help'],
                parse_mode='HTML',
                disable_web_page_preview=True
            )

        # ***** Tip bot section end *****
        # ***** Verification section begin *****
        elif cmd.startswith("/start"):
            self.auth_user()

    def check_username_on_change(self):
        """
            Check username on change in the bot
        """
        _is_username_in_db = self.col_users.find_one(
            {"username": self.username}) is not None \
            if self.username is not None \
            else True
        if not _is_username_in_db:
            self.col_users.update_one(
                {
                    "_id": self.user_id
                },
                {
                    "$set":
                        {
                            "username": self.username
                        }
                }
            )

        _is_first_name_in_db = self.col_users.find_one(
            {"first_name": self.first_name}) is not None if self.first_name is not None else True
        if not _is_first_name_in_db:
            self.col_users.update_one(
                {
                    "_id": self.user_id
                },
                {
                    "$set":
                        {
                            "first_name": self.first_name
                        }
                }
            )

    def get_wallet_balance(self):
        try:
            r = self.wallet_api.listsparkmints()
            result = sum([_x['amount'] for _x in r['result'] if not _x['isUsed']])
            logger.info("Current Balance: %s FIRO", to_decimal(result) / SATS_IN_BTC)
        except Exception as exc:
            logger.error(exc, exc_info=True)

    def update_balance(self):
        """
            Update user's balance using transactions history.
            Uses atomic operations to prevent double-crediting deposits
            or double-processing withdrawals.
        """
        logger.info("Handle TXs")
        unused_mints = []
        mints = wallet_api.listsparkmints()

        for mnt in mints['result']:
            if not mnt['isUsed']:
                unused_mints.append(mnt)
        response = self.wallet_api.get_txs_list()

        for _tx in response['result']:
            # Skip transactions we've already fully processed
            if self.col_txs.find_one({"txId": _tx['txid']}) is not None:
                continue

            for unused_mnt in unused_mints:
                try:
                    if unused_mnt['txid'] == _tx['txid']:
                        sparkcoin_addr = wallet_api.get_spark_coin_address(unused_mnt['txid'])

                        # --- Check deposit txs ---
                        _user_receiver = self.col_users.find_one(
                            {"Address": sparkcoin_addr[0]['address']}
                        )

                        _is_tx_exist_deposit = self.col_txs.find_one(
                            {"txId": _tx['txid'], "type": "deposit"}
                        ) is not None
                        if _user_receiver is not None and \
                                not _is_tx_exist_deposit and \
                                _tx['confirmations'] >= 2 and _tx['category'] == 'receive':

                            value_in_coins = to_decimal(_tx['amount'])
                            _id = str(uuid.uuid4())
                            # Insert tx record first to claim it (prevents double-credit)
                            self.col_txs.insert_one({
                                '_id': _id,
                                'txId': _tx['txid'],
                                **_tx,
                                'type': "deposit",
                                'timestamp': datetime.datetime.now()
                            })
                            # Use $inc for atomic balance update
                            self.col_users.update_one(
                                {"_id": _user_receiver['_id']},
                                {"$inc": {"Balance": decimal_to_store(value_in_coins)}}
                            )
                            self.create_receive_tips_image(
                                _user_receiver['_id'],
                                "{0:.8f}".format(value_in_coins),
                                "Deposit")

                            logger.info("*Deposit Success* Address %s recharged %s FIRO",
                                        sparkcoin_addr[0]['address'], value_in_coins)
                            continue

                        # --- Check withdraw txs ---
                        _is_tx_exist_withdraw = self.col_txs.find_one(
                            {"txId": _tx['txid'], "type": "withdraw"}
                        ) is not None

                        pending_sender = self.col_senders.find_one(
                            {"txId": _tx['txid'], "status": "pending"}
                        )
                        if not pending_sender:
                            continue
                        _user_sender = self.col_users.find_one({"_id": pending_sender['user_id']})
                        if _user_sender is not None and not _is_tx_exist_withdraw and _tx['category'] == "spend":

                            value_in_coins = to_decimal(abs(_tx['amount']))

                            if _tx['confirmations'] >= 2:
                                _id = str(uuid.uuid4())
                                self.col_txs.insert_one({
                                    '_id': _id,
                                    "txId": _tx['txid'],
                                    **_tx,
                                    'type': "withdraw",
                                    'timestamp': datetime.datetime.now()
                                })
                                # Use $inc to atomically reduce Locked; clamp to 0
                                current_locked = to_decimal(_user_sender['Locked'])
                                locked_deduction = min(current_locked, value_in_coins)
                                remainder = value_in_coins - locked_deduction
                                update_ops = {
                                    "$inc": {
                                        "Locked": decimal_to_store(-locked_deduction),
                                    },
                                    "$set": {
                                        "IsWithdraw": False
                                    }
                                }
                                if remainder > 0:
                                    update_ops["$inc"]["Balance"] = decimal_to_store(-remainder)
                                self.col_users.update_one(
                                    {"_id": _user_sender['_id']},
                                    update_ops
                                )

                                self.create_send_tips_image(_user_sender['_id'],
                                                            "{0:.8f}".format(value_in_coins),
                                                            "%s..." % _user_sender['Address'][0][:8])

                                self.col_senders.update_one(
                                    {"txId": _tx['txid'], "status": "pending", "user_id": _user_sender['_id']},
                                    {"$set": {"status": "completed"}}
                                )
                                logger.info("*Withdrawal Success* Address %s deducted %s FIRO",
                                            _user_sender['Address'], value_in_coins)
                                continue

                except Exception as exc:
                    logger.error(exc, exc_info=True)

    def expire_envelopes(self):
        """
            Find envelopes older than 24 hours with unclaimed funds,
            refund the remaining balance to the creator, and mark as expired.
        """
        try:
            cutoff = int(datetime.datetime.now().timestamp()) - ENVELOPE_EXPIRY_SECONDS
            expired = self.col_envelopes.find({
                "created_at": {"$lte": cutoff},
                "remains": {"$gt": 0},
                "expired": {"$ne": True}
            })

            for envelope in expired:
                # Atomically zero out the envelope remains and mark as expired
                # Use BEFORE to capture the actual remains at update time,
                # avoiding a race where catch_envelope reduces remains after
                # the cursor read but before this update.
                updated = self.col_envelopes.find_one_and_update(
                    {
                        "_id": envelope['_id'],
                        "remains": {"$gt": 0},
                        "expired": {"$ne": True}
                    },
                    {
                        "$set": {
                            "remains": decimal_to_store(Decimal('0')),
                            "expired": True,
                            "expired_at": int(datetime.datetime.now().timestamp())
                        }
                    },
                    return_document=ReturnDocument.BEFORE
                )
                if not updated:
                    continue

                # Read the actual remains from the atomically-returned document
                remains = to_decimal(updated['remains'])
                if remains <= 0:
                    continue
                store_remains = decimal_to_store(remains)

                # Credit the creator
                self.col_users.update_one(
                    {"_id": envelope['creator_id']},
                    {"$inc": {"Balance": store_remains}}
                )

                logger.info("Envelope %s expired: refunded %s FIRO to user %s",
                            envelope['_id'], remains, envelope['creator_id'])

                # Notify the creator
                try:
                    self.bot.send_message(
                        envelope['creator_id'],
                        "<b>Your red envelope expired.</b>\n"
                        "<b>%s FIRO</b> unclaimed funds have been returned to your balance." %
                        "{0:.8f}".format(remains),
                        parse_mode='HTML'
                    )
                except Exception as exc:
                    logger.error("Failed to notify envelope creator %s: %s",
                                 envelope['creator_id'], exc)

                # Clean up the group message button
                try:
                    self.bot.delete_message(envelope['group_id'], envelope['msg_id'])
                except Exception:
                    pass

        except Exception as exc:
            logger.error(exc, exc_info=True)

    def get_user_data(self):
        """
            Get user data
        """
        try:
            _user = self.col_users.find_one({"_id": self.user_id})
            if self.update_address_and_balance(_user):
                _user = self.col_users.find_one({"_id": self.user_id})
            return _user['Address'], to_decimal(_user['Balance']), to_decimal(_user['Locked']), _user['IsWithdraw']
        except Exception as exc:
            logger.error(exc, exc_info=True)
            return None, None, None, None

    def update_address_and_balance(self, _user):
        # Check if User has a Spark address
        valid = wallet_api.validate_address(_user['Address'][0])['result']
        is_valid_spark = 'isvalidSpark'
        if is_valid_spark in valid:
            # no update required
            return False
        else:
            # Get a spark address and update user
            spark_address = wallet_api.create_user_wallet()
            self.col_users.update_one(
                _user,
                {
                    "$set":
                        {
                            "Address": spark_address[0],
                        }
                }
            )
            return True

        # TODO: Check if balances should be updated as per the function name?
        # mints = wallet_api.listsparkmints()
        # if len(mints) > 0:

    def withdraw_coins(self, address, amount, comment=""):
        """
            Withdraw coins to address with params:
            address
            amount

            The user pays `amount` from their balance. The network receives
            `amount - AV_FEE` (fee is deducted from the sent amount via subtractFee).
            The full `amount` is moved from Balance to Locked until confirmed.
        """
        try:
            try:
                amount = to_decimal(amount)
                if amount <= 0:
                    raise ValueError("Amount must be positive")
            except Exception as exc:
                self.send_message(self.user_id,
                                  dictionary['incorrect_amount'],
                                  parse_mode='HTML')
                logger.error(exc, exc_info=True)
                return

            validate = self.wallet_api.validate_address(address)['result']
            is_valid_spark = 'isvalidSpark'
            is_valid_firo = 'isvalid'
            if is_valid_spark not in validate and is_valid_firo not in validate:
                self.send_message(
                    self.user_id,
                    "<b>You specified incorrect address</b>",
                    parse_mode='HTML'
                )
                return

            if amount < AV_FEE:
                self.send_message(
                    self.user_id,
                    "<b>Amount must be greater than the fee (%s FIRO)</b>" % AV_FEE,
                    parse_mode='HTML'
                )
                return

            # Atomic check-and-deduct: only succeeds if Balance >= amount
            user = self.col_users.find_one_and_update(
                {"_id": self.user_id, "Balance": {"$gte": decimal_to_store(amount)}},
                {"$inc": {
                    "Balance": decimal_to_store(-amount),
                    "Locked": decimal_to_store(amount),
                }},
                return_document=ReturnDocument.AFTER
            )
            if not user:
                self.insufficient_balance_image()
                return

            # Send on-chain (subtractFee=True means the fee comes out of the sent amount)
            response = self.wallet_api.spendspark(
                address,
                float(amount),
                comment
            )
            logger.info("Withdraw response: %s", response)
            if response.get('error'):
                # Rollback: restore the exact amount we moved
                self.col_users.update_one(
                    {"_id": self.user_id},
                    {"$inc": {
                        "Balance": decimal_to_store(amount),
                        "Locked": decimal_to_store(-amount),
                    }}
                )
                self.send_message(
                    self.user_id, "Not enough inputs. Try to repeat a bit later!"
                )
                self.send_to_logs(f"Unavailable Withdraw\n{str(response)}")
                return

            self.col_senders.insert_one(
                {"txId": response['result'], "status": "pending", "user_id": self.user_id}
            )
            self.withdraw_image(self.user_id,
                                "{0:.8f}".format(amount),
                                address,
                                msg=f"Your txId {response['result']}")

        except Exception as exc:
            logger.error(exc, exc_info=True)

    def tip_user(self, username, amount, comment, _type=None):
        """
            Tip user with params:
            username
            amount
        """
        try:
            try:
                amount = to_decimal(amount)
                if amount < Decimal('0.00000001'):
                    raise ValueError("Amount too small")
            except Exception as exc:
                self.incorrect_parametrs_image()
                logger.error(exc, exc_info=True)
                return

            username = username.replace('@', '')

            _user = self.col_users.find_one({"username": username})
            _is_username_exists = _user is not None

            if not _is_username_exists:
                self.send_message(self.user_id,
                                  dictionary['username_error'],
                                  parse_mode='HTML')
                return

            self.send_tip(_user['_id'], amount, _type, comment)

        except Exception as exc:
            logger.error(exc, exc_info=True)

    def tip_in_the_chat(self, amount, comment="", _type=None):
        """
            Send a tip to user in the chat
        """
        try:
            try:
                amount = to_decimal(amount)
                if amount < Decimal('0.00000001'):
                    raise ValueError("Amount too small")
            except Exception as exc:
                self.incorrect_parametrs_image()
                logger.error(exc, exc_info=True)
                return

            self.send_tip(
                self.message.reply_to_message.from_user.id,
                amount,
                _type,
                comment
            )

        except Exception as exc:
            logger.error(exc, exc_info=True)

    def send_tip(self, user_id, amount, _type, comment):
        """
            Send tip to user with params
            user_id - user identifier
            amount - amount of a tip

            Uses atomic find_one_and_update with $gte guard to prevent
            spending more than the sender's balance.
        """
        try:
            if self.user_id == user_id:
                self.send_message(
                    self.user_id,
                    "<b>You can't send tips to yourself!</b>",
                    parse_mode='HTML'
                )
                return

            _user_receiver = self.col_users.find_one({"_id": user_id})

            if _user_receiver is None or _user_receiver['IsVerified'] is False:
                self.send_message(self.user_id,
                                  dictionary['username_error'],
                                  parse_mode='HTML')
                return

            if _type == 'anonymous':
                sender_name = str(_type).title()
            else:
                sender_name = self.first_name

            store_amount = decimal_to_store(amount)

            # Atomic deduct: only succeeds if sender has sufficient balance
            sender_updated = self.col_users.find_one_and_update(
                {"_id": self.user_id, "Balance": {"$gte": store_amount}},
                {"$inc": {"Balance": -store_amount}},
                return_document=ReturnDocument.AFTER
            )
            if not sender_updated:
                self.insufficient_balance_image()
                return

            # Credit receiver atomically
            self.col_users.update_one(
                {"_id": _user_receiver['_id']},
                {"$inc": {"Balance": store_amount}}
            )

            # Log the tip
            self.col_tip_logs.insert_one(
                {
                    "type": "atip" if _type == 'anonymous' else "tip",
                    "from_user_id": self.user_id,
                    "to_user_id": _user_receiver['_id'],
                    "amount": store_amount,
                    "timestamp": datetime.datetime.now()
                }
            )

            self.create_send_tips_image(
                self.user_id,
                "{0:.8f}".format(amount),
                _user_receiver['first_name'],
                comment
            )

            self.create_receive_tips_image(
                _user_receiver['_id'],
                "{0:.8f}".format(amount),
                sender_name,
                comment
            )

        except Exception as exc:
            logger.error(exc, exc_info=True)

    def create_receive_tips_image(self, user_id, amount, first_name, comment=""):
        try:
            im = Image.open("images/receive_template.png")
            d = ImageDraw.Draw(im)

            location_f = (266, 21)
            location_s = (266, 45)
            location_t = (266, 67)
            if "Deposit" in first_name:
                d.text(location_f, "%s" % first_name, font=bold, fill='#000000')
                d.text(location_s, "has recharged", font=regular, fill='#000000')
                d.text(location_t, "%s Firo" % "{0:.4f}".format(float(amount)), font=bold, fill='#000000')

            else:
                d.text(location_f, "%s" % first_name, font=bold, fill='#000000')
                d.text(location_s, "sent you a tip of", font=regular, fill='#000000')
                d.text(location_t, "%s Firo" % "{0:.4f}".format(float(amount)), font=bold, fill='#000000')

            receive_img = 'receive.png'
            im.save(receive_img)
            if comment == "":
                self.bot.send_photo(
                    user_id,
                    open(receive_img, 'rb')
                )
            else:
                self.bot.send_photo(
                    user_id,
                    open(receive_img, 'rb'),
                    caption="<b>Comment:</b> <i>%s</i>" % self.cleanhtml(comment),
                    parse_mode='HTML'
                )


        except Exception as exc:
            try:
                logger.error(exc, exc_info=True)
                if 'blocked' in str(exc):
                    self.send_message(self.group_id,
                                      "<a href='tg://user?id=%s'>User</a> <b>needs to unblock the bot in order to check their balance!</b>" % user_id,
                                      parse_mode='HTML')
            except Exception as exc:
                logger.error(exc, exc_info=True)

    def create_send_tips_image(self, user_id, amount, first_name, comment=""):
        try:
            im = Image.open("images/send_template.png")

            d = ImageDraw.Draw(im)
            location_f = (276, 21)
            location_s = (276, 45)
            location_t = (276, 67)
            d.text(location_f, "%s Firo" % "{0:.4f}".format(float(amount)), font=bold, fill='#000001')
            d.text(location_s, "tip was sent to", font=regular, fill='#000000')
            d.text(location_t, "%s" % first_name, font=bold, fill='#000000')
            send_img = 'send.png'
            im.save(send_img)
            if comment == "":
                self.bot.send_photo(
                    user_id,
                    open(send_img, 'rb'))
            else:
                self.bot.send_photo(
                    user_id,
                    open(send_img, 'rb'),
                    caption="<b>Comment:</b> <i>%s</i>" % self.cleanhtml(comment),
                    parse_mode='HTML'
                )

        except Exception as exc:
            try:
                logger.error(exc, exc_info=True)
                if 'blocked' in str(exc):
                    self.send_message(self.group_id,
                                      "<a href='tg://user?id=%s'>User</a> <b>needs to unblock the bot in order to check their balance!</b>" % user_id,
                                      parse_mode='HTML')
            except Exception as exc:
                logger.error(exc, exc_info=True)

    def withdraw_image(self, user_id, amount, address, msg=None):
        try:
            im = Image.open("images/withdraw_template.png")

            d = ImageDraw.Draw(im)
            location_transfer = (256, 21)
            location_amount = (276, 45)
            location_addess = (256, 65)

            d.text(location_transfer, "Transaction transfer", font=regular,
                   fill='#000000')
            d.text(location_amount, "%s Firo" % amount, font=bold, fill='#000001')
            d.text(location_addess, "to %s..." % address[:8], font=bold,
                   fill='#000000')
            image_name = 'withdraw.png'
            im.save(image_name)
            self.bot.send_photo(
                user_id,
                open(image_name, 'rb'),
                caption=f'{msg}'
            )
        except Exception as exc:
            logger.error(exc, exc_info=True)

    def create_wallet_image(self, public_address):
        try:
            im = Image.open("images/create_wallet_template.png")

            d = ImageDraw.Draw(im)
            location_transfer = (258, 32)

            d.text(location_transfer, "Wallet created", font=bold,
                   fill='#000000')
            image_name = 'create_wallet.png'
            im.save(image_name)
            self.bot.send_photo(
                self.user_id,
                open(image_name, 'rb'),
                caption=dictionary['welcome'] % public_address,
                parse_mode='HTML',
                timeout=200
            )
        except Exception as exc:
            logger.error(exc, exc_info=True)

    def withdraw_failed_image(self, user_id):
        try:
            im = Image.open("images/withdraw_failed_template.png")

            d = ImageDraw.Draw(im)
            location_text = (230, 52)

            d.text(location_text, "Withdraw failed", font=bold, fill='#000000')

            image_name = 'withdraw_failed.png'
            im.save(image_name)
            self.bot.send_photo(
                user_id,
                open(image_name, 'rb'),
                dictionary['withdrawal_failed'],
                parse_mode='HTML'
            )
        except Exception as exc:
            logger.error(exc, exc_info=True)

    def insufficient_balance_image(self):
        try:
            im = Image.open("images/insufficient_balance_template.png")

            d = ImageDraw.Draw(im)
            location_text = (230, 62)

            d.text(location_text, "Insufficient Balance", font=bold, fill='#000000')

            image_name = 'insufficient_balance.png'
            im = im.convert("RGB")
            im.save(image_name)
            try:
                self.bot.send_photo(
                    self.user_id,
                    open(image_name, 'rb'),
                    caption=dictionary['incorrect_balance'] % "{0:.8f}".format(
                        float(self.balance_in_firo)),
                    parse_mode='HTML'
                )
            except Exception as exc:
                logger.error(exc, exc_info=True)
        except Exception as exc:
            logger.error(exc, exc_info=True)

    def red_envelope_catched(self, amount):
        try:
            im = Image.open("images/red_envelope_catched.png")

            d = ImageDraw.Draw(im)
            location_transfer = (236, 35)
            location_amount = (256, 65)
            location_addess = (205, 95)

            d.text(location_transfer, "You caught", font=bold, fill='#000000')
            d.text(location_amount, "%s Firo" % amount, font=bold, fill='#f72c56')
            d.text(location_addess, "FROM A RED ENVELOPE", font=regular, fill='#000000')
            image_name = 'catched.png'
            im.save(image_name)
            try:
                self.bot.send_photo(
                    self.user_id,
                    open(image_name, 'rb')
                )
            except Exception as exc:
                logger.error(exc, exc_info=True)
        except Exception as exc:
            logger.error(exc, exc_info=True)

    def red_envelope_created(self, first_name, envelope_id):
        im = Image.open("images/red_envelope_created.png")

        d = ImageDraw.Draw(im)
        location_who = (230, 35)
        location_note = (256, 70)

        d.text(location_who, "%s CREATED" % first_name, font=bold, fill='#000000')
        d.text(location_note, "A RED ENVELOPE", font=bold,
               fill='#f72c56')
        image_name = 'created.png'
        im.save(image_name)
        try:
            response = self.bot.send_photo(
                self.group_id,
                open(image_name, 'rb'),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(
                        text='Catch Firo✋',
                        callback_data='catch_envelope|%s' % envelope_id
                    )]]
                )
            )
            return response['message_id']
        except Exception as exc:
            logger.error(exc, exc_info=True)
            return 0

    def red_envelope_ended(self):
        im = Image.open("images/red_envelope_ended.png")

        d = ImageDraw.Draw(im)
        location_who = (256, 41)
        location_note = (306, 75)

        d.text(location_who, "RED ENVELOPE", font=bold, fill='#000000')
        d.text(location_note, "ENDED", font=bold, fill='#f72c56')
        image_name = 'ended.png'
        im.save(image_name)
        try:
            self.bot.send_photo(
                self.user_id,
                open(image_name, 'rb'),
            )
        except Exception as exc:
            logger.error(exc, exc_info=True)

    def incorrect_parametrs_image(self):
        try:
            im = Image.open("images/incorrect_parametrs_template.png")

            d = ImageDraw.Draw(im)
            location_text = (230, 62)

            d.text(location_text, "Incorrect parameters", font=bold,
                   fill='#000000')

            image_name = 'incorrect_parametrs.png'
            im = im.convert("RGB")
            im.save(image_name)
            self.bot.send_photo(
                self.user_id,
                open(image_name, 'rb'),
                caption=dictionary['incorrect_parametrs'],
                parse_mode='HTML'
            )
        except Exception as exc:
            logger.error(exc, exc_info=True)

    def create_red_envelope(self, amount):
        try:
            amount = to_decimal(amount)

            if amount < Decimal('0.001'):
                self.incorrect_parametrs_image()
                return

            store_amount = decimal_to_store(amount)

            # Atomic deduct: only succeeds if balance is sufficient
            user = self.col_users.find_one_and_update(
                {"_id": self.user_id, "Balance": {"$gte": store_amount}},
                {"$inc": {"Balance": -store_amount}},
                return_document=ReturnDocument.AFTER
            )
            if not user:
                self.insufficient_balance_image()
                return

            envelope_id = str(uuid.uuid4())[:8]
            msg_id = self.red_envelope_created(self.first_name[:8], envelope_id)

            self.col_envelopes.insert_one(
                {
                    "_id": envelope_id,
                    "amount": store_amount,
                    "remains": store_amount,
                    "group_id": self.group_id,
                    "group_username": self.group_username,
                    "group_type": self.message.chat['type'],
                    "creator_id": self.user_id,
                    "msg_id": msg_id,
                    "takers": [],
                    "created_at": int(datetime.datetime.now().timestamp()),
                    "expired": False
                }
            )

        except Exception as exc:
            self.incorrect_parametrs_image()
            logger.error(exc, exc_info=True)

    def catch_envelope(self, envelope_id):
        try:
            envelope = self.col_envelopes.find_one({"_id": envelope_id})
            if envelope is None:
                self.insufficient_balance_image()
                return

            _is_ended = to_decimal(envelope['remains']) == 0
            _is_user_catched = str(self.user_id) in str(envelope['takers'])
            _is_expired = envelope.get('expired', False) or \
                (int(datetime.datetime.now().timestamp()) - envelope['created_at']) > ENVELOPE_EXPIRY_SECONDS

            if _is_expired:
                self.answer_call_back(text="❗️This red envelope has expired❗️",
                                      query_id=self.new_message.callback_query.id)
                self.delete_tg_message(self.group_id, self.message.message_id)
                return

            if _is_user_catched:
                self.answer_call_back(text="❗️You have already caught Firo from this envelope❗️",
                                      query_id=self.new_message.callback_query.id)
                return

            if _is_ended:
                self.answer_call_back(text="❗RED ENVELOPE ENDED❗️",
                                      query_id=self.new_message.callback_query.id)
                self.red_envelope_ended()
                self.delete_tg_message(self.group_id, self.message.message_id)
                return

            remains = to_decimal(envelope['remains'])
            minimal_amount = Decimal('0.001')
            if remains <= minimal_amount:
                catch_amount = remains
            else:
                if len(envelope['takers']) < 5:
                    catch_amount = to_decimal(random.uniform(float(minimal_amount), float(remains / 2)))
                else:
                    catch_amount = to_decimal(random.uniform(float(minimal_amount), float(remains)))

            store_catch = decimal_to_store(catch_amount)

            # Atomic envelope update: only deducts if remains >= catch_amount
            # and user hasn't already caught (takers check above is advisory;
            # the real guard is the find_one check, but since this is single-threaded
            # polling the race window is minimal)
            updated_envelope = self.col_envelopes.find_one_and_update(
                {
                    "_id": envelope_id,
                    "remains": {"$gte": store_catch},
                    "expired": {"$ne": True}
                },
                {
                    "$push": {
                        "takers": [self.user_id, store_catch]
                    },
                    "$inc": {
                        "remains": -store_catch
                    }
                },
                return_document=ReturnDocument.AFTER
            )

            if not updated_envelope:
                # Envelope ran out between our read and update
                self.answer_call_back(text="❗RED ENVELOPE ENDED❗️",
                                      query_id=self.new_message.callback_query.id)
                return

            # Credit the user atomically
            self.col_users.update_one(
                {"_id": self.user_id},
                {"$inc": {"Balance": store_catch}}
            )

            try:
                if envelope['group_username'] != "None":
                    msg_text = '<i><a href="tg://user?id=%s">%s</a> caught %s Firo from a <a href="https://t.me/%s/%s">RED ENVELOPE</a></i>' % (
                        self.user_id,
                        self.first_name,
                        "{0:.8f}".format(catch_amount),
                        envelope['group_username'],
                        envelope['msg_id']
                    )
                else:
                    msg_text = '<i><a href="tg://user?id=%s">%s</a> caught %s Firo from a RED ENVELOPE</i>' % (
                        self.user_id,
                        self.first_name,
                        "{0:.8f}".format(catch_amount),
                    )
                self.send_message(
                    envelope['group_id'],
                    text=msg_text,
                    disable_web_page_preview=True,
                    parse_mode='HTML'
                )
            except Exception as exc:
                logger.error(exc, exc_info=True)

            self.answer_call_back(text="✅YOU CAUGHT %s Firo from ENVELOPE✅️" % catch_amount,
                                  query_id=self.new_message.callback_query.id)
            self.red_envelope_catched("{0:.8f}".format(catch_amount))

        except Exception as exc:
            self.incorrect_parametrs_image()
            logger.error(exc, exc_info=True)

    def delete_tg_message(self, user_id, message_id):
        try:
            self.bot.delete_message(user_id, message_id=message_id)
        except Exception:
            pass

    def answer_call_back(self, text, query_id):
        try:
            self.bot.answer_callback_query(
                query_id,
                text=text,
                show_alert=True
            )
        except Exception as exc:
            logger.error(exc, exc_info=True)

    def auth_user(self):
        try:
            if self.firo_address is None:
                public_address = self.wallet_api.create_user_wallet()
                if not self._is_verified:
                    self.send_message(
                        self.user_id,
                        WELCOME_MESSAGE,
                        parse_mode='html'
                    )

                    self.col_users.update_one(
                        {
                            "_id": self.user_id
                        },
                        {
                            "$set":
                                {
                                    "IsVerified": True,
                                    "Address": public_address,
                                    "Balance": Decimal('0'),
                                    "Locked": Decimal('0'),
                                    "IsWithdraw": False
                                }
                        }, upsert=True
                    )
                    self.create_wallet_image(public_address)


                else:
                    self.col_users.update_one(
                        {
                            "_id": self.user_id
                        },
                        {
                            "$set":
                                {
                                    "_id": self.user_id,
                                    "first_name": self.first_name,
                                    "username": self.username,
                                    "IsVerified": True,
                                    "JoinDate": datetime.datetime.now(),
                                    "Address": public_address,
                                    "Balance": Decimal('0'),
                                    "Locked": Decimal('0'),
                                    "IsWithdraw": False,
                                }
                        }, upsert=True
                    )

                    self.send_message(
                        self.user_id,
                        WELCOME_MESSAGE,
                        parse_mode='html',
                    )
                    self.create_wallet_image(public_address)

            else:
                self.col_users.update_one(
                    {
                        "_id": self.user_id
                    },
                    {
                        "$set":
                            {
                                "IsVerified": True,
                            }
                    }, upsert=True
                )
                self.send_message(
                    self.user_id,
                    WELCOME_MESSAGE,
                    parse_mode='html',
                )
        except Exception as exc:
            logger.error(exc, exc_info=True)

    def create_qr_code(self):
        try:
            url = pyqrcode.create(self.firo_address[0])
            url.png('qrcode.png', scale=6, module_color="#000000",
                    background="#d8e4ee")
            time.sleep(0.5)
            self.bot.send_photo(
                self.user_id,
                open('qrcode.png', 'rb'),
                parse_mode='HTML'
            )
        except Exception as exc:
            logger.error(exc, exc_info=True)

    def cleanhtml(self, string_html):
        cleanr = re.compile('<.*?>')
        cleantext = re.sub(cleanr, '', string_html)
        return cleantext

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
            logger.error(exc, exc_info=True)


def main():
    try:
        TipBot(wallet_api)

    except Exception as e:
        logger.error(e, exc_info=True)


if __name__ == '__main__':
    main()

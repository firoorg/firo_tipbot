from pymongo import MongoClient
import json
from api.firo_wallet_api import FiroWalletAPI

with open('services.json') as conf_file:
    conf = json.load(conf_file)
    connectionString = conf['mongo']['connectionString']
    httpprovider = conf['httpprovider']

wallet_api = FiroWalletAPI(httpprovider)


class AddressFix:
    def __init__(self, wallet_api):
        # INIT
        self.wallet_api = wallet_api
        client = MongoClient(connectionString)
        db = client.get_default_database()
        self.col_users = db['users']
        self.update_addresses()

    def update_addresses(self):
        address = self.wallet_api.get_default_address()
        users = self.col_users.find({"Address": address[0]})
        for user in users:
            new_address = wallet_api.create_user_wallet()
            # Update address
            self.col_users.update_one(
                {
                    "_id": user.get('_id')
                },
                {
                    "$set":
                        {
                            "Address": new_address[0],
                        }
                }
            )


def main():
    try:
        AddressFix(wallet_api)

    except Exception as e:
        print(e)


if __name__ == '__main__':
    main()

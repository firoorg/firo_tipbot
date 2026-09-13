import json

from pymongo import MongoClient

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
        default_addresses = self.wallet_api.get_default_address()
        if isinstance(default_addresses, str):
            default_addresses = [default_addresses]
        if not isinstance(default_addresses, list) or not all(
            isinstance(value, str) and value for value in default_addresses
        ):
            raise RuntimeError("getsparkdefaultaddress returned invalid data")
        users = self.col_users.find({"Address": {"$in": default_addresses}})
        for user in users:
            new_address = self.wallet_api.create_user_wallet()
            addresses = user.get("Address", [])
            if isinstance(addresses, str):
                addresses = [addresses]
            addresses = [
                value for value in addresses
                if value not in default_addresses
            ]
            for value in new_address:
                if value not in addresses:
                    addresses.append(value)
            self.col_users.update_one(
                {
                    "_id": user.get('_id')
                },
                {
                    "$set":
                        {
                            "Address": addresses,
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

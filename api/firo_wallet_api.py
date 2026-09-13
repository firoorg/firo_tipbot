import requests


class FiroRPCError(RuntimeError):
    def __init__(self, error):
        self.error = error
        super().__init__(str(error))


class FiroTransportError(RuntimeError):
    pass


class FiroWalletAPI:
    def __init__(self, httpprovider, timeout=(5, 120)):
        self.httpprovider = httpprovider
        self.timeout = timeout
        self.session = requests.Session()

    def _rpc(self, method, params=None, request_id=1):
        payload = {"jsonrpc": "1.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params

        try:
            response = self.session.post(
                self.httpprovider,
                json=payload,
                timeout=self.timeout,
            )
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise FiroTransportError(
                "Firo RPC transport returned no usable response"
            ) from exc

        if not isinstance(result, dict) or "error" not in result:
            raise FiroTransportError("Firo RPC returned an invalid JSON-RPC response")
        if result.get("error") is None:
            if "result" not in result:
                raise FiroTransportError("Firo RPC response has no result")
            try:
                response.raise_for_status()
            except requests.RequestException as exc:
                raise FiroTransportError(
                    "Firo RPC transport returned an HTTP error"
                ) from exc
        return result

    def _result(self, method, params=None, request_id=1):
        response = self._rpc(method, params, request_id)
        if response.get("error"):
            raise FiroRPCError(response["error"])
        return response["result"]

    def create_user_wallet(self):
        return self._result("getnewsparkaddress")

    def get_default_address(self):
        return self._result("getsparkdefaultaddress")

    def get_spark_coin_address(self, tx_hash):
        return self._result("getsparkcoinaddr", [tx_hash])

    def get_txs_list(self, page_size=1000):
        transactions = []
        skip = 0

        # ponytail: full history scan favors correctness; move to a persisted
        # listsinceblock cursor when wallet history makes this measurably slow.
        while True:
            response = self._rpc(
                "listtransactions",
                ["*", page_size, skip],
                request_id=2,
            )
            if response.get("error"):
                return response

            page = response["result"]
            if not isinstance(page, list):
                raise FiroTransportError("listtransactions returned a non-list result")
            transactions.extend(page)
            if len(page) < page_size:
                response["result"] = transactions
                return response
            skip += len(page)

    def listsparkmints(self):
        return self._rpc("listsparkmints", request_id=2)

    def get_wallet_status(self):
        return self._rpc("getinfo", request_id=6)

    def get_tx_status(self, tx_id):
        return self._rpc("gettransaction", [tx_id], request_id=4)

    def automintunspent(self):
        return self._result("automintspark", request_id=4)

    def spendspark(self, address, value, memo="", subtract_fee=False):
        return self._rpc(
            "spendspark",
            [
                {
                    address: {
                        "amount": value,
                        "memo": memo,
                        "subtractFee": subtract_fee,
                    }
                }
            ],
            request_id=4,
        )

    def mintspark(self, address, value):
        return self._rpc(
            "mintspark",
            [
                {
                    address: {
                        "amount": value,
                        "memo": "",
                        "subtractFee": False,
                    }
                }
            ],
            request_id=4,
        )

    def listsparkspends(self):
        return self._rpc("listsparkspends", request_id=4)

    def lelantustospark(self):
        return self._rpc("lelantustospark", request_id=4)

    def validate_address(self, address):
        return self._rpc("validateaddress", [address])

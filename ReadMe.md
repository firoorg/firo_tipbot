## How to deploy Firo Tip bot

Use Ubuntu 22.04 or newer.

Update Ubuntu packages
<pre>sudo apt update</pre>
<pre>sudo apt upgrade</pre>
<pre>sudo apt-get install python3-dev python3-pip python3-virtualenv</pre>

Python 3.10 or newer is required.

Clone firo tip bot repo:
<pre>git clone https://github.com/firoorg/firo_tipbot.git</pre>
<pre>cd firo_tipbot</pre>

Install python requirement packages
<pre>python3 -m pip install -r requirements.txt</pre>

Use Firo Core 0.14.18.0 or newer. This release is required after the
September 2026 hard fork and includes the corrected Spark address lookup.

To check if the bot works correct:
<pre>python3 tipbot.py</pre>
If there's not exceptions, use Ctrl+C to break the process.

Configure TipBot init script
<pre>vim /etc/systemd/system/tipbot.service</pre>

Paste
<pre>
[Unit]
Description=firotipbot
After=network.target
After=mongodb.service


[Service]
Type=simple
WorkingDirectory=/root/firo_tipbot
ExecStart=/usr/bin/python3 tipbot.py
EnvironmentFile=/etc/environment
RestartSec=10
SyslogIdentifier=tipbot
TimeoutStopSec=120
TimeoutStartSec=2
StartLimitInterval=120
StartLimitBurst=5
KillMode=mixed
Restart=always
PrivateTmp=true


[Install]
WantedBy=multi-user.target
</pre>

<pre>systemctl daemon-reload</pre>

Run the following systemctl command to start the MongoDB service:
<pre>sudo systemctl start tipbot.service</pre>
 
Then check the service’s status.
<pre>sudo systemctl status tipbot.service</pre>

After confirming that the service is running as expected, enable the MongoDB service to start up at boot:
<pre>sudo systemctl enable tipbot.service</pre>

To stop the service
<pre>sudo systemctl stop tipbot.service</pre>

### Install Mongodb on ubuntu

Install MongoDB 6.0 or newer from the
[official MongoDB installation guide](https://www.mongodb.com/docs/manual/administration/install-on-linux/).

The tipbot uses MongoDB transactions for every multi-account balance change.
Run MongoDB as a replica set, including on a single server. Add this to
`/etc/mongod.conf`:

<pre>
replication:
  replSetName: rs0
</pre>

Restart MongoDB and initialize the set once:

<pre>sudo systemctl restart mongod</pre>
<pre>mongosh --eval "rs.initiate()"</pre>

Use a connection string containing `?replicaSet=rs0`, as shown in
`services.json`. The bot refuses to start against standalone MongoDB because
standalone writes cannot safely move funds between accounts.

Before the first start of this version, stop every old tipbot process and back
up both MongoDB and the Firo wallet. Reconcile wallet sends and deposits as
described below, then set `mongo.migrationConfirmedOffline` to `true` in
`services.json` and start one updated process. It converts balances to integer
groth, refunds the unclaimed remainder of legacy red envelopes, and quarantines
legacy withdrawals that cannot be reconstructed safely. Do not run old and new
bot versions against the same database. Reset the setting to `false` after the
first successful start.

The old bot could send from the wallet before recording or debiting a withdrawal.
Compare every wallet Spark spend (`listsparkspends`, deduplicated by `txid`) and
other outgoing wallet-history entry with the `senders` and `txs` collections.
Use the original wallet or a verified complete history; a missing transaction
in a restored wallet is not proof that no payout occurred.
For every old send, including one with a matching `senders` record, use the
wallet details, logs, and database backup to verify the requested amount,
recipient, fee, and the sender's balance and lock changes. The old bot could
save a sender record before debiting the account, and its later completion
could debit a different amount. Correct any discrepancy while the bot is
stopped. If ownership or the balance effect cannot be proven, leave the bot
stopped. After a verified correction or classification of a non-bot wallet
send, record the decision in `state`:

```
{_id: "wallet_spend_review:<txid>", balanceReconciled: true,
 reviewNote: "<evidence and balance correction>"}
```

Startup and recurring reconciliation reject old wallet spends without this
review record. New withdrawals require a matching sender record. The review
record is a manual attestation; it does not correct a balance itself.

Legacy deposit records do not identify which user received the old credit.
Reconcile each deposit against wallet outputs, the database backup, and the
current user balance. Replace the old record with a verified output-level event
using `_id: "deposit:<txid>:<address>"`, the verified `txId`, `address`,
`user_id`, positive integer `amount_groth`, `eventVersion: 2`, and status
`"confirmed"` or `"reversed"`. Set `legacyCreditPresent: true` only if this
verified amount was credited to this `user_id` and has not already been
reversed, even if the user later tipped or withdrew it; otherwise set it to
`false`. Correct any wrong recipient or wrong amount separately while offline.
Add a nonempty `legacyReviewNote` explaining the evidence. Do not also apply
the event's expected credit or reversal manually: the migration applies
the difference between `legacyCreditPresent` and `status` exactly once in a
MongoDB transaction. A reversed deposit whose old credit was spent may leave a
negative account, which pauses outgoing transfers until resolved. A legacy
locked balance without a matching withdrawal is preserved and quarantined for
review; it is included in the wallet coverage check.
The bot refuses to start while noncanonical legacy deposit records remain,
including on subsequent restarts.

Run one bot process per database. The bot claims a unique `bot_owner` document
in the `state` collection before migration or recovery and releases it on a
clean shutdown after the accounting worker stops. A second process refuses
to start. After a crash or forced shutdown, stop and verify every bot process
on every host before removing that stale document with
`db.state.deleteOne({_id: "bot_owner"})` in the tipbot database. The record
includes its host and process ID. Never remove it while a bot may still be
running. Completed money migrations are not rerun on ordinary restarts.

Withdrawals with an uncertain RPC outcome retain their reserved funds and
are marked for review. Generic wallet errors can occur after a transaction
was stored, so they cannot establish that a refund is safe. Address, amount,
and time matches are logged as candidates for manual verification. They do
not automatically settle a withdrawal. Reconcile these cases against the
wallet before assigning a transaction ID or refunding funds.

Transfers and envelope claims pause when wallet reconciliation fails, a
confirmed deposit needs review, a reorg leaves any account negative, or
confirmed spendable wallet assets cannot cover positive user balances, envelope
remainders, unresolved withdrawal locks, and unassigned deposits. When assets
fall short, the bot reports the FIRO coverage gap and funding instructions to
the configured admin log (`log_ch` in `services.json`; configure an
administrator-only chat). Startup errors also appear in the service log. On
its first startup it creates a dedicated Spark funding address and saves it
in `state` for reuse; deposits to this address fund the bot's
reserve and are never credited to a user. Back up the wallet again after this
address is generated so a restore retains it; startup checks that the saved
address belongs to the active wallet. If Telegram delivery fails, read the
saved address with `db.state.findOne({_id: "admin_funding_address"}).address`
in `mongosh` only after confirming it appears in the active wallet's
`getallsparkaddresses` result. Send at least the reported shortfall
*net received* to that address from an external wallet, allowing for the
sending wallet's network fee. Pending Spark receipts are excluded until final;
unresolved withdrawal claims are counted conservatively, so review those before
deciding the final top-up. The bot stays online with transfers paused during
a funding-only shortfall and checks wallet assets
periodically. It logs the top-up receipt after two confirmations and chainlock;
the solvency check excludes nonfinal Spark receipts even if the wallet reports
them as available. Do not top up through a user's `/deposit` address, since
that also increases the amount owed
to that user. A top-up does not establish ownership or resolve a legacy wallet
send or deposit review; reconcile each historical event as described above.
The aggregate check can also pause transfers temporarily while automint or
withdrawal change is waiting for confirmation.

The 0.002 FIRO bot fee is included in the command amount. The Firo network fee
is deducted from the recipient output, so the amount shown before confirmation
is a maximum rather than the exact received amount.

The migration also replaces the old shared default deposit address. The bot
sends each affected user their replacement address directly and retries failed
delivery while running; they can also request it with `/deposit` before sending
funds. Any wallet-owned
deposit output without a matching user address, apart from the dedicated admin
funding address, is stored as a `deposit-orphan` review record and logged for
manual ownership checks. It is never assigned to an arbitrary user.

Configure init script
<pre>vim /etc/systemd/system/mongod.service</pre>
<pre>
[Unit]
Description=High-performance, schema-free document-oriented database
After=network.target
Documentation=https://docs.mongodb.org/manual

[Service]
User=mongodb
Group=mongodb
ExecStart=/usr/bin/mongod --quiet --config /etc/mongod.conf
RestartSec=10
TimeoutStopSec=120
TimeoutStartSec=2
StartLimitInterval=120
StartLimitBurst=5
TasksMax=infinity
TasksAccounting=false
KillMode=mixed
Restart=always
PrivateTmp=true

[Install]
WantedBy=multi-user.target
</pre>


<pre>systemctl daemon-reload</pre>

Run the following systemctl command to start the MongoDB service:
<pre>sudo systemctl start mongod.service</pre>
 
Then check the service’s status.
<pre>sudo systemctl status mongod.service</pre>

After confirming that the service is running as expected, enable the MongoDB service to start up at boot:
<pre>sudo systemctl enable mongod.service</pre>

To stop the service
<pre>sudo systemctl stop mongod.service</pre>

## Install Firewall
#### To install Firewall follow instructoins
https://firo.org/guide/masternode-setup.html

We are installing UFW (uncomplicated firewall) to further secure your VPS server. This is optional but highly recommended.

While still in root user on your VPS (or alternatively you can sudo within your newly created user).

<pre>apt install ufw</pre>

(press Y and Enter to confirm)

<pre>ufw allow ssh/tcp</pre>

<pre>ufw limit ssh/tcp</pre>

<pre>ufw logging on</pre>

<pre>ufw enable</pre> 

## How to install Firo Wallet/Node on Ubuntu

#### Download and unpack Firo Core 0.14.18.0 or newer

Use the current Linux release from https://github.com/firoorg/firo/releases.

#### Send files to binary folder

<code>cd firo-&lt;version&gt;; cp bin/* /usr/local/bin</code>

#### Create config file
<pre>nano /root/.firo/firo.conf</pre>

<pre>
#----
rpcuser=user
rpcpassword=password
rpcallowip=127.0.0.1
rpcport=8888
#----
listen=1
server=1
daemon=1
logtimestamps=1
txindex=1
</pre>

#### Run node as daemon with systemctl
https://github.com/firoorg/firo/wiki/Configuring-masternode-with-systemd

# Firo CLI HELP

<pre>
== Addressindex ==
getaddressbalance
getaddressdeltas
getaddressmempool
getaddresstxids
getaddressutxos
gettotalsupply

== Blockchain ==
clearmempool
getbestblockhash
getblock "blockhash" ( verbose )
getblockchaininfo
getblockcount
getblockhash height
getblockhashes timestamp
getblockheader "hash" ( verbose )
getchaintips
getdifficulty
getmempoolancestors txid (verbose)
getmempooldescendants txid (verbose)
getmempoolentry txid
getmempoolinfo
getrawmempool ( verbose )
getspecialtxes "blockhash" ( type count skip verbosity )
gettxout "txid" n ( include_mempool )
gettxoutproof ["txid",...] ( blockhash )
gettxoutsetinfo
preciousblock "blockhash"
pruneblockchain
verifychain ( checklevel nblocks )
verifytxoutproof "proof"

== Control ==
getinfo
getmemoryinfo
help ( "command" )
stop

== Evo ==
bls "command" ...
protx "command" ...
quorum "command" ...
spork list

== Firo ==
evoznode "command"...
evoznode list ( "mode" "filter" )
evoznsync [status|next|reset]

== Generating ==
generate nblocks ( maxtries )
generatetoaddress nblocks address (maxtries)
setgenerate generate ( genproclimit )

== Mining ==
getblocktemplate ( TemplateRequest )
getmininginfo
getnetworkhashps ( nblocks height )
prioritisetransaction txid priority delta fee delta
submitblock "hexdata" ( "jsonparametersobject" )

== Mobile ==
getanonymityset
getlatestcoinids
getmintmetadata
getusedcoinserials

== Network ==
addnode "node" "add|remove|onetry"
clearbanned
disconnectnode "address"
getaddednodeinfo ( "node" )
getconnectioncount
getnettotals
getnetworkinfo
getpeerinfo
listbanned
ping
setban "subnet" "add|remove" (bantime) (absolute)
setnetworkactive true|false

== Rawtransactions ==
createrawtransaction [{"txid":"id","vout":n},...] {"address":amount,"data":"hex",...} ( locktime )
decoderawtransaction "hexstring"
decodescript "hexstring"
fundrawtransaction "hexstring" ( options )
getrawtransaction "txid" ( verbose )
sendrawtransaction "hexstring" ( allowhighfees )
signrawtransaction "hexstring" ( [{"txid":"id","vout":n,"scriptPubKey":"hex","redeemScript":"hex"},...] ["privatekey1",...] sighashtype )

== Util ==
createmultisig nrequired ["key",...]
estimatefee nblocks
estimatepriority nblocks
estimatesmartfee nblocks
estimatesmartpriority nblocks
signmessagewithprivkey "privkey" "message"
validateaddress "address"
verifymessage "address" "signature" "message"

== Wallet ==
abandontransaction "txid"
addmultisigaddress nrequired ["key",...] ( "account" )
addwitnessaddress "address"
This function automatically mints all unspent transparent funds
backupwallet "destination"
bumpfee "txid" ( options )
dumpprivkey "firoaddress"
dumpwallet "filename"
encryptwallet "passphrase"
getaccount "firoaddress"
getaccountaddress "account"
getaddressesbyaccount "account"
getbalance ( "account" minconf include_watchonly )
getnewaddress ( "account" )
getrawchangeaddress
getreceivedbyaccount "account" ( minconf )
getreceivedbyaddress "firoaddress" ( minconf )
gettransaction "txid" ( include_watchonly )
getunconfirmedbalance
getwalletinfo
importaddress "address" ( "label" rescan p2sh )
importmulti "requests" "options"
importprivkey "firoprivkey" ( "label" ) ( rescan )
importprunedfunds
importpubkey "pubkey" ( "label" rescan )
importwallet "filename"
joinsplit {"address":amount,...} (["address",...] )
keypoolrefill ( newsize )
listaccounts ( minconf include_watchonly)
listaddressbalances ( minamount )
listaddressgroupings
listlelantusjoinsplits
listlelantusmints all(false/true)
listlockunspent
listmintzerocoins all(false/true)
listpubcoins all(1/10/25/50/100)
listreceivedbyaccount ( minconf include_empty include_watchonly)
listreceivedbyaddress ( minconf include_empty include_watchonly)
listsigmamints all(false/true)
listsigmapubcoins all(0.05/0.1/0.5/1/10/25/100)
listsigmaspends
listsinceblock ( "blockhash" target_confirmations include_watchonly)
listspendzerocoins
listtransactions ( "account" count skip include_watchonly)
listunspent ( minconf maxconf  ["addresses",...] [include_unsafe] )
listunspentsigmamints [minconf=1] [maxconf=9999999]
listunspentmintzerocoins [minconf=1] [maxconf=9999999]
resetlelantusmint
resetmintzerocoin
resetsigmamint
sendfrom "fromaccount" "toaddress" amount ( minconf "comment" "comment_to" )
sendmany "fromaccount" {"address":amount,...} ( minconf "comment" ["address",...] )
sendtoaddress "firoaddress" amount ( "comment" "comment-to" subtractfeefromamount )
setaccount "firoaddress" "account"
setlelantusmintstatus "coinserial" isused(true/false)
setmininput amount
setmintzerocoinstatus "coinserial" isused(true/false)
setsigmamintstatus "coinserial" isused(true/false)
settxfee amount
signmessage "firoaddress" "message"
spendmany "fromaccount" {"address":amount,...} ( minconf "comment" ["address",...] )
spendmanyzerocoin "{"address":"third party address or blank for internal", "denominations": [{"value":(1,10,25,50,100), "amount":}, {"value":(1,10,25,50,100), "amount":},...]}"
spendzerocoin amount(1,10,25,50,100) ("firoaddress")
</pre>


#### Curl Request 

<code>curl --data-binary '{"jsonrpc": "1.0", "id":"curltest", "method": "getbalance"}' http://user:password@127.0.0.1:8888</code>

<code> curl --data-binary '{"jsonrpc": "1.0", "id":"curltest", "method": "getaddressbalance", "params": [{"addresses": ["XwnLY9Tf7Zsef8gMGL2fhWA9ZmMjt4KPwg"]}] }' -H 'content-type: text/plain;' http://user:password@127.0.0.1:8888</code>

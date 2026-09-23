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

Before the first start of this version, stop every old tipbot process and take
a MongoDB backup. Set `mongo.migrationConfirmedOffline` to `true` in
`services.json`, then start one updated process first. It converts balances to
integer groth, refunds the unclaimed remainder of legacy red envelopes, and
quarantines legacy withdrawals that cannot be reconstructed safely. The bot
refuses to migrate an existing database without this explicit confirmation. Do
not run old and new bot versions against the same database during this
migration. Reset the setting to `false` after the first successful start.

Legacy deposit records do not identify which user received the old credit.
Before starting this version, reconcile every legacy deposit against the wallet
and database backup, then convert each record to a verified output-level deposit
event or resolve the affected balances manually. The bot refuses to start while
untracked legacy deposit records remain, including on subsequent restarts.

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
confirmed deposit needs review, or a reorg leaves any account negative. Resolve
the underlying wallet or accounting issue before transfers resume.

The 0.002 FIRO bot fee is included in the command amount. The Firo network fee
is deducted from the recipient output, so the amount shown before confirmation
is a maximum rather than the exact received amount.

The migration also replaces the old shared default deposit address. Users must
request `/deposit` again before sending funds. Any wallet-owned deposit output
without a matching user address is stored as a `deposit-orphan` review record
and logged for manual ownership checks. It is never assigned to an arbitrary
user.

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

#### Download and unpack Firo Core 0.14.15.1 or newer

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

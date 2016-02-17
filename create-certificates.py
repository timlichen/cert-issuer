#!/usr/bin/env python3

import sys
if sys.version_info.major < 3:
    sys.stderr.write('Sorry, Python 3.x required by this script.\n')
    sys.exit(1)

import getopt
import argparse
import hashlib
import bitcoin.rpc
import sys
import json
import glob
import binascii
import requests
import io
import random
from datetime import datetime
import time
import urllib.parse
from itertools import islice
import shutil
import os

from bitcoin import params
from bitcoin.core import *
from bitcoin.core.script import *
from bitcoin.wallet import CBitcoinAddress, CBitcoinSecret
from bitcoin.signmessage import BitcoinMessage, VerifyMessage, SignMessage

from pycoin.tx import Tx, TxOut
from pycoin.tx.pay_to import build_hash160_lookup
from pycoin.key import Key
from pycoin.encoding import wif_to_secret_exponent
from pycoin.serialize import h2b

import config
import helpers
import secrets

unhexlify = binascii.unhexlify
hexlify = binascii.hexlify
if sys.version > '3':
    unhexlify = lambda h: binascii.unhexlify(h.encode('utf8'))
    hexlify = lambda b: binascii.hexlify(b).decode('utf8')

REMOTE_CONNECT = True
proxy = None

def make_remote_url(command, extras={}, remote_url=False):
    if remote_url == True:
        url = "http://blockchain.info/merchant/%s/%s?password=%s" % (secrets.WALLET_GUID, command, secrets.WALLET_PASSWORD)
    else:
        url = "http://localhost:3000/merchant/%s/%s?password=%s" % (secrets.WALLET_GUID, command, secrets.WALLET_PASSWORD)
    if len(extras) > 0:
        addon = ''
        for name in list(extras.keys()):
            addon = "%s&%s=%s" % (addon, name, extras[name])
        url = url+addon
    return url

def check_for_errors(r):
    if int(r.status_code) != 200:
        sys.stderr.write("Error: %s\n" % (r.json()['error']))
        sys.exit(1)
    # elif 'error' in r.json():
    #     sys.stderr.write("Error: %s\n" % (r.json()['error']))
    #     sys.exit(1)
    return r

def check_if_confirmed(address):
    """Checks if all the BTC in the address has been confirmed. Returns true if is has been confirmed and false if it has not."""
    confirmed_url =  make_remote_url('address_balance', {"address": address, "confirmations": 1}, remote_url = False)
    unconfirmed_url = make_remote_url('address_balance', {"address": address, "confirmations": 0}, remote_url = False)

    unconfirmed_result = check_for_errors(requests.get(unconfirmed_url))
    confirmed_result = check_for_errors(requests.get(confirmed_url))
    
    unconfirmed_balance = unconfirmed_result.json().get("balance", None)
    confirmed_balance = confirmed_result.json().get("balance", None)

    if unconfirmed_balance and confirmed_balance:
        if int(confirmed_balance) == int(unconfirmed_balance):
            return True
    return False

def wait_for_confirmation(address):
    print("Waiting for a pending transaction to be confirmed for address %s" % (address))
    benchmark = datetime.now()
    while(True):
        # confirmed_tx = check_if_confirmed(secrets.STORAGE_ADDRESS)
        confirmed_tx = check_if_confirmed(address)
        elapsed_time = str(datetime.now()-benchmark)
        if confirmed_tx == True:
            print("It took %s to process the trasaction" % (elapsed_time))
            break
        print("Time: %s, waiting 30 seconds and then checking if transaction is confirmed" % (elapsed_time))
        time.sleep(30)
    return confirmed_tx

def prepare_btc():
    print("Starting script...\n")
    if REMOTE_CONNECT == True:

        r = check_for_errors( requests.get( make_remote_url('login', {'api_code': secrets.API_KEY})) )

        # first make sure that there are no pending transactions for the storage address
        confirmed_tx = wait_for_confirmation(secrets.STORAGE_ADDRESS)

        num_certs = len(glob.glob(config.UNSIGNED_CERTS_FOLDER + "*"))
        print("Creating %s temporary addresses...\n" % num_certs)

        temp_addresses = {}
        for i in range(num_certs):
            r = check_for_errors( requests.get( make_remote_url('new_address', {"label": "temp-address-%s" % (i)})) )
            temp_addresses[r.json()["address"]] = int((2*config.BLOCKCHAIN_DUST+2*config.TX_FEES)* COIN)

        print("Transfering BTC to temporary addresses...\n")

        r = check_for_errors( requests.get( make_remote_url('sendmany', {"from": secrets.STORAGE_ADDRESS, "recipients": urllib.parse.quote_plus(json.dumps(temp_addresses)), "fee": int(helpers.calculate_txfee(1, len(temp_addresses))) }) ) )

        print("Waiting for confirmation of transfer...")
        random_address = random.choice(list(temp_addresses.keys()))

        confirmed_tx = wait_for_confirmation(random_address)

        print("\nMaking transfer to issuing address...\n")
        for address in list(temp_addresses.keys()):
            r = check_for_errors( requests.get( make_remote_url('payment', {
                "from": address, 
                "to": secrets.ISSUING_ADDRESS, 
                "amount": int((2*config.BLOCKCHAIN_DUST+config.TX_FEES)*COIN), 
                "fee": int(config.TX_FEES*COIN)} )))
            r = check_for_errors( requests.get( make_remote_url('archive_address', {"address": address} ) ) )
        wait_for_confirmation(secrets.ISSUING_ADDRESS)
        
        return "\nTransfered BTC needed to issue certificates\n"

def prepare_certs():
    folders_to_clear = [config.SIGNED_CERTS_FOLDER, config.HASHED_CERTS_FOLDER, config.UNSIGNED_TXS_FOLDER, config.UNSENT_TXS_FOLDER, config.SENT_TXS_FOLDER]
    for folder in folders_to_clear:
        helpers.clear_folder(folder)
    cert_info = {}
    for f in glob.glob(config.UNSIGNED_CERTS_FOLDER + "*"):
        cert = json.loads(open(f).read())
        cert_info[f.split("/")[-1].split(".")[0]] = {
            "name": cert["recipient"]["givenName"] + " " + cert["recipient"]["familyName"],
            "pubkey": cert["recipient"]["pubkey"]
        }
    return cert_info


# Sign the certificates
def sign_certs():
    privkey = CBitcoinSecret(helpers.import_key())
    for f in glob.glob(config.UNSIGNED_CERTS_FOLDER + "*"):
        uid = helpers.get_uid(f)
        cert = json.loads(open(f).read())
        message = BitcoinMessage(cert["assertion"]["uid"])
        print("Signing certificate for recipient id: %s ..." % (uid))
        signature = SignMessage(privkey, message)
        cert["signature"] = str(signature, 'utf-8')
        cert = json.dumps(cert)
        open(config.SIGNED_CERTS_FOLDER+uid+".json", "wb").write(bytes(cert, 'utf-8'))
    return "Signed certificates for recipients\n"

# Hash the certificates
def hash_certs():
    for f in glob.glob(config.SIGNED_CERTS_FOLDER+"*"):
        uid = helpers.get_uid(f)
        cert = open(f, 'rb').read()
        print("Hashing certificate for recipient id: %s ..." % (uid))
        hashed_cert = hashlib.sha256(cert).digest()
        open(config.HASHED_CERTS_FOLDER + uid + ".txt", "wb").write(hashed_cert)
    return "Hashed certificates for recipients\n"


# Make transactions for the certificates
def build_cert_txs(cert_info, f, ct):
    uid = helpers.get_uid(f)
    cert = open(f, 'rb').read()
    print("Creating tx of certificate for recipient id: %s ..." % (uid))

    txouts = []
    if REMOTE_CONNECT == True:
        r = requests.get("https://blockchain.info/unspent?active=%s&format=json" % (secrets.ISSUING_ADDRESS)).json()
        unspent = []
        for u in r['unspent_outputs']:
            u['outpoint'] = COutPoint(unhexlify(u['tx_hash']), u['tx_output_n'])
            del u['tx_hash']
            del u['tx_output_n']
            u['address'] = CBitcoinAddress(secrets.ISSUING_ADDRESS)
            u['scriptPubKey'] = CScript(unhexlify(u['script']))
            u['amount'] = int(u['value'])
            unspent.append(u)
    else:
        unspent = proxy.listunspent(addrs=[secrets.ISSUING_ADDRESS])
    
    unspent = sorted(unspent, key=lambda x: hash(x['amount']))
    
    if REMOTE_CONNECT == True:
        last_input = unspent[ct] #problem
    else:
        last_input = unspent[-1]

    txins = [CTxIn(last_input['outpoint'])]
    value_in = last_input['amount']

    recipient_addr = CBitcoinAddress(cert_info[uid]["pubkey"])
    recipient_out = CMutableTxOut(int(config.BLOCKCHAIN_DUST*COIN), recipient_addr.to_scriptPubKey())

    revoke_addr = CBitcoinAddress(secrets.REVOCATION_ADDRESS)
    revoke_out = CMutableTxOut(int(config.BLOCKCHAIN_DUST*COIN), revoke_addr.to_scriptPubKey())

    cert_out = CMutableTxOut(0, CScript([OP_RETURN, cert]))
    txouts = [recipient_out] + [revoke_out]

    if int(value_in-((config.BLOCKCHAIN_DUST*2+config.TX_FEES)*COIN)) > 0:
        change_addr = CBitcoinAddress(secrets.ISSUING_ADDRESS)
        change_out = CMutableTxOut(int(value_in-((config.BLOCKCHAIN_DUST*2+config.TX_FEES)*COIN)), change_addr.to_scriptPubKey())
        txouts = txouts + [change_out]
    
    txouts = txouts + [cert_out]

    tx = CMutableTransaction(txins, txouts)
    hextx = hexlify(tx.serialize())
    open(config.UNSIGNED_TXS_FOLDER + uid + ".txt", "wb").write(bytes(hextx, 'utf-8'))
        
    return ("Created unsigned tx for recipient \n" , last_input)

def sign_cert_txs(last_input, f):
    uid = helpers.get_uid(f)
    hextx = str(open(f, 'rb').read(), 'utf-8')

    print("Signing tx with private key for recipient id: %s ..." % uid)

    tx = Tx.from_hex(hextx)
    wif = wif_to_secret_exponent(helpers.import_key())
    lookup = build_hash160_lookup([wif])

    if REMOTE_CONNECT == True:
        tx.set_unspents([ TxOut(coin_value=last_input['amount'], script=unhexlify(last_input['script'])) ])
    else:
        tx.set_unspents([ TxOut(coin_value=last_input['amount'], script=last_input["scriptPubKey"]) ])

    tx = tx.sign(lookup)
    hextx = tx.as_hex()
    open(config.UNSENT_TXS_FOLDER + uid + ".txt", "wb").write(bytes(hextx, 'utf-8'))
    return "Signed tx with private key\n"

def verify_cert_txs(f):

    def verify_signature(address, signed_cert):
        message = BitcoinMessage(signed_cert["assertion"]["uid"])
        signature = signed_cert["signature"]
        return VerifyMessage(address, message, signature)

    def verify_doc(uid):
        hashed_cert = hashlib.sha256(open(config.SIGNED_CERTS_FOLDER+uid+".json", 'rb').read()).hexdigest()
        op_return_hash = open(config.UNSENT_TXS_FOLDER+uid+".txt").read()[-72:-8]
        if hashed_cert == op_return_hash:
            return True
        return False

    uid = helpers.get_uid(f)
    print("UID: \t\t\t" + uid)
    verified_sig = verify_signature(secrets.ISSUING_ADDRESS, json.loads(open(config.SIGNED_CERTS_FOLDER+uid+".json").read()))
    verified_doc = verify_doc(uid)
    print("VERIFY SIGNATURE: \t%s " % (verified_sig))
    print("VERIFY_OP_RETURN: \t%s " % (verified_doc))
    if verified_sig == False or verified_doc == False:
        sys.stderr.write('Sorry, there seems to be an issue with the certificate for recipient id: %s' % (uid))
        sys.exit(1)

    return "Verified transaction is complete.\n"

def send_txs(f):
    uid = helpers.get_uid(f)
    hextx = str(open(f, 'rb').read(), 'utf-8')
    if REMOTE_CONNECT == True:
        r = requests.post("https://insight.bitpay.com/api/tx/send", json={"rawtx": hextx})
        if int(r.status_code) != 200:
            sys.stderr.write("Error broadcasting the transaction through the Insight API. Error msg: %s" % r.text)
            sys.exit(1)
        else:
            txid = r.json().get('txid', None)
    else:
        txid = b2lx(lx(proxy._call('sendrawtransaction', hextx)))
    open(config.SENT_TXS_FOLDER + uid + ".txt", "wb").write(bytes(txid, 'utf-8'))
    print("Broadcast transaction for certificate id %s with a txid of %s" % (uid, txid))
    return "Broadcast transaction.\n"

def main(argv):
    parser = argparse.ArgumentParser(description='Create digital certificates')
    parser.add_argument('--remote', default=1, help='Use remote or local bitcoind (default: remote=1)')
    parser.add_argument('--transfer', default=1, help='Transfer BTC to issuing address (default: 1). Only change this option for troubleshooting.')
    parser.add_argument('--create', default=1, help='Create certificate transactions (default: 1). Only change this option for troubleshooting.')
    parser.add_argument('--broadcast', default=1, help='Broadcast transactions (default: 1). Only change this option for troubleshooting.')
    parser.add_argument('--wificheck', default=1, help='Used to make sure your private key is not plugged in with the wifi on (default: 1). Only change this option for troubleshooting.')
    args = parser.parse_args()

    timestamp = str(time.time())
    global REMOTE_CONNECT
    global proxy
    global COIN
    
    if int(args.remote) == 0:
        REMOTE_CONNECT = False
        args.transfer = 0
        proxy = bitcoin.rpc.Proxy()

    if int(args.remote) == 1:
        REMOTE_CONNECT = True
        COIN = config.COIN

    if int(args.transfer)==0:
        if REMOTE_CONNECT == True:
            r =  make_remote_url('address_balance', {"address": secrets.ISSUING_ADDRESS, "confirmations": 1}, remote_url = True)
            r_result = check_for_errors(requests.get(r))
            address_balance = r_result.json().get("balance", 0)
        else:
            address_balance = 0
            unspent = proxy.listunspent(addrs=[secrets.ISSUING_ADDRESS])
            for u in unspent: 
                address_balance = address_balance + u.get("amount", 0)
        
        cost_to_issue = int((config.BLOCKCHAIN_DUST*2+config.TX_FEES)*COIN) * len(glob.glob(config.UNSIGNED_CERTS_FOLDER + "*"))
        if address_balance < cost_to_issue:
            sys.stderr.write('Sorry, please add %s BTC to the address %s.\n' % ((cost_to_issue - address_balance)/COIN, secrets.ISSUING_ADDRESS))
            sys.exit(1)

    if int(args.transfer)==1:
        if int(args.remote) == 1:
            if int(args.wificheck) == 1:
                helpers.check_internet_on()
            print(prepare_btc())

    if int(args.create)==1:
        cert_info = prepare_certs()
        
        if args.wificheck == 1:
            helpers.check_internet_off()
        print(sign_certs())
        shutil.copytree(config.SIGNED_CERTS_FOLDER, config.ARCHIVE_CERTS_FOLDER+timestamp)
        
        if int(args.wificheck) == 1:
            helpers.check_internet_on()
        print(hash_certs())

    ct = -1
    for f in glob.glob(config.HASHED_CERTS_FOLDER+"*"):
        filename = helpers.get_uid(f)+".txt"
        message, last_input = build_cert_txs(cert_info, f, ct)
        print(message)
        if int(args.create)==1:
            if int(args.wificheck) == 1:
                helpers.check_internet_off()
            print(sign_cert_txs(last_input, config.UNSIGNED_TXS_FOLDER+filename))
            print(verify_cert_txs(config.UNSENT_TXS_FOLDER+filename))
        if int(args.broadcast)==1:
            if int(args.wificheck) == 1:
                helpers.check_internet_on()
            print(send_txs(config.UNSENT_TXS_FOLDER+filename))
        ct -= 1

    if int(args.broadcast)==1:
        shutil.copytree(config.SENT_TXS_FOLDER, config.ARCHIVE_TXS_FOLDER+timestamp)
        print("Archived sent transactions folder for safe keeping.\n")

if __name__ == "__main__":
    main(sys.argv[1:])

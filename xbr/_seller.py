###############################################################################
#
# Copyright (c) Crossbar.io Technologies GmbH and contributors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at https://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations
# under the License.
#
###############################################################################

import os
import time
import uuid
import binascii

import cbor2
import nacl.secret
import nacl.utils

from twisted.internet.defer import inlineCallbacks
from twisted.internet.task import LoopingCall

import txaio

from autobahn.wamp.types import RegisterOptions
from autobahn.wamp.exception import ApplicationError, TransportLost
from autobahn.twisted.util import sleep

import eth_keys
from eth_account import Account

from ._interfaces import IProvider, ISeller
from ._util import hl


class KeySeries(object):

    log = txaio.make_logger()

    def __init__(self, api_id, price, interval, on_rotate=None):
        assert type(api_id) == bytes and len(api_id) == 16
        assert type(price) == int and price > 0
        assert type(interval) == int and interval > 0

        self._api_id = api_id
        self._price = price
        self._interval = interval
        self._on_rotate = on_rotate

        self._id = None
        self._key = None
        self._box = None
        self._archive = {}

        self._run_loop = None
        self._started = None

    @property
    def key_id(self):
        """
        Get current XBR data encryption key ID.

        :return:
        """
        return self._id

    def encrypt(self, payload):
        """
        Encrypt data with the current XBR data encryption key.

        :param payload:
        :return:
        """
        data = cbor2.dumps(payload)
        ciphertext = self._box.encrypt(data)

        return self._id, 'cbor', ciphertext

    def encrypt_key(self, key_id, buyer_pubkey):
        """
        Encrypt a previously used XBR data encryption key with a buyer public key.

        :param key_id:
        :param buyer_pubkey:
        :return:
        """

        # FIXME: check amount paid, post balance and signature
        # FIXME: sign transaction
        key, _ = self._archive[key_id]

        sendkey_box = nacl.public.SealedBox(nacl.public.PublicKey(buyer_pubkey,
                                                                  encoder=nacl.encoding.RawEncoder))

        encrypted_key = sendkey_box.encrypt(key, encoder=nacl.encoding.RawEncoder)

        return encrypted_key

    def start(self):
        assert self._run_loop is None

        self.log.info('Starting key rotation every {interval} seconds for api_id="{api_id}" ..',
                      interval=hl(self._interval), api_id=hl(uuid.UUID(bytes=self._api_id)))

        self._run_loop = LoopingCall(self._rotate)
        self._started = self._run_loop.start(self._interval)

        return self._started

    def stop(self):
        assert self._run_loop

        if self._run_loop:
            self._run_loop.stop()
            self._run_loop = None

        return self._started

    @inlineCallbacks
    def _rotate(self):
        self._id = os.urandom(16)
        self._key = nacl.utils.random(nacl.secret.SecretBox.KEY_SIZE)
        self._box = nacl.secret.SecretBox(self._key)

        self._archive[self._id] = (self._key, self._box)

        self.log.info('Key rotated with new key_id="{key_id}" for api_id={api_id}',
                      key_id=hl(uuid.UUID(bytes=self._id)),
                      api_id=hl(uuid.UUID(bytes=self._api_id)))

        if self._on_rotate:
            yield self._on_rotate(self)


class SimpleSeller(object):

    log = txaio.make_logger()

    def __init__(self, private_key, provider_id=None):
        """

        :param private_key:
        """
        # seller private key/account
        self._pkey = eth_keys.keys.PrivateKey(private_key)
        self._acct = Account.privateKeyToAccount(self._pkey)

        self._provider_id = provider_id or str(self._pkey.public_key)

        self._keys = {}
        self._keys_map = {}

        # after start() is running, these will be set
        self._session = None
        self._session_regs = None

    @property
    def public_key(self):
        """
        Get the seller public key.

        :return:
        """
        return self._pkey.public_key

    def add(self, api_id, prefix, price, interval, categories=None):
        """
        Add a new (rotating) private encryption key for encrypting data on the given API.

        :param api_id: API for which to create a new series of rotating encryption keys.
        :param price: Price in XBR token per key.
        :param interval: Interval (in seconds) in which to auto-rotate the encryption key.
        """
        assert api_id not in self._keys

        @inlineCallbacks
        def on_rotate(key_series):

            key_id = key_series.key_id

            self._keys_map[key_id] = key_series

            # offer the key to the market maker (retry 5x in specific error cases)
            retries = 5
            while retries:
                try:
                    valid_from = time.time_ns() - 10 * 10 ** 9
                    signature = None

                    offer = yield self._session.call('xbr.marketmaker.place_offer',
                                                     key_id,
                                                     api_id,
                                                     prefix,
                                                     valid_from,
                                                     signature,
                                                     privkey=None,
                                                     price=price,
                                                     categories=categories,
                                                     expires=None,
                                                     copies=None,
                                                     provider_id=self._provider_id)
                    self.log.info('Key key_id="{key_id}" offered with offer_id="{offer_id}"',
                                  key_id=hl(uuid.UUID(bytes=key_id)), offer_id=hl(offer['offer']))
                    break

                except ApplicationError as e:
                    if e.error == 'wamp.error.no_such_procedure':
                        self.log.warn('xbr.marketmaker.offer: procedure unavailable!')
                    else:
                        self.log.failure()
                        break
                except TransportLost:
                    self.log.warn('TransportLost while calling xbr.marketmaker.offer!')
                    break
                except:
                    self.log.failure()

                retries -= 1
                self.log.warn('Failed to place offer for key! Retrying {retries}/5 ..', retries=retries)
                yield sleep(1)

        key_series = KeySeries(api_id, price, interval, on_rotate)
        self._keys[api_id] = key_series

        return key_series

    @inlineCallbacks
    def start(self, session):
        """
        Start rotating keys and placing key offers with the XBR market maker.

        :param session: WAMP session over which to communicate with the XBR market maker.
        :param provider_id: The XBR provider ID.
        :return:
        """
        assert self._session is None

        self._session = session
        self._session_regs = []

        procedure = 'xbr.provider.{}.sell'.format(self._provider_id)
        reg = yield session.register(self.sell, procedure, options=RegisterOptions(details_arg='details'))
        self._session_regs.append(reg)
        self.log.info('Registered procedure "{procedure}"', procedure=hl(reg.procedure))

        for key_series in self._keys.values():
            key_series.start()

        if False:
            dl = []
            for func in [self.sell]:
                procedure = 'xbr.provider.{}.{}'.format(self._provider_id, func.__name__)
                d = session.register(func, procedure, options=RegisterOptions(details_arg='details'))
                dl.append(d)
            d = txaio.gather(dl)

            def registered(regs):
                for reg in regs:
                    self.log.info('Registered procedure "{procedure}"', procedure=hl(reg.procedure))
                self._session_regs = regs

            d.addCallback(registered)

            return d

    def stop(self):
        """

        :return:
        """
        dl = []
        for key_series in self._keys.values():
            d = key_series.stop()
            dl.append(d)

        if self._session_regs:
            if self._session and self._session.is_attached():
                # voluntarily unregister interface
                for reg in self._session_regs:
                    d = reg.unregister()
                    dl.append(d)
            self._session_regs = None

        d = txaio.gather(dl)
        return d

    async def wrap(self, api_id, uri, payload):
        """

        :param uri:
        :param payload:
        :return:
        """
        assert api_id in self._keys
        assert type(uri) == str
        assert payload is not None

        keyseries = self._keys[api_id]

        key_id, serializer, ciphertext = keyseries.encrypt(payload)

        return key_id, serializer, ciphertext

    def sell(self, key_id, buyer_pubkey, amount_paid, post_balance, signature, details=None):
        """

        :param key_id:
        :param buyer_pubkey:
        :param amount_paid:
        :param post_balance:
        :param signature:
        :param details:
        :return:
        """
        if key_id not in self._keys_map:
            raise ApplicationError('crossbar.error.no_such_object', 'no key with ID "{}"'.format(key_id))

        key_series = self._keys_map[key_id]

        encrypted_key = key_series.encrypt_key(key_id, buyer_pubkey)

        self.log.info('{tx_type} Key key_id="{key_id}" sold to buyer_pubkey="{buyer_pubkey}" [caller={caller}, caller_authid="{caller_authid}"]',
                      tx_type=hl('XBR SELL', color='magenta'),
                      key_id=hl(uuid.UUID(bytes=key_id)),
                      caller=hl(details.caller),
                      caller_authid=hl(details.caller_authid),
                      buyer_pubkey=hl(binascii.b2a_hex(buyer_pubkey).decode()))

        return encrypted_key


ISeller.register(SimpleSeller)
IProvider.register(SimpleSeller)

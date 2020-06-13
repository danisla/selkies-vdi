# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

import asyncio
import json
import time
import websockets

logger = logging.getLogger("signalling")

"""Signalling API for Gstreamer WebRTC demo

Interfaces with signalling server found at:
  https://github.com/centricular/gstwebrtc-demos/tree/master/signalling

    Usage example:
    from webrtc_signalling import WebRTCSinalling
    signalling = WebRTCSinalling(server, id, peer_id)
    signalling.on_connect = lambda: signalling.setup_call()
    signalling.connect()
    signalling.start()

"""


class WebRTCSignallingError(Exception):
    pass


class WebRTCSignallingErrorNoPeer(Exception):
    pass


class WebRTCSignallingErrorAlreadyInRoom(Exception):
    pass


class WebRTCSignalling:
    def __init__(self, server, id):
        """Initialize the signalling instnance

        Arguments:
            server {string} -- websocket URI to connect to, example: ws://127.0.0.1:8080
            id {string} -- ID of this client when registering.
        """

        self.server = server
        self.id = id
        self.conn = None

        self.on_ice = lambda peer_id, mlineindex, candidate: logger.warning(
            'unhandled ice event')
        self.on_sdp = lambda peer_id, sdp_type, sdp: logger.warning('unhandled sdp event')
        self.on_connect = lambda: logger.warning('unhandled on_connect callback')
        self.on_room_ok = lambda peers: logger.warning('unhandled on_room_ok callback')
        self.on_room_join = lambda peer_id: logger.warning('unhandled on_room_join callback')
        self.on_room_leave = lambda peer_id: logger.warning('unhandled on_room_leave callback')
        self.on_start_session = lambda peer_id: logger.warning('unhandled on_start_session callback')
        self.on_error = lambda v: logger.warning('unhandled on_error callback: %s', v)

    async def setup_call(self, peer_id):
        """Creates session with peer

        Should be called after HELLO is received.

        """
        logger.debug("setting up call")
        await self.conn.send('SESSION %s' % peer_id)

    async def join_room(self, room_id):
        logger.debug("joining room: %s" % room_id)
        await self.conn.send('ROOM %s' % room_id)

    async def connect(self):
        """Connects to and registers id with signalling server

        Sends the HELLO command to the signalling server.

        """

        self.conn = await websockets.connect(self.server)
        await self.conn.send('HELLO %s' % self.id)

    async def disconnect(self):
        logger.info("disconnecting")
        await self.conn.close()

    async def send_ice(self, peer_id, mlineindex, candidate):
        """Sends ice candidate to peer

        Arguments:
            peer_id {string} -- peer in room to send to.
            mlineindex {integer} -- the mlineindex
            candidate {string} -- the candidate
        """

        logger.debug("sending ICE candidate to '%s': '%s'" % (peer_id, candidate))

        msg = json.dumps(
            {'ice': {'candidate': candidate, 'sdpMLineIndex': mlineindex}})
        await self.conn.send('ROOM_PEER_MSG {} {}'.format(peer_id, msg))

    async def send_sdp(self, peer_id, sdp_type, sdp):
        """Sends the SDP to peer

        Arguments:
            peer_id {string} -- peer in room to send to.
            sdp_type {string} -- SDP type, answer or offer.
            sdp {string} -- the SDP
        """

        logger.info("sending sdp type: %s to %s" % (sdp_type, peer_id))
        logger.debug("SDP:\n%s" % sdp)

        sdp = json.dumps({'sdp': {'type': sdp_type, 'sdp': sdp}})
        await self.conn.send('ROOM_PEER_MSG {} {}'.format(peer_id, sdp))
    
    async def send_user_is_host(self, peer_id):
        await self.conn.send('ROOM_PEER_MSG {} user_is_host'.format(peer_id))

    async def send_waiting_for_host(self, peer_id):
        await self.conn.send('ROOM_PEER_MSG {} waiting_for_host'.format(peer_id))

    async def start(self):
        """Handles messages from the signalling server websocket.

        Message types:
          HELLO: response from server indicating peer is registered.
          ERROR*: error messages from server.
          {"sdp": ...}: JSON SDP message
          {"ice": ...}: JSON ICE message
        """
        async for message in self.conn:
            if message == 'HELLO':
                logger.info("connected")
                await self.on_connect()
            elif message.startswith("ROOM"):
                logger.info("room message: %s", message)

                if message.startswith("ROOM_OK"):
                    peers = [p for p in message.replace("ROOM_OK", "").split(" ") if p]
                    await self.on_room_ok(peers)

                if message.startswith("ROOM_PEER_JOINED"):
                    await self.on_room_join(message.split(" ")[-1])

                if message.startswith("ROOM_PEER_LEFT"):
                    await self.on_room_leave(message.split(" ")[-1])

                if message.startswith("ROOM_PEER_MSG"):
                    peer_id = message.split(" ")[1]
                    msg = message.replace("ROOM_PEER_MSG {}".format(peer_id), "").strip()

                    if msg.startswith("start_session"):
                        await self.on_start_session(peer_id)
                        continue

                    # Attempt to parse JSON SDP or ICE message
                    data = None
                    try:
                        data = json.loads(msg)
                    except Exception as e:
                        if isinstance(e, json.decoder.JSONDecodeError):
                            await self.on_error(WebRTCSignallingError("error parsing message as JSON: %s" % msg))
                        else:
                            await self.on_error(WebRTCSignallingError("failed to prase message: %s" % msg))
                        continue
                    if data.get("sdp", None):
                        logger.info("received SDP")
                        logger.debug("SDP:\n%s" % data["sdp"])
                        await self.on_sdp(peer_id, data['sdp'].get('type'),
                                    data['sdp'].get('sdp'))
                    elif data.get("ice", None):
                        logger.info("received ICE")
                        logger.debug("ICE:\n%s" % data.get("ice"))
                        await self.on_ice(peer_id, data['ice'].get('sdpMLineIndex'),
                                    data['ice'].get('candidate'))
                    else:
                        await self.on_error(WebRTCSignallingError("unhandled JSON message: %s", json.dumps(data)))


            elif message.startswith('ERROR'):
                if message == "ERROR peer ":
                    await self.on_error(WebRTCSignallingErrorNoPeer("'%s' not found" % message.split(" ")[2]))
                elif message == "ERROR invalid msg, already in room":
                    await self.on_error(WebRTCSignallingErrorAlreadyInRoom("user is already in room"))
                else:
                    await self.on_error(WebRTCSignallingError("unhandled signalling message: %s" % message))

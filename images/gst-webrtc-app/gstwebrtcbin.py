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

import asyncio
import base64
import json
import logging
import time

import gi
gi.require_version("Gst", "1.0")
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst
from gi.repository import GstWebRTC
from gi.repository import GstSdp

logger = logging.getLogger("gstwebrtcbin")


class GSTWebRTCBinError(Exception):
    pass


class GSTWebRTCBin:
    def __init__(self, name, stun_server=None, turn_server=None):
        """Initialize gstreamer webrtc app.

        Initializes GObjects and checks for required plugins.

        Arguments:
            name {[string]} -- name of the gstwebrtcbin element
            stun_server {[string]} -- Optional STUN server uri in the form of:
                                    stun:<host>:<port>
            turn_server {[string]} -- Optional TURN server uri in the form of:
                                    turn://<user>:<password>@<host>:<port>
        """

        self.name = name
        self.stun_server = stun_server
        self.turn_server = turn_server
        self.webrtcbin = None
        self.video_queue = None
        self.audio_queue = None
        self.data_channel = None

        # WebRTC ICE and SDP events
        self.on_ice = lambda mlineindex, candidate: logger.warning(
            'unhandled ice event')
        self.on_sdp = lambda sdp_type, sdp: logger.warning('unhandled sdp event')

        # Data channel events
        self.on_data_open = lambda: logger.warning('unhandled on_data_open')
        self.on_data_close = lambda: logger.warning('unhandled on_data_close')
        self.on_data_error = lambda: logger.warning('unhandled on_data_error')
        self.on_data_message = lambda msg: logger.warning('unhandled on_data_message')
        self.on_pad_added = lambda gstwebrtcbin, pad: logger.warning('unhandled on_pad_added')

        self.build_webrtcbin_pipeline()

    def build_webrtcbin_pipeline(self):
        """Build webrtcbin element and queues for audio and video
        """

        # Create queue elements
        self.video_queue = Gst.ElementFactory.make("queue", self.name + "_video_queue")
        self.audio_queue = Gst.ElementFactory.make("queue", self.name + "_audio_queue")

        # Create webrtcbin element
        self.webrtcbin = Gst.ElementFactory.make("webrtcbin", self.name)

        # The bundle policy affects how the SDP is generated.
        # This will ultimately determine how many tracks the browser receives.
        # Setting this to max-compat will generate separate tracks for
        # audio and video.
        # See also: https://webrtcstandards.info/sdp-bundle/
        self.webrtcbin.set_property("bundle-policy", "max-compat")

        # Add STUN server
        if self.stun_server:
            self.webrtcbin.set_property("stun-server", self.stun_server)

        # Add TURN server
        if self.turn_server:
            logger.info("adding TURN server: %s" % self.turn_server)
            self.webrtcbin.emit("add-turn-server", self.turn_server)

        self.webrtcbin.connect("pad-added", lambda webrtcbin, pad: self.__pad_added(webrtcbin, pad))

    def set_sdp(self, sdp_type, sdp):
        """Sets remote SDP received by peer.

        Arguments:
            sdp_type {string} -- type of sdp, offer or answer
            sdp {object} -- SDP object

        Raises:
            GSTWebRTCBinError -- thrown if SDP is recevied before session has been started.
            GSTWebRTCBinError -- thrown if SDP type is not 'answer', this script initiates the call, not the peer.
        """

        if not self.webrtcbin:
            raise GSTWebRTCBinError('Received SDP before session started')

        if sdp_type != 'answer':
            raise GSTWebRTCBinError('ERROR: sdp type was not "answer"')

        _, sdpmsg = GstSdp.SDPMessage.new()
        GstSdp.sdp_message_parse_buffer(bytes(sdp.encode()), sdpmsg)
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
        promise = Gst.Promise.new()
        self.webrtcbin.emit('set-remote-description', answer, promise)
        promise.interrupt()

    def set_ice(self, mlineindex, candidate):
        """Adds ice candidate received from signaling server

        Arguments:
            mlineindex {integer} -- the mlineindex
            candidate {string} -- the candidate

        Raises:
            GSTWebRTCBinError -- thrown if called before session is started.
        """

        logger.info("setting ICE candidate: %d, %s" % (mlineindex, candidate))

        if not self.webrtcbin:
            raise GSTWebRTCBinError('Received ICE before session started')

        self.webrtcbin.emit('add-ice-candidate', mlineindex, candidate)
    
    def close_data_channel(self):
        if self.data_channel:
            self.data_channel.close()
    
    def is_data_channel_closed(self):
        if not self.data_channel:
            return True

        return self.data_channel.get_property("ready-state").value_name == 'GST_WEBRTC_DATA_CHANNEL_STATE_CLOSED'

    def is_data_channel_ready(self):
        """Checks to see if the data channel is open.

        Returns:
            [bool] -- true if data channel is open
        """

        return self.data_channel and self.data_channel.get_property("ready-state").value_name == 'GST_WEBRTC_DATA_CHANNEL_STATE_OPEN'

    def send_data_channel_message(self, msg_type, data):
        """Sends message to the peer through the data channel

        Message is dropped if the channel is not open.

        Arguments:
            msg_type {string} -- the type of message being sent
            data {dict} -- data to send, this is JSON serialized.
        """

        if not self.is_data_channel_ready():
            logger.debug(
                "skipping messaage because data channel is not ready: %s" % msg_type)
            return

        msg = {
            "type": msg_type,
            "data": data,
        }
        self.data_channel.emit("send-string", json.dumps(msg))

    def __on_offer_created(self, promise, _, __):
        """Handles on-offer-created promise resolution

        The offer contains the local description.
        Generate a set-local-description action with the offer.
        Sends the offer to the on_sdp handler.

        Arguments:
            promise {GstPromise} -- the promise
            _ {object} -- unused
            __ {object} -- unused
        """

        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer')
        promise = Gst.Promise.new()
        self.webrtcbin.emit('set-local-description', offer, promise)
        promise.interrupt()
        self.on_sdp('offer', offer.sdp.as_text())

    def __on_negotiation_needed(self, webrtcbin):
        """Handles on-negotiation-needed signal, generates create-offer action

        Arguments:
            webrtcbin {GstWebRTCBin gobject} -- webrtcbin gobject
        """

        logger.info("handling on-negotiation-needed, creating offer.")
        promise = Gst.Promise.new_with_change_func(
            self.__on_offer_created, webrtcbin, None)
        webrtcbin.emit('create-offer', None, promise)

    def __send_ice(self, webrtcbin, mlineindex, candidate):
        """Handles on-ice-candidate signal, generates on_ice event

        Arguments:
            webrtcbin {GstWebRTCBin gobject} -- webrtcbin gobject
            mlineindex {integer} -- ice candidate mlineindex
            candidate {string} -- ice candidate string
        """

        logger.debug("received ICE candidate: %d %s", mlineindex, candidate)
        self.on_ice(mlineindex, candidate)

    def __pad_added(self, webrtcbin, pad):
        logger.info("pad-added")
        self.on_pad_added(self, pad)

    def connect_handlers(self):
        # Connect signal handlers
        # Note this must be connected after the webrtcbin has been linked.
        self.webrtcbin.connect(
            'on-negotiation-needed', lambda webrtcbin: self.__on_negotiation_needed(self.webrtcbin))

        self.webrtcbin.connect('on-ice-candidate', lambda webrtcbin, mlineindex,
                            candidate: self.__send_ice(self.webrtcbin, mlineindex, candidate))

    def start_data_channels(self):
        # Create the data channel, this has to be done after the pipeline is PLAYING.
        options = Gst.Structure("application/data-channel")
        options.set_value("ordered", True)
        options.set_value("max-retransmits", 0)
        self.data_channel = self.webrtcbin.emit(
            'create-data-channel', self.name + "_input", options)
        self.data_channel.connect('on-open', lambda _: self.on_data_open())
        self.data_channel.connect('on-close', lambda _: self.on_data_close())
        self.data_channel.connect('on-error', lambda _: self.on_data_error())
        self.data_channel.connect(
            'on-message-string', lambda _, msg: self.on_data_message(msg))

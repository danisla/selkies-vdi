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
import http.client
import json
import logging
import os
import sys
import urllib.parse
import subprocess

from aiohttp import web
from gpu_monitor import GPUMonitor
from metrics import Metrics
from gstwebrtcbin import GSTWebRTCBin
from gstwebrtc_pipeline import GSTX11StreamingPipeline
from webrtc_signalling import WebRTCSignalling, WebRTCSignallingErrorAlreadyInRoom
from webrtc_input import WebRTCInput

logger = logging.getLogger("gstwebrtc_conference")

class GSTWebRTCConferenceRestartRequiredError(Exception):
    pass


class GSTWebRTCConference:
    """Multi peer conference class
    """
    def __init__(self,
            server_id=None,
            metadata_url=None,
            sharing_enabled=False,
            signalling_server=None,
            metrics_port=None,
            framerate=None,
            resolution=None,
            enable_audio=None,
            uinput_mouse_socket=None,
            uinput_js_socket=None,
            enable_clipboard=None,
            config_path=None,
            coturn_web_uri=None,
            coturn_web_username=None,
            coturn_auth_header_name=None,
            encoder=None,
        ):

        self.server_id               = server_id
        self.metadata_url            = metadata_url
        self.sharing_enabled         = sharing_enabled
        self.signalling_server       = signalling_server
        self.metrics_port            = metrics_port
        self.framerate               = framerate
        self.resolution              = resolution
        self.enable_audio            = enable_audio
        self.uinput_mouse_socket     = uinput_mouse_socket
        self.uinput_js_socket        = uinput_js_socket
        self.enable_clipboard        = enable_clipboard
        self.config_path             = config_path
        self.coturn_web_uri          = coturn_web_uri
        self.coturn_web_username     = coturn_web_username
        self.coturn_auth_header_name = coturn_auth_header_name
        self.encoder                 = encoder
        self.pointer_visible         = 0

        # Room ID is obtained from metadata server as 'session_id'
        self.room_id                 = None
        # Host ID is obtained from metadata server as 'user'
        self.host_id                 = None

        self.num_peers_to_link = 0
        self.num_peers_linked = 0
        self.waiting_room = {}
        self.peers = {}
        self.sharing_peers = []
        self.loop = None
        self.session_info_running = False
        self.session_info = {}
        self.running = False

        # Initialize metrics server.
        self.metrics = Metrics(self.metrics_port)

        # Initialize the signalling instance
        self.signalling = WebRTCSignalling(self.signalling_server, self.server_id)
        self.signalling.on_error = self.on_signalling_error
        self.signalling.on_connect = self.on_signalling_connect
        self.signalling.on_room_ok = self.on_signalling_room_ok
        self.signalling.on_room_join = self.on_signalling_room_join
        self.signalling.on_room_leave = self.on_signalling_room_leave
        self.signalling.on_start_session = self.on_signalling_start_session
        self.signalling.on_sdp = self.on_signalling_sdp
        self.signalling.on_ice = self.on_signalling_ice
        
        # Create instance of the streaming pipeline
        self.pipeline = GSTX11StreamingPipeline(
            audio=self.enable_audio,
            framerate=self.framerate,
            encoder=self.encoder,
            resolution=self.resolution
        )
        self.pipeline.on_linked = self.on_linked

        # Initialize the Xinput instance
        self.webrtc_input = WebRTCInput(
            self.uinput_mouse_socket,
            self.uinput_js_socket,
            self.enable_clipboard.lower())
        # Mute the on_clipboard_read callback until a client is bound to it.
        #self.webrtc_input.on_clipboard_read = self.do_nothing
        self.bind_inputs()
        
        # NYI
        self.webrtc_input.on_client_fps = self.do_nothing
        # NYI
        self.webrtc_input.on_client_latency = self.do_nothing
        
        # Initialize GPU monitor
        self.gpu_mon = GPUMonitor(enabled=self.encoder.startswith("nv"))
        self.gpu_mon.on_stats = self.on_gpu_stats

        # Initialize web server for serving status.
        self.web_app = web.Application()
        self.web_app.add_routes([
            web.get('/', self.web_get_status)
        ])
        # Get web server port from environment or default.
        self.web_server_port = int(os.environ.get("WEB_SERVER_PORT", "8084"))

    
    # No-op for callbacks.
    def do_nothing(*args):
        pass
    
    def get_num_peers(self):
        return len(self.waiting_room) + len(self.peers)

    def bind_inputs(self):
        """Bind the webrtc_input callbacks to the connected peers
        This should be called after all peers have joined.
        """

        self.webrtc_input.on_video_encoder_bit_rate = lambda *args: [peer.on_video_encoder_bit_rate(*args) for peer in self.peers.values()]
        self.webrtc_input.on_audio_encoder_bit_rate = lambda *args: [peer.on_audio_encoder_bit_rate(*args) for peer in self.peers.values()]
        self.webrtc_input.on_mouse_pointer_visible = lambda *args: [peer.on_mouse_pointer_visible(*args) for peer in self.peers.values()]
        self.webrtc_input.on_clipboard_read = lambda *args: [peer.on_clipboard_read(*args) for peer in self.peers.values()]
        self.webrtc_input.on_set_fps = lambda *args: [peer.on_set_fps(*args) for peer in self.peers.values()]
        self.webrtc_input.on_set_enable_audio = lambda *args: [peer.on_set_enable_audio(*args) for peer in self.peers.values()]
        self.webrtc_input.on_set_resolution = lambda *args: [peer.on_set_resolution(*args) for peer in self.peers.values()]

    def fetch_metadata(self):
        try:
            return json.load(urllib.request.urlopen(self.metadata_url))
        except Exception as e:
            if "404" in str(e):
                pass
            else:
                logger.error("failed to fetch metadata from %s: %s" % (self.metadata_url, str(e)))
            return None
    
    async def start_session_info_watcher(self):
        logger.info("starting session info metadata watcher")
        self.session_info_running = True
        while self.session_info_running:
            metadata = self.fetch_metadata()
            if metadata:
                self.host_id = metadata.get("user", "")
                self.room_id = metadata.get("session_key", "")
                self.session_info = metadata
            await asyncio.sleep(2)
    
    def stop_session_info_watcher(self):
        logger.info("shutting down session into metadata watcher")
        self.session_info_running = False

    # Handle errors from the signalling server.
    async def on_signalling_error(self, e):
        if isinstance(e, WebRTCSignallingErrorAlreadyInRoom):
            logger.error("server already in room, reconnecting.")
            await self.signalling.disconnect()
            await asyncio.sleep(2)
            await self.signalling.connect()
        else:
            logger.error("signalling error: %s", str(e))
    
    async def on_signalling_connect(self):
        logger.info("on_signalling_connect")

        # Wait for session info to become available from the metadata server.
        logger.info("Waiting for session info from metadata server")
        while self.room_id is None and self.host_id is None:
            await asyncio.sleep(0.1)
        logger.info("Got session info from metadata, joining room: %s" % self.room_id)

        await self.signalling.join_room(self.room_id)

    # Handle new peers joining the room.
    async def on_signalling_room_join(self, peer_id):
        logger.info("peer joined room: {}".format(peer_id))

        #self.num_peers += 1
        #if peer_id == self.host_id or self.room_id is None:
        #    await self.signalling.send_user_is_host(peer_id)
        #    if peer_id not in self.peers:
        #        if len(self.peers) > 0:
        #            # Peers are sharing, but host is reconnecting, restart stream.
        #            logger.info("Restarting app to reset session")
        #            self.running = False
        #        else:
        #            # Host joined and is only peer, add stream.
        #            await self.add_peer(peer_id)
        #else:
        #    await self.signalling.send_waiting_for_host(peer_id)

        if peer_id not in self.waiting_room:
            peer = await self.make_peer(peer_id)
            self.waiting_room[peer_id] = peer
            logger.info("peer joined waiting room: %s" % peer_id)
        
        # Autostart sharing when all peers are present.
        await self.autostart_sharing()
        
        #if peer_id not in self.peers:
        #    if peer_id == self.host_id:    
        #        logger.info("peer is host: %s" % peer_id)
        #    self.num_peers += 1
        #    await self.add_peer(peer_id)
    
    async def autostart_sharing(self):
        # If peer list is found, and all peers have joined, start the session.
        if os.path.exists("/var/run/appconfig/sharing_peers.txt"):
            with open("/var/run/appconfig/sharing_peers.txt", 'r') as f:
                self.sharing_peers = [line.rstrip() for line in f.readlines()]
            
            all_found = True
            for peer_id in self.sharing_peers:
                if peer_id not in self.waiting_room:
                    all_found = False
                    break
            
            if all_found:
                logger.info("all peers found, starting session")
                os.unlink("/var/run/appconfig/sharing_peers.txt")
                await self.start_with_peers()

    async def on_signalling_room_leave(self, peer_id):        
        # Remove peer from waiting room, if present.
        if peer_id in self.waiting_room:
            del self.waiting_room[peer_id]

        # Remove peer from active peers.
        if peer_id in self.peers:
            self.pipeline.del_webrtcbin(peer_id)
            del self.peers[peer_id]

        logger.info("peer left: {}, total peers: {}".format(peer_id, self.get_num_peers()))

    async def make_peer(self, peer_id):
        stun_uri, turn_uri = await self.fetch_coturn(
            self.coturn_web_uri,
            "{}-{}".format(peer_id, self.coturn_web_username),
            self.coturn_auth_header_name
        )

        peer = GSTWebRTCPeer(
            loop = self.loop,
            peer_id = peer_id,
            host_id = self.host_id,
            input_enabled = (peer_id == self.host_id or self.room_id == None),
            on_set_peer_input = self.on_set_peer_input,
            config_path = self.config_path,
            pipeline = self.pipeline,
            signalling = self.signalling,
            webrtc_input = self.webrtc_input,
            stun_server = stun_uri,
            turn_server = turn_uri,
            on_pad_added = self.on_peer_pad_added,
            shutdown_fn = self.stop
        )
        return peer

    async def add_peer(self, peer):
        self.peers[peer.peer_id] = peer
        self.pipeline.add_peer(peer.gstwebrtcbin)
        logger.info("added peer: {}, total peers: {}".format(peer.peer_id, self.get_num_peers()))
    
    def on_peer_pad_added(self, peer, gstwebrtcbin, pad):
        logger.info("pad added: {}, {}".format(gstwebrtcbin.name, pad.get_property("name")))

    # Called each time a peer is linked to the pipeline.
    def on_linked(self, gstwebrtcbin):
        logger.info("on_linked: {}".format(gstwebrtcbin.name))

        # Connect WebRTC handlers now that pipeline is linked with all video and (optional) audio pads.
        gstwebrtcbin.connect_handlers()

        #self.num_peers_linked += 1
        #logger.info("peers linked: %d/%d" % (self.num_peers_linked, self.num_peers_to_link))

        gstwebrtcbin.video_queue.sync_state_with_parent()
        self.pipeline.video_tee.sync_state_with_parent()
        
        if self.pipeline.audio:
            gstwebrtcbin.audio_queue.sync_state_with_parent()
            self.pipeline.audio_tee.sync_state_with_parent()

        gstwebrtcbin.webrtcbin.sync_state_with_parent()

        logger.info("current state: %s" % self.pipeline.get_state())

        logger.info("starting data channel")
        gstwebrtcbin.start_data_channels()

        #if self.num_peers_linked == self.num_peers:
        #    logger.info("binding inputs")
        #    self.bind_inputs()
        #self.bind_inputs()
    
    async def on_signalling_start_session(self, peer_id):
        if peer_id == self.host_id:
            await self.start_with_peers()
            return
            
            if len(self.waiting_room) == 1:
                if self.pipeline.is_running():
                    logger.warning("will not start when pipeline already running.")
                else:
                    await self.start_with_peers()
            else:
                logger.info("restarting with list of peers")
                # Write list of peers to file and restart app.
                with open("/var/run/appconfig/sharing_peers.txt", 'w') as f:
                    for peer_id in self.waiting_room.keys():
                        f.write("%s\n" % peer_id)
                self.running = False
        else:
            logger.warning("rejecting start share request from {} because user is not host".format(peer_id))
    
    async def start_with_peers(self):
        if not self.pipeline.is_running():
            self.pipeline.start_pipeline()

        # on_linked method is called per peer indicating that peer is linked.
        # the pipeline is started and transitioned to the PLAYING state after all peers are linked.
        self.num_peers_to_link = len(self.waiting_room)
        self.num_peers_linked = 0

        for peer_id, peer in self.waiting_room.items():
            if peer_id not in self.peers:
                self.peers[peer_id] = peer
                await self.add_peer(peer)

    async def on_signalling_room_ok(self, peer_ids=[]):
        logger.info("server joined room, peer_ids: {}".format(peer_ids))

        #if not self.host_id in peer_ids and self.room_id is not None:
        #    logging.info("waiting for host to join before adding other peers.")
        #    for peer_id in peer_ids:
        #        await self.signalling.send_waiting_for_host(peer_id)
        #    return

        #for peer_id in peer_ids:
        #    if peer_id == self.host_id:
        #        await self.signalling.send_user_is_host(peer_id)
        #    await self.add_peer(peer_id)
        for peer_id in peer_ids:
            peer = await self.make_peer(peer_id)
            self.waiting_room[peer_id] = peer
            logger.info("added peer to waiting room: %s" % peer_id)
        
        # Autostart sharing when all peers are present.
        await self.autostart_sharing()
    
    async def on_signalling_sdp(self, peer_id, *args):
        logger.info("on_signalling_sdp: {}".format(peer_id))
        peer = self.peers.get(peer_id, None)
        if peer:
            peer.gstwebrtcbin.set_sdp(*args)
        else:
            logger.warning("on_signalling_sdp: peer_id not found: {}".format(peer_id))

    async def on_signalling_ice(self, peer_id, *args):
        logger.info("on_signalling_ice: {}".format(peer_id))
        peer = self.peers.get(peer_id, None)
        if peer:
            peer.gstwebrtcbin.set_ice(*args)
        else:
            logger.warning("on_signalling_ice: peer_id not found: {}".format(peer_id))
    
    def on_gpu_stats(self, *args):
        for peer in self.peers.values():
            self.loop.run_in_executor(None, peer.send_gpu_stats, *args)
    
    def on_set_peer_input(self, peer_id, input_enabled=False):
        logger.info("on_set_peer_input: %s=%s" % (peer_id, input_enabled))
        peer = self.peers.get(peer_id, None)
        if peer:
            peer.input_enabled = input_enabled
            
            # Broadcast state to all peers.
            for p in self.peers.values():
                p.send_peer_input_state(peer_id, input_enabled)
        else:
            logger.warning("attempt to set peer input but peer not found: %s" % peer_id)
    
    async def fetch_coturn(self, uri, user, auth_header_name):
        """Fetches TURN uri from a coturn web API

        Arguments:
            uri {string} -- uri of coturn web service, example: http://localhost:8081/
            user {string} -- username used to generate coturn credential, for example: <hostname>

        Raises:
            Exception -- if response http status code is >= 400

        Returns:
            [string] -- TURN URI used with gstwebrtcbin in the form of:
                            turn://<user>:<password>@<host>:<port>
                        NOTE that the user and password are URI encoded to escape special characters like '/'
        """

        parsed_uri = urllib.parse.urlparse(uri)

        conn = http.client.HTTPConnection(parsed_uri.netloc)
        auth_headers = {
            auth_header_name: user
        }

        conn.request("GET", parsed_uri.path, headers=auth_headers)
        resp = conn.getresponse()
        if resp.status >= 400:
            raise Exception(resp.reason)

        ice_servers = json.loads(resp.read())['iceServers']
        stun = turn = ice_servers[0]['urls'][0]
        stun_host = stun.split(":")[1]
        stun_port = stun.split(":")[2].split("?")[0]

        turn = ice_servers[1]['urls'][0]
        turn_host = turn.split(':')[1]
        turn_port = turn.split(':')[2].split('?')[0]
        turn_user = ice_servers[1]['username']
        turn_password = ice_servers[1]['credential']

        stun_uri = "stun://%s:%s" % (
            stun_host,
            stun_port
        )

        turn_uri = "turn://%s:%s@%s:%s" % (
            urllib.parse.quote(turn_user, safe=""),
            urllib.parse.quote(turn_password, safe=""),
            turn_host,
            turn_port
        )

        return stun_uri, turn_uri
    
    def make_status(self):
        webrtc_status = {
            "sharing_enabled": self.sharing_enabled,
            "audio_enabled": self.enable_audio,
            "clipboard": self.enable_clipboard,
            "resolution": self.resolution,
            "framerate": self.framerate,
        }

        return {
            "webrtc": webrtc_status,
            "session_info": self.session_info,
        }

    # Web server status route handler.
    async def web_get_status(self, req):
        def dumps(data):
            return json.dumps(data, sort_keys=True, indent=2)
        return web.json_response(self.make_status(), dumps=dumps)
    
    # Run async web server
    async def start_web_server(self):
        logger.info("Starting web server on port %d" % self.web_server_port)
        self.web_app_runner = web.AppRunner(self.web_app)
        await self.web_app_runner.setup()
        site = web.TCPSite(self.web_app_runner, '0.0.0.0', self.web_server_port)
        await site.start()
    
    def start(self):
        self.loop = asyncio.get_event_loop()
        self.metrics.start()
        self.loop.run_until_complete(self.webrtc_input.connect())
        self.loop.run_until_complete(self.signalling.connect())

        tasks = [
            self.start_session_info_watcher(),
            self.start_web_server(),
            self.webrtc_input.start_clipboard(),
            self.gpu_mon.start(),
            self.signalling.start(),
            self.shutdown(),
        ]
        self.tasks = asyncio.gather(*tasks, loop=self.loop)
        self.running = True
        self.loop.run_until_complete(self.tasks)
        logger.info("loop complete")
        sys.exit(0)

    async def shutdown(self):
        while self.running:
            await asyncio.sleep(0.2)

        self.stop_session_info_watcher()
        self.webrtc_input.stop_clipboard()
        self.webrtc_input.disconnect()
        self.gpu_mon.stop()
        await self.stop_web_server()
        await self.signalling.disconnect()
            
    async def stop_web_server(self):
        logger.info("Shutting down web server")
        await self.web_app_runner.cleanup()

    def stop(self):
        logger.info("Shutting down app")
        self.running = False

class GSTWebRTCPeer:
    """Peer connected to conference
    """
    def __init__(self,
            loop = None,
            peer_id = None,
            host_id = None,
            input_enabled = False,
            on_set_peer_input = None,
            config_path = None,
            pipeline = None,
            signalling = None,
            webrtc_input = None,
            stun_server = None,
            turn_server = None,
            on_pad_added = None,
            shutdown_fn = None
        ):
        
        self.loop          = loop
        self.peer_id       = peer_id
        self.host_id       = host_id,
        self.input_enabled = input_enabled
        self.on_set_peer_input = on_set_peer_input
        self.config_path   = config_path
        self.pipeline      = pipeline
        self.signalling    = signalling
        self.webrtc_input  = webrtc_input
        self.stun_server   = stun_server
        self.turn_server   = turn_server
        self.on_pad_added  = on_pad_added
        self.shutdown_fn   = shutdown_fn

        # TODO, figure out why this is being passed in as tuple.
        self.host_id = list(self.host_id)[0]

        self.sdp_sent = False

        # Initialize the peer webrtcbin
        self.gstwebrtcbin = GSTWebRTCBin(
            self.peer_id,
            self.stun_server,
            self.turn_server
        )
        self.gstwebrtcbin.on_sdp = self.on_sdp
        self.gstwebrtcbin.on_ice = self.on_ice
        self.gstwebrtcbin.on_data_open = self.on_data_open
        self.gstwebrtcbin.on_data_message = self.on_data_message
        self.gstwebrtcbin.on_pad_added = lambda *args: self.on_pad_added(self, *args)
        
    def log_info(self, msg):
        logger.info("[{}] {}".format(self.peer_id, msg))
    
    def log_warning(self, msg):
        logger.warning("[{}] {}".format(self.peer_id, msg))
    
    def set_json_app_argument(self, key, value):
        """Writes kv pair to json argument file

        Arguments:
            key {string} -- the name of the argument to set
            value {any} -- the value of the argument to set
        """
        assert self.config_path

        if not os.path.exists(self.config_path):
            # Create new file
            with open(self.config_path, 'w') as f:
                json.dump({}, f)

        # Read current config JSON
        json_data = json.load(open(self.config_path))

        # Set the new value for the argument.
        json_data[key] = value

        # Save the json file
        json.dump(json_data, open(self.config_path, 'w'))
    
    def on_sdp(self, *args):
        if not self.sdp_sent:
            self.loop.create_task(self.signalling.send_sdp(self.peer_id, *args))
            self.sdp_sent = True
        
    def on_ice(self, *args):
        self.loop.create_task(self.signalling.send_ice(self.peer_id, *args))
    
    def on_data_open(self, *args):
        self.log_info("on_data_open")
        self.send_video_bitrate(self.pipeline.video_bitrate)
        self.send_audio_bitrate(self.pipeline.audio_bitrate)
        self.send_pointer_visible(self.pipeline.pointer_visible)
        self.send_framerate(self.pipeline.framerate)
        self.send_audio_enabled(self.pipeline.audio)
        self.send_supported_resolutions(self.webrtc_input.supported_resolutions, self.pipeline.resolution)
        self.send_peer_input_state(self.host_id, True)
    
    def send_video_bitrate(self, bitrate):
        self.gstwebrtcbin.send_data_channel_message(
            "pipeline", {"status": "Video bitrate set to: %d" % bitrate})
    
    def send_audio_bitrate(self, bitrate):
        self.gstwebrtcbin.send_data_channel_message(
            "pipeline", {"status": "Audio bitrate set to: %d" % bitrate})
    
    def send_pointer_visible(self, visible):
        self.gstwebrtcbin.send_data_channel_message(
            "pipeline", {"status": "Set pointer visibility to: %d" % visible})

    def send_clipboard_data(self, data):
        self.gstwebrtcbin.send_data_channel_message(
            "clipboard", {"content": base64.b64encode(data.encode()).decode("utf-8")})
    
    def send_peer_input_state(self, peer_id, input_enabled):
        self.gstwebrtcbin.send_data_channel_message(
            "system", {"action": "peer_input,%s,%s" % (peer_id, input_enabled)})

    def send_gpu_stats(self, load, memory_total, memory_used):
        """Sends GPU stats to the data channel

        Arguments:
            load {float} -- utilization of GPU between 0 and 1
            memory_total {float} -- total memory on GPU in MB
            memory_used {float} -- memor used on GPU in MB
        """

        self.gstwebrtcbin.send_data_channel_message("gpu_stats", {
            "load": load,
            "memory_total": memory_total,
            "memory_used": memory_used,
        })

    def send_reload_window(self):
        """Sends reload window command to the data channel
        """

        logger.info("sending window reload")
        self.gstwebrtcbin.send_data_channel_message(
            "system", {"action": "reload"})

    def send_framerate(self, framerate):
        """Sends the current frame rate to the data channel
        """

        logger.info("sending frame rate")
        self.gstwebrtcbin.send_data_channel_message(
            "system", {"action": "framerate,"+str(framerate)})

    def send_audio_enabled(self, audio_enabled):
        """Sends the current audio state
        """

        logger.info("sending audio enabled")
        self.gstwebrtcbin.send_data_channel_message(
            "system", {"action": "audio,"+str(audio_enabled)})

    def send_supported_resolutions(self, supported_resolutions, resolution):
        """Sends the list of supported resolutions and the current resolution
        """

        logger.info("sending supported resolutions")
        self.gstwebrtcbin.send_data_channel_message(
            "system", {"action": "supported_resolutions," + ";".join(supported_resolutions)})
        self.gstwebrtcbin.send_data_channel_message(
            "system", {"action": "resolution,"+str(resolution)})

    def on_data_message(self, *args):
        msg = args[0]
        toks = msg.split(",")
        if toks[0] == "pi":
            if self.peer_id == self.host_id:
                # Peer Input command, enable peer input.
                peer_id = toks[1]
                input_enabled = toks[2] == "1"
                self.log_warning("input control for peer: %s = %s" % (peer_id, input_enabled))
                self.on_set_peer_input(peer_id, input_enabled)
            else:
                self.log_warning("attempt to change input from non host peer: %s != %s" % (self.peer_id, self.host_id))

            return

        if self.input_enabled:
            self.webrtc_input.on_message(*args)

    def on_video_encoder_bit_rate(self, bitrate):
        self.log_info("on_video_encoder_bit_rate")
        if self.input_enabled:
            self.log_info("setting video bitrate")
            self.pipeline.set_video_bitrate(int(bitrate))
            self.log_info("sending video bitrate")
            self.send_video_bitrate(self.pipeline.video_bitrate)
    
    def on_audio_encoder_bit_rate(self, bitrate):
        self.log_info("on_audio_encoder_bit_rate")
        if self.input_enabled:
            self.log_info("setting audio bitrate")
            self.pipeline.set_audio_bitrate(int(bitrate))
            self.log_info("sending audio bitrate")
            self.send_audio_bitrate(self.pipeline.audio_bitrate)
    
    def on_mouse_pointer_visible(self, visible):
        self.log_info("on_mouse_pointer_visible")
        if self.input_enabled:
            self.pipeline.set_pointer_visible(visible)
            self.send_pointer_visible(self.pipeline.pointer_visible)

    def on_clipboard_read(self, data):
        self.log_info("on_clipboard_read")
        if self.input_enabled:
            self.send_clipboard_data(data)

    def on_set_fps(self, fps):
        """FPS is a setting that affects all peers and requires pipeline restart.
        
        Arguments:
            fps {int} -- framerate
        
        Raises:
            GSTWebRTCConferenceRestartRequiredError
        """
        self.log_info("on_set_fps(%s)" % str(fps))
        if self.input_enabled:
            self.set_json_app_argument("framerate", fps)
            self.log_warning("reload required to change fps")
            self.send_reload_window()
            self.shutdown_fn()
    
    def on_set_enable_audio(self, enabled):
        """Audio is a setting that affects all peers and requires a pipeline restart.

        Arguments:
            enabled {boolean} -- audio enabled

        Raises:
            GSTWebRTCConferenceRestartRequiredError
        """
        self.log_info("on_set_enable_audio")
        if self.input_enabled and self.pipeline.audio != enabled:
            self.set_json_app_argument("enable_audio", enabled)
            self.log_warning("reload required to change enable_audio")
            self.send_reload_window()
            self.shutdown_fn()

    def on_set_resolution(self, resolution):
        """Resolution is a setting that affects all peers and requires a pipeline restart.

        Arguments:
            resolution {string} -- widthxheight

        Raises:
            GSTWebRTCConferenceRestartRequiredError
        """
        self.log_info("on_set_resolution")
        if self.input_enabled and self.pipeline.resolution != resolution:
            if resolution in self.webrtc_input.supported_resolutions:
                self.set_json_app_argument("resolution", resolution)
                if subprocess.run(['xrandr', '-s', resolution]).returncode != 0:
                    self.log_warning("failed to set resolution")
                else:    
                    if subprocess.run(['xrandr', '--fb', resolution]).returncode != 0:
                        self.log_warning("failed to set resolution")
                    else:
                        self.log_warning("reload required to change resolution")
                        self.send_reload_window()
                        self.shutdown_fn()
            else:
                self.log_warning("unsupported resolution: " + resolution)

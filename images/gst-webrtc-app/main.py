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

import argparse
import json
import logging
import os
import socket
import sys
import time

from gstwebrtc_conference import GSTWebRTCConference

import logging
logger = logging.getLogger("main")

def wait_for_app_ready(ready_file, app_auto_init = True):
    """Wait for streaming app ready signal.

    returns when either app_auto_init is True OR the file at ready_file exists.

    Keyword Arguments:
        app_auto_init {bool} -- skip wait for appready file (default: {True})
    """

    logger.info("Waiting for streaming app ready")
    logger.debug("app_auto_init=%s, ready_file=%s" % (app_auto_init, ready_file))

    while not (app_auto_init or os.path.exists(ready_file)):
        time.sleep(0.2)



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--json_config',
                        default=os.environ.get(
                            'JSON_CONFIG', '/var/run/appconfig/streaming_args.json'),
                        help='Path to JSON file containing argument key-value pairs that are overlayed with cli args/env.')
    parser.add_argument('--metadata_url',
                        default=os.environ.get(
                            'METADATA_URL', ''),
                        help='URL to metadata service where session info and sharing key is obtained')
    parser.add_argument('--sharing_enabled',
                        default=os.environ.get(
                            'SHARING_ENABLED', 'false'),
                        help='enable session sharing, true or false, default: false')
    parser.add_argument('--server',
                        default=os.environ.get(
                            'SIGNALLING_SERVER', 'ws://127.0.0.1:8080'),
                        help='Signalling server to connect to, default: "ws://127.0.0.1:8080"')
    parser.add_argument('--coturn_web_uri',
                        default=os.environ.get(
                            'COTURN_WEB_URI', 'http://localhost:8081'),
                        help='URI for coturn REST API service, example: http://localhost:8081')
    parser.add_argument('--coturn_web_username',
                        default=os.environ.get(
                            'COTURN_WEB_USERNAME', socket.gethostname()),
                        help='URI for coturn REST API service, default is the system hostname')
    parser.add_argument('--coturn_auth_header_name',
                        default=os.environ.get(
                            'COTURN_AUTH_HEADER_NAME', 'x-auth-user'),
                        help='header name to pass user to coturn web service')
    parser.add_argument('--uinput_mouse_socket',
                        default=os.environ.get('UINPUT_MOUSE_SOCKET', ''),
                        help='path to uinput mouse socket provided by uinput-device-plugin, if not provided, uinput is used directly.')
    parser.add_argument('--uinput_js_socket',
                        default=os.environ.get('UINPUT_JS_SOCKET', ''),
                        help='path to uinput joystick socket provided by uinput-device-plugin, if not provided, uinput is used directly.')
    parser.add_argument('--enable_audio',
                        default=os.environ.get('ENABLE_AUDIO', 'true'),
                        help='enable or disable audio stream')
    parser.add_argument('--enable_clipboard',
                        default=os.environ.get('ENABLE_CLIPBOARD', 'true'),
                        help='enable or disable the clipboard features, supported values: true, false, in, out')
    parser.add_argument('--app_auto_init',
                        default=os.environ.get('APP_AUTO_INIT', 'true'),
                        help='if true, skips wait for APP_READY_FILE to exist before starting stream.')
    parser.add_argument('--app_ready_file',
                        default=os.environ.get('APP_READY_FILE', '/var/run/appconfig/appready'),
                        help='file set by sidecar used to indicate that app is initialized and ready')
    parser.add_argument('--framerate',
                        default=os.environ.get('WEBRTC_FRAMERATE', '30'),
                        help='framerate of streaming pipeline')
    parser.add_argument('--resolution',
                        default=os.environ.get('RESOLUTION', "1920x1080"),
                        help='default resolution to set X display to')
    parser.add_argument('--encoder',
                        default=os.environ.get('WEBRTC_ENCODER', 'nvh264enc'),
                        help='gstreamer encoder plugin to use')
    parser.add_argument('--metrics_port',
                        default=os.environ.get('METRICS_PORT', '8000'),
                        help='port to start metrics server on')
    parser.add_argument('--debug', action='store_true',
                        help='Enable debug logging')
    args = parser.parse_args()

    if os.path.exists(args.json_config):
        # Read and overlay args from json file
        # Note that these are explicit overrides only.
        try:
            json_args = json.load(open(args.json_config))
            for k, v in json_args.items():
                if k == "framerate":
                    args.framerate = int(v)
                if k == "enable_audio":
                    args.enable_audio = str((str(v).lower() == 'true')).lower()
                if k == "encoder":
                    args.ecoder = v.lower()
                if k == "resolution":
                    args.resolution = v.lower()
        except Exception as e:
            logger.error("failed to load json config from %s: %s" % (args.json_config, str(e)))

    # Set log level
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    logger.warning(args)

    # Wait for streaming app to initialize
    wait_for_app_ready(args.app_ready_file, args.app_auto_init == "true")

    # Peer id for this app, default is 0, expecting remote peer id to be 1
    my_id = 'webrtc@localhost'

    # Initialize conference
    conference = GSTWebRTCConference(
        server_id = my_id,
        metadata_url = args.metadata_url,
        sharing_enabled = args.sharing_enabled.lower() == "true",
        signalling_server = args.server,
        metrics_port = int(args.metrics_port),
        framerate = int(args.framerate),
        resolution = args.resolution,
        enable_audio = args.enable_audio.lower() == "true",
        uinput_mouse_socket = args.uinput_mouse_socket,
        uinput_js_socket = args.uinput_js_socket,
        enable_clipboard = args.enable_clipboard.lower(),
        config_path = args.json_config,
        coturn_web_uri = args.coturn_web_uri,
        coturn_web_username = args.coturn_web_username,
        coturn_auth_header_name = args.coturn_auth_header_name,
        encoder = args.encoder
    )

    try:
        conference.start()
    except Exception as e:
        logger.error("Caught exception: %s" % e)
        sys.exit(1)
    finally:
        conference.stop()
        sys.exit(0)
    # [END main_start]

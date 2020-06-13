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

import gi
gi.require_version("Gst", "1.0")
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst
from gi.repository import GstWebRTC
from gi.repository import GstSdp

logger = logging.getLogger("gstwebrtc_pipeline")


class GSTX11StreamingPipelineError(Exception):
    pass


class GSTX11StreamingPipeline:
    def __init__(self, audio=True, framerate=30, encoder=None, video_bitrate=2000, audio_bitrate=64000, pointer_visible=0, resolution=None):
        """Initializes the GStreamer X11 streaming pipeline

        Keyword Arguments:
            audio {bool} -- Enable audio pipeline (default: {True})
            framerate {int} -- X11 capture framerate (default: {30})
        """

        self.audio = audio
        self.framerate = framerate
        self.encoder = encoder
        if self.encoder is None:
            self.encoder = "nvh264enc"
        self.video_bitrate = video_bitrate
        self.audio_bitrate = audio_bitrate
        self.pointer_visible = pointer_visible
        self.resolution = resolution

        self.on_linked = lambda gstwebrtcbin: logger.warning("unhandled on_linked")

        self.pipeline = None
        self.video_tee = None
        self.audio_tee = None
        self.peers = 0

        Gst.init(None)

        self.check_plugins()

        # Create a new pipeline, elements will be added to this.
        # see build_video_pipeline() and build_audio_pipeline()
        self.pipeline = Gst.Pipeline.new()

        # Construct the media pipeline with video and audio.
        self.build_video_pipeline()

        if self.audio:
            self.build_audio_pipeline()

    def check_plugins(self):
        """Check for required gstreamer plugins.

        Raises:
            GSTX11StreamingPipelineError -- thrown if any plugins are missing.
        """

        required = ["opus", "nice", "webrtc", "dtls", "srtp", "rtp", "sctp",
                    "rtpmanager", "ximagesrc"]

        supported = ["nvh264enc", "vp8enc", "vp9enc"]
        if self.encoder not in supported:
            raise GSTX11StreamingPipelineError('Unsupported encoder, must be one of: ' + ','.join(supported))

        if self.encoder.startswith("nv"):
            required.append("nvcodec")

        if self.encoder.startswith("vp"):
            required.append("vpx")

        missing = list(
            filter(lambda p: Gst.Registry.get().find_plugin(p) is None, required))
        if missing:
            raise GSTX11StreamingPipelineError('Missing gstreamer plugins:', missing)

    # [START build_video_pipeline]
    def build_video_pipeline(self):
        """Adds the RTP video stream to the pipeline.
        """

        # Create ximagesrc element named x11
        # Note that when using the ximagesrc plugin, ensure that the X11 server was
        # started with shared memory support: '+extension MIT-SHM' to achieve
        # full frame rates.
        # You can check if XSHM is in use with the following command:
        #   GST_DEBUG=default:5 gst-launch-1.0 ximagesrc ! fakesink num-buffers=1 2>&1 |grep -i xshm
        ximagesrc = Gst.ElementFactory.make("ximagesrc", "x11")

        # disables display of the pointer using the XFixes extension,
        # common when building a remote desktop interface as the clients
        # mouse pointer can be used to give the user perceived lower latency.
        # This can be programmatically toggled after the pipeline is started
        # for example if the user is viewing full screen in the browser,
        # they may want to revert to seeing the remote cursor when the
        # client side cursor disappears.
        ximagesrc.set_property("show-pointer", self.pointer_visible)

        # Tells GStreamer that you are using an X11 window manager or
        # compositor with off-screen buffer. If you are not using a
        # window manager this can be set to 0. It's also important to
        # make sure that your X11 server is running with the XSHM extension
        # to ensure direct memory access to frames which will reduce latency.
        ximagesrc.set_property("remote", 1)

        # Defines the size in bytes to read per buffer. Increasing this from
        # the default of 4096 bytes helps performance when capturing high
        # resolutions like 1080P, and 2K.
        ximagesrc.set_property("blocksize", 16384)

        # The X11 XDamage extension allows the X server to indicate when a
        # regions of the screen has changed. While this can significantly
        # reduce CPU usage when the screen is idle, it has little effect with
        # constant motion. This can also have a negative consequences with H.264
        # as the video stream can drop out and take several seconds to recover
        # until a valid I-Frame is received.
        # Set this to 0 for most streaming use cases.
        ximagesrc.set_property("use-damage", 0)

        # Create capabilities for ximagesrc
        ximagesrc_caps = Gst.caps_from_string("video/x-raw")

        # Setting the framerate=60/1 capability instructs the ximagesrc element
        # to generate buffers at 60 frames per second (FPS).
        # The higher the FPS, the lower the latency so this parameter is one
        # way to set the overall target latency of the pipeline though keep in
        # mind that the pipeline may not always perfom at the full 60 FPS.
        ximagesrc_caps.set_value("framerate", Gst.Fraction(self.framerate, 1))

        # Create a capability filter for the ximagesrc_caps
        ximagesrc_capsfilter = Gst.ElementFactory.make("capsfilter")
        ximagesrc_capsfilter.set_property("caps", ximagesrc_caps)

        if self.encoder in ["nvh264enc"]:
            # Upload buffers from ximagesrc directly to CUDA memory where
            # the colorspace conversion will be performed.
            cudaupload = Gst.ElementFactory.make("cudaupload")

            # Convert the colorspace from BGRx to NVENC compatible format.
            # This is performed with CUDA which reduces the overall CPU load
            # compared to using the software videoconvert element.
            cudaconvert = Gst.ElementFactory.make("cudaconvert")

            # Convert ximagesrc BGRx format to I420 using cudaconvert.
            # This is a more compatible format for client-side software decoders.
            cudaconvert_caps = Gst.caps_from_string("video/x-raw(memory:CUDAMemory)")
            cudaconvert_caps.set_value("format", "I420")
            cudaconvert_capsfilter = Gst.ElementFactory.make("capsfilter")
            cudaconvert_capsfilter.set_property("caps", cudaconvert_caps)

            # Create the nvh264enc element named nvenc.
            # This is the heart of the video pipeline that converts the raw
            # frame buffers to an H.264 encoded byte-stream on the GPU.
            nvh264enc = Gst.ElementFactory.make("nvh264enc", "nvenc")

            # The initial bitrate of the encoder in bits per second.
            # Setting this to 0 will use the bitrate from the NVENC preset.
            # This parameter can be set while the pipeline is running using the
            # set_video_bitrate() method. This helps to match the available
            # bandwidth. If set too high, the cliend side jitter buffer will
            # not be unable to lock on to the stream and it will fail to render.
            nvh264enc.set_property("bitrate", self.video_bitrate)

            # Rate control mode tells the encoder how to compress the frames to
            # reach the target bitrate. A Constant Bit Rate (CBR) setting is best
            # for streaming use cases as bit rate is the most important factor.
            # A Variable Bit Rate (VBR) setting tells the encoder to adjust the
            # compression level based on scene complexity, something not needed
            # when streaming in real-time.
            nvh264enc.set_property("rc-mode", "cbr")

            # Group of Pictures (GOP) size is the distance between I-Frames that
            # contain the full frame data needed to render a whole frame.
            # Infinite GOP is best for streaming because it reduces the number
            # of large I-Frames being transmitted. At higher resolutions, these
            # I-Frames can dominate the bandwidth and add additional latency.
            # With infinite GOP, you can use a higher bit rate to increase quality
            # without a linear increase in total bandwidth.
            # A negative consequence when using infinite GOP size is that
            # when packets are lost, it may take the decoder longer to recover.
            # NVENC supports infinite GOP by setting this to -1.
            nvh264enc.set_property("gop-size", -1)

            # Instructs encoder to handle Quality of Service (QOS) events from
            # the rest of the pipeline. Setting this to true increases
            # encoder stability.
            nvh264enc.set_property("qos", True)

            # The NVENC encoder supports a limited nubmer of encoding presets.
            # These presets are different than the open x264 standard.
            # The presets control the picture coding technique, bitrate,
            # and encoding quality.
            # The low-latency-hq is the NVENC preset reccomended for streaming.
            #
            # See this link for details on each preset:
            #   https://streamquality.report/docs/report.html#1080p60-nvenc-h264-picture-quality
            nvh264enc.set_property("preset", "low-latency-hq")

            # Set the capabilities for the nvh264enc element.
            nvh264enc_caps = Gst.caps_from_string("video/x-h264")

            # Sets the H.264 encoding profile to one compatible with WebRTC.
            # The high profile is used for streaming HD video.
            # Browsers only support specific H.264 profiles and they are
            # coded in the RTP payload type set by the rtph264pay_caps below.
            nvh264enc_caps.set_value("profile", "high")

            # Create a capability filter for the nvh264enc_caps.
            nvh264enc_capsfilter = Gst.ElementFactory.make("capsfilter")
            nvh264enc_capsfilter.set_property("caps", nvh264enc_caps)

            # Create the rtph264pay element to convert buffers into
            # RTP packets that are sent over the connection transport.
            rtph264pay = Gst.ElementFactory.make("rtph264pay")

            # Set the capabilities for the rtph264pay element.
            rtph264pay_caps = Gst.caps_from_string("application/x-rtp")

            # Set the payload type to video.
            rtph264pay_caps.set_value("media", "video")

            # Set the video encoding name to match our encoded format.
            rtph264pay_caps.set_value("encoding-name", "H264")

            # Set the payload type to one that matches the encoding profile.
            # Payload number 123 corresponds to H.264 encoding with the high profile.
            # Other payloads can be derived using WebRTC specification:
            #   https://tools.ietf.org/html/rfc6184#section-8.2.1
            rtph264pay_caps.set_value("payload", 123)

            # Create a capability filter for the rtph264pay_caps.
            rtph264pay_capsfilter = Gst.ElementFactory.make("capsfilter")
            rtph264pay_capsfilter.set_property("caps", rtph264pay_caps)

        elif self.encoder in ["vp8enc", "vp9enc"]:
            videoconvert = Gst.ElementFactory.make("videoconvert")
            videoconvert_caps = Gst.caps_from_string("video/x-raw,format=I420")
            videoconvert_capsfilter = Gst.ElementFactory.make("capsfilter")
            videoconvert_capsfilter.set_property("caps", videoconvert_caps)

            if self.encoder == "vp8enc":
                vpenc = Gst.ElementFactory.make("vp8enc", "vpenc")
                vpenc_caps = Gst.caps_from_string("video/x-vp8")
                vpenc_capsfilter = Gst.ElementFactory.make("capsfilter")
                vpenc_capsfilter.set_property("caps", vpenc_caps)

                rtpvppay = Gst.ElementFactory.make("rtpvp8pay")
                rtpvppay_caps = Gst.caps_from_string("application/x-rtp")
                rtpvppay_caps.set_value("media", "video")
                rtpvppay_caps.set_value("encoding-name", "VP8")
                rtpvppay_caps.set_value("payload", 123)
                rtpvppay_capsfilter = Gst.ElementFactory.make("capsfilter")
                rtpvppay_capsfilter.set_property("caps", rtpvppay_caps)

            if self.encoder == "vp9enc":
                vpenc = Gst.ElementFactory.make("vp9enc", "vpenc")
                vpenc.set_property("threads", 4)
                vpenc_caps = Gst.caps_from_string("video/x-vp9")
                vpenc_capsfilter = Gst.ElementFactory.make("capsfilter")
                vpenc_capsfilter.set_property("caps", vpenc_caps)

                rtpvppay = Gst.ElementFactory.make("rtpvp9pay")
                rtpvppay_caps = Gst.caps_from_string("application/x-rtp")
                rtpvppay_caps.set_value("media", "video")
                rtpvppay_caps.set_value("encoding-name", "VP9")
                rtpvppay_caps.set_value("payload", 123)
                rtpvppay_capsfilter = Gst.ElementFactory.make("capsfilter")
                rtpvppay_capsfilter.set_property("caps", rtpvppay_caps)

            # VPX Parameters
            # Borrowed from: https://github.com/nurdism/neko/blob/df98368137732b8aaf840e27cdf2bd41067b2161/server/internal/gst/gst.go#L94
            vpenc.set_property("threads", 2)
            vpenc.set_property("cpu-used", 8)
            vpenc.set_property("deadline", 1)
            vpenc.set_property("error-resilient", "partitions")
            vpenc.set_property("keyframe-max-dist", 10)
            vpenc.set_property("auto-alt-ref", True)
            vpenc.set_property("target-bitrate", 2000*1000)

        else:
            raise GSTX11StreamingPipelineError("Unsupported encoder for pipeline: %s" % self.encoder)


        # Create tee for video
        self.video_tee = Gst.ElementFactory.make("tee", "video_tee")

        # Add all elements to the pipeline.
        self.pipeline.add(ximagesrc)
        self.pipeline.add(ximagesrc_capsfilter)
        self.pipeline.add(self.video_tee)

        if self.encoder == "nvh264enc":
            self.pipeline.add(cudaupload)
            self.pipeline.add(cudaconvert)
            self.pipeline.add(cudaconvert_capsfilter)
            self.pipeline.add(nvh264enc)
            self.pipeline.add(nvh264enc_capsfilter)
            self.pipeline.add(rtph264pay)
            self.pipeline.add(rtph264pay_capsfilter)

        elif self.encoder.startswith("vp"):
            self.pipeline.add(videoconvert)
            self.pipeline.add(videoconvert_capsfilter)
            self.pipeline.add(vpenc)
            self.pipeline.add(vpenc_capsfilter)
            self.pipeline.add(rtpvppay)
            self.pipeline.add(rtpvppay_capsfilter)

        # Link the pipeline elements and raise exception of linking fails
        # due to incompatible element pad capabilities.
        if not Gst.Element.link(ximagesrc, ximagesrc_capsfilter):
            raise GSTX11StreamingPipelineError("Failed to link ximagesrc -> ximagesrc_capsfilter")

        if self.encoder == "nvh264enc":
            if not Gst.Element.link(ximagesrc_capsfilter, cudaupload):
                raise GSTX11StreamingPipelineError(
                    "Failed to link ximagesrc_capsfilter -> cudaupload")

            if not Gst.Element.link(cudaupload, cudaconvert):
                raise GSTX11StreamingPipelineError(
                    "Failed to link cudaupload -> cudaconvert")

            if not Gst.Element.link(cudaconvert, cudaconvert_capsfilter):
                raise GSTX11StreamingPipelineError(
                    "Failed to link cudaconvert -> cudaconvert_capsfilter")

            if not Gst.Element.link(cudaconvert_capsfilter, nvh264enc):
                raise GSTX11StreamingPipelineError(
                    "Failed to link cudaconvert_capsfilter -> nvh264enc")

            if not Gst.Element.link(nvh264enc, nvh264enc_capsfilter):
                raise GSTX11StreamingPipelineError(
                    "Failed to link nvh264enc -> nvh264enc_capsfilter")

            if not Gst.Element.link(nvh264enc_capsfilter, rtph264pay):
                raise GSTX11StreamingPipelineError(
                    "Failed to link nvh264enc_capsfilter -> rtph264pay")

            if not Gst.Element.link(rtph264pay, rtph264pay_capsfilter):
                raise GSTX11StreamingPipelineError(
                    "Failed to link rtph264pay -> rtph264pay_capsfilter")
           
            # Link the last element to the video_tee
            if not Gst.Element.link(rtph264pay_capsfilter, self.video_tee):
                raise GSTX11StreamingPipelineError(
                    "Failed to link rtph264pay_capsfilter -> video_tee")

        elif self.encoder.startswith("vp"):
            if not Gst.Element.link(ximagesrc_capsfilter, videoconvert):
                raise GSTX11StreamingPipelineError(
                    "Failed to link ximagesrc_capsfilter -> videoconvert")

            if not Gst.Element.link(videoconvert, videoconvert_capsfilter):
                raise GSTX11StreamingPipelineError(
                    "Failed to link videoconvert -> videoconvert_capsfilter")

            if not Gst.Element.link(videoconvert_capsfilter, vpenc):
                raise GSTX11StreamingPipelineError(
                    "Failed to link videoconvert_capsfilter -> vpenc")

            if not Gst.Element.link(vpenc, vpenc_capsfilter):
                raise GSTX11StreamingPipelineError(
                    "Failed to link vpenc -> vpenc_capsfilter")

            if not Gst.Element.link(vpenc_capsfilter, rtpvppay):
                raise GSTX11StreamingPipelineError(
                    "Failed to link vpenc_capsfilter -> rtpvppay")

            if not Gst.Element.link(rtpvppay, rtpvppay_capsfilter):
                raise GSTX11StreamingPipelineError(
                    "Failed to link rtpvppay -> rtpvppay_capsfilter")

            # Link the last element to the video_tee
            if not Gst.Element.link(rtpvppay_capsfilter, self.video_tee):
                raise GSTX11StreamingPipelineError(
                    "Failed to link rtpvppay_capsfilter -> video_tee")
    
    # [END build_video_pipeline]

    # [START build_audio_pipeline]
    def build_audio_pipeline(self):
        """Adds the RTP audio stream to the pipeline.
        """

        # Create the audio tee element
        self.audio_tee = Gst.ElementFactory.make("tee", "audio_tee")

        # Create element for receiving audio from pulseaudio.
        pulsesrc = Gst.ElementFactory.make("pulsesrc", "pulsesrc")

        # Let the audio source provide the global clock.
        # This is important when trying to keep the audio and video
        # jitter buffers in sync. If there is skew between the video and audio
        # buffers, features like NetEQ will continuously increase the size of the
        # jitter buffer to catch up and will never recover.
        pulsesrc.set_property("provide-clock", True)

        # Apply stream time to buffers, this helps with pipeline synchronization.
        pulsesrc.set_property("do-timestamp", True)

        # Encode the raw pulseaudio stream to opus format which is the
        # default packetized streaming format for the web.
        opusenc = Gst.ElementFactory.make("opusenc", "opusenc")

        # Set audio bitrate to 64kbps.
        # This can be dynamically changed using set_audio_bitrate()
        opusenc.set_property("bitrate", self.audio_bitrate)

        # Create the rtpopuspay element to convert buffers into
        # RTP packets that are sent over the connection transport.
        rtpopuspay = Gst.ElementFactory.make("rtpopuspay")

        # Insert a queue for the RTP packets.
        rtpopuspay_queue = Gst.ElementFactory.make("queue", "rtpopuspay_queue")

        # Make the queue leaky, so just drop packets if the queue is behind.
        rtpopuspay_queue.set_property("leaky", True)

        # Set the queue max time to 16ms (16000000ns)
        # If the pipeline is behind by more than 1s, the packets
        # will be dropped.
        # This helps buffer out latency in the audio source.
        rtpopuspay_queue.set_property("max-size-time", 16000000)

        # Set the other queue sizes to 0 to make it only time-based.
        rtpopuspay_queue.set_property("max-size-buffers", 0)
        rtpopuspay_queue.set_property("max-size-bytes", 0)

        # Set the capabilities for the rtpopuspay element.
        rtpopuspay_caps = Gst.caps_from_string("application/x-rtp")

        # Set the payload type to audio.
        rtpopuspay_caps.set_value("media", "audio")

        # Set the audio encoding name to match our encoded format.
        rtpopuspay_caps.set_value("encoding-name", "OPUS")

        # Set the payload type to match the encoding format.
        # A value of 96 is the default that most browsers use for Opus.
        # See the RFC for details:
        #   https://tools.ietf.org/html/rfc4566#section-6
        rtpopuspay_caps.set_value("payload", 96)

        # Create a capability filter for the rtpopuspay_caps.
        rtpopuspay_capsfilter = Gst.ElementFactory.make("capsfilter")
        rtpopuspay_capsfilter.set_property("caps", rtpopuspay_caps)

        # Add all elements to the pipeline.
        self.pipeline.add(pulsesrc)
        self.pipeline.add(opusenc)
        self.pipeline.add(rtpopuspay)
        self.pipeline.add(rtpopuspay_queue)
        self.pipeline.add(rtpopuspay_capsfilter)
        self.pipeline.add(self.audio_tee)

        # Link the pipeline elements and raise exception of linking fails
        # due to incompatible element pad capabilities.
        if not Gst.Element.link(pulsesrc, opusenc):
            raise GSTX11StreamingPipelineError("Failed to link pulsesrc -> opusenc")

        if not Gst.Element.link(opusenc, rtpopuspay):
            raise GSTX11StreamingPipelineError("Failed to link opusenc -> rtpopuspay")

        if not Gst.Element.link(rtpopuspay, rtpopuspay_queue):
            raise GSTX11StreamingPipelineError("Failed to link rtpopuspay -> rtpopuspay_queue")

        if not Gst.Element.link(rtpopuspay_queue, rtpopuspay_capsfilter):
            raise GSTX11StreamingPipelineError(
                "Failed to link rtpopuspay_queue -> rtpopuspay_capsfilter")

        # Link the last element to the audio_tee
        if not Gst.Element.link(rtpopuspay_capsfilter, self.audio_tee):
            raise GSTX11StreamingPipelineError(
                "Failed to link rtpopuspay_capsfilter -> audio_tee")

    # [END build_audio_pipeline]

    def add_peer(self, gstwebrtcbin):
        """Adds the given gstwebrtcbin to the pipeline with a queue

        Arguments:
            webrtcbin {GstElement} -- the gstwebrtc bin element
        """

        assert self.pipeline
        assert self.video_tee
        assert gstwebrtcbin.webrtcbin
        assert gstwebrtcbin.video_queue

        # Add video elements to the pipeline.
        self.pipeline.add(gstwebrtcbin.video_queue)
        self.pipeline.add(gstwebrtcbin.webrtcbin)

        # Link the video_tee to the video_queue.
        if not Gst.Element.link(self.video_tee, gstwebrtcbin.video_queue):
            raise GSTX11StreamingPipelineError(
                "Failed to link video_tee -> video_queue")

        # Link the video_queue to the webrtcbin.
        if not Gst.Element.link(gstwebrtcbin.video_queue, gstwebrtcbin.webrtcbin):
            raise GSTX11StreamingPipelineError(
                "Failed to link video_queue -> webrtcbin")

        # Add audio elements to the pipeline.        
        if self.audio:
            assert self.audio_tee
            assert gstwebrtcbin.audio_queue

            self.pipeline.add(gstwebrtcbin.audio_queue)

            # Link the audio_tee to the audio_queue.
            if not Gst.Element.link(self.audio_tee, gstwebrtcbin.audio_queue):
                raise GSTX11StreamingPipelineError(
                    "Failed to link audio_tee -> audio_queue")

            # Link the audio_queue to the webrtcbin.
            if not Gst.Element.link(gstwebrtcbin.audio_queue, gstwebrtcbin.webrtcbin):
                raise GSTX11StreamingPipelineError(
                    "Failed to link audio_queue -> webrtcbin")

        logger.info("pipeline linked")

        self.peers += 1

        self.on_linked(gstwebrtcbin)

    def del_webrtcbin(self, name):
        # Rust Example: https://github.com/centricular/gstwebrtc-demos/blob/afb927c0facb3793767d4c1d8d247fd1709afe66/multiparty-sendrecv/gst-rust/src/main.rs#L516

        if not self.pipeline:
            return

        logger.info("deleting peer from pipeline: %s" % name)

        if self.peers == 1:
            self.pipeline.set_state(Gst.State.READY)

        # Fetch the elements to remove.
        webrtcbin = Gst.Bin.get_by_name(self.pipeline, name)
        webrtcbin_video_queue = Gst.Bin.get_by_name(self.pipeline, name + "_video_queue")

        webrtcbin.set_state(Gst.State.NULL)
        webrtcbin_video_queue.set_state(Gst.State.NULL)

        if self.audio:
            webrtcbin_audio_queue = Gst.Bin.get_by_name(self.pipeline, name + "_audio_queue")
            webrtcbin_audio_queue.set_state(Gst.State.NULL)
            self.pipeline.remove(webrtcbin_audio_queue)

        self.pipeline.remove(webrtcbin_video_queue)
        self.pipeline.remove(webrtcbin)

        self.peers = max(0, self.peers - 1)

        if self.peers == 0:
            self.pipeline.set_state(Gst.State.NULL)

    def stop_pipeline(self):
        """Stops the gstreamer pipeline
        """

        if not self.pipeline:
            return

        # Advance the state of the pipeline to NULL.
        res = self.pipeline.set_state(Gst.State.NULL)
        if res.value_name != 'GST_STATE_CHANGE_SUCCESS':
            raise GSTX11StreamingPipelineError(
                "Failed to transition pipeline to NULL: %s" % res)

        logger.info("pipeline started")        

    def start_pipeline(self):
        """Starts the gstreamer pipeline
        """

        # Advance the state of the pipeline to PLAYING.
        res = self.pipeline.set_state(Gst.State.PLAYING)
        if res.value_name != 'GST_STATE_CHANGE_SUCCESS':
            raise GSTX11StreamingPipelineError(
                "Failed to transition pipeline to PLAYING: %s" % res)

        logger.info("pipeline started")

    def set_video_bitrate(self, bitrate):
        """Set NvEnc encoder target bitrate in bps

        Arguments:
            bitrate {integer} -- bitrate in bits per second, for example, 2000 for 2kbits/s or 10000 for 1mbit/sec.
        """
        
        if self.encoder.startswith("nv"):
            element = Gst.Bin.get_by_name(self.pipeline, "nvenc")
            element.set_property("bitrate", bitrate)
        elif self.encoder.startswith("vp"):
            element = Gst.Bin.get_by_name(self.pipeline, "vpenc")
            element.set_property("target-bitrate", bitrate*1000)
        else:
            logger.warning("set_video_bitrate not supported with encoder: %s" % self.encoder)
    
    def set_audio_bitrate(self, bitrate):
        """Set Opus encoder target bitrate in bps

        Arguments:
            bitrate {integer} -- bitrate in bits per second, for example, 96000 for 96kbits/s.
        """

        element = Gst.Bin.get_by_name(self.pipeline, "opusenc")
        if element:
            element.set_property("bitrate", bitrate)
            self.audio_bitrate = bitrate

    def set_pointer_visible(self, visible):
        """Set pointer visibiltiy on the ximagesrc element

        Arguments:
            visible {bool} -- True to enable pointer visibility
        """

        element = Gst.Bin.get_by_name(self.pipeline, "x11")
        element.set_property("show-pointer", visible)
        self.pointer_visible = visible

    def dump_pipeline(self):
        details = Gst.DebugGraphDetails.ALL
        logger.info("dumping pipeline graph: {}".format(Gst.debug_bin_to_dot_file_with_ts(self.pipeline, details, "pipeline")))

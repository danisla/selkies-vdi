# Copyright 2020 Google LLC
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

FROM ubuntu:19.10

# Use GCE apt servers
ARG GCE_REGION=us-west1
RUN cp /etc/apt/sources.list /etc/apt/sources.list.orig && \
    sed -i "s/archive.ubuntu.com/${GCE_REGION}.gce.archive.ubuntu.com/g" /etc/apt/sources.list

# Install essentials
RUN \
    apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        curl \
        build-essential \
        ca-certificates \
        git \
        vim

WORKDIR /opt

# Build and install Intel Media SDK
RUN \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        cmake libudev-dev libpciaccess-dev \
        autoconf libtool libdrm-dev xorg xorg-dev openbox libx11-dev libgl1-mesa-glx libgl1-mesa-dev

# Install meson build deps
RUN \
    apt-get update && apt install -y python3-pip python-gi-dev ninja-build && \
    pip3 install meson

ARG LIBVA_VERSION=2.8.0
RUN \
    git clone --depth 1 https://github.com/intel/libva.git -b ${LIBVA_VERSION} /opt/libva && \
    cd /opt/libva && \
    meson build --prefix=/usr && \
    ninja -C build install

ARG GMMLIB_VERSION=intel-gmmlib-20.3.2
RUN \
    mkdir -p /opt/intel/gmmlib && \
    git clone --depth 1 https://github.com/intel/gmmlib.git -b ${GMMLIB_VERSION} /opt/intel/gmmlib && \
    cd /opt/intel/gmmlib && \
    (mkdir build && cd build && cmake --prefix=/usr .. -G Ninja) && \
    ninja -C build install

ARG INTEL_MEDIA_VERSION=intel-media-20.2.0
RUN \
    mkdir -p /opt/intel/intel-media && \
    git clone --depth 1 https://github.com/intel/media-driver.git -b ${INTEL_MEDIA_VERSION} /opt/intel/intel-media && \
    cd /opt/intel/intel-media && \
    (mkdir build && cd build && cmake --prefix=/usr .. -G Ninja) && \
    ninja -C build install

ARG INTEL_MEDIA_SDK_VERSION=intel-mediasdk-20.2.1
RUN \
    mkdir -p /opt/intel/msdk && \
    git clone --depth 1 https://github.com/Intel-Media-SDK/MediaSDK.git -b ${INTEL_MEDIA_SDK_VERSION} /opt/intel/msdk && \
    cd /opt/intel/msdk && \
    (mkdir build && cd build && cmake .. -G Ninja) && \
    ninja -C build install

# Install test tools
RUN \
    apt-get install -y vainfo pciutils

ENV LIBVA_DRIVERS_PATH=/usr/lib/x86_64-linux-gnu/dri
ENV LIBVA_DRIVER_NAME=iHD

# Testing:
# vainfo
# /opt/intel/intel-media/build/media_driver/linux/ult/ult_app/devult /usr/lib/x86_64-linux-gnu/dri/iHD_drv_video.so

###
# Run below from gst-base image
# Build and install the gstreamer-media-SDK plugin
###
#ARG GSTREAMER_MEDIA_SDK_VERSION=v1.3.3
#COPY gstreamer-media-sdk-v1.3.3.patch /tmp/
#RUN \
#    git clone --depth 1 https://github.com/intel/gstreamer-media-SDK.git -b ${GSTREAMER_MEDIA_SDK_VERSION} /opt/gstreamer-media-sdk && \
#    cd /opt/gstreamer-media-sdk && \
#    patch -p0 < /tmp/gstreamer-media-sdk-v1.3.3.patch && \
#    (mkdir build && cd build && cmake .. -G Ninja -DUSE_VP8_DECODER=yes -DWITH_WAYLAND=no -DMFX_HOME=/opt/intel/mediasdk -DWITH_X11=yes -DUSE_EGL_RENDERER=no -DUSE_DRI3_RENDERER=no) && \
#    ninja -C build install
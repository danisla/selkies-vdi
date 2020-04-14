# Copyright 2019 Google Inc.
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

FROM golang:1.13-alpine as build
RUN apk add -u git
ENV GO111MODULE=on
WORKDIR /go/src/gcp.solutions/gpu-sharing-autoscaler
COPY . ./
RUN go build cmd/gpu_sharing_autoscaler/gpu_sharing_autoscaler.go

FROM gcr.io/google.com/cloudsdktool/cloud-sdk:alpine

# Install kubectl with integrated kustomize 
RUN wget -qO /usr/bin/kubectl https://storage.googleapis.com/kubernetes-release/release/v1.14.5/bin/linux/amd64/kubectl && \
    chmod +x /usr/bin/kubectl

# Copy build from previous layer
COPY --from=build /go/src/gcp.solutions/gpu-sharing-autoscaler/gpu_sharing_autoscaler /usr/local/bin/gpu-sharing-autoscaler

ENTRYPOINT ["/usr/local/bin/gpu-sharing-autoscaler"]

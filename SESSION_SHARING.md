## Session Sharing

## Using

1. Add remote to your existing repo clone and checkout the branch:

```bash
git remote add danisla https://github.com/danisla/solutions-webrtc-gpu-streaming.git
git fetch danisla
git checkout shared-session
```

2. Build the images and deploy the manifests:

```bash
REGION=us-west1
```

```bash
(cd images && gcloud builds submit)
(cd manifests && gcloud builds submit --substitutions=_REGION=${REGION})
```

2. Edit your BrokerAppConfig and add the following to `spec.appParams`:

```yaml
- name: enableSessionSharing
  default: "true"
```

3. From the App Launcher, refresh and launch the app.

4. Open the right hand side panel and scroll to the bottom to find the `Session Sharing` link.

5. Open the session sharing link in a new incognito window logged in with a different user that is also IAP authorized to the app launcher.

> NOTE: Dynamic joining is not yet supported so the streaming pipeline has to be restarted when a new peer joins, this results in the stream being restarted for the initial user. 

## TODO

- Audio tee to pipeline.
- Input switching.
- Restart pipeline when resolution changes.
  - Resolution change + browser refresh does not restart pipeline like it used to.
- UX improvements:
  - peer join/pipeline reset workflow.
  - notify when user joins.
  - communicate that video bitrate affects all peers.
  - communicate that sharing URL only works for IAP authorized users.
  - show which peer has input control

## Issues

- Dynamic join not working, stream starts but no video stream received.
  - Workaround is to restart whole pipeline after all peers have joined.

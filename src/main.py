import asyncio
import json
import uuid

import scrypted_sdk
from scrypted_sdk import (
    ScryptedDeviceBase,
    ScryptedInterface,
    ScryptedDeviceType,
    DeviceProvider,
    DeviceCreator,
    DeviceCreatorSettings,
    HttpRequestHandler,
    HttpRequest,
    HttpResponse,
    Settings,
    Setting,
    VideoCamera,
    ResponseMediaStreamOptions,
    RequestMediaStreamOptions,
    MediaObject,
    ScryptedMimeTypes,
)

PLUGIN_NATIVE_ID = "@scrypted/whip"


async def https_port() -> int:
    return await scrypted_sdk.systemManager.getComponent('SCRYPTED_SECURE_PORT')

async def http_port() -> int:
    return await scrypted_sdk.systemManager.getComponent('SCRYPTED_INSECURE_PORT')

async def server_ip() -> str:
    ip = await scrypted_sdk.systemManager.getComponent('SCRYPTED_IP_ADDRESS')
    if ":" in ip:
        return f"[{ip}]"
    return ip

async def endpoint_path(id: str | None) -> str:
    if not id:
        id = PLUGIN_NATIVE_ID
    return f"/endpoint/{id}/public/"


class WHIPSession:

    def __init__(self, offer: str, resolve_answer: asyncio.Future) -> None:
        self.offer = offer
        self.resolve_answer = resolve_answer

    async def createLocalDescription(self, type, setup, sendIceCandidate=None) -> dict:
        if type != "offer":
            raise Exception("can only create offers in WHIPSession.createLocalDescription")
        return {
            "sdp": self.offer,
            "type": "offer",
        }

    async def setRemoteDescription(self, description, setup) -> None:
        if description["type"] != "answer":
            raise Exception("can only accept answers in WHIPSession.setRemoteDescription")
        self.resolve_answer.set_result(description["sdp"])


class WHIPSessionControl:

    async def getRefreshAt(self) -> int:
        pass

    async def extendSession(self) -> None:
        pass

    async def endSession(self) -> None:
        pass

    async def setPlayback(self, options) -> None:
        pass


class WHIPDevice(ScryptedDeviceBase, HttpRequestHandler, Settings, VideoCamera):

    def __init__(self, nativeId: str | None = None) -> None:
        super().__init__(nativeId)
        self.pending_webrtc = asyncio.Queue()

    async def getSettings(self) -> list[Setting]:
        ip = await server_ip()
        endpoint = await endpoint_path(self.id)
        return [
            {
                "title": "HTTP endpoint",
                "key": "http_endpoint",
                "description": "HTTP ingestion endpoint",
                "value": f"http://{ip}:{await http_port()}{endpoint}",
                "readonly": True,
            },
            {
                "title": "HTTPS endpoint",
                "key": "https_endpoint",
                "description": "HTTPS ingestion endpoint",
                "value": f"https://{ip}:{await https_port()}{endpoint}",
                "readonly": True,
            },
        ]

    async def startRTCSignalingSession(self, scrypted_session):
        offer_fut = asyncio.Future()
        self.pending_webrtc.put_nowait(offer_fut)

        try:
            offer, answer_fut = await asyncio.wait_for(offer_fut, timeout=60)
        except asyncio.TimeoutError:
            self.print("Timeout waiting for camera offer")
            raise
        except asyncio.CancelledError:
            self.print("Session cancelled")
            raise

        camera_session = WHIPSession(offer, answer_fut)

        scrypted_setup = {
            "type": "answer",
            "audio": {
                "direction": "recvonly",
            },
            "video": {
                "direction": "recvonly",
            },
            "configuration": {
                "iceServers": [
                    {"urls": ["stun:stun.l.google.com:19302"]},
                ],
                "iceCandidatePoolSize": 0,
            }
        }
        plugin_setup = {}

        try:
            camera_offer = await camera_session.createLocalDescription("offer", plugin_setup)
            self.print(f"Camera offer sdp:\n{camera_offer['sdp']}")
            await scrypted_session.setRemoteDescription(camera_offer, scrypted_setup)
            scrypted_offer = await scrypted_session.createLocalDescription("answer", scrypted_setup)
            self.print(f"Scrypted answer sdp:\n{scrypted_offer['sdp']}")
            await camera_session.setRemoteDescription(scrypted_offer, plugin_setup)
        except Exception as e:
            self.print(f"Error setting up session: {e}")
            raise

        return WHIPSessionControl()

    async def onRequest(self, request: HttpRequest, response: HttpResponse) -> None:
        if not request.get("body") or request["body"] == "{}":
            return

        try:
            # body is parsed as a Buffer, convert to string
            body = json.loads(request["body"])
            body = bytearray(body.get("data", "")).decode("utf-8")

            if not body:
                response.send("No SDP provided", { "code": 400 })
                return

            # do we have a pending connection?
            if self.pending_webrtc.empty():
                response.send("No pending connection", { "code": 503 })
                return

            offer_fut: asyncio.Future = None
            while not self.pending_webrtc.empty():
                offer_fut = self.pending_webrtc.get_nowait()
                if not offer_fut.done():
                    break

            if not offer_fut:
                response.send("No pending connection", { "code": 503 })
                return

            answer_fut = asyncio.Future()
            offer_fut.set_result((body, answer_fut))

            answer = await asyncio.wait_for(answer_fut, timeout=1)
            response.send(answer, { "code": 201 })
        except asyncio.TimeoutError:
            self.print("Timeout waiting for Scrypted answer")
            response.send("Timeout waiting for answer", { "code": 504 })
        except Exception as e:
            self.print(f"Error processing request: {e}")
            response.send(f"Error processing request", { "code": 500 })

    async def getVideoStreamOptions(self) -> list[ResponseMediaStreamOptions]:
        return [
            {
                "id": 'default',
                "name": 'WHIP',
                "container": 'rtsp',
                "video": {
                    "codec": 'h264',
                },
                "audio": {
                    "codec": 'pcm_alaw',
                },
                "source": 'local',
                "tool": 'scrypted',
                "userConfigurable": False,
            },
        ]

    async def getVideoStream(self, options: RequestMediaStreamOptions = None) -> MediaObject:
        return await scrypted_sdk.mediaManager.createMediaObject(self, ScryptedMimeTypes.RTCSignalingChannel.value)


class WHIPPlugin(ScryptedDeviceBase, DeviceProvider, DeviceCreator):

    def __init__(self, nativeId: str | None = None) -> None:
        super().__init__(nativeId)
        self.devices = {}

    async def getDevice(self, nativeId: str) -> scrypted_sdk.Any:
        if nativeId not in self.devices:
            self.devices[nativeId] = WHIPDevice(nativeId)
        return self.devices[nativeId]

    async def releaseDevice(self, id: str, nativeId: str) -> None:
        if nativeId in self.devices:
            del self.devices[nativeId]

    async def createDevice(self, settings: DeviceCreatorSettings) -> str:
        nativeId = str(uuid.uuid4().hex)
        name = settings.get("name", "New WHIP Camera")
        await scrypted_sdk.deviceManager.onDeviceDiscovered({
            'nativeId': nativeId,
            'name': name,
            'interfaces': [
                ScryptedInterface.VideoCamera.value,
                ScryptedInterface.Settings.value,
                ScryptedInterface.HttpRequestHandler.value,
            ],
            'type': ScryptedDeviceType.Camera.value,
        })
        await self.getDevice(nativeId)
        return nativeId

    async def getCreateDeviceSettings(self) -> list[Setting]:
        return [
            {
                'title': 'Name',
                'key': 'name'
            }
        ]

def create_scrypted_plugin():
    return WHIPPlugin()
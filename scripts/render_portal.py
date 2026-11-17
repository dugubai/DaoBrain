"""可选Chrome视觉验收；通过CDP强制真实390px视口，不依赖Chrome最小窗口宽度。"""
import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import uuid
import websockets

REPO = Path(__file__).resolve().parents[1]
OUTPUT = Path(os.environ.get("REVIEW_OUTPUT", REPO.parent))


async def render(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=3) as response:
        targets = json.load(response)
    target = next(item for item in targets if item["type"] == "page")
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=20_000_000) as ws:
        sequence = 0
        async def cdp(method, params=None):
            nonlocal sequence
            sequence += 1
            request_id = sequence
            await ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
            while True:
                response = json.loads(await ws.recv())
                if response.get("id") == request_id:
                    if "error" in response:
                        raise RuntimeError(response["error"])
                    return response.get("result", {})
        await cdp("Page.enable")
        for name, width, height in [("desktop", 1440, 1050), ("mobile", 390, 1400)]:
            await cdp("Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                      "deviceScaleFactor": 1, "mobile": name == "mobile"})
            await cdp("Page.navigate", {"url": (REPO / "portal" / "index.html").as_uri()})
            for _ in range(30):
                state = await cdp("Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True})
                if state.get("result", {}).get("value") == "complete":
                    break
                await asyncio.sleep(0.1)
            metrics = await cdp("Runtime.evaluate", {"expression":
                "JSON.stringify({viewport:innerWidth,doc:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight})",
                "returnByValue": True})
            values = json.loads(metrics["result"]["value"])
            assert values["viewport"] == width and values["doc"] <= width, values
            screenshot = await cdp("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True,
                "clip": {"x": 0, "y": 0, "width": width, "height": max(height, values["height"]), "scale": 1}})
            (OUTPUT / ("daobrain-portal-" + name + ".png")).write_bytes(base64.b64decode(screenshot["data"]))
            print(name, values)


def main():
    chrome = os.environ.get("CHROME_BIN", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    profile = OUTPUT / (".chrome-daobrain-cdp-" + uuid.uuid4().hex)
    profile.mkdir()
    proc = subprocess.Popen([chrome, "--headless", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--remote-debugging-port=0", "--user-data-dir=" + str(profile), "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        active = profile / "DevToolsActivePort"
        while not active.exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Chrome调试端口未就绪")
            time.sleep(0.1)
        port = int(active.read_text().splitlines()[0])
        asyncio.run(render(port))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


if __name__ == "__main__":
    main()

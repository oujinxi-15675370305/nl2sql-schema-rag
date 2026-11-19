"""生成 README 的 3 张截图 + 1 个多轮 GIF（开发工具，应用本身不依赖）。

用法（仓库根目录）：

    python scripts/shots.py                 # 起服务 → 无头 Chrome → docs/screenshots/
    python scripts/shots.py --no-gif        # 只出 PNG（不需要 Pillow）
    python scripts/shots.py --base-url http://127.0.0.1:8000   # 用已在跑的服务

依赖：Chrome 或 Edge（无头）；合成 GIF 另需 Pillow（`python -m pip install pillow`）。
驱动浏览器的部分只用标准库（Chrome DevTools Protocol + WebSocket），不引 Selenium/Playwright。

固定脚本的好处：页面改样式、加功能后重跑一次就能更新素材，不用手工对时间点。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "docs" / "screenshots"
VIEWPORT = (1440, 1000)
SCALE = 1.5  # 截图按 1.5x 渲染，README 里缩放后中文更清楚

CHROME_CANDIDATES = [
    os.getenv("CHROME_PATH", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def log(message: str) -> None:
    print(f"[shots] {message}", flush=True)


def display(path: Path) -> str:
    """仓库内的路径打相对路径，临时目录里的打绝对路径。"""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def find_chrome(explicit: str | None = None) -> str:
    for candidate in [explicit or "", *CHROME_CANDIDATES, shutil.which("google-chrome") or "",
                      shutil.which("chromium") or "", shutil.which("chrome") or "",
                      shutil.which("msedge") or ""]:
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit("找不到 Chrome/Edge：用 --chrome 指定可执行文件，或设置 CHROME_PATH")


def http_json(url: str, timeout: float = 2.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_http(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http_json(url, timeout=1.0)
            return
        except Exception:
            time.sleep(0.1)
    raise TimeoutError(f"等服务就绪超时：{url}")


class DevTools:
    """最小 WebSocket + CDP 客户端：够发命令、读结果、收事件。"""

    def __init__(self, ws_url: str, timeout: float = 60.0) -> None:
        parsed = urllib.parse.urlparse(ws_url)
        if parsed.scheme != "ws" or not parsed.hostname or not parsed.port:
            raise ValueError(f"不支持的 DevTools 地址：{ws_url}")
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""
        self._next_id = 0
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket 握手失败：连接被关闭")
            head += chunk
        header, _, self._buf = head.partition(b"\r\n\r\n")
        status = header.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise ConnectionError(f"WebSocket 握手失败：{status.decode(errors='replace')}")

    # ---- 帧读写 ----
    def _read_exact(self, size: int) -> bytes:
        while len(self._buf) < size:
            chunk = self.sock.recv(max(4096, size - len(self._buf)))
            if not chunk:
                raise ConnectionError("DevTools 连接已关闭")
            self._buf += chunk
        data, self._buf = self._buf[:size], self._buf[size:]
        return data

    def _read_frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._read_exact(2)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        data = self._read_exact(length)
        if masked:
            data = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
        return fin, opcode, data

    def _send_frame(self, opcode: int, data: bytes) -> None:
        header = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        header += mask
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def _read_message(self) -> tuple[int, bytes]:
        fin, opcode, data = self._read_frame()
        if opcode == 8:
            raise ConnectionError("DevTools 主动关闭了连接")
        if opcode == 9:  # ping → pong
            self._send_frame(10, data)
            return self._read_message()
        chunks = [data]
        while not fin:
            fin, continuation_opcode, more = self._read_frame()
            if continuation_opcode == 9:
                self._send_frame(10, more)
                continue
            chunks.append(more)
        return opcode, b"".join(chunks)

    def call(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        self._next_id += 1
        message_id = self._next_id
        payload = json.dumps({"id": message_id, "method": method, "params": params or {}}).encode()
        self._send_frame(1, payload)
        deadline = time.time() + timeout
        while time.time() < deadline:
            opcode, data = self._read_message()
            if opcode != 1:
                continue
            message = json.loads(data.decode("utf-8"))
            if message.get("id") != message_id:
                continue  # 事件（比如 Page.loadEventFired）先丢掉，这里用轮询等状态
            if "error" in message:
                raise RuntimeError(f"CDP {method} 失败：{message['error']}")
            return message.get("result", {})
        raise TimeoutError(f"CDP 调用超时：{method}")


class Page:
    def __init__(self, ws_url: str) -> None:
        self.devtools = DevTools(ws_url)

    # ---- 基础 ----
    def js(self, expression: str):
        result = self.devtools.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "userGesture": True,
        })
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            raise RuntimeError(f"页面脚本报错：{details.get('text')} {details.get('exception', {})}")
        return result.get("result", {}).get("value")

    def setup(self, width: int, height: int, scale: float) -> None:
        self.devtools.call("Page.enable")
        self.devtools.call("Runtime.enable")
        self.devtools.call("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": scale, "mobile": False,
        })

    def goto(self, url: str) -> None:
        self.devtools.call("Page.navigate", {"url": url})
        self.wait_js("document.readyState === 'complete' && !!document.querySelector('#btn-run')")

    def wait_js(self, expression: str, timeout: float = 20.0, interval: float = 0.05):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = self.js(expression)
            if value:
                return value
            time.sleep(interval)
        raise TimeoutError(f"等待页面状态超时：{expression[:80]}")

    def shot(self, path: Path) -> None:
        data = self.devtools.call("Page.captureScreenshot", {"format": "png"}, timeout=120)["data"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))
        log(f"截图 {display(path)}（{path.stat().st_size // 1024} KB）")

    # ---- 页面操作 ----
    def new_session(self) -> None:
        self.js("document.querySelector('#btn-new').click()")

    def set_role(self, role: str) -> None:
        self.js(
            "(() => { const el = document.querySelector('#role');"
            f" el.value = {json.dumps(role)}; el.dispatchEvent(new Event('change')); }})()"
        )
        time.sleep(0.15)

    def type_question(self, text: str, delay: float = 0.05, on_char=None) -> None:
        self.js("(() => { const q = document.querySelector('#q'); q.value = ''; q.focus(); })()")
        for index in range(1, len(text) + 1):
            self.js(f"document.querySelector('#q').value = {json.dumps(text[:index], ensure_ascii=False)}")
            time.sleep(delay)
            if on_char:
                on_char(index)

    def ask(self, question: str, delay: float = 0.0) -> str:
        self.js(f"document.querySelector('#q').value = {json.dumps(question, ensure_ascii=False)}")
        self.js("document.querySelector('#btn-run').click()")
        state = self.wait_state()
        if delay:
            time.sleep(delay)
        return state

    def ask_current(self, delay: float = 0.0) -> str:
        self.js("document.querySelector('#btn-run').click()")
        state = self.wait_state()
        if delay:
            time.sleep(delay)
        return state

    def demo_repair(self) -> str:
        self.js("document.querySelector('#btn-demo').click()")
        return self.wait_state()

    def wait_state(self) -> str:
        # 生成中 / 评测中 / 演示中都不算结束；完成、失败、意图、请求失败才算
        return self.wait_js(
            "(() => { const s = (document.querySelector('#state').textContent || '').trim();"
            " return /^(完成|失败|意图|请求失败)/.test(s) ? s : ''; })()"
        )


def start_server(port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({"PORT": str(port), "HOST": "127.0.0.1", "PYTHONIOENCODING": "utf-8",
                "LLM_API_KEY": ""})
    log(f"启动服务：http://127.0.0.1:{port}")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py")],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    wait_http(f"http://127.0.0.1:{port}/api/health")
    return proc


def start_chrome(chrome: str, debug_port: int, profile_dir: Path) -> subprocess.Popen:
    log(f"启动无头浏览器：{Path(chrome).name}")
    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={profile_dir}",
            f"--window-size={VIEWPORT[0]},{VIEWPORT[1]}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            targets = http_json(f"http://127.0.0.1:{debug_port}/json/list", timeout=1.0)
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return proc
        except Exception:
            pass
        time.sleep(0.1)
    raise TimeoutError("等 Chrome DevTools 就绪超时")


def page_ws_url(debug_port: int) -> str:
    targets = http_json(f"http://127.0.0.1:{debug_port}/json/list")
    for target in targets:
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
            return target["webSocketDebuggerUrl"]
    raise RuntimeError("没有可用的页面 target")


def kill_tree(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def take_stills(page: Page, out: Path) -> None:
    log("场景 1/3：analyst 看手机号 → 服务端掩码")
    page.new_session()
    page.set_role("analyst")
    page.ask("各客户的GMV和手机号")
    time.sleep(0.35)
    page.shot(out / "01-role-mask-analyst.png")

    log("场景 2/3：切 viewer 问同一句 → 越权，不生成 SQL")
    page.set_role("viewer")
    page.ask("各客户的GMV和手机号")
    time.sleep(0.35)
    page.shot(out / "02-role-mask-viewer.png")

    log("场景 3/3：演示修复（写错列名 → 带报错改对）")
    page.set_role("analyst")
    page.new_session()
    page.demo_repair()
    time.sleep(0.35)
    page.shot(out / "03-demo-repair.png")


def record_gif(page: Page, frames_dir: Path, gif_path: Path, keep_frames: bool = False) -> bool:
    frames: list[Path] = []
    durations: list[int] = []

    def capture(milliseconds: int) -> None:
        path = frames_dir / f"frame-{len(frames):02d}.png"
        page.shot(path)
        frames.append(path)
        durations.append(milliseconds)

    log("录 GIF：多轮承接 + 改成华南")
    page.new_session()
    page.set_role("analyst")

    page.type_question("各地区的GMV", delay=0.06)
    time.sleep(0.2)
    capture(900)

    page.ask_current(delay=0.3)
    capture(1500)

    page.type_question("那上个月呢", delay=0.06)
    time.sleep(0.2)
    capture(900)

    page.ask_current(delay=0.35)
    capture(1900)

    # 逐字敲"改成华南"，每个字留一帧，GIF 里能看出是在改写上一句
    def on_char(index: int) -> None:
        if index >= 2:
            capture(260)

    page.type_question("改成华南", delay=0.12, on_char=on_char)
    time.sleep(0.2)
    page.ask_current(delay=0.45)
    capture(2400)

    ok = build_gif(frames, durations, gif_path)
    if not keep_frames:
        shutil.rmtree(frames_dir, ignore_errors=True)
    return ok


def build_gif(frames: list[Path], durations: list[int], out: Path, width: int = 1200) -> bool:
    try:
        from PIL import Image
    except ImportError:
        log("没装 Pillow，跳过 GIF（PNG 已生成）：python -m pip install pillow")
        return False
    images = []
    for frame in frames:
        image = Image.open(frame).convert("RGB")
        height = round(image.height * width / image.width)
        images.append(image.resize((width, height), Image.LANCZOS))
    # 统一色板：每帧各自量化会闪烁，先从第一帧取色板再套到所有帧
    palette = images[0].quantize(colors=192, method=Image.Quantize.MEDIANCUT)
    quantized = [image.quantize(palette=palette) for image in images]
    out.parent.mkdir(parents=True, exist_ok=True)
    quantized[0].save(
        out, save_all=True, append_images=quantized[1:],
        duration=durations, loop=0, optimize=True, disposal=2,
    )
    log(f"GIF {display(out)}（{out.stat().st_size // 1024} KB，{len(frames)} 帧）")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 README 截图与 GIF（需要 Chrome/Edge）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录，默认 docs/screenshots")
    parser.add_argument("--base-url", default="", help="复用已在跑的服务；不传就自己起一个")
    parser.add_argument("--chrome", default="", help="Chrome/Edge 可执行文件路径")
    parser.add_argument("--no-gif", action="store_true", help="只出 3 张 PNG，不合成 GIF")
    parser.add_argument("--keep-frames", action="store_true", help="保留 GIF 的中间帧，便于排查")
    args = parser.parse_args(argv)

    out = Path(args.out).resolve()
    chrome = find_chrome(args.chrome or None)
    server: subprocess.Popen | None = None
    browser: subprocess.Popen | None = None
    temp_dir = Path(tempfile.mkdtemp(prefix="nl2sql-shots-"))
    frames_dir = temp_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    try:
        base_url = args.base_url.rstrip("/")
        if not base_url:
            port = free_port()
            server = start_server(port)
            base_url = f"http://127.0.0.1:{port}"

        debug_port = free_port()
        browser = start_chrome(chrome, debug_port, temp_dir / "profile")
        page = Page(page_ws_url(debug_port))
        page.setup(*VIEWPORT, SCALE)
        page.goto(f"{base_url}/")
        log(f"页面就绪：{base_url}/")

        take_stills(page, out)
        if args.no_gif:
            log("跳过 GIF（--no-gif）")
        else:
            record_gif(page, frames_dir, out / "multi-turn.gif", keep_frames=args.keep_frames)
        log("完成")
        return 0
    finally:
        kill_tree(browser)
        kill_tree(server)
        shutil.rmtree(temp_dir / "profile", ignore_errors=True)
        if args.keep_frames:
            log(f"中间帧保留在 {display(frames_dir)}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

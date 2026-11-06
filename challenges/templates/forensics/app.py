import os, struct, zlib
from flask import Flask, Response

app = Flask(__name__)
FLAG = os.environ["FLAG"]  # injected by K8s Secret at runtime


def make_png(comment: str) -> bytes:
    """Build a minimal 1x1 white PNG with FLAG hidden in a tEXt Comment chunk."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 100, 100, 8, 2, 0, 0, 0))
    # 100x100 white image
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * 100 for _ in range(100))
    idat = chunk(b"IDAT", zlib.compress(raw))
    text = chunk(b"tEXt", b"Comment\x00" + comment.encode())
    iend = chunk(b"IEND", b"")
    return signature + ihdr + idat + text + iend


PNG_DATA = make_png(FLAG)

PAGE = """<!DOCTYPE html>
<html><head><title>[TEST] Forensics 1</title></head><body>
<h2>InfoDays CTF — Forensics Challenge</h2>
<p>There is something hidden in this image. Can you find it?</p>
<p><a href="/image.png">Download image.png</a></p>
<p><em>Hint: Try <code>strings image.png</code> or <code>exiftool image.png</code></em></p>
</body></html>"""

@app.route("/")
def index():
    return PAGE

@app.route("/image.png")
def download():
    return Response(PNG_DATA, mimetype="image/png",
                    headers={"Content-Disposition": "attachment; filename=image.png"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

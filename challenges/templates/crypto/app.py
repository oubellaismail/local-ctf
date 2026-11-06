import os, base64
from flask import Flask, Response

app = Flask(__name__)
FLAG = os.environ["FLAG"]  # injected by K8s Secret at runtime
ENCODED = base64.b64encode(FLAG.encode()).decode()

PAGE = f"""<!DOCTYPE html>
<html><head><title>[TEST] Crypto 1</title></head><body>
<h2>InfoDays CTF — Crypto Challenge</h2>
<p>This message has been encoded. Decode it to find the flag.</p>
<pre style="background:#111;color:#0f0;padding:1em">{ENCODED}</pre>
<p><em>Hint: This is a very common encoding. Not encryption.</em></p>
</body></html>"""

@app.route("/")
def index():
    return PAGE

@app.route("/challenge.txt")
def download():
    content = f"InfoDays CTF — [TEST] Crypto 1\n\nEncoded: {ENCODED}\n\nHint: This is a very common encoding. Not encryption.\n"
    return Response(content, mimetype="text/plain",
                    headers={"Content-Disposition": "attachment; filename=challenge.txt"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import re
from urllib.parse import urljoin
import logging

# Enable logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

VIDEO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
    "Referer": "https://www.animesaturn.cx/watch?file=xNIuYkLOOfAwo&server=0",
}

# Persistent AsyncClient
client = httpx.AsyncClient(verify=False, timeout=None)


@app.get("/proxy")
async def proxy_stream(request: Request):
    origin_url = request.query_params.get("url")
    if not origin_url:
        return {"error": "Missing 'url' query parameter"}

    # Detect file type
    is_m3u8 = origin_url.lower().endswith(".m3u8")
    is_ts = origin_url.lower().endswith(".ts")

    # ---------------- HLS (.m3u8) proxy ----------------
    if is_m3u8:
        resp = await client.get(origin_url, headers=VIDEO_HEADERS)
        body = resp.text
        origin_base = re.sub(r"playlist\.m3u8$", "", origin_url)

        def rewrite_match(uri):
            if not uri.startswith("http"):
                abs_uri = urljoin(origin_base, uri)
                return f"/proxy?url={abs_uri}"
            return uri

        new_lines = []
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                line = rewrite_match(line)
            new_lines.append(line)

        return Response(
            "\n".join(new_lines),
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Content-Type": "application/vnd.apple.mpegurl",
                "Cache-Control": "no-cache",
            },
        )

    # ---------------- MP4 / TS proxy ----------------
    range_header = request.headers.get("range")
    headers = VIDEO_HEADERS.copy()
    if range_header:
        headers["Range"] = range_header

    logger.info(f"Proxying → {origin_url}, Range: {range_header}")

    async def iterfile():
        async with client.stream("GET", origin_url, headers=headers) as resp:
            async for chunk in resp.aiter_bytes(128 * 1024):  # 128 KB chunks
                yield chunk

    # Pre-open stream to get headers
    async with client.stream("GET", origin_url, headers=headers) as tmp_resp:
        status_code = tmp_resp.status_code
        content_type = "video/MP2T" if is_ts else tmp_resp.headers.get("content-type", "video/mp4")
        response_headers = {
            "Content-Type": content_type,
            "Content-Length": tmp_resp.headers.get("content-length"),
            "Content-Range": tmp_resp.headers.get("content-range"),
            "Accept-Ranges": tmp_resp.headers.get("accept-ranges", "bytes"),
        }

    return StreamingResponse(
        iterfile(),
        status_code=status_code,
        headers={k: v for k, v in response_headers.items() if v},
        media_type=content_type,
    )


@app.get("/")
def root():
    return {"message": "Proxy server ready (HLS + MP4/TS)"}


@app.get("/embed", response_class=HTMLResponse)
async def embed(request: Request):
    video_url = request.query_params.get("url", "")
    if not video_url:
        return HTMLResponse("<h3>Error: Missing ?url= parameter</h3>")

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Embed Player</title>
        <style>
            body {{
                margin: 0;
                background: #000;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
            }}
            video {{
                width: 100%;
                height: 100%;
                background: black;
            }}
        </style>
    </head>
    <body>
        <video id="video" controls autoplay playsinline></video>

        <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
        <script>
            const video = document.getElementById('video');
            const source = "/proxy?url={video_url}";

            // === Load video ===
            if (source.endsWith(".m3u8")) {{
                if (Hls.isSupported()) {{
                    const hls = new Hls();
                    hls.loadSource(source);
                    hls.attachMedia(video);
                    hls.on(Hls.Events.MANIFEST_PARSED, () => video.play());
                }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
                    video.src = source;
                    video.addEventListener('loadedmetadata', () => video.play());
                }} else {{
                    document.body.innerHTML = "<h3 style='color:white'>Browser cannot play HLS streams.</h3>";
                }}
            }} else {{
                video.src = source;
                video.play();
            }}

            // === Auto-next + progress tracking ===
            video.addEventListener('ended', () => {{
                window.parent.postMessage({{ type: 'saturn-video-ended' }}, '*');
            }});

            let lastSent = 0;
            video.addEventListener('timeupdate', () => {{
                const t = Math.floor(video.currentTime);
                if (t % 5 === 0 && t !== lastSent) {{
                    lastSent = t;
                    window.parent.postMessage(
                        {{
                            type: 'saturn-progress',
                            currentTime: video.currentTime,
                            duration: video.duration
                        }},
                        '*'
                    );
                }}
            }});

            // === Resume support ===
            window.addEventListener('message', (e) => {{
                if (e.data?.type === 'resume-video' && e.data?.time) {{
                    video.currentTime = e.data.time;
                }}
            }});
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
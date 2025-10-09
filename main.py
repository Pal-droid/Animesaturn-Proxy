from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
from urllib.parse import urljoin
import logging

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- FastAPI app ----------------
app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- Headers & client ----------------
VIDEO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
    "Referer": "https://www.animesaturn.cx/watch?file=xNIuYkLOOfAwo&server=0",
}

client = httpx.AsyncClient(verify=False, timeout=None)


# ---------------- Proxy endpoint ----------------
@app.get("/proxy")
async def proxy_stream(request: Request):
    origin_url = request.query_params.get("url")
    if not origin_url:
        return {"error": "Missing 'url' query parameter"}

    is_m3u8 = origin_url.lower().endswith(".m3u8")
    is_ts = origin_url.lower().endswith(".ts")

    # -------- HLS playlist (.m3u8) --------
    if is_m3u8:
        resp = await client.get(origin_url, headers=VIDEO_HEADERS)
        body = resp.text
        origin_base = origin_url.rsplit("/", 1)[0] + "/"

        def rewrite_line(line: str):
            line = line.strip()
            if not line or line.startswith("#"):
                return line
            abs_uri = urljoin(origin_base, line)
            return f"/proxy?url={abs_uri}"

        new_body = "\n".join(rewrite_line(line) for line in body.splitlines())
        return Response(
            content=new_body,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )

    # -------- TS / MP4 streaming --------
    headers = VIDEO_HEADERS.copy()
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    logger.info(f"Proxying → {origin_url}, Range: {range_header}")

    async def stream_video():
        async with client.stream("GET", origin_url, headers=headers) as resp:
            async for chunk in resp.aiter_bytes(128 * 1024):
                yield chunk

    # Get headers from origin
    async with client.stream("GET", origin_url, headers=headers) as resp:
        content_type = "video/MP2T" if is_ts else resp.headers.get("content-type", "video/mp4")
        response_headers = {
            "Content-Type": content_type,
            "Content-Length": resp.headers.get("content-length"),
            "Content-Range": resp.headers.get("content-range"),
            "Accept-Ranges": resp.headers.get("accept-ranges", "bytes"),
        }
        status_code = resp.status_code

    return StreamingResponse(
        stream_video(),
        status_code=status_code,
        headers={k: v for k, v in response_headers.items() if v},
        media_type=content_type,
    )


# ---------------- Root ----------------
@app.get("/")
def root():
    return {"message": "Proxy server ready (HLS + MP4/TS)"}


# ---------------- Embed Player ----------------
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

            // Auto-next + progress tracking
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

            // Resume support
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
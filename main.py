from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
from urllib.parse import urljoin, unquote
import logging
import re
import json
import asyncio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("proxy")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

VIDEO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
    "Referer": "https://www.animesaturn.cx/watch?file=xNIuYkLOOfAwo&server=0",
}

# single shared client
client = httpx.AsyncClient(verify=False, timeout=None)

def make_cors_headers(extra: dict = None):
    base = {"Access-Control-Allow-Origin": "*"}
    if extra:
        base.update(extra)
    return base

@app.get("/")
async def root():
    return {"message": "Proxy server ready (HLS + MP4/TS)"}

@app.get("/proxy")
async def proxy_stream(request: Request):
    origin_url = request.query_params.get("url")
    if not origin_url:
        return PlainTextResponse("Missing 'url' query parameter", status_code=400)

    # origin_url might be encoded; decode for checks and for making absolute URIs
    origin_url = unquote(origin_url)
    logger.info("Requested /proxy -> %s", origin_url)

    # --- Bypass ONLY if the URL ends with /uwu.m3u8 (serve it raw, no rewriting) ---
    if origin_url.endswith("/uwu.m3u8"):
        logger.info("Bypass: serving uwu.m3u8 raw via server for %s", origin_url)
        try:
            resp = await client.get(origin_url, headers=VIDEO_HEADERS)
        except Exception as e:
            logger.exception("Error fetching uwu.m3u8: %s", e)
            raise HTTPException(status_code=502, detail="Upstream fetch failed")

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/vnd.apple.mpegurl"),
            headers={**make_cors_headers({"Cache-Control": "no-cache"})}
        )

    is_m3u8 = origin_url.lower().endswith(".m3u8")
    is_ts = origin_url.lower().endswith(".ts") or origin_url.lower().endswith(".m4s")

    # -------- Playlist handling (.m3u8) --------
    if is_m3u8:
        logger.info("Fetching and rewriting playlist: %s", origin_url)
        try:
            resp = await client.get(origin_url, headers=VIDEO_HEADERS)
        except Exception as e:
            logger.exception("Error fetching playlist: %s", e)
            raise HTTPException(status_code=502, detail="Upstream playlist fetch failed")

        if resp.status_code >= 400:
            logger.warning("Upstream playlist returned %s", resp.status_code)
            return Response(resp.content, status_code=resp.status_code, media_type=resp.headers.get("content-type", "text/plain"),
                            headers=make_cors_headers())

        body = resp.text
        origin_base = origin_url.rsplit("/", 1)[0] + "/"
        new_lines = []
        skip_next = False  # for #EXT-X-STREAM-INF lines

        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue

            if line.startswith("#EXT-X-STREAM-INF"):
                new_lines.append(line)
                skip_next = True
                continue

            if skip_next:
                # next line is variant URI
                abs_uri = urljoin(origin_base, line)
                line = f"/proxy?url={abs_uri}"
                skip_next = False
            elif not line.startswith("#") and not line.lower().startswith("http"):
                # media segments or relative URIs
                abs_uri = urljoin(origin_base, line)
                line = f"/proxy?url={abs_uri}"
            elif 'URI="' in line:
                # audio/subs external URIs
                match = re.search(r'URI="([^"]+)"', line)
                if match:
                    uri = match.group(1)
                    abs_uri = urljoin(origin_base, uri)
                    line = line.replace(uri, f"/proxy?url={abs_uri}")

            new_lines.append(line)

        content = "\n".join(new_lines)
        return Response(content, media_type="application/vnd.apple.mpegurl",
                        headers=make_cors_headers({"Cache-Control": "no-cache"}))

    # -------- TS / fMP4 streaming --------
    # Forward Range header if provided
    headers = VIDEO_HEADERS.copy()
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    logger.info("Streaming request -> %s (Range: %s)", origin_url, range_header)

    # We'll prefetch minimal HEAD to obtain status & relevant headers, then stream in generator
    try:
        head_resp = await client.head(origin_url, headers=VIDEO_HEADERS)
    except Exception:
        head_resp = None

    # If HEAD failed or not allowed, do a small GET to probe
    if not head_resp or head_resp.status_code >= 400:
        try:
            probe = await client.get(origin_url, headers=VIDEO_HEADERS, timeout=10.0)
            probe_status = probe.status_code
            probe_headers = probe.headers
        except Exception as e:
            logger.exception("Error probing upstream for streaming: %s", e)
            raise HTTPException(status_code=502, detail="Upstream stream probe failed")
    else:
        probe_status = head_resp.status_code
        probe_headers = head_resp.headers

    content_type = "video/MP2T" if origin_url.lower().endswith(".ts") else probe_headers.get("content-type", "video/mp4")
    response_headers = {
        "Content-Type": content_type,
        "Content-Length": probe_headers.get("content-length"),
        "Content-Range": probe_headers.get("content-range"),
        "Accept-Ranges": probe_headers.get("accept-ranges", "bytes"),
    }
    # ensure CORS on streaming responses
    response_headers.update(make_cors_headers())

    async def stream_video():
        # Single streaming connection which the generator keeps open while yielding
        try:
            async with client.stream("GET", origin_url, headers=headers) as resp:
                logger.info("Upstream stream opened, status=%s", resp.status_code)
                # yield raw chunks to client
                async for chunk in resp.aiter_bytes(128 * 1024):
                    if chunk:
                        yield chunk
        except Exception as e:
            logger.exception("Streaming error: %s", e)
            # try to surface an error to client by terminating generator
            return

    return StreamingResponse(
        stream_video(),
        status_code=probe_status or 200,
        headers={k: v for k, v in response_headers.items() if v},
        media_type=content_type,
    )

# ---------------- Embed Player ----------------
@app.get("/embed", response_class=HTMLResponse)
async def embed(request: Request):
    video_url = request.query_params.get("url", "")
    if not video_url:
        return HTMLResponse("<h3>Error: Missing ?url= parameter</h3>", status_code=400)

    # safe JSON encode the URL for insertion into JS
    video_url_js = json.dumps(video_url)

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Embed Player</title>
        <style>
            body {{ margin: 0; background: #000; display:flex; justify-content:center; align-items:center; height:100vh; overflow:hidden; }}
            video {{ width:100%; height:100%; background:black; }}
            select {{ position:absolute; top:10px; right:10px; background:rgba(0,0,0,0.6); color:white; border:none; padding:6px 10px; border-radius:6px; font-size:14px; z-index:9999; }}
        </style>
    </head>
    <body>
        <video id="video" controls autoplay playsinline></video>

        <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
        <script>
            const video = document.getElementById('video');
            const urlParam = {video_url_js};
            const isHls = urlParam.toLowerCase().endsWith('.m3u8');
            const source = "/proxy?url=" + encodeURIComponent(urlParam);

            console.info("embed: urlParam=", urlParam, "isHls=", isHls, "source=", source);

            if (isHls) {{
                if (Hls.isSupported()) {{
                    const hls = new Hls({{ autoStartLoad: true }});
                    hls.on(Hls.Events.ERROR, function(event, data) {{
                        console.error('HLS error', event, data);
                    }});
                    hls.loadSource(source);
                    hls.attachMedia(video);

                    hls.on(Hls.Events.MANIFEST_PARSED, (_, data) => {{
                        const levels = data.levels;
                        if (levels && levels.length > 1) {{
                            const selector = document.createElement('select');
                            const autoOpt = document.createElement('option');
                            autoOpt.value = -1;
                            autoOpt.textContent = 'Auto';
                            selector.appendChild(autoOpt);

                            levels.forEach((lvl, i) => {{
                                const opt = document.createElement('option');
                                opt.value = i;
                                opt.textContent = (lvl.height || lvl.bitrate) + 'p';
                                selector.appendChild(opt);
                            }});

                            selector.addEventListener('change', (e) => {{
                                const level = parseInt(e.target.value);
                                hls.currentLevel = level;
                            }});
                            document.body.appendChild(selector);
                        }}
                        video.play().catch(e => console.warn('play prevented', e));
                    }});
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
"""Code-driven video: CSS keyframes stepped by `currentTime`, one Chrome screenshot per frame, one FFmpeg encode.

Frame k is shot after the animations are paused and set to k/fps seconds and two animation frames have passed. The encode
is H.264, yuv420p, bit-exact, with only local files allowed as input. No audio, narration, loudness or aesthetic review."""
import importlib.util
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _sib(name):
    key = "aiarch_media_" + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


common, visuals = _sib("common"), _sib("visuals")

# Recorded verbatim in inspection.json; paths are relative to the artifact dir and no argument is a URL.
FFMPEG_ARGV = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file", "-framerate", "{fps}",
               "-i", "frames/frame%04d.png", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "1", "-map_metadata", "-1",
               "-fflags", "+bitexact", "-flags:v", "+bitexact", "-y", "video.mp4"]


def build(spec, kdir, font):
    """Write index.html, frames/frameNNNN.png and video.mp4 into kdir. Returns {'product', 'files', 'ffmpeg_argv'}."""
    width, height = spec["canvas"]
    fps, count = spec["fps"], spec["fps"] * spec["seconds"]
    visuals.write(kdir, "index.html", visuals.video_page(spec, font))
    with common.Session(kdir, (width, height)) as session:
        for k in range(count):
            session.seek(k * 1000 / fps)
            visuals.write(kdir, f"frames/frame{k:04d}.png", session.shot())
        product = session.product
    argv = [a.replace("{fps}", str(fps)) for a in FFMPEG_ARGV]
    exe = shutil.which("ffmpeg")
    if not exe:
        raise common.Exit(3, "renderer unavailable: ffmpeg")
    code, _, _ = common.run_tool([exe, *argv[1:]], "ffmpeg", 120, cwd=kdir)
    if code != 0:
        raise common.Exit(1, "encode failed")
    picks = sorted({0, count // 2, count - 1})
    return {"product": product, "ffmpeg_argv": argv,
            "files": ["index.html", "video.mp4"] + [f"frames/frame{k:04d}.png" for k in picks]}

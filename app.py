import os
import uuid
import subprocess
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
TEMP_DIR = "/tmp/ffmpeg_workspace"
os.makedirs(TEMP_DIR, exist_ok=True)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://bqueqwxbcreenijbbwgt.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
BUCKET_NAME = "merged-videos"


def download_file(url: str, dest_path: str) -> bool:
    try:
        resp = requests.get(url, timeout=60, stream=True)
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        print(f"Download failed: {url} -> {e}")
        return False


def upload_to_supabase(file_path: str, file_name: str, content_type: str = "video/mp4") -> str:
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET_NAME}/{file_name}"
    with open(file_path, "rb") as f:
        resp = requests.post(
            upload_url,
            headers={
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": content_type,
            },
            data=f,
            timeout=120,
        )
    if resp.status_code not in (200, 201):
        raise Exception(f"Supabase upload failed: {resp.status_code} {resp.text}")
    public_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{file_name}"
    return public_url


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/merge", methods=["POST"])
def merge_videos():
    """原有合并接口，保持不变"""
    data = request.get_json(force=True)
    video_urls = data.get("videos", [])
    if not video_urls or len(video_urls) < 2:
        return jsonify({"error": "At least 2 video URLs required"}), 400

    session_id = uuid.uuid4().hex
    session_dir = os.path.join(TEMP_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)

    local_paths = []
    for i, url in enumerate(video_urls):
        dest = os.path.join(session_dir, f"clip_{i:02d}.mp4")
        if not download_file(url, dest):
            return jsonify({"error": f"Failed to download video {i+1}: {url}"}), 500
        local_paths.append(dest)

    concat_file = os.path.join(session_dir, "concat.txt")
    with open(concat_file, "w") as f:
        for path in local_paths:
            f.write(f"file '{path}'\n")

    output_path = os.path.join(session_dir, "merged.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_file,
        "-c", "copy",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("FFmpeg stderr:", result.stderr)
        return jsonify({"error": "FFmpeg merge failed", "detail": result.stderr}), 500

    file_name = f"{session_id}.mp4"
    try:
        public_url = upload_to_supabase(output_path, file_name)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"merged_url": public_url, "clips": video_urls})


@app.route("/extract-audio", methods=["POST"])
def extract_audio():
    """
    从视频中提取音轨，上传到 Supabase 返回音频 URL
    请求体: { "video_url": "https://..." }
    返回: { "audio_url": "https://..." }
    """
    data = request.get_json(force=True)
    video_url = data.get("video_url")
    if not video_url:
        return jsonify({"error": "video_url is required"}), 400

    session_id = uuid.uuid4().hex
    session_dir = os.path.join(TEMP_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)

    # 下载视频
    video_path = os.path.join(session_dir, "input.mp4")
    if not download_file(video_url, video_path):
        return jsonify({"error": "Failed to download video"}), 500

    # 提取音轨为 mp3
    audio_path = os.path.join(session_dir, "audio.mp3")
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn",                  # 不要视频轨
        "-acodec", "libmp3lame",
        "-ab", "192k",
        audio_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("FFmpeg stderr:", result.stderr)
        return jsonify({"error": "Audio extraction failed", "detail": result.stderr}), 500

    # 检查是否有音轨（视频可能没有音频）
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
        return jsonify({"error": "No audio track found in video"}), 400

    # 上传到 Supabase
    file_name = f"audio_{session_id}.mp3"
    try:
        public_url = upload_to_supabase(audio_path, file_name, content_type="audio/mpeg")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"audio_url": public_url})


@app.route("/merge-with-audio", methods=["POST"])
def merge_with_audio():
    """
    合并视频并替换音轨，支持音频 crossfade
    请求体:
    {
        "videos": ["url1", "url2", "url3"],   # 无音频的视频
        "audio_urls": ["url1", "url2", "url3"], # 对应的已处理音频（ElevenLabs 处理后）
        "crossfade_duration": 0.5              # crossfade 时长（秒），默认 0.5
    }
    返回: { "merged_url": "https://..." }
    """
    data = request.get_json(force=True)
    video_urls = data.get("videos", [])
    audio_urls = data.get("audio_urls", [])
    crossfade = float(data.get("crossfade_duration", 0.5))

    if not video_urls or len(video_urls) < 2:
        return jsonify({"error": "At least 2 video URLs required"}), 400
    if len(audio_urls) != len(video_urls):
        return jsonify({"error": "audio_urls count must match videos count"}), 400

    session_id = uuid.uuid4().hex
    session_dir = os.path.join(TEMP_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)

    # 下载所有视频和音频
    video_paths = []
    audio_paths = []

    for i, url in enumerate(video_urls):
        dest = os.path.join(session_dir, f"clip_{i:02d}.mp4")
        if not download_file(url, dest):
            return jsonify({"error": f"Failed to download video {i+1}"}), 500
        video_paths.append(dest)

    for i, url in enumerate(audio_urls):
        dest = os.path.join(session_dir, f"audio_{i:02d}.mp3")
        if not download_file(url, dest):
            return jsonify({"error": f"Failed to download audio {i+1}"}), 500
        audio_paths.append(dest)

    # 把每段视频的音轨替换为处理后的音频
    replaced_paths = []
    for i, (vpath, apath) in enumerate(zip(video_paths, audio_paths)):
        out = os.path.join(session_dir, f"replaced_{i:02d}.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-i", vpath,
            "-i", apath,
            "-c:v", "copy",
            "-c:a", "aac",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            out
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print("FFmpeg stderr:", result.stderr)
            return jsonify({"error": f"Audio replace failed for clip {i+1}", "detail": result.stderr}), 500
        replaced_paths.append(out)

    # 合并替换好的视频，对音频加 crossfade
    n = len(replaced_paths)

    # 先用 concat demuxer 合并视频轨（copy 无损）
    concat_file = os.path.join(session_dir, "concat.txt")
    with open(concat_file, "w") as f:
        for path in replaced_paths:
            f.write(f"file '{path}'\n")

    # 先合并视频（带音频）
    merged_raw = os.path.join(session_dir, "merged_raw.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_file,
        "-c", "copy",
        merged_raw
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return jsonify({"error": "FFmpeg concat failed", "detail": result.stderr}), 500

    # 对合并后的音频做整体 crossfade 处理
    # 用 afade 在每个拼接点做淡入淡出
    # 先获取每段视频时长
    durations = []
    for path in replaced_paths:
        probe_cmd = [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            path
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
        try:
            durations.append(float(probe_result.stdout.strip()))
        except:
            durations.append(4.0)  # 默认4秒

    # 构建音频 afade 滤镜
    # 在每个拼接点前后各做 crossfade_duration 秒的淡出/淡入
    cumulative = 0.0
    audio_filters = []
    for i, dur in enumerate(durations):
        if i > 0:
            # 淡入：从拼接点开始
            fade_in_start = cumulative
            audio_filters.append(f"afade=t=in:st={fade_in_start:.3f}:d={crossfade:.3f}")
        if i < n - 1:
            # 淡出：在下一个拼接点前
            fade_out_start = cumulative + dur - crossfade
            audio_filters.append(f"afade=t=out:st={fade_out_start:.3f}:d={crossfade:.3f}")
        cumulative += dur

    output_path = os.path.join(session_dir, "final.mp4")
    if audio_filters:
        filter_str = ",".join(audio_filters)
        cmd = [
            "ffmpeg", "-y",
            "-i", merged_raw,
            "-c:v", "copy",
            "-af", filter_str,
            "-c:a", "aac",
            output_path
        ]
    else:
        output_path = merged_raw

    if audio_filters:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print("FFmpeg stderr:", result.stderr)
            return jsonify({"error": "FFmpeg audio crossfade failed", "detail": result.stderr}), 500

    # 上传到 Supabase
    file_name = f"{session_id}_final.mp4"
    try:
        public_url = upload_to_supabase(output_path, file_name)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"merged_url": public_url, "clips": video_urls})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

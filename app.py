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


def download_video(url: str, dest_path: str) -> bool:
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


def upload_to_supabase(file_path: str, file_name: str) -> str:
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET_NAME}/{file_name}"
    with open(file_path, "rb") as f:
        resp = requests.post(
            upload_url,
            headers={
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "video/mp4",
            },
            data=f,
            timeout=120,
        )
    if resp.status_code not in (200, 201):
        raise Exception(f"Supabase upload failed: {resp.status_code} {resp.text}")
    public_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{file_name}"
    return public_url


@app.route("/merge", methods=["POST"])
def merge_videos():
    data = request.get_json(force=True)
    video_urls = data.get("videos", [])

    if not video_urls or len(video_urls) < 2:
        return jsonify({"error": "At least 2 video URLs required"}), 400

    session_id = uuid.uuid4().hex
    session_dir = os.path.join(TEMP_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)

    # Download all videos
    local_paths = []
    for i, url in enumerate(video_urls):
        dest = os.path.join(session_dir, f"clip_{i:02d}.mp4")
        if not download_video(url, dest):
            return jsonify({"error": f"Failed to download video {i+1}: {url}"}), 500
        local_paths.append(dest)

    # Write concat list
    concat_file = os.path.join(session_dir, "concat.txt")
    with open(concat_file, "w") as f:
        for path in local_paths:
            f.write(f"file '{path}'\n")

    # Run FFmpeg concat
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

    # Upload to Supabase Storage
    file_name = f"{session_id}.mp4"
    try:
        public_url = upload_to_supabase(output_path, file_name)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "merged_url": public_url,
        "clips": video_urls
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

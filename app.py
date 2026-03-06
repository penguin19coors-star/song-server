from flask import Flask, request, jsonify, send_file
import subprocess
import os
import uuid
import threading
import time

app = Flask(__name__)
AUDIO_DIR = "/tmp/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)


def cleanup_old_files():
    """Delete audio files older than 10 minutes"""
    while True:
        now = time.time()
        for f in os.listdir(AUDIO_DIR):
            path = os.path.join(AUDIO_DIR, f)
            try:
                if now - os.path.getmtime(path) > 600:
                    os.remove(path)
            except Exception:
                pass
        time.sleep(60)


threading.Thread(target=cleanup_old_files, daemon=True).start()


@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "Song server is running!"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/stream", methods=["GET"])
def stream_audio():
    """Downloads YouTube audio, converts to MP3, and serves it (Twilio-compatible)"""
    query = request.args.get("q", "")
    if not query:
        return jsonify({"error": "No query provided. Use ?q=song+name"}), 400

    file_id = str(uuid.uuid4())[:8]
    output_template = os.path.join(AUDIO_DIR, f"{file_id}.%(ext)s")

    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "-f", "bestaudio[filesize<10M]/bestaudio",
                "--no-playlist",
                "-x",
                "--audio-format", "mp3",
                "--audio-quality", "10",
                "-o", output_template,
                f"ytsearch1:{query}",
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )

        actual_file = None
        for f in os.listdir(AUDIO_DIR):
            if f.startswith(file_id) and f.endswith(".mp3"):
                actual_file = os.path.join(AUDIO_DIR, f)
                break

        if not actual_file or not os.path.exists(actual_file):
            return jsonify({
                "error": "Could not find or convert audio",
                "stderr": result.stderr[-500:] if result.stderr else "no error output",
            }), 500

        return send_file(actual_file, mimetype="audio/mpeg")

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Download timed out"}), 504


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

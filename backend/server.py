from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO
import redis

app = Flask(__name__)
CORS(app)

# NEW: Initialize SocketIO
socketio = SocketIO(app, cors_allowed_origins="*")

# Connect to Redis
try:
    redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)
    redis_client.ping()
    print("✅ Successfully connected to Redis!")
except redis.ConnectionError:
    print("❌ Failed to connect to Redis.")

@app.route('/', methods=['GET'])
def home():
    return jsonify({"message": "LivePulse API is running!"})

@app.route('/api/vote', methods=['POST'])
def handle_vote():
    data = request.json
    candidate_name = data.get('candidate')

    if not candidate_name:
        return jsonify({"error": "No candidate provided"}), 400

    # 1. Increase the score in Redis (Fast Write)
    redis_client.hincrby("poll_results", candidate_name, 1)

    # 2. Fetch the entire scoreboard
    all_scores = redis_client.hgetall("poll_results")

    # 3. NEW: The Broadcast
    socketio.emit('score_update', all_scores)

    return jsonify({"status": "success"})

if __name__ == '__main__':
    # NEW: Run using socketio.run instead of app.run to enable WebSockets
    socketio.run(app, debug=True, port=5000)
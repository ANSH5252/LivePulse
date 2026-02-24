from flask import Flask, jsonify, request
from flask_cors import CORS
import redis

# 1. Initialize the Flask App
app = Flask(__name__)
CORS(app) # Allows frontend to talk to the backend safely

# 2. Connect to Redis
try:
    redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)
    redis_client.ping() # Tests the connection
    print("Successfully connected to Redis!")
except redis.ConnectionError:
    print("Failed to connect to Redis. Is the Redis server running?")

# 3. Creating a Simple Test Route
@app.route('/', methods=['GET'])
def home():
    return jsonify({"message": "LivePulse API is running!"})

# 4. Creating the Voting Route
@app.route('/api/vote', methods=['POST'])
def handle_vote():
    data = request.json
    candidate_name = data.get('candidate')

    if not candidate_name:
        return jsonify({"error": "No candidate provided"}), 400

    # Increase the candidate's score by 1 in Redis
    redis_client.hincrby("poll_results", candidate_name, 1)
    new_total = redis_client.hget("poll_results", candidate_name)

    return jsonify({
        "status": "success",
        "candidate": candidate_name,
        "new_total": new_total
    })

if __name__ == '__main__':
    app.run(debug=True, port=5000)
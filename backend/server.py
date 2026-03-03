import os
import redis
import mysql.connector
import secrets
import string
import json
from flask import Flask, request, jsonify, render_template, redirect, url_for
from flask_socketio import SocketIO, join_room, leave_room
from dotenv import load_dotenv, find_dotenv
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user

# --- SETUP & INITIALIZATION ---
load_dotenv(find_dotenv())

# BULLETPROOF PATHS: Automatically find the frontend folder no matter where you run this script from
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, '../frontend')

app = Flask(__name__, template_folder=FRONTEND_DIR, static_folder=FRONTEND_DIR)
app.secret_key = os.getenv("SECRET_KEY", "super_secret_livepulse_key")

socketio = SocketIO(app, cors_allowed_origins="*")
redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST"),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DB")
    )

class User(UserMixin):
    def __init__(self, id, username, role):
        self.id = id
        self.username = username
        self.role = role

@login_manager.user_loader
def load_user(user_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    user_data = cursor.fetchone()
    cursor.close()
    conn.close()
    if user_data: return User(id=user_data['id'], username=user_data['username'], role=user_data['role'])
    return None

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated: return redirect(url_for('index'))
    if request.method == 'POST':
        username, email, password = request.form.get('username'), request.form.get('email'), request.form.get('password')
        conn = get_db_connection(); cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO users (username, email, password_hash, role) VALUES (%s, %s, %s, 'user')", (username, email, password))
            conn.commit()
            login_user(User(id=cursor.lastrowid, username=username, role='user'))
            return redirect(url_for('index'))
        except mysql.connector.IntegrityError:
            return "❌ That username or email is already taken!", 400
        finally:
            cursor.close(); conn.close()
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username, password = request.form.get('username'), request.form.get('password')
        conn = get_db_connection(); cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
        user_data = cursor.fetchone()
        cursor.close(); conn.close()

        if user_data and user_data['password_hash'] == password:
            user = User(id=user_data['id'], username=user_data['username'], role=user_data['role'])
            login_user(user)
            return redirect(url_for('admin_dashboard')) if user.role == 'admin' else redirect(url_for('index'))
        return "❌ Invalid username or password. Try again!", 401
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@socketio.on('join')
def on_join(data):
    if data.get('user_id'): join_room(f"user_{data.get('user_id')}")

@app.route('/')
@login_required
def index():
    if current_user.role == 'admin': return redirect(url_for('admin_dashboard'))

    conn = get_db_connection(); cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM polls WHERE is_active = TRUE ORDER BY id DESC LIMIT 1")
    poll = cursor.fetchone()
    
    options, initial_scores = [], {}
    is_present, has_verified, has_voted, my_token = False, False, False, None

    if poll:
        cursor.execute("SELECT * FROM poll_options WHERE poll_id = %s", (poll['id'],))
        options = cursor.fetchall()
        
        raw_scores = redis_client.hgetall(f"poll_{poll['id']}_results")
        for opt in options: initial_scores[opt['option_name']] = int(raw_scores.get(opt['option_name'], 0))
            
        cursor.execute("SELECT is_present FROM event_attendees WHERE user_id=%s AND poll_id=%s", (current_user.id, poll['id']))
        att = cursor.fetchone()
        if att and att['is_present']: is_present = True
            
        cursor.execute("SELECT is_used, token_code FROM poll_tokens WHERE used_by_user_id=%s AND poll_id=%s", (current_user.id, poll['id']))
        token_record = cursor.fetchone()
        if token_record:
            my_token = token_record['token_code'] # Now it persists even if used!
            if token_record['is_used']: 
                has_verified = True
            
        # Check Redis first for instant feedback, fallback to DB
        if redis_client.sismember(f"poll_{poll['id']}_voted_users", current_user.id):
            has_voted = True
        else:
            cursor.execute("SELECT id FROM user_votes WHERE user_id=%s AND poll_id=%s", (current_user.id, poll['id']))
            if cursor.fetchone(): has_voted = True

    cursor.close(); conn.close()
    return render_template('index.html', user=current_user, poll=poll, options=options, initial_scores=initial_scores, is_present=is_present, has_verified=has_verified, has_voted=has_voted, my_token=my_token)

@app.route('/api/verify_token', methods=['POST'])
@login_required
def verify_token():
    poll_id, token = request.json.get('poll_id'), request.json.get('token')
    conn = get_db_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, is_used FROM poll_tokens WHERE poll_id = %s AND token_code = %s AND used_by_user_id = %s", (poll_id, token, current_user.id))
        token_record = cursor.fetchone()
        if not token_record: return jsonify({"error": "Invalid PIN code."}), 400
        if token_record['is_used']: return jsonify({"error": "This PIN has already been used!"}), 400

        cursor.execute("UPDATE poll_tokens SET is_used = TRUE WHERE id = %s", (token_record['id'],))
        conn.commit()
        return jsonify({"message": "PIN Verified Successfully! Voting Unlocked."}), 200
    finally:
        cursor.close(); conn.close()

# --- NO DB WRITES OCCUR IN THIS ROUTE ANYMORE ---
@app.route('/api/vote', methods=['POST'])
@login_required
def vote():
    poll_id, option_id, option_name = request.json.get('poll_id'), request.json.get('option_id'), request.json.get('option_name')

    # 1. Instant Duplicate Check via Redis Memory
    voted_key = f"poll_{poll_id}_voted_users"
    if redis_client.sismember(voted_key, current_user.id):
        return jsonify({"error": "You have already voted on this poll!"}), 403

    # 2. Verify PIN (Read-Only DB Call)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM poll_tokens WHERE poll_id = %s AND used_by_user_id = %s AND is_used = TRUE", (poll_id, current_user.id))
        if not cursor.fetchone(): return jsonify({"error": "Security breach: You must verify your PIN before voting!"}), 403
    finally:
        cursor.close()
        conn.close()

    # 3. Lock vote in Redis and update live chart
    redis_client.sadd(voted_key, current_user.id)
    redis_client.hincrby(f"poll_{poll_id}_results", option_name, 1)

    # 4. Push payload to Redis Message Queue for the Worker to handle
    vote_payload = {
        "user_id": current_user.id,
        "poll_id": poll_id,
        "option_id": option_id,
        "option_name": option_name
    }
    redis_client.lpush("vote_queue", json.dumps(vote_payload))

    socketio.emit('update_chart', redis_client.hgetall(f"poll_{poll_id}_results"))
    return jsonify({"message": "Vote cast successfully!"}), 200

@app.route('/api/admin/scan_ticket', methods=['POST'])
@login_required
def scan_ticket():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    qr_text, poll_id = request.json.get('qr_text'), request.json.get('poll_id')
    try: scanned_user_id = int(qr_text.split('_')[-1])
    except: return jsonify({"error": "Corrupted User ID."}), 400

    conn = get_db_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT title FROM polls WHERE id = %s AND is_active = TRUE", (poll_id,))
        active_poll = cursor.fetchone()
        if not active_poll: return jsonify({"error": "This poll is no longer active!"}), 400

        cursor.execute("SELECT is_present FROM event_attendees WHERE user_id = %s AND poll_id = %s", (scanned_user_id, poll_id))
        if cursor.fetchone(): return jsonify({"error": "Already Scanned! User is already checked in."}), 400

        cursor.execute("INSERT INTO event_attendees (user_id, poll_id, is_present) VALUES (%s, %s, TRUE)", (scanned_user_id, poll_id))
        conn.commit()
        socketio.emit('scan_success', {'poll_id': poll_id, 'poll_name': active_poll['title']}, to=f"user_{scanned_user_id}")
        return jsonify({"message": "Check-in successful!"}), 200
    finally:
        cursor.close(); conn.close()

@app.route('/api/admin/dispatch_tokens', methods=['POST'])
@login_required
def dispatch_tokens():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    poll_id = request.json.get('poll_id')
    conn = get_db_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT COUNT(*) as count FROM poll_tokens WHERE poll_id = %s", (poll_id,))
        if cursor.fetchone()['count'] > 0: return jsonify({"error": "Tokens have already been generated!"}), 400

        cursor.execute("SELECT title FROM polls WHERE id = %s", (poll_id,))
        poll_title = cursor.fetchone()['title']

        cursor.execute("SELECT u.id FROM event_attendees ea JOIN users u ON ea.user_id = u.id WHERE ea.poll_id = %s AND ea.is_present = TRUE", (poll_id,))
        attendees = cursor.fetchall()
        
        for attendee in attendees:
            token = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(7))
            cursor.execute("INSERT INTO poll_tokens (poll_id, token_code, used_by_user_id) VALUES (%s, %s, %s)", (poll_id, token, attendee['id']))
            socketio.emit('new_notification', {'poll_name': poll_title, 'token': token}, to=f"user_{attendee['id']}")
        
        conn.commit()
        return jsonify({"message": f"✅ Successfully generated {len(attendees)} PINs!"}), 200
    finally:
        cursor.close(); conn.close()

@app.route('/api/admin/simulate', methods=['POST'])
@login_required
def simulate_votes():
    data = request.json
    redis_client.hincrby(f"poll_{data.get('poll_id')}_results", data.get('option_name'), int(data.get('count', 1)))
    socketio.emit('update_chart', redis_client.hgetall(f"poll_{data.get('poll_id')}_results"))
    return jsonify({"message": "Simulated!"}), 200

@app.route('/api/admin/end_poll', methods=['POST'])
@login_required
def end_poll():
    poll_id = request.json.get('poll_id')
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("UPDATE polls SET is_active = FALSE WHERE id = %s", (poll_id,))
    conn.commit(); cursor.close(); conn.close()
    return jsonify({"message": "Ended!"}), 200

@app.route('/admin', methods=['GET', 'POST'])
@login_required
def admin_dashboard():
    if current_user.role != 'admin': return "❌ Unauthorized", 403
    conn = get_db_connection(); cursor = conn.cursor(dictionary=True)

    if request.method == 'POST':
        title = request.form.get('title')
        valid_options = [opt.strip() for opt in request.form.getlist('options[]') if opt.strip()]
        cursor.execute("UPDATE polls SET is_active = FALSE")
        cursor.execute("INSERT INTO polls (title, is_active) VALUES (%s, TRUE)", (title,))
        new_poll_id = cursor.lastrowid
        for option_name in valid_options: cursor.execute("INSERT INTO poll_options (poll_id, option_name) VALUES (%s, %s)", (new_poll_id, option_name))
        conn.commit(); return redirect(url_for('admin_dashboard'))

    def fetch_poll_details(query):
        cursor.execute(query); polls = cursor.fetchall()
        for poll in polls:
            cursor.execute("SELECT * FROM poll_options WHERE poll_id = %s", (poll['id'],))
            options = cursor.fetchall(); poll['options'] = options
            raw_scores = redis_client.hgetall(f"poll_{poll['id']}_results")
            poll['scores'] = {opt['option_name']: int(raw_scores.get(opt['option_name'], 0)) for opt in options}
        return polls

    active_polls = fetch_poll_details("SELECT * FROM polls WHERE is_active = TRUE ORDER BY created_at DESC")
    ended_polls = fetch_poll_details("SELECT * FROM polls WHERE is_active = FALSE ORDER BY created_at DESC")
    cursor.close(); conn.close()
    return render_template('admin.html', user=current_user, active_polls=active_polls, ended_polls=ended_polls)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
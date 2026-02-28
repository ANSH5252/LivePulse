import os
import redis
import mysql.connector
import secrets
import string
from flask import Flask, request, jsonify, render_template, redirect, url_for
from flask_socketio import SocketIO, join_room, leave_room
from dotenv import load_dotenv, find_dotenv
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user

# --- SETUP & INITIALIZATION ---
load_dotenv(find_dotenv())

app = Flask(__name__, template_folder="../frontend", static_folder="../frontend")
app.secret_key = "super_secret_livepulse_key_change_this_later"

# use_reloader=False prevents Socket.IO from starting twice during development
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

# --- FLASK-LOGIN SETUP ---
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
    
    if user_data:
        return User(id=user_data['id'], username=user_data['username'], role=user_data['role'])
    return None

# --- AUTHENTICATION ROUTES ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')

        conn = get_db_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                "INSERT INTO users (username, email, password_hash, role) VALUES (%s, %s, %s, 'user')",
                (username, email, password)
            )
            conn.commit()
            
            new_user_id = cursor.lastrowid
            user = User(id=new_user_id, username=username, role='user')
            login_user(user)
            return redirect(url_for('index'))
            
        except mysql.connector.IntegrityError:
            return "❌ That username or email is already taken!", 400
        except Exception as e:
            return f"❌ Error: {str(e)}", 500
        finally:
            cursor.close()
            conn.close()

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
        user_data = cursor.fetchone()
        cursor.close()
        conn.close()

        if user_data and user_data['password_hash'] == password:
            user = User(id=user_data['id'], username=user_data['username'], role=user_data['role'])
            login_user(user)
            
            if user.role == 'admin':
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('index'))
        else:
            return "❌ Invalid username or password. Try again!", 401
            
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- PRIVATE WEBSOCKET ROOMS ---
@socketio.on('join')
def on_join(data):
    user_id = data.get('user_id')
    if user_id:
        room = f"user_{user_id}"
        join_room(room)

# --- USER DASHBOARD (SPA STATE MANAGER) ---
@app.route('/')
@login_required
def index():
    if current_user.role == 'admin':
        return redirect(url_for('admin_dashboard'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute("SELECT * FROM polls WHERE is_active = TRUE ORDER BY id DESC LIMIT 1")
    poll = cursor.fetchone()
    
    options = []
    initial_scores = {}
    
    is_present = False
    has_verified = False
    has_voted = False
    my_token = None # Recover assigned token from DB if browser was refreshed

    if poll:
        cursor.execute("SELECT * FROM poll_options WHERE poll_id = %s", (poll['id'],))
        options = cursor.fetchall()
        
        raw_scores = redis_client.hgetall(f"poll_{poll['id']}_results")
        for opt in options:
            initial_scores[opt['option_name']] = int(raw_scores.get(opt['option_name'], 0))
            
        # 1. Check Scan Status
        cursor.execute("SELECT is_present FROM event_attendees WHERE user_id=%s AND poll_id=%s", (current_user.id, poll['id']))
        att = cursor.fetchone()
        if att and att['is_present']: is_present = True
            
        # 2. Check Token Status and Fetch Token Code if not used yet
        cursor.execute("SELECT is_used, token_code FROM poll_tokens WHERE used_by_user_id=%s AND poll_id=%s", (current_user.id, poll['id']))
        token_record = cursor.fetchone()
        if token_record:
            if token_record['is_used']: 
                has_verified = True
            else:
                my_token = token_record['token_code'] # Persistence: recovered token
            
        # 3. Check Voting Status
        cursor.execute("SELECT id FROM user_votes WHERE user_id=%s AND poll_id=%s", (current_user.id, poll['id']))
        if cursor.fetchone(): has_voted = True

    cursor.close()
    conn.close()
    
    return render_template('index.html', 
                           user=current_user, poll=poll, options=options, 
                           initial_scores=initial_scores, is_present=is_present, 
                           has_verified=has_verified, has_voted=has_voted, my_token=my_token)

# --- TOKEN VERIFICATION API ---
@app.route('/api/verify_token', methods=['POST'])
@login_required
def verify_token():
    data = request.json
    poll_id = data.get('poll_id')
    token = data.get('token')

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Check if token exists, belongs to user, and is for this poll
        cursor.execute("""
            SELECT id, is_used, used_by_user_id FROM poll_tokens 
            WHERE poll_id = %s AND token_code = %s AND used_by_user_id = %s
        """, (poll_id, token, current_user.id))
        token_record = cursor.fetchone()

        if not token_record:
            return jsonify({"error": "Invalid PIN code. Please check your Notification Center."}), 400
        if token_record['is_used']:
            return jsonify({"error": "This PIN has already been used!"}), 400

        # Mark PIN as used
        cursor.execute("UPDATE poll_tokens SET is_used = TRUE WHERE id = %s", (token_record['id'],))
        conn.commit()
        
        return jsonify({"message": "PIN Verified Successfully! Voting Unlocked."}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# --- VOTING API & SOCKETS ---
@app.route('/api/vote', methods=['POST'])
@login_required
def vote():
    data = request.json
    poll_id, option_id, option_name = data.get('poll_id'), data.get('option_id'), data.get('option_name')

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Double-check verification status in DB
        cursor.execute("SELECT id FROM poll_tokens WHERE poll_id = %s AND used_by_user_id = %s AND is_used = TRUE", (poll_id, current_user.id))
        if not cursor.fetchone():
            return jsonify({"error": "Security breach: You must verify your 7-digit PIN before voting!"}), 403

        cursor.execute(
            "INSERT INTO user_votes (user_id, poll_id, option_id) VALUES (%s, %s, %s)",
            (current_user.id, poll_id, option_id)
        )
        conn.commit()

        redis_client.hincrby(f"poll_{poll_id}_results", option_name, 1)
        socketio.emit('update_chart', redis_client.hgetall(f"poll_{poll_id}_results"))

        return jsonify({"message": "Vote cast successfully!"}), 200

    except mysql.connector.IntegrityError:
        return jsonify({"error": "You have already voted on this poll!"}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# --- ADMIN SCANNER API ---
@app.route('/api/admin/scan_ticket', methods=['POST'])
@login_required
def scan_ticket():
    if current_user.role != 'admin':
        return jsonify({"error": "Unauthorized! Admins only."}), 403

    data = request.json
    qr_text, poll_id = data.get('qr_text'), data.get('poll_id')

    if not qr_text or not qr_text.startswith("LIVEPULSE_USER_"):
        return jsonify({"error": "Invalid QR Code format."}), 400

    try:
        scanned_user_id = int(qr_text.split('_')[-1])
    except ValueError:
        return jsonify({"error": "Corrupted User ID."}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT title FROM polls WHERE id = %s AND is_active = TRUE", (poll_id,))
        active_poll = cursor.fetchone()

        if not active_poll:
            return jsonify({"error": "This poll is no longer active!"}), 400

        cursor.execute("SELECT username FROM users WHERE id = %s", (scanned_user_id,))
        user_info = cursor.fetchone()

        cursor.execute("SELECT is_present FROM event_attendees WHERE user_id = %s AND poll_id = %s", (scanned_user_id, poll_id))
        attendance_record = cursor.fetchone()

        if attendance_record and attendance_record['is_present']:
            return jsonify({"error": f"Already Scanned! {user_info['username']} is already checked in."}), 400

        cursor.execute("""
            INSERT INTO event_attendees (user_id, poll_id, is_present)
            VALUES (%s, %s, TRUE)
            ON DUPLICATE KEY UPDATE is_present = TRUE
        """, (scanned_user_id, poll_id))
        conn.commit()

        # Real-time success alert to the specific user's room
        socketio.emit('scan_success', {
            'poll_id': poll_id, 
            'poll_name': active_poll['title']
        }, to=f"user_{scanned_user_id}")

        return jsonify({"message": f"✅ Check-in successful: {user_info['username']}!"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# --- ADMIN DISPATCH API ---
@app.route('/api/admin/dispatch_tokens', methods=['POST'])
@login_required
def dispatch_tokens():
    if current_user.role != 'admin':
        return jsonify({"error": "Unauthorized! Admins only."}), 403

    poll_id = request.json.get('poll_id')
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT COUNT(*) as count FROM poll_tokens WHERE poll_id = %s", (poll_id,))
        if cursor.fetchone()['count'] > 0:
            return jsonify({"error": "Tokens have already been generated for this poll!"}), 400

        cursor.execute("SELECT title FROM polls WHERE id = %s", (poll_id,))
        poll_title = cursor.fetchone()['title']

        cursor.execute("""
            SELECT u.id, u.username 
            FROM event_attendees ea
            JOIN users u ON ea.user_id = u.id
            WHERE ea.poll_id = %s AND ea.is_present = TRUE
        """, (poll_id,))
        
        attendees = cursor.fetchall()
        if not attendees:
            return jsonify({"error": "No attendees have checked in yet!"}), 400

        tokens_generated = 0

        for attendee in attendees:
            alphabet = string.ascii_uppercase + string.digits
            token = ''.join(secrets.choice(alphabet) for _ in range(7))
            
            # LINK TOKEN TO USER during generation for refresh-persistence
            cursor.execute("INSERT INTO poll_tokens (poll_id, token_code, used_by_user_id) VALUES (%s, %s, %s)", (poll_id, token, attendee['id']))
            
            # Send to user room
            socketio.emit('new_notification', {
                'poll_name': poll_title, 
                'token': token
            }, to=f"user_{attendee['id']}")
            
            tokens_generated += 1
        
        conn.commit()
        return jsonify({"message": f"✅ Successfully generated and dispatched {tokens_generated} PINs!"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# --- ADMIN API ENDPOINTS (SIMULATE/END) ---
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

# --- ADMIN DASHBOARD & POLL CREATION ---
@app.route('/admin', methods=['GET', 'POST'])
@login_required
def admin_dashboard():
    if current_user.role != 'admin': return "❌ Unauthorized! Admins only.", 403
    conn = get_db_connection(); cursor = conn.cursor(dictionary=True)

    if request.method == 'POST':
        title = request.form.get('title')
        valid_options = [opt.strip() for opt in request.form.getlist('options[]') if opt.strip()]
        cursor.execute("UPDATE polls SET is_active = FALSE")
        cursor.execute("INSERT INTO polls (title, is_active) VALUES (%s, TRUE)", (title,))
        new_poll_id = cursor.lastrowid
        for option_name in valid_options:
            cursor.execute("INSERT INTO poll_options (poll_id, option_name) VALUES (%s, %s)", (new_poll_id, option_name))
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
    socketio.run(app, debug=True)
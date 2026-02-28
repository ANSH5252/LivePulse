import os
import redis
import mysql.connector
import secrets
import string
from flask import Flask, request, jsonify, render_template, redirect, url_for
from flask_socketio import SocketIO
from dotenv import load_dotenv, find_dotenv
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user

# --- SETUP & INITIALIZATION ---
load_dotenv(find_dotenv())

app = Flask(__name__, template_folder="../frontend", static_folder="../frontend")
app.secret_key = "super_secret_livepulse_key_change_this_later"

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

# --- MAIN APP ROUTE (USER DASHBOARD) ---
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
    
    if poll:
        cursor.execute("SELECT * FROM poll_options WHERE poll_id = %s", (poll['id'],))
        options = cursor.fetchall()
        
        raw_scores = redis_client.hgetall(f"poll_{poll['id']}_results")
        
        for opt in options:
            name = opt['option_name']
            initial_scores[name] = int(raw_scores.get(name, 0))
            
    cursor.close()
    conn.close()
    
    return render_template('index.html', user=current_user, poll=poll, options=options, initial_scores=initial_scores)

# --- VOTING API & SOCKETS ---
@app.route('/api/vote', methods=['POST'])
@login_required
def vote():
    data = request.json
    poll_id = data.get('poll_id')
    option_id = data.get('option_id')
    option_name = data.get('option_name')

    if not poll_id or not option_id:
        return jsonify({"error": "Missing data"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "INSERT INTO user_votes (user_id, poll_id, option_id) VALUES (%s, %s, %s)",
            (current_user.id, poll_id, option_id)
        )
        conn.commit()

        redis_client.hincrby(f"poll_{poll_id}_results", option_name, 1)
        
        all_scores = redis_client.hgetall(f"poll_{poll_id}_results")
        socketio.emit('update_chart', all_scores)

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
    qr_text = data.get('qr_text')
    poll_id = data.get('poll_id')

    if not qr_text or not qr_text.startswith("LIVEPULSE_USER_"):
        return jsonify({"error": "Invalid QR Code format."}), 400
    if not poll_id:
        return jsonify({"error": "No poll selected for check-in."}), 400

    try:
        scanned_user_id = int(qr_text.split('_')[-1])
    except (ValueError, IndexError):
        return jsonify({"error": "Corrupted User ID in QR Code."}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # 1. Verify poll is active
        cursor.execute("SELECT id FROM polls WHERE id = %s AND is_active = TRUE", (poll_id,))
        if not cursor.fetchone():
            return jsonify({"error": "This poll is no longer active! Cannot check-in."}), 400

        # 2. Get user info
        cursor.execute("SELECT username FROM users WHERE id = %s", (scanned_user_id,))
        user_info = cursor.fetchone()
        
        if not user_info:
            return jsonify({"error": "User not found in database."}), 404

        # 3. Check for duplicates
        cursor.execute("""
            SELECT is_present FROM event_attendees 
            WHERE user_id = %s AND poll_id = %s
        """, (scanned_user_id, poll_id))
        attendance_record = cursor.fetchone()

        if attendance_record and attendance_record['is_present']:
            return jsonify({"error": f"Already Scanned! {user_info['username']} is already checked in."}), 400

        # 4. Insert/Update Check-in
        cursor.execute("""
            INSERT INTO event_attendees (user_id, poll_id, is_present)
            VALUES (%s, %s, TRUE)
            ON DUPLICATE KEY UPDATE is_present = TRUE
        """, (scanned_user_id, poll_id))
        conn.commit()

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
    if not poll_id:
        return jsonify({"error": "No poll selected."}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Prevent double-generation
        cursor.execute("SELECT COUNT(*) as count FROM poll_tokens WHERE poll_id = %s", (poll_id,))
        if cursor.fetchone()['count'] > 0:
            return jsonify({"error": "Tokens have already been generated for this poll!"}), 400

        # Fetch checked-in attendees
        cursor.execute("""
            SELECT u.id, u.email, u.username 
            FROM event_attendees ea
            JOIN users u ON ea.user_id = u.id
            WHERE ea.poll_id = %s AND ea.is_present = TRUE
        """, (poll_id,))
        
        attendees = cursor.fetchall()
        
        if not attendees:
            return jsonify({"error": "No attendees have checked in yet! Scan QRs first."}), 400

        tokens_generated = 0
        print(f"\n--- 🚀 GENERATING IN-APP TOKENS FOR POLL {poll_id} ---")

        for attendee in attendees:
            # Generate secure 7-char PIN
            alphabet = string.ascii_uppercase + string.digits
            token = ''.join(secrets.choice(alphabet) for _ in range(7))
            
            # Save to Vault
            cursor.execute("""
                INSERT INTO poll_tokens (poll_id, token_code) 
                VALUES (%s, %s)
            """, (poll_id, token))
            
            # Print to console for testing
            print(f"🔐 TOKEN GENERATED FOR {attendee['username']}: {token}")
            
            tokens_generated += 1
        
        conn.commit()
        return jsonify({"message": f"✅ Successfully generated {tokens_generated} PINs for In-App Dispatch!"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# --- ADMIN API ENDPOINTS (SIMULATE/END) ---
@app.route('/api/admin/simulate', methods=['POST'])
@login_required
def simulate_votes():
    if current_user.role != 'admin':
        return jsonify({"error": "Unauthorized! Admins only."}), 403

    data = request.json
    poll_id = data.get('poll_id')
    option_name = data.get('option_name')
    
    try:
        vote_count = int(data.get('count', 1))
    except ValueError:
        return jsonify({"error": "Invalid number"}), 400

    redis_client.hincrby(f"poll_{poll_id}_results", option_name, vote_count)
    
    all_scores = redis_client.hgetall(f"poll_{poll_id}_results")
    socketio.emit('update_chart', all_scores)

    return jsonify({"message": f"Injected {vote_count} fake votes for {option_name}!"}), 200

@app.route('/api/admin/end_poll', methods=['POST'])
@login_required
def end_poll():
    if current_user.role != 'admin':
        return jsonify({"error": "Unauthorized! Admins only."}), 403

    poll_id = request.json.get('poll_id')
    
    if not poll_id:
        return jsonify({"error": "Missing poll ID"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("UPDATE polls SET is_active = FALSE WHERE id = %s", (poll_id,))
        conn.commit()
        return jsonify({"message": "Poll ended successfully!"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# --- ADMIN DASHBOARD & POLL CREATION ---
@app.route('/admin', methods=['GET', 'POST'])
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        return "❌ Unauthorized! Admins only.", 403

    if request.method == 'POST':
        title = request.form.get('title')
        options = request.form.getlist('options[]') 
        valid_options = [opt.strip() for opt in options if opt.strip()]

        if not title or len(valid_options) < 2:
            return "❌ You need a title and at least 2 options!", 400

        conn = get_db_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("UPDATE polls SET is_active = FALSE")
            
            cursor.execute("INSERT INTO polls (title, is_active) VALUES (%s, TRUE)", (title,))
            new_poll_id = cursor.lastrowid

            for option_name in valid_options:
                cursor.execute(
                    "INSERT INTO poll_options (poll_id, option_name) VALUES (%s, %s)",
                    (new_poll_id, option_name)
                )
            
            conn.commit()
            return redirect(url_for('admin_dashboard'))

        except Exception as e:
            return f"❌ Error: {str(e)}", 500
        finally:
            cursor.close()
            conn.close()

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    def fetch_poll_details(query):
        cursor.execute(query)
        polls = cursor.fetchall()
        
        for poll in polls:
            cursor.execute("SELECT * FROM poll_options WHERE poll_id = %s", (poll['id'],))
            options = cursor.fetchall()
            poll['options'] = options
            
            raw_scores = redis_client.hgetall(f"poll_{poll['id']}_results")
            scores = {}
            for opt in options:
                name = opt['option_name']
                scores[name] = int(raw_scores.get(name, 0))
            poll['scores'] = scores
            
        return polls

    try:
        active_polls = fetch_poll_details("SELECT * FROM polls WHERE is_active = TRUE ORDER BY created_at DESC")
        ended_polls = fetch_poll_details("SELECT * FROM polls WHERE is_active = FALSE ORDER BY created_at DESC")
    finally:
        cursor.close()
        conn.close()

    return render_template('admin.html', user=current_user, active_polls=active_polls, ended_polls=ended_polls)

if __name__ == '__main__':
    socketio.run(app, debug=True)
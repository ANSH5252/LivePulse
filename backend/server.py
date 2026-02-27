import os
import redis
import mysql.connector
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
            return redirect(url_for('index'))
        else:
            return "❌ Invalid username or password. Try again!", 401
            
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- MAIN APP ROUTE ---
@app.route('/')
@login_required
def index():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Fetch the most recently created active poll
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

# --- ADMIN POLL CREATION ---
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
            return redirect(url_for('index'))

        except Exception as e:
            return f"❌ Error: {str(e)}", 500
        finally:
            cursor.close()
            conn.close()

    return render_template('admin.html', user=current_user)

if __name__ == '__main__':
    socketio.run(app, debug=True)
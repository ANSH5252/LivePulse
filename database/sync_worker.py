import redis
import mysql.connector
import time
import os
from dotenv import load_dotenv

# 1. Load the secret variables from the .env file
load_dotenv()

# 2. Connect to Redis (Short-term memory)
redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)

# 3. Connect to MySQL (Permanent storage)
try:
    db = mysql.connector.connect(
        host=os.getenv("MYSQL_HOST"),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DB")
    )
    cursor = db.cursor()
    print("✅ Worker connected to MySQL securely!")
except mysql.connector.Error as err:
    print(f"❌ MySQL Connection Error: {err}")
    exit()

def sync_to_mysql():
    try:
        # Fetch all current scores from Redis
        scores = redis_client.hgetall("poll_results")
        
        if not scores:
            print("⏳ No votes in Redis yet. Waiting...")
            return

        # Loop through each candidate's score and save it to MySQL
        for candidate, votes in scores.items():
            # This inserts the candidate if new, or updates their score if they exist
            sql = """
            INSERT INTO votes (candidate_name, total_votes) 
            VALUES (%s, %s) 
            ON DUPLICATE KEY UPDATE total_votes = %s
            """
            cursor.execute(sql, (candidate, int(votes), int(votes)))
        
        db.commit()
        print(f"💾 Successfully backed up live scores to MySQL at {time.strftime('%X')}")

    except Exception as e:
        print(f"❌ Error syncing: {e}")

if __name__ == "__main__":
    print("🚀 LivePulse Background Sync Worker Started...")
    # Run a continuous loop that syncs every 10 seconds
    while True:
        sync_to_mysql()
        time.sleep(10)
import redis
import mysql.connector
import time
import os
import json
from dotenv import load_dotenv

# 1. Load the secret variables
load_dotenv()

# 2. Connect to Redis (Short-term memory & Message Queue)
redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)

def get_db():
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST"),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DB")
    )

def process_vote_queue():
    # Check if there are any votes waiting in the queue
    queue_len = redis_client.llen("vote_queue")
    if queue_len == 0:
        print("⏳ No new votes in queue. Waiting...")
        return

    print(f"📥 Found {queue_len} votes in queue. Syncing to MySQL...")
    
    db = get_db()
    cursor = db.cursor()
    
    try:
        # Process every vote currently in the queue
        while True:
            # Pop the oldest vote from the right side of the list
            raw_vote = redis_client.rpop("vote_queue")
            if not raw_vote:
                break # Queue is empty
            
            vote = json.loads(raw_vote)
            
            # Write the individual user's vote history to MySQL
            try:
                cursor.execute(
                    "INSERT INTO user_votes (user_id, poll_id, option_id) VALUES (%s, %s, %s)",
                    (vote['user_id'], vote['poll_id'], vote['option_id'])
                )
            except mysql.connector.IntegrityError:
                # Safety catch: Ignore if the user somehow already has a recorded vote
                pass 
            
        # Commit all the database writes at once for maximum efficiency
        db.commit()
        print(f"💾 Successfully wrote batch of votes to MySQL at {time.strftime('%X')}")

    except Exception as e:
        print(f"❌ Error syncing to database: {e}")
        db.rollback()
    finally:
        cursor.close()
        db.close()

if __name__ == "__main__":
    print("🚀 LivePulse Background Sync Worker Started...")
    # Run a continuous loop that checks the queue every 10 seconds
    while True:
        process_vote_queue()
        time.sleep(10)
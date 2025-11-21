from flask import Flask, jsonify, request, render_template, send_from_directory
from flask_cors import CORS
import sqlite3
import os
import time
import threading
from datetime import datetime

# ------------------ App setup ------------------
app = Flask(__name__, instance_relative_config=True)
CORS(app)

DB_PATH = os.path.join(app.instance_path, 'animal_feeder.db')

# ------------------ Database helper ------------------
def query_db(query, args=(), one=False):
    con = None
    try:
        con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute(query, args)
        rv = cur.fetchall()
        con.commit()
        return (rv[0] if rv else None) if one else rv
    except sqlite3.OperationalError as e:
        if con:
            con.rollback()
        raise e
    finally:
        if con:
            con.close()

os.makedirs(app.instance_path, exist_ok=True)

# ------------------ Table creation ------------------
query_db("""
CREATE TABLE IF NOT EXISTS camera (
    cam_id TEXT PRIMARY KEY,
    status TEXT NOT NULL
)
""")

query_db("""
CREATE TABLE IF NOT EXISTS modules (
    module_id TEXT PRIMARY KEY,
    cam_id TEXT NOT NULL,
    status TEXT NOT NULL,
    weight REAL,
    FOREIGN KEY (cam_id) REFERENCES camera(cam_id)
)
""")

query_db("""
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id TEXT NOT NULL,
    feed_time TEXT NOT NULL,
    amount REAL NOT NULL,
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'done')),
    FOREIGN KEY (module_id) REFERENCES modules(module_id)
)
""")

query_db("""
CREATE TABLE IF NOT EXISTS history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (schedule_id) REFERENCES schedules(schedule_id)
)
""")



# ------------------ ESP32/DEVICE ROUTES ------------------
@app.route("/health")
def health_check():
    """mDNS/health check endpoint for devices"""
    return "mDNS OK"

@app.route("/check_schedule", methods=["POST"])
def check_schedule():
    """Check if a module should dispense food now"""
    module_id = request.form.get("module_id")
    
    if not module_id:
        return jsonify({"error": "Missing module_id"}), 400
    
    # Verify module exists and is active
    module = query_db("""
        SELECT module_id FROM modules
        WHERE module_id=? AND status='active'
    """, (module_id,), one=True)
    
    if not module:
        return jsonify({"error": "Invalid or inactive module_id"}), 404
    
    # Get current time in HH:MM format
    now = datetime.now().strftime("%H:%M")
    
    # Check for pending schedules at or before current time
    row = query_db("""
        SELECT schedule_id, amount, feed_time FROM schedules
        WHERE module_id=? AND feed_time<=? AND status='pending'
        ORDER BY feed_time ASC
        LIMIT 1
    """, (module_id, now), one=True)
    
    if row:
        return jsonify({
            "dispense": True, 
            "amount": row['amount'],
            "schedule_id": row['schedule_id'],
            "scheduled_time": row['feed_time']
        })
    else:
        return jsonify({"dispense": False})
    
@app.route("/complete_schedule", methods=["POST"])
def complete_schedule():
    """Mark a schedule as done and add to history"""
    schedule_id = request.form.get("schedule_id")
    module_id = request.form.get("module_id")  # For verification
    
    if not schedule_id:
        return jsonify({"error": "Missing schedule_id"}), 400
    
    # Verify schedule exists and is still pending
    schedule = query_db("""
        SELECT schedule_id, module_id, status FROM schedules
        WHERE schedule_id=?
    """, (schedule_id,), one=True)
    
    if not schedule:
        return jsonify({"error": "Schedule not found"}), 404
    
    if schedule['status'] == 'done':
        return jsonify({"error": "Schedule already completed"}), 400
    
    # Optional: Verify module_id matches (security check)
    if module_id and schedule['module_id'] != module_id:
        return jsonify({"error": "Module ID mismatch"}), 403
    
    # Mark schedule as done
    query_db("""
        UPDATE schedules SET status='done' 
        WHERE schedule_id=?
    """, (schedule_id,))
    
    # Add to history
    query_db("""
        INSERT INTO history (schedule_id) VALUES (?)
    """, (schedule_id,))
    
    print(f"Schedule {schedule_id} completed by module {schedule['module_id']}")
    
    return jsonify({
        "success": True,
        "message": "Schedule completed successfully",
        "schedule_id": schedule_id
    })

@app.route("/weight_update", methods=["POST"])
def weight_update():
    """Update module weight from ESP32"""
    module_id = request.form.get("module_id")
    weight = request.form.get("weight")
    
    if not module_id or weight is None:
        return jsonify({"error": "Missing module_id or weight"}), 400
    
    # Validate weight value
    try:
        weight_value = float(weight)
        if weight_value < 0 or weight_value > 10000:  # Max 10kg
            return jsonify({"error": "Invalid weight value"}), 400
    except ValueError:
        return jsonify({"error": "Weight must be a number"}), 400
    
    print(f"Weight update - Device: {module_id}, Weight: {weight_value}g")
    
    # Check if module exists
    existing = query_db("""
        SELECT module_id FROM modules WHERE module_id=?
    """, (module_id,), one=True)
    
    if existing:
        # Update existing module with timestamp
        query_db("""
            UPDATE modules 
            SET weight=?, status='active'
            WHERE module_id=?
        """, (weight_value, module_id))
    else:
        # Reject new modules (require manual registration for security)
        return jsonify({
            "error": "Module not registered. Please register module first."
        }), 403
    
    return jsonify({
        "success": True,
        "message": f"Weight updated for {module_id}: {weight_value}g"
    })

# API: Delete specific snapshot
@app.route('/api/snapshots/<filename>', methods=['DELETE'])
def delete_snapshot(filename):
    image_dir = 'instance/images'
    try:
        filepath = os.path.join(image_dir, filename)
       
        # Check if file exists
        if not os.path.exists(filepath):
            return jsonify({'success': False, 'error': 'File not found'}), 404
       
        # Security check: ensure filename doesn't contain path traversal
        if '..' in filename or '/' in filename or '\\' in filename:
            return jsonify({'success': False, 'error': 'Invalid filename'}), 400
       
        # Delete the file
        os.remove(filepath)
        print(f"Deleted image: {filename}")
       
        return jsonify({'success': True, 'message': f'Image {filename} deleted successfully'})
    except Exception as e:
        print(f"Error deleting image {filename}: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
        
@app.route("/upload_image", methods=["POST"])
def upload_image():
    """Receive image from ESP32-CAM"""  
    camera_id = request.form.get("camera_id")
    
    if not camera_id:
        return jsonify({"error": "Missing camera_id"}), 400
    
    # Verify camera exists and is active
    camera = query_db("""
        SELECT cam_id FROM camera
        WHERE cam_id=? AND status='active'
    """, (camera_id,), one=True)
    
    if not camera:
        return jsonify({"error": "Invalid or inactive camera_id"}), 404
    
    # Get image data
    image = request.files.get('image')
    if not image:
        return jsonify({"error": "No image data"}), 400
    
    # Create images directory if it doesn't exist
    images_dir = os.path.join(app.instance_path, 'images')
    os.makedirs(images_dir, exist_ok=True)
    
    # Save with camera_id and timestamp in filename
    timestamp = int(time.time())
    filename = f"{camera_id}_{timestamp}.jpg"
    filepath = os.path.join(images_dir, filename)
    
    image.save(filepath)
    file_size = os.path.getsize(filepath)
    
    print(f"Saved: {filename}, Size: {file_size} bytes, Camera: {camera_id}")
    
    return jsonify({
        "success": True,
        "filename": filename,
        "size": file_size,
        "camera_id": camera_id
    }), 200

# ------------------ CAMERA ROUTES ------------------
@app.route("/cameras", methods=["GET"])
def get_cameras():
    rows = query_db("SELECT * FROM camera")
    return jsonify([dict(row) for row in rows])

@app.route("/cameras", methods=["POST"])
def add_camera():
    data = request.get_json()
    query_db("INSERT INTO camera (cam_id, status) VALUES (?, ?)",
             (data["cam_id"], data["status"]))
    return jsonify({"success": True})

@app.route("/cameras/<cam_id>", methods=["PUT"])
def update_camera(cam_id):
    data = request.get_json()
    query_db("UPDATE camera SET status = ? WHERE cam_id = ?",
             (data["status"], cam_id))
    return jsonify({"success": True})

@app.route("/cameras/<cam_id>", methods=["DELETE"])
def delete_camera(cam_id):
    query_db("DELETE FROM camera WHERE cam_id = ?", (cam_id,))
    return jsonify({"success": True})

# ------------------ CAMERA ROUTES FOR WEBVIEW ------------------

# API: Get list of all captured images
@app.route('/api/snapshots', methods=['GET'])
def get_snapshots():
    image_dir = 'instance/images'
    try:
        # Check if directory exists
        if not os.path.exists(image_dir):
            os.makedirs(image_dir)
            return jsonify({'success': True, 'images': []})
        
        # Get all image files
        images = [f for f in os.listdir(image_dir) 
                  if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
        
        # Sort by filename (newest first based on timestamp in name)
        images.sort(reverse=True)
        
        return jsonify({'success': True, 'images': images})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# Serve individual snapshot image
@app.route('/snapshots/<filename>')
def serve_snapshot(filename):
    try:
        return send_from_directory('instance/images', filename)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 404

# API: Get snapshots for specific camera
@app.route('/api/snapshots/<cam_id>', methods=['GET'])
def get_camera_snapshots(cam_id):
    image_dir = 'instance/images'
    try:
        if not os.path.exists(image_dir):
            return jsonify({'success': True, 'images': []})
        
        # Filter images by camera ID
        all_images = os.listdir(image_dir)
        camera_images = [f for f in all_images 
                        if f.startswith(f'CAMERA{cam_id}') or f.startswith(f'Camera{cam_id}')]
        
        camera_images.sort(reverse=True)
        
        return jsonify({'success': True, 'cam_id': cam_id, 'images': camera_images})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ------------------ MODULE ROUTES ------------------
@app.route("/modules", methods=["GET"])
def get_modules():
    rows = query_db("SELECT * FROM modules")
    return jsonify([dict(row) for row in rows])

@app.route("/modules", methods=["POST"])
def add_module():
    data = request.get_json()
    query_db("""
        INSERT INTO modules (module_id, cam_id, status, weight)
        VALUES (?, ?, ?, ?)
    """, (data["module_id"], data["cam_id"], data["status"], data["weight"]))
    return jsonify({"success": True})

@app.route("/modules/<module_id>", methods=["PUT"])
def update_module(module_id):
    data = request.get_json()
    query_db("""
        UPDATE modules
        SET cam_id = ?, status = ?, weight = ?
        WHERE module_id = ?
    """, (data["cam_id"], data["status"], data["weight"], module_id))
    return jsonify({"success": True})

@app.route("/modules/<module_id>", methods=["DELETE"])
def delete_module(module_id):
    query_db("DELETE FROM modules WHERE module_id = ?", (module_id,))
    return jsonify({"success": True})

# ------------------ SCHEDULE ROUTES ------------------
@app.route("/schedules", methods=["GET"])
def get_schedules():
    rows = query_db("SELECT * FROM schedules")
    return jsonify([dict(row) for row in rows])

@app.route("/schedules", methods=["POST"])
def add_schedule():
    data = request.get_json()
    query_db("""
        INSERT INTO schedules (module_id, feed_time, amount, status)
        VALUES (?, ?, ?, ?)
    """, (data["module_id"], data["feed_time"], data["amount"], data.get("status", "pending")))
    return jsonify({"success": True})

@app.route("/schedules/<int:schedule_id>", methods=["PUT"])
def update_schedule(schedule_id):
    data = request.get_json()
    query_db("""
        UPDATE schedules
        SET module_id = ?, feed_time = ?, amount = ?, status = ?
        WHERE schedule_id = ?
    """, (data["module_id"], data["feed_time"], data["amount"], data["status"], schedule_id))
    return jsonify({"success": True})

@app.route("/schedules/<int:schedule_id>", methods=["DELETE"])
def delete_schedule(schedule_id):
    query_db("DELETE FROM schedules WHERE schedule_id = ?", (schedule_id,))
    return jsonify({"success": True})

# ------------------ HISTORY ROUTES ------------------
@app.route("/history", methods=["GET"])
def get_history():
    rows = query_db("""
        SELECT h.history_id, h.created_at, s.schedule_id, s.module_id, s.feed_time, s.amount, s.status
        FROM history h
        LEFT JOIN schedules s ON h.schedule_id = s.schedule_id
        ORDER BY h.created_at DESC
    """)
    return jsonify([dict(row) for row in rows])

@app.route("/history", methods=["POST"])
def add_history():
    data = request.get_json()
    query_db("INSERT INTO history (schedule_id) VALUES (?)",
             (data["schedule_id"],))
    return jsonify({"success": True})

@app.route("/history/<int:history_id>", methods=["DELETE"])
def delete_history(history_id):
    query_db("DELETE FROM history WHERE history_id = ?", (history_id,))
    return jsonify({"success": True})

# ------------------ FRONTEND ROUTES ------------------
@app.route("/")
def serve_index():
    return render_template("index.html")

@app.route("/module.html")
def serve_module():
    return render_template("module.html")

@app.route("/schedule.html")
def serve_schedule():
    return render_template("schedule.html")

@app.route("/history.html")
def serve_history():
    return render_template("history.html")

@app.route("/feeders.html")
def serve_feeders():
    return render_template("feeders.html")

@app.route("/camera.html")
def serve_camera():
    return render_template("camera.html")

# ------------------ Run App ------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True, threaded=True)

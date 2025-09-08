from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import base64, os, re, smtplib
from email.mime.text import MIMEText
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ✅ Flask Setup
app = Flask(__name__)
app.secret_key = "YourSecretKey"
app.permanent_session_lifetime = timedelta(days=1)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=False,  # Set True in production with HTTPS
    SESSION_COOKIE_SAMESITE='Lax'
)

# ✅ Google API Setup
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
creds = Credentials.from_service_account_file("service_account.json", scopes=scope)

# ✅ Google Sheets
gc = gspread.authorize(creds)
sheet = gc.open("OptiSupply")
ws_employees = sheet.worksheet("Employees")
ws_attendance = sheet.worksheet("Attendance Logs")
ws_leaves = sheet.worksheet("Leave Requests")

# ✅ Google Drive
drive_service = build('drive', 'v3', credentials=creds)
DRIVE_FOLDER_ID = "1UQTWjHrHxduG7NyKqEWWLnRelHPnqPYp"  # Replace with your folder ID

# ✅ Save base64 photo to Drive
def save_photo_to_drive(data_url, emp_id):
    match = re.match("data:image/(.*?);base64,(.*)", data_url)
    if not match:
        return None
    image_data = base64.b64decode(match.group(2))
    filename = f"{emp_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    filepath = f"temp_{filename}"
    with open(filepath, "wb") as f:
        f.write(image_data)

    file_metadata = {"name": filename, "parents": [DRIVE_FOLDER_ID]}
    media = MediaFileUpload(filepath, mimetype="image/png")
    file = drive_service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    os.remove(filepath)

    drive_service.permissions().create(
        fileId=file["id"],
        body={"role": "reader", "type": "anyone"}
    ).execute()

    return f"https://drive.google.com/uc?id={file['id']}"

# ✅ Home Route
@app.route("/")
def home():
    return render_template("home.html")

# ✅ Attendance Login Route
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        emp_id = request.form["emp_id"].strip()
        password = request.form["password"].strip()
        lat = request.form.get("lat", "")
        lon = request.form.get("lon", "")

        for rec in ws_employees.get_all_records():
            if rec.get("EmployeeID", "").strip() == emp_id:
                stored_hash = rec.get("PasswordHash")
                if stored_hash and check_password_hash(stored_hash, password):
                    session.permanent = True
                    session["emp_id"] = emp_id
                    session["location"] = f"{lat},{lon}"
                    session["login_time"] = datetime.now().strftime("%H:%M:%S")

                    # ✅ Append to Google Sheet (Attendance Logs)
                    ws_attendance.append_row([
                        emp_id,
                        datetime.now().strftime("%Y-%m-%d"),
                        session["login_time"],
                        "",  # Logout time
                        session["location"],
                        "Pending Photo"
                    ])
                    print("📍 Location received:", session["location"])

                    return redirect(url_for("dashboard"))
                else:
                    flash("❌ Incorrect password")
                    return redirect(url_for("login"))

        flash("❌ Employee ID not found")
        return redirect(url_for("login"))

    return render_template("login.html")

# ✅ Dashboard Route
@app.route("/dashboard")
def dashboard():
    if "emp_id" not in session:
        return redirect(url_for("login"))
    return render_template("dashboard.html", emp_id=session["emp_id"])

# ✅ Log Attendance Photo
@app.route("/log_attendance", methods=["POST"])
def log_attendance():
    if "emp_id" not in session:
        return jsonify({"status": "unauthorized"}), 401

    data = request.get_json()
    photo = data.get("photo")
    emp_id = session["emp_id"]
    location = session.get("location", "")
    photo_url = save_photo_to_drive(photo, emp_id)

    # ✅ Update last row’s photo link in Sheet
    all_rows = ws_attendance.get_all_values()
    last_row_index = len(all_rows)
    ws_attendance.update_cell(last_row_index, 6, photo_url)  # Column F = Photo URL

    return jsonify({"status": "success", "link": photo_url})

# ✅ Logout Route
@app.route("/logout")
def logout():
    if "emp_id" in session:
        emp_id = session["emp_id"]
        today = datetime.now().strftime("%Y-%m-%d")
        logout_time = datetime.now().strftime("%H:%M:%S")
        logs = ws_attendance.get_all_records()
        for i in reversed(range(len(logs))):
            row = logs[i]
            if row["EmployeeID"] == emp_id and row["Date"] == today and row.get("Logout Time", "") == "":
                ws_attendance.update_cell(i + 2, 4, logout_time)  # Column D = Logout Time
                break
        session.clear()
    return redirect(url_for("login"))

@app.route("/leave_login", methods=["GET", "POST"])
def leave_login():
    print("➡️ Incoming:", request.method, request.path)  # Debug

    if request.method == "POST":
        emp_id = request.form["emp_id"].strip()
        password = request.form["password"].strip()

        for rec in ws_employees.get_all_records():
            if rec.get("EmployeeID", "").strip() == emp_id:
                stored_hash = rec.get("PasswordHash")
                if stored_hash and check_password_hash(stored_hash, password):
                    session["emp_id"] = emp_id
                    return redirect(url_for("leave"))
                else:
                    flash("❌ Invalid password")
                    return redirect(url_for("leave_login"))

        flash("❌ Employee ID not found")
        return redirect(url_for("leave_login"))

    return render_template("leave_login.html")

# ✅ Leave Application Route
@app.route("/leave", methods=["GET", "POST"])
def leave():
    if "emp_id" not in session:
        return redirect(url_for("leave_login"))

    emp_id = session["emp_id"]

    if request.method == "POST":
        leave_type = request.form["type"]
        start_date = request.form["start_date"]
        end_date = request.form["end_date"]
        reason = request.form["reason"]

        # ✅ Store in Google Sheets
        ws_leaves.append_row([emp_id, leave_type, start_date, end_date, reason, "Pending"])

        # ✅ Send Email to HR
        sender = "your_email@gmail.com"
        password = "your_app_password"
        recipient = "hr_email@gmail.com"

        msg = MIMEText(f"""Employee ID: {emp_id}
Leave Type: {leave_type}
Dates: {start_date} to {end_date}
Reason: {reason}
Status: Pending
Please review the leave request.""")
        msg["Subject"] = f"Leave Request - {emp_id}"
        msg["From"] = sender
        msg["To"] = recipient

        try:
            smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465)
            smtp.login(sender, password)
            smtp.send_message(msg)
            smtp.quit()
        except Exception as e:
            flash(f"Email failed: {e}")

        flash("✅ Leave request submitted. Status: Pending")
        return redirect(url_for("home"))

    # ✅ Fetch all leave requests for this employee to display in table
    all_leaves = ws_leaves.get_all_values()
    employee_leaves = [row for row in all_leaves[1:] if row[0] == emp_id]  # skip header row

    return render_template("leave.html", emp_id=emp_id, leave_requests=employee_leaves)
# ✅ Run Flask App
if __name__ == "__main__":
    app.run(debug=True)

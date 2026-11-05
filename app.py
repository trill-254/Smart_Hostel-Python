from flask import Flask, render_template, request, redirect, url_for, flash, session, Response
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime
from functools import wraps
import csv
from io import StringIO
import os
import threading
import webbrowser
import requests
from base64 import b64encode

app = Flask(__name__)
app.config['SECRET_KEY'] = 'hostel-secure-production-key-2026'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///hostel.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Local Image Upload Configuration
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# M-Pesa Daraja API Configuration (Sandbox defaults)
app.config['MPESA_CONSUMER_KEY'] = 'your_consumer_key_here'
app.config['MPESA_CONSUMER_SECRET'] = 'your_consumer_secret_here'
app.config['MPESA_SHORTCODE'] = '174379'
app.config['MPESA_PASSKEY'] = 'bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919'

db = SQLAlchemy(app)

# --- DATABASE MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False) # 'guest' or 'manager'
    bookings = db.relationship('Booking', backref='guest', lazy=True)
    notifications = db.relationship('Notification', backref='user', lazy=True)

class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room_number = db.Column(db.String(10), unique=True, nullable=False)
    room_type = db.Column(db.String(50), nullable=False)
    price_per_semester = db.Column(db.Float, nullable=False)
    capacity = db.Column(db.Integer, default=1)
    image_url = db.Column(db.String(255), nullable=True)
    bookings = db.relationship('Booking', backref='room', lazy=True)

    def available_beds(self):
        occupied_count = Booking.query.filter_by(room_id=self.id).filter(Booking.status.in_(['Pending Approval', 'Confirmed'])).count()
        return max(0, self.capacity - occupied_count)

class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey('room.id'), nullable=False)
    check_in = db.Column(db.String(10), nullable=False)
    check_out = db.Column(db.String(10), nullable=False)
    status = db.Column(db.String(30), default='Pending Approval')
    payment_status = db.Column(db.String(20), default='Unpaid')
    mpesa_receipt = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    message = db.Column(db.String(255), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# --- INITIALIZE DB & SEED DATA ---
def init_db():
    with app.app_context():
        db.create_all()
        
        # Seed default manager if none exists
        if not User.query.filter_by(role='manager').first():
            mgr = User(
                name="Head Manager",
                email="manager@hostel.com",
                password_hash=generate_password_hash("admin123"),
                role="manager"
            )
            db.session.add(mgr)
            db.session.commit()

# --- AUTH DECORATORS ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'guest':
            flash('Please log in as a guest.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def manager_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'manager':
            flash('Access denied. Manager login required.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- ROUTES ---
@app.route('/')
def index():
    if 'user_id' in session:
        if session.get('role') == 'manager':
            return redirect(url_for('manager_dashboard'))
        return redirect(url_for('guest_dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'guest_login':
            email = request.form.get('email')
            password = request.form.get('password')
            user = User.query.filter_by(email=email, role='guest').first()
            if user and check_password_hash(user.password_hash, password):
                session['user_id'] = user.id
                session['name'] = user.name
                session['role'] = user.role
                flash(f'Welcome back, {user.name}!', 'success')
                return redirect(url_for('guest_dashboard'))
            else:
                flash('Invalid guest email or password.', 'danger')
                
        elif action == 'guest_register':
            name = request.form.get('name')
            email = request.form.get('email')
            password = request.form.get('password')
            if User.query.filter_by(email=email).first():
                flash('Email address already registered.', 'danger')
            else:
                hashed_password = generate_password_hash(password, method='scrypt')
                new_user = User(name=name, email=email, password_hash=hashed_password, role='guest')
                db.session.add(new_user)
                db.session.commit()
                flash('Guest registration successful! Please log in.', 'success')
                
        elif action == 'manager_register':
            name = request.form.get('name')
            email = request.form.get('email')
            password = request.form.get('password')
            if User.query.filter_by(email=email).first():
                flash('Email address already registered.', 'danger')
            else:
                hashed_password = generate_password_hash(password, method='scrypt')
                new_mgr = User(name=name, email=email, password_hash=hashed_password, role='manager')
                db.session.add(new_mgr)
                db.session.commit()
                flash('Manager registration successful! Please log in.', 'success')

        elif action == 'manager_login':
            email = request.form.get('email')
            password = request.form.get('password')
            user = User.query.filter_by(email=email, role='manager').first()
            if user and check_password_hash(user.password_hash, password):
                session['user_id'] = user.id
                session['name'] = user.name
                session['role'] = user.role
                flash(f'Welcome back, Manager {user.name}!', 'success')
                return redirect(url_for('manager_dashboard'))
            else:
                flash('Invalid manager credentials.', 'danger')
                
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

# --- GUEST DASHBOARD ---
@app.route('/dashboard/guest')
@login_required
def guest_dashboard():
    rooms = Room.query.all()
    my_bookings = Booking.query.filter_by(user_id=session['user_id']).order_by(Booking.created_at.desc()).all()
    notifications = Notification.query.filter_by(user_id=session['user_id'], is_read=False).all()
    return render_template('guest_dashboard.html', rooms=rooms, bookings=my_bookings, notifications=notifications)

@app.route('/book', methods=['POST'])
@login_required
def book_room():
    room_id = request.form.get('room_id')
    check_in = request.form.get('check_in')
    check_out = request.form.get('check_out')
    
    active_booking = Booking.query.filter_by(user_id=session['user_id']).filter(
        Booking.status.in_(['Pending Approval', 'Confirmed'])
    ).first()
    
    if active_booking:
        flash('You already have an active or pending booking. You cannot book more than one room at a time.', 'danger')
        return redirect(url_for('guest_dashboard'))
    
    room = Room.query.get(room_id)
    if room and room.available_beds() > 0:
        booking = Booking(
            user_id=session['user_id'],
            room_id=room.id,
            check_in=check_in,
            check_out=check_out,
            status='Pending Approval'
        )
        db.session.add(booking)
        db.session.commit()
        flash('Booking request submitted! Awaiting manager confirmation.', 'success')
    else:
        flash('Sorry, this room is fully booked.', 'danger')
    return redirect(url_for('guest_dashboard'))

# --- M-PESA PAYMENT HELPER & ROUTE ---
def generate_mpesa_access_key():
    consumer_key = app.config.get('MPESA_CONSUMER_KEY')
    consumer_secret = app.config.get('MPESA_CONSUMER_SECRET')
    api_URL = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    try:
        r = requests.get(api_URL, auth=(consumer_key, consumer_secret))
        return r.json().get('access_token')
    except Exception:
        return None

@app.route('/pay/mpesa/<int:booking_id>', methods=['POST'])
@login_required
def initiate_stk_push(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    phone_number = request.form.get('phone_number', '').strip()
    
    if phone_number.startswith('0'):
        phone_number = '254' + phone_number[1:]
    elif phone_number.startswith('+'):
        phone_number = phone_number.lstrip('+')
        
    if not phone_number.startswith('254') or len(phone_number) != 12:
        flash('Invalid phone number format. Use 2547XXXXXXXX or 07XXXXXXXX.', 'danger')
        return redirect(url_for('guest_dashboard'))
    
    access_token = generate_mpesa_access_key()
    if not access_token:
        flash('Failed to connect to M-Pesa sandbox gateway.', 'danger')
        return redirect(url_for('guest_dashboard'))
        
    api_url = "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    password = b64encode((app.config['MPESA_SHORTCODE'] + app.config['MPESA_PASSKEY'] + timestamp).encode()).decode('utf-8')
    
    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {
        "BusinessShortCode": app.config['MPESA_SHORTCODE'],
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(booking.room.price_per_semester),
        "PartyA": phone_number,
        "PartyB": app.config['MPESA_SHORTCODE'],
        "PhoneNumber": phone_number,
        "CallBackURL": "https://sandbox.safaricom.co.ke/net",
        "AccountReference": f"Hostel-{booking.id}",
        "TransactionDesc": f"Payment for Room {booking.room.room_number}"
    }
    
    try:
        response = requests.post(api_url, json=payload, headers=headers)
        data = response.json()
        if data.get('ResponseCode') == '0':
            flash('STK Push sent! Please check your phone to enter your M-Pesa PIN.', 'success')
        else:
            flash(f"M-Pesa error: {data.get('errorMessage', 'Failed to initiate payment.')}", 'danger')
    except Exception as e:
        flash(f"Connection error: {e}", 'danger')
        
    return redirect(url_for('guest_dashboard'))

# --- MANAGER DASHBOARD ---
@app.route('/dashboard/manager')
@manager_required
def manager_dashboard():
    rooms = Room.query.all()
    all_bookings = Booking.query.order_by(Booking.created_at.desc()).all()
    total_revenue = sum([b.room.price_per_semester for b in all_bookings if b.status == 'Confirmed'])
    return render_template('manager_dashboard.html', rooms=rooms, bookings=all_bookings, revenue=total_revenue)

@app.route('/manager/room/add', methods=['POST'])
@manager_required
def add_room():
    room_number = request.form.get('room_number')
    room_type = request.form.get('room_type')
    price = request.form.get('price_per_semester')
    capacity = int(request.form.get('capacity', 1))
    
    image_url = "/static/uploads/default_room.jpg"
    
    if 'room_image' in request.files:
        file = request.files['room_image']
        if file and file.filename != '':
            if allowed_file(file.filename):
                filename = secure_filename(file.filename)
                unique_filename = f"{int(datetime.utcnow().timestamp())}_{filename}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
                image_url = f"/static/uploads/{unique_filename}"
            else:
                flash('Invalid image file type. Allowed: png, jpg, jpeg, gif, webp', 'danger')
                return redirect(url_for('manager_dashboard'))
    
    if Room.query.filter_by(room_number=room_number).first():
        flash('Room number already exists.', 'danger')
    else:
        new_room = Room(room_number=room_number, room_type=room_type, price_per_semester=float(price), capacity=capacity, image_url=image_url)
        db.session.add(new_room)
        db.session.commit()
        flash('New room added successfully!', 'success')
    return redirect(url_for('manager_dashboard'))

@app.route('/manager/booking/<int:booking_id>/<action>', methods=['POST'])
@manager_required
def update_booking_status(booking_id, action):
    booking = Booking.query.get_or_404(booking_id)
    room = Room.query.get(booking.room_id)
    
    if action == 'confirm':
        if room.available_beds() > 0:
            booking.status = 'Confirmed'
            notif = Notification(user_id=booking.user_id, message=f"Your booking for Room {room.room_number} has been confirmed by management.")
            db.session.add(notif)
            flash('Booking confirmed successfully.', 'success')
        else:
            flash('Cannot confirm: Room is at full capacity.', 'danger')
    elif action == 'reject':
        booking.status = 'Rejected'
        notif = Notification(user_id=booking.user_id, message=f"Your booking for Room {room.room_number} was rejected.")
        db.session.add(notif)
        flash('Booking rejected.', 'info')
    elif action == 'checkout':
        booking.status = 'Completed'
        flash('Guest checked out successfully.', 'info')
        
    db.session.commit()
    return redirect(url_for('manager_dashboard'))

@app.route('/manager/booking/<int:booking_id>/edit', methods=['POST'])
@manager_required
def edit_booking(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    
    new_check_in = request.form.get('new_check_in') or request.form.get('check_in')
    new_check_out = request.form.get('new_check_out') or request.form.get('check_out')
    new_status = request.form.get('status')
    
    if new_check_in:
        booking.check_in = new_check_in
    if new_check_out:
        booking.check_out = new_check_out
    if new_status in ['Pending Approval', 'Confirmed', 'Rejected', 'Completed']:
        booking.status = new_status
        
    db.session.commit()
    flash('Booking updated successfully.', 'success')
    return redirect(url_for('manager_dashboard'))

@app.route('/manager/booking/<int:booking_id>/delete', methods=['POST'])
@manager_required
def delete_booking(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    db.session.delete(booking)
    db.session.commit()
    flash('Booking record deleted successfully.', 'info')
    return redirect(url_for('manager_dashboard'))
@app.route('/manager/room/<int:room_id>/edit', methods=['POST'])
@manager_required
def edit_room(room_id):
    room = Room.query.get_or_404(room_id)
    
    room.room_number = request.form.get('room_number', room.room_number)
    room.room_type = request.form.get('room_type', room.room_type)
    room.price_per_semester = float(request.form.get('price_per_semester', room.price_per_semester))
    room.capacity = int(request.form.get('capacity', room.capacity))
    
    # Handle optional replacement image upload
    if 'room_image' in request.files:
        file = request.files['room_image']
        if file and file.filename != '':
            if allowed_file(file.filename):
                filename = secure_filename(file.filename)
                unique_filename = f"{int(datetime.utcnow().timestamp())}_{filename}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
                room.image_url = f"/static/uploads/{unique_filename}"
            else:
                flash('Invalid image file type. Room updated without changing image.', 'warning')
                
    db.session.commit()
    flash(f'Room {room.room_number} updated successfully!', 'success')
    return redirect(url_for('manager_dashboard'))

@app.route('/manager/room/<int:room_id>/delete', methods=['POST'])
@manager_required
def delete_room(room_id):
    room = Room.query.get_or_404(room_id)
    db.session.delete(room)
    db.session.commit()
    flash('Room removed successfully from catalog.', 'info')
    return redirect(url_for('manager_dashboard'))

@app.route('/manager/export/csv')
@manager_required
def export_csv_report():
    bookings = Booking.query.all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Booking ID', 'Guest Name', 'Guest Email', 'Room Number', 'Room Type', 'Check-In', 'Check-Out', 'Status', 'Date Requested'])
    for b in bookings:
        writer.writerow([
            b.id, b.guest.name, b.guest.email, b.room.room_number,
            b.room.room_type, b.check_in, b.check_out, b.status,
            b.created_at.strftime('%Y-%m-%d %H:%M')
        ])
    output.seek(0)
    return Response(output, mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=hostel_report.csv"})

# --- RUN APPLICATION ---
if __name__ == '__main__':
    init_db()
    
    is_local = os.environ.get("PORT") is None
    
    if is_local and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
        
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=is_local)
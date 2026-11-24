from flask import Flask, render_template, request, redirect, url_for, flash, session, Response
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, date
from functools import wraps
import csv
from io import StringIO
import os
import re
import threading
import webbrowser
import hmac
import uuid

app = Flask(__name__)
app.config['SECRET_KEY'] = 'hostel-secure-production-key-2026'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///hostel.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MANAGER_REGISTRATION_CODE'] = 'trill'

# Initialize Flask-SQLAlchemy
db = SQLAlchemy(app)

# Local Image Upload Configuration
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def clean_filename(filename):
    filename = secure_filename(filename)
    filename = re.sub(r'^\d+_', '', filename)
    return filename


def parse_iso_date(date_str):
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def sync_checkout_reminders(user_id, bookings):
    """Create a one-time notification when an active booking's checkout is within 7 days."""
    today_date = date.today()
    created_any = False

    for b in bookings:
        if b.status in ['Completed', 'Rejected']:
            continue
        checkout_dt = parse_iso_date(b.check_out)
        if not checkout_dt:
            continue

        days_left = (checkout_dt - today_date).days
        if 0 <= days_left <= 7:
            room_num = b.room.room_number if b.room else str(b.room_id)
            tag = f"[Checkout #{b.id} - {b.check_out}]"
            already_notified = Notification.query.filter_by(user_id=user_id).filter(
                Notification.message.contains(tag)
            ).first()

            if not already_notified:
                when_text = (
                    "today"
                    if days_left == 0
                    else f"in {days_left} day{'s' if days_left != 1 else ''}"
                )
                msg = (
                    f"{tag} Reminder: Your checkout for Room {room_num} "
                    f"is {when_text} ({b.check_out})."
                )
                db.session.add(Notification(user_id=user_id, message=msg))
                created_any = True

    if created_any:
        db.session.commit()


# --- DATABASE MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'guest' or 'manager'
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
        occupied_count = (
            Booking.query.filter_by(room_id=self.id)
            .filter(Booking.status == 'Confirmed')
            .count()
        )
        return max(0, self.capacity - occupied_count)


class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey('room.id'), nullable=False)
    check_in = db.Column(db.String(10), nullable=False)
    check_out = db.Column(db.String(10), nullable=False)
    status = db.Column(db.String(30), default='Pending Approval')
    payment_status = db.Column(db.String(20), default='Unpaid')
    payment_method = db.Column(db.String(30), nullable=True)
    payment_reference = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def days_until_checkout(self):
        checkout_dt = parse_iso_date(self.check_out)
        if not checkout_dt:
            return None
        return (checkout_dt - date.today()).days


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
        if not User.query.filter_by(role='manager').first():
            mgr = User(
                name="Head Manager",
                email="manager@hostel.com",
                password_hash=generate_password_hash("admin123"),
                role="manager",
            )
            db.session.add(mgr)
            db.session.commit()

        table_columns = db.session.execute(db.text("PRAGMA table_info(booking)")).fetchall()
        column_names = {column[1] for column in table_columns}
        if 'payment_method' not in column_names:
            db.session.execute(db.text("ALTER TABLE booking ADD COLUMN payment_method VARCHAR(30)"))
        if 'payment_reference' not in column_names:
            db.session.execute(db.text("ALTER TABLE booking ADD COLUMN payment_reference VARCHAR(100)"))
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
                hashed_password = generate_password_hash(password)
                new_user = User(
                    name=name, email=email, password_hash=hashed_password, role='guest'
                )
                db.session.add(new_user)
                db.session.commit()
                flash('Guest registration successful! Please log in.', 'success')

        elif action == 'manager_register':
            name = request.form.get('name')
            email = request.form.get('email')
            password = request.form.get('password')
            registration_code = request.form.get('registration_code', '')

            if not hmac.compare_digest(registration_code, app.config['MANAGER_REGISTRATION_CODE']):
                flash('Invalid manager registration code.', 'danger')
            elif User.query.filter_by(email=email).first():
                flash('Email address already registered.', 'danger')
            else:
                hashed_password = generate_password_hash(password)
                new_mgr = User(
                    name=name, email=email, password_hash=hashed_password, role='manager'
                )
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
    my_bookings = (
        Booking.query.filter_by(user_id=session['user_id'])
        .order_by(Booking.created_at.desc())
        .all()
    )

    sync_checkout_reminders(session['user_id'], my_bookings)

    checkout_reminders = []
    for b in my_bookings:
        if b.status in ['Completed', 'Rejected']:
            continue
        days_left = b.days_until_checkout
        if days_left is not None and days_left <= 7:
            checkout_reminders.append({'booking': b, 'days_left': days_left})

    notifications = (
        Notification.query.filter_by(user_id=session['user_id'], is_read=False)
        .order_by(Notification.created_at.desc())
        .all()
    )
    return render_template(
        'guest_dashboard.html',
        rooms=rooms,
        bookings=my_bookings,
        notifications=notifications,
        checkout_reminders=checkout_reminders,
    )


@app.route('/book', methods=['POST'])
@login_required
def book_room():
    room_id = request.form.get('room_id')
    check_in = request.form.get('check_in')
    check_out = request.form.get('check_out')

    in_dt = parse_iso_date(check_in)
    out_dt = parse_iso_date(check_out)
    if not in_dt or not out_dt or out_dt <= in_dt:
        flash('Please select a valid check-in and check-out date range.', 'danger')
        return redirect(url_for('guest_dashboard'))

    active_booking = (
        Booking.query.filter_by(user_id=session['user_id'])
        .filter(Booking.status.in_(['Pending Approval', 'Confirmed']))
        .first()
    )
    if active_booking:
        flash(
            'You already have an active or pending booking. Complete payment or checkout before booking another room.',
            'warning',
        )
        return redirect(url_for('payment_page', booking_id=active_booking.id))

    room = Room.query.get(room_id)
    if room and room.available_beds() > 0:
        booking = Booking(
            user_id=session['user_id'],
            room_id=room.id,
            check_in=check_in,
            check_out=check_out,
            status='Pending Approval',
            payment_status='Unpaid',
        )
        db.session.add(booking)
        db.session.commit()
        flash('Booking request submitted! Please complete your payment below.', 'info')
        return redirect(url_for('payment_page', booking_id=booking.id))
    else:
        flash('Sorry, this room is fully booked.', 'danger')
    return redirect(url_for('guest_dashboard'))


@app.route('/payment/<int:booking_id>', methods=['GET', 'POST'])
@login_required
def payment_page(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if booking.user_id != session['user_id']:
        flash('Unauthorized access to this booking payment.', 'danger')
        return redirect(url_for('guest_dashboard'))

    if request.method == 'POST':
        payment_method = request.form.get('payment_method', 'M-Pesa')

        # Generate a unique payment reference if not already set
        if not booking.payment_reference:
            booking.payment_reference = f"REF{booking.id}-{uuid.uuid4().hex[:6].upper()}"

        booking.payment_status = 'Paid'
        booking.payment_method = payment_method
        notif = Notification(
            user_id=booking.user_id,
            message=(
                f"Payment of Ksh {booking.room.price_per_semester:,.2f} via "
                f"{payment_method} (Ref: {booking.payment_reference}) recorded for Room {booking.room.room_number}."
            ),
        )
        db.session.add(notif)
        db.session.commit()

        flash('Payment recorded! You can now view or download your receipt.', 'success')
        return redirect(url_for('booking_receipt', booking_id=booking.id))

    return render_template('payment.html', booking=booking)


@app.route('/booking/<int:booking_id>/receipt')
@login_required
def booking_receipt(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if booking.user_id != session['user_id']:
        flash('Unauthorized access to this receipt.', 'danger')
        return redirect(url_for('guest_dashboard'))

    if request.args.get('download') == 'txt':
        lines = [
            "==========================================",
            "        HOSTEL ACCOMMODATION RECEIPT       ",
            "==========================================",
            f"Receipt / Booking ID : #{booking.id}",
            f"Issued Date          : {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
            f"Student Name         : {booking.guest.name}",
            f"Student Email        : {booking.guest.email}",
            "------------------------------------------",
            f"Room Number          : {booking.room.room_number}",
            f"Room Type            : {booking.room.room_type}",
            f"Check-In Date        : {booking.check_in}",
            f"Check-Out Date       : {booking.check_out}",
            f"Amount               : Ksh {booking.room.price_per_semester:,.2f}",
            f"Booking Status       : {booking.status}",
            f"Payment Status       : {booking.payment_status}",
            f"Payment Reference    : {booking.payment_reference}",
            "==========================================",
        ]
        content = "\n".join(lines)
        return Response(
            content,
            mimetype="text/plain",
            headers={
                "Content-Disposition": f"attachment;filename=receipt_booking_{booking.id}.txt"
            },
        )

    return render_template('receipt.html', booking=booking, generated_at=datetime.utcnow())


# --- MANAGER DASHBOARD ---
@app.route('/dashboard/manager')
@manager_required
def manager_dashboard():
    rooms = Room.query.all()
    all_bookings = Booking.query.order_by(Booking.created_at.desc()).all()

    total_revenue = sum(
        [
            b.room.price_per_semester
            for b in all_bookings
            if b.status in ['Confirmed', 'Completed'] and b.room is not None
        ]
    )
    return render_template(
        'manager_dashboard.html', rooms=rooms, bookings=all_bookings, revenue=total_revenue
    )


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
                filename = clean_filename(file.filename)
                unique_filename = f"{int(datetime.utcnow().timestamp())}_{filename}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
                image_url = f"/static/uploads/{unique_filename}"
            else:
                flash(
                    'Invalid image file type. Allowed: png, jpg, jpeg, gif, webp',
                    'danger',
                )
                return redirect(url_for('manager_dashboard'))

    if Room.query.filter_by(room_number=room_number).first():
        flash('Room number already exists.', 'danger')
    else:
        new_room = Room(
            room_number=room_number,
            room_type=room_type,
            price_per_semester=float(price),
            capacity=capacity,
            image_url=image_url,
        )
        db.session.add(new_room)
        db.session.commit()
        flash('New room added successfully!', 'success')
    return redirect(url_for('manager_dashboard'))


@app.route('/manager/booking/<int:booking_id>/<action>', methods=['POST'])
@manager_required
def update_booking_status(booking_id, action):
    booking = Booking.query.get_or_404(booking_id)
    room = db.session.get(Room, booking.room_id)
    if action == 'confirm':
        if room.available_beds() > 0:
            booking.status = 'Confirmed'
            booking.payment_status = 'Paid'
            notif = Notification(
                user_id=booking.user_id,
                message=(
                    f"Your booking for Room {room.room_number} has been confirmed "
                    f"and marked as paid."
                ),
            )
            db.session.add(notif)
            flash('Booking confirmed and payment marked as Paid.', 'success')
        else:
            flash('Cannot confirm: Room is at full capacity.', 'danger')
    elif action == 'reject':
        booking.status = 'Rejected'
        booking.payment_status = 'Unpaid'
        notif = Notification(
            user_id=booking.user_id,
            message=f"Your booking for Room {room.room_number} was rejected.",
        )
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
    new_payment_status = request.form.get('payment_status')
    if new_check_in:
        booking.check_in = new_check_in
    if new_check_out:
        booking.check_out = new_check_out
    if new_status in ['Pending Approval', 'Confirmed', 'Rejected', 'Completed']:
        booking.status = new_status
    if new_payment_status in ['Paid', 'Unpaid']:
        booking.payment_status = new_payment_status
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
    room.price_per_semester = float(
        request.form.get('price_per_semester', room.price_per_semester)
    )
    room.capacity = int(request.form.get('capacity', room.capacity))
    if 'room_image' in request.files:
        file = request.files['room_image']
        if file and file.filename != '':
            if allowed_file(file.filename):
                filename = clean_filename(file.filename)
                unique_filename = f"{int(datetime.utcnow().timestamp())}_{filename}"
                new_image_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
                if room.image_url and 'default_room.jpg' not in room.image_url:
                    old_image_path = os.path.join(app.root_path, room.image_url.lstrip('/'))
                    if os.path.exists(old_image_path):
                        try:
                            os.remove(old_image_path)
                        except Exception:
                            pass
                file.save(new_image_path)
                room.image_url = f"/static/uploads/{unique_filename}"
            else:
                flash(
                    'Invalid image file type. Room updated without changing image.',
                    'warning',
                )
    db.session.commit()
    flash(f'Room {room.room_number} updated successfully!', 'success')
    return redirect(url_for('manager_dashboard'))


@app.route('/manager/room/<int:room_id>/delete', methods=['POST'])
@manager_required
def delete_room(room_id):
    room = Room.query.get_or_404(room_id)
    if room.image_url and 'default_room.jpg' not in room.image_url:
        image_path = os.path.join(app.root_path, room.image_url.lstrip('/'))
        if os.path.exists(image_path):
            try:
                os.remove(image_path)
            except Exception:
                pass
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
    writer.writerow(
        [
            'Booking ID',
            'Guest Name',
            'Guest Email',
            'Room Number',
            'Room Type',
            'Check-In',
            'Check-Out',
            'Status',
            'Payment Status',
            'Date Requested',
        ]
    )
    for b in bookings:
        writer.writerow(
            [
                b.id,
                b.guest.name,
                b.guest.email,
                b.room.room_number,
                b.room.room_type,
                b.check_in,
                b.check_out,
                b.status,
                b.payment_status,
                b.created_at.strftime('%Y-%m-%d %H:%M'),
            ]
        )
    output.seek(0)
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=hostel_report.csv"},
    )


@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if 'user_id' not in session:
        flash('Please log in first.', 'warning')
        return redirect(url_for('login'))
    user = User.query.get_or_404(session['user_id'])
    if request.method == 'POST':
        user.name = request.form.get('name', user.name)
        new_email = request.form.get('email')
        existing_user = User.query.filter_by(email=new_email).first()
        if existing_user and existing_user.id != user.id:
            flash('Email address is already in use by another account.', 'danger')
        else:
            user.email = new_email
            new_password = request.form.get('password')
            if new_password:
                user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            session['name'] = user.name
            flash('Profile updated successfully!', 'success')
        return redirect(url_for('profile'))
    return render_template('profile.html', user=user)


# --- RUN APPLICATION ---
if __name__ == '__main__':
    init_db()
    is_local = os.environ.get("PORT") is None
    if is_local and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=is_local)

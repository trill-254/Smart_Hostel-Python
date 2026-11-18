
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session, Response
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime
from functools import wraps
from sqlalchemy.exc import SQLAlchemyError
from io import StringIO
import csv
import os
import re
import threading
import webbrowser

# --------------------------------------------------
# APPLICATION CONFIGURATION
# --------------------------------------------------

app = Flask(__name__)

app.config['SECRET_KEY'] = os.environ.get(
    'SECRET_KEY',
    'local-development-secret-change-this'
)

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Store SQLite database inside the Flask instance folder.
os.makedirs(app.instance_path, exist_ok=True)

DATABASE_PATH = os.path.join(app.instance_path, 'hostel.db')

app.config['SQLALCHEMY_DATABASE_URI'] = (
    'sqlite:///' + DATABASE_PATH
)

# Image upload configuration
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

db = SQLAlchemy(app)


# --------------------------------------------------
# IMAGE HELPERS
# --------------------------------------------------

def allowed_file(filename):
    return (
        '.' in filename
        and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def clean_filename(filename):
    filename = secure_filename(filename)
    filename = re.sub(r'^\d+_', '', filename)
    return filename


# --------------------------------------------------
# DATABASE MODELS
# --------------------------------------------------

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)

    bookings = db.relationship(
        'Booking', backref='guest', lazy=True
    )

    notifications = db.relationship(
        'Notification', backref='user', lazy=True
    )


class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room_number = db.Column(db.String(10), unique=True, nullable=False)
    room_type = db.Column(db.String(50), nullable=False)
    price_per_semester = db.Column(db.Float, nullable=False)
    capacity = db.Column(db.Integer, default=1)
    image_url = db.Column(db.String(255), nullable=True)

    bookings = db.relationship(
        'Booking', backref='room', lazy=True
    )

    def available_beds(self):
        occupied_count = Booking.query.filter_by(
            room_id=self.id,
            status='Confirmed'
        ).count()

        return max(0, self.capacity - occupied_count)


class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
        db.Integer,
        db.ForeignKey('user.id'),
        nullable=False
    )

    room_id = db.Column(
        db.Integer,
        db.ForeignKey('room.id'),
        nullable=False
    )

    check_in = db.Column(db.String(10), nullable=False)
    check_out = db.Column(db.String(10), nullable=False)

    status = db.Column(
        db.String(30),
        default='Pending Approval'
    )

    payment_status = db.Column(
        db.String(20),
        default='Unpaid'
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
        db.Integer,
        db.ForeignKey('user.id'),
        nullable=False
    )

    message = db.Column(db.String(255), nullable=False)

    is_read = db.Column(db.Boolean, default=False)

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )


# --------------------------------------------------
# DATABASE INITIALIZATION
# --------------------------------------------------

def init_db():
    with app.app_context():
        try:
            db.create_all()

            # Create the initial manager account if none exists.
            if not User.query.filter_by(role='manager').first():

                manager_email = os.environ.get(
                    'INITIAL_MANAGER_EMAIL',
                    'manager@hostel.com'
                )

                manager_password = os.environ.get(
                    'INITIAL_MANAGER_PASSWORD',
                    'admin123'
                )

                manager = User(
                    name='Head Manager',
                    email=manager_email,
                    password_hash=generate_password_hash(
                        manager_password
                    ),
                    role='manager'
                )

                db.session.add(manager)
                db.session.commit()

                print('Initial manager account created.')

            print('Database initialized successfully.')

        except SQLAlchemyError as error:
            db.session.rollback()
            print(f'Database initialization error: {error}')
            raise


# IMPORTANT:
# This runs when Gunicorn imports app.py on Render.
# Do not move it inside the __main__ block.

init_db()


# --------------------------------------------------
# AUTHENTICATION DECORATORS
# --------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):

        if (
            'user_id' not in session
            or session.get('role') != 'guest'
        ):
            flash('Please log in as a guest.', 'warning')
            return redirect(url_for('login'))

        return f(*args, **kwargs)

    return decorated_function


def manager_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):

        if (
            'user_id' not in session
            or session.get('role') != 'manager'
        ):
            flash(
                'Access denied. Manager login required.',
                'danger'
            )
            return redirect(url_for('login'))

        return f(*args, **kwargs)

    return decorated_function


# --------------------------------------------------
# HOME PAGE
# --------------------------------------------------

@app.route('/')
def index():

    if 'user_id' in session:

        if session.get('role') == 'manager':
            return redirect(url_for('manager_dashboard'))

        if session.get('role') == 'guest':
            return redirect(url_for('guest_dashboard'))

    return redirect(url_for('login'))


# --------------------------------------------------
# LOGIN AND REGISTRATION
# --------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
def login():

    if request.method == 'POST':

        action = request.form.get('action')

        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''

        try:

            # GUEST LOGIN
            if action == 'guest_login':

                user = User.query.filter_by(
                    email=email,
                    role='guest'
                ).first()

                if user and check_password_hash(
                    user.password_hash, password
                ):

                    session.clear()
                    session['user_id'] = user.id
                    session['name'] = user.name
                    session['role'] = user.role

                    flash(
                        f'Welcome back, {user.name}!',
                        'success'
                    )

                    return redirect(url_for('guest_dashboard'))

                flash(
                    'Invalid guest email or password.',
                    'danger'
                )

            # GUEST REGISTRATION
            elif action == 'guest_register':

                name = (request.form.get('name') or '').strip()

                if not name or not email or not password:
                    flash(
                        'Please complete all registration fields.',
                        'danger'
                    )

                elif len(password) < 6:
                    flash(
                        'Password must contain at least 6 characters.',
                        'danger'
                    )

                elif User.query.filter_by(email=email).first():

                    flash(
                        'Email address already registered.',
                        'danger'
                    )

                else:

                    new_user = User(
                        name=name,
                        email=email,
                        password_hash=generate_password_hash(password),
                        role='guest'
                    )

                    db.session.add(new_user)
                    db.session.commit()

                    flash(
                        'Guest registration successful! Please log in.',
                        'success'
                    )

            # MANAGER REGISTRATION
            elif action == 'manager_register':

                # Manager registration requires a secret code.
                registration_code = request.form.get(
                    'registration_code', ''
                )

                expected_code = os.environ.get(
                    'MANAGER_REGISTRATION_CODE'
                )

                name = (request.form.get('name') or '').strip()

                if not expected_code:
                    flash(
                        'Manager registration is disabled. '
                        'Contact the system administrator.',
                        'danger'
                    )

                elif registration_code != expected_code:
                    flash(
                        'Invalid manager registration code.',
                        'danger'
                    )

                elif not name or not email or not password:
                    flash(
                        'Please complete all registration fields.',
                        'danger'
                    )

                elif len(password) < 6:
                    flash(
                        'Password must contain at least 6 characters.',
                        'danger'
                    )

                elif User.query.filter_by(email=email).first():

                    flash(
                        'Email address already registered.',
                        'danger'
                    )

                else:

                    new_manager = User(
                        name=name,
                        email=email,
                        password_hash=generate_password_hash(password),
                        role='manager'
                    )

                    db.session.add(new_manager)
                    db.session.commit()

                    flash(
                        'Manager registration successful! Please log in.',
                        'success'
                    )

            # MANAGER LOGIN
            elif action == 'manager_login':

                user = User.query.filter_by(
                    email=email,
                    role='manager'
                ).first()

                if user and check_password_hash(
                    user.password_hash, password
                ):

                    session.clear()
                    session['user_id'] = user.id
                    session['name'] = user.name
                    session['role'] = user.role

                    flash(
                        f'Welcome back, Manager {user.name}!',
                        'success'
                    )

                    return redirect(url_for('manager_dashboard'))

                flash(
                    'Invalid manager credentials.',
                    'danger'
                )

            else:
                flash(
                    'Invalid login request.',
                    'danger'
                )

        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception('Database error during login or registration.')

            flash(
                'A database error occurred. Please try again.',
                'danger'
            )

    return render_template('login.html')


# --------------------------------------------------
# LOGOUT
# --------------------------------------------------

@app.route('/logout')
def logout():

    session.clear()

    flash('You have been logged out.', 'info')

    return redirect(url_for('login'))


# --------------------------------------------------
# GUEST DASHBOARD
# --------------------------------------------------

@app.route('/dashboard/guest')
@login_required
def guest_dashboard():

    rooms = Room.query.all()

    my_bookings = Booking.query.filter_by(
        user_id=session['user_id']
    ).order_by(
        Booking.created_at.desc()
    ).all()

    notifications = Notification.query.filter_by(
        user_id=session['user_id'],
        is_read=False
    ).all()

    return render_template(
        'guest_dashboard.html',
        rooms=rooms,
        bookings=my_bookings,
        notifications=notifications
    )


# --------------------------------------------------
# ROOM BOOKING
# --------------------------------------------------

@app.route('/book', methods=['POST'])
@login_required
def book_room():

    room_id = request.form.get('room_id')
    check_in = request.form.get('check_in')
    check_out = request.form.get('check_out')

    if not room_id or not check_in or not check_out:
        flash('Please provide all booking details.', 'danger')
        return redirect(url_for('guest_dashboard'))

    try:
        room_id = int(room_id)

    except ValueError:
        flash('Invalid room selected.', 'danger')
        return redirect(url_for('guest_dashboard'))

    active_booking = Booking.query.filter_by(
        user_id=session['user_id']
    ).filter(
        Booking.status.in_(['Pending Approval', 'Confirmed'])
    ).first()

    if active_booking:

        flash(
            'You already have an active or pending booking. '
            'You cannot book more than one room at a time.',
            'danger'
        )

        return redirect(url_for('guest_dashboard'))

    room = db.session.get(Room, room_id)

    if room and room.available_beds() > 0:

        booking = Booking(
            user_id=session['user_id'],
            room_id=room.id,
            check_in=check_in,
            check_out=check_out,
            status='Pending Approval',
            payment_status='Unpaid'
        )

        db.session.add(booking)
        db.session.commit()

        flash(
            'Booking request submitted! Awaiting manager confirmation.',
            'success'
        )

    else:

        flash(
            'Sorry, this room is fully booked or unavailable.',
            'danger'
        )

    return redirect(url_for('guest_dashboard'))


# --------------------------------------------------
# MANAGER DASHBOARD
# --------------------------------------------------

@app.route('/dashboard/manager')
@manager_required
def manager_dashboard():

    rooms = Room.query.all()

    all_bookings = Booking.query.order_by(
        Booking.created_at.desc()
    ).all()

    total_revenue = sum(
        booking.room.price_per_semester
        for booking in all_bookings
        if booking.status in ['Confirmed', 'Completed']
        and booking.room is not None
    )

    return render_template(
        'manager_dashboard.html',
        rooms=rooms,
        bookings=all_bookings,
        revenue=total_revenue
    )


# --------------------------------------------------
# ADD ROOM
# --------------------------------------------------

@app.route('/manager/room/add', methods=['POST'])
@manager_required
def add_room():

    room_number = (
        request.form.get('room_number') or ''
    ).strip()

    room_type = (
        request.form.get('room_type') or ''
    ).strip()

    try:

        price = float(request.form.get('price_per_semester', 0))
        capacity = int(request.form.get('capacity', 1))

        if not room_number or not room_type:
            raise ValueError

        if price < 0 or capacity < 1:
            raise ValueError

    except (ValueError, TypeError):

        flash(
            'Enter a valid room number, room type, price and capacity.',
            'danger'
        )

        return redirect(url_for('manager_dashboard'))

    image_url = '/static/uploads/default_room.jpg'

    if 'room_image' in request.files:

        file = request.files['room_image']

        if file and file.filename:

            if allowed_file(file.filename):

                filename = clean_filename(file.filename)

                unique_filename = (
                    f"{int(datetime.utcnow().timestamp())}_{filename}"
                )

                file.save(
                    os.path.join(
                        app.config['UPLOAD_FOLDER'],
                        unique_filename
                    )
                )

                image_url = f'/static/uploads/{unique_filename}'

            else:

                flash(
                    'Invalid image file type. '
                    'Allowed: png, jpg, jpeg, gif, webp.',
                    'danger'
                )

                return redirect(url_for('manager_dashboard'))

    if Room.query.filter_by(room_number=room_number).first():

        flash('Room number already exists.', 'danger')

    else:

        new_room = Room(
            room_number=room_number,
            room_type=room_type,
            price_per_semester=price,
            capacity=capacity,
            image_url=image_url
        )

        db.session.add(new_room)
        db.session.commit()

        flash('New room added successfully!', 'success')

    return redirect(url_for('manager_dashboard'))


# --------------------------------------------------
# UPDATE BOOKING STATUS
# --------------------------------------------------

@app.route('/manager/booking/<int:booking_id>/<action>', methods=['POST'])
@manager_required
def update_booking_status(booking_id, action):

    booking = Booking.query.get_or_404(booking_id)
    room = db.session.get(Room, booking.room_id)

    if room is None:
        flash('The room associated with this booking was not found.', 'danger')
        return redirect(url_for('manager_dashboard'))

    if action == 'confirm':

        if booking.status == 'Confirmed':
            flash('This booking is already confirmed.', 'info')

        elif room.available_beds() > 0:

            booking.status = 'Confirmed'
            booking.payment_status = 'Paid'

            notification = Notification(
                user_id=booking.user_id,
                message=(
                    f'Your booking for Room {room.room_number} '
                    'has been confirmed and marked as paid.'
                )
            )

            db.session.add(notification)

            flash(
                'Booking confirmed and payment marked as Paid.',
                'success'
            )

        else:

            flash(
                'Cannot confirm: Room is at full capacity.',
                'danger'
            )

    elif action == 'reject':

        booking.status = 'Rejected'
        booking.payment_status = 'Unpaid'

        notification = Notification(
            user_id=booking.user_id,
            message=(
                f'Your booking for Room {room.room_number} was rejected.'
            )
        )

        db.session.add(notification)

        flash('Booking rejected.', 'info')

    elif action == 'checkout':

        booking.status = 'Completed'

        flash('Guest checked out successfully.', 'info')

    else:

        flash('Invalid booking action.', 'danger')

    db.session.commit()

    return redirect(url_for('manager_dashboard'))


# --------------------------------------------------
# EDIT BOOKING
# --------------------------------------------------

@app.route('/manager/booking/<int:booking_id>/edit', methods=['POST'])
@manager_required
def edit_booking(booking_id):

    booking = Booking.query.get_or_404(booking_id)

    new_check_in = (
        request.form.get('new_check_in')
        or request.form.get('check_in')
    )

    new_check_out = (
        request.form.get('new_check_out')
        or request.form.get('check_out')
    )

    new_status = request.form.get('status')
    new_payment_status = request.form.get('payment_status')

    if new_check_in:
        booking.check_in = new_check_in

    if new_check_out:
        booking.check_out = new_check_out

    if new_status in [
        'Pending Approval',
        'Confirmed',
        'Rejected',
        'Completed'
    ]:
        booking.status = new_status

    if new_payment_status in ['Paid', 'Unpaid']:
        booking.payment_status = new_payment_status

    db.session.commit()

    flash('Booking updated successfully.', 'success')

    return redirect(url_for('manager_dashboard'))


# --------------------------------------------------
# DELETE BOOKING
# --------------------------------------------------

@app.route('/manager/booking/<int:booking_id>/delete', methods=['POST'])
@manager_required
def delete_booking(booking_id):

    booking = Booking.query.get_or_404(booking_id)

    db.session.delete(booking)
    db.session.commit()

    flash('Booking record deleted successfully.', 'info')

    return redirect(url_for('manager_dashboard'))


# --------------------------------------------------
# EDIT ROOM
# --------------------------------------------------

@app.route('/manager/room/<int:room_id>/edit', methods=['POST'])
@manager_required
def edit_room(room_id):

    room = Room.query.get_or_404(room_id)

    room_number = (
        request.form.get('room_number') or room.room_number
    ).strip()

    room_type = (
        request.form.get('room_type') or room.room_type
    ).strip()

    try:

        price = float(
            request.form.get(
                'price_per_semester',
                room.price_per_semester
            )
        )

        capacity = int(
            request.form.get('capacity', room.capacity)
        )

        if not room_number or not room_type:
            raise ValueError

        if price < 0 or capacity < 1:
            raise ValueError

    except (ValueError, TypeError):

        flash('Please enter valid room details.', 'danger')

        return redirect(url_for('manager_dashboard'))

    existing_room = Room.query.filter_by(
        room_number=room_number
    ).first()

    if existing_room and existing_room.id != room.id:

        flash('Another room already uses that room number.', 'danger')

        return redirect(url_for('manager_dashboard'))

    room.room_number = room_number
    room.room_type = room_type
    room.price_per_semester = price
    room.capacity = capacity

    if 'room_image' in request.files:

        file = request.files['room_image']

        if file and file.filename:

            if allowed_file(file.filename):

                filename = clean_filename(file.filename)

                unique_filename = (
                    f"{int(datetime.utcnow().timestamp())}_{filename}"
                )

                new_image_path = os.path.join(
                    app.config['UPLOAD_FOLDER'],
                    unique_filename
                )

                file.save(new_image_path)

                if (
                    room.image_url
                    and 'default_room.jpg' not in room.image_url
                ):

                    old_image_path = os.path.join(
                        app.root_path,
                        room.image_url.lstrip('/')
                    )

                    if os.path.exists(old_image_path):
                        try:
                            os.remove(old_image_path)
                        except OSError:
                            app.logger.exception(
                                'Could not remove old room image.'
                            )

                room.image_url = (
                    f'/static/uploads/{unique_filename}'
                )

            else:

                flash(
                    'Invalid image file type. '
                    'Room updated without changing its image.',
                    'warning'
                )

    db.session.commit()

    flash(
        f'Room {room.room_number} updated successfully!',
        'success'
    )

    return redirect(url_for('manager_dashboard'))


# --------------------------------------------------
# DELETE ROOM
# --------------------------------------------------

@app.route('/manager/room/<int:room_id>/delete', methods=['POST'])
@manager_required
def delete_room(room_id):

    room = Room.query.get_or_404(room_id)

    # Do not delete a room that has booking records.
    if Booking.query.filter_by(room_id=room.id).first():

        flash(
            'This room has booking records. '
            'Remove its bookings before deleting the room.',
            'danger'
        )

        return redirect(url_for('manager_dashboard'))

    if (
        room.image_url
        and 'default_room.jpg' not in room.image_url
    ):

        image_path = os.path.join(
            app.root_path,
            room.image_url.lstrip('/')
        )

        if os.path.exists(image_path):
            try:
                os.remove(image_path)
            except OSError:
                app.logger.exception('Could not remove room image.')

    db.session.delete(room)
    db.session.commit()

    flash('Room removed successfully from catalog.', 'info')

    return redirect(url_for('manager_dashboard'))


# --------------------------------------------------
# EXPORT CSV REPORT
# --------------------------------------------------

@app.route('/manager/export/csv')
@manager_required
def export_csv_report():

    bookings = Booking.query.all()

    output = StringIO()
    writer = csv.writer(output)

    writer.writerow([
        'Booking ID',
        'Guest Name',
        'Guest Email',
        'Room Number',
        'Room Type',
        'Check-In',
        'Check-Out',
        'Status',
        'Payment Status',
        'Date Requested'
    ])

    for booking in bookings:

        guest_name = (
            booking.guest.name
            if booking.guest else 'Unknown'
        )

        guest_email = (
            booking.guest.email
            if booking.guest else 'Unknown'
        )

        room_number = (
            booking.room.room_number
            if booking.room else 'Unknown'
        )

        room_type = (
            booking.room.room_type
            if booking.room else 'Unknown'
        )

        created_at = (
            booking.created_at.strftime('%Y-%m-%d %H:%M')
            if booking.created_at else ''
        )

        writer.writerow([
            booking.id,
            guest_name,
            guest_email,
            room_number,
            room_type,
            booking.check_in,
            booking.check_out,
            booking.status,
            booking.payment_status,
            created_at
        ])

    output.seek(0)

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={
            'Content-Disposition':
            'attachment; filename=hostel_report.csv'
        }
    )


# --------------------------------------------------
# USER PROFILE
# --------------------------------------------------

@app.route('/profile', methods=['GET', 'POST'])
def profile():

    if 'user_id' not in session:

        flash('Please log in first.', 'warning')

        return redirect(url_for('login'))

    user = db.session.get(User, session['user_id'])

    if user is None:

        session.clear()

        flash('Account not found. Please log in again.', 'warning')

        return redirect(url_for('login'))

    if request.method == 'POST':

        new_name = (
            request.form.get('name') or user.name
        ).strip()

        new_email = (
            request.form.get('email') or user.email
        ).strip().lower()

        existing_user = User.query.filter_by(
            email=new_email
        ).first()

        if existing_user and existing_user.id != user.id:

            flash(
                'Email address is already in use by another account.',
                'danger'
            )

        else:

            user.name = new_name
            user.email = new_email

            new_password = request.form.get('password')

            if new_password:

                if len(new_password) < 6:

                    flash(
                        'Password must contain at least 6 characters.',
                        'danger'
                    )

                    return redirect(url_for('profile'))

                user.password_hash = generate_password_hash(
                    new_password
                )

            db.session.commit()

            session['name'] = user.name

            flash(
                'Profile updated successfully!',
                'success'
            )

        return redirect(url_for('profile'))

    return render_template('profile.html', user=user)


# --------------------------------------------------
# APPLICATION START
# --------------------------------------------------

if __name__ == '__main__':

    is_local = os.environ.get('PORT') is None

    if is_local and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':

        threading.Timer(
            1.2,
            lambda: webbrowser.open('http://127.0.0.1:5000')
        ).start()

    port = int(os.environ.get('PORT', 5000))

    app.run(
        host='0.0.0.0',
        port=port,
        debug=is_local
    )

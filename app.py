import os
import uuid
import qrcode
import io
import base64
import json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, abort
from flask_sqlalchemy import SQLAlchemy
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
from square import Square
from square.environment import SquareEnvironment

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-in-prod')

# ── DATABASE ──
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    f'sqlite:///{os.path.join(basedir, "osa_workshop.db")}'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ── MODELS ──
class Registration(db.Model):
    id            = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    # Dancer info
    studio_name   = db.Column(db.String(200))
    first_name    = db.Column(db.String(100))
    last_name     = db.Column(db.String(100))
    gender        = db.Column(db.String(50))
    birth_date    = db.Column(db.String(20))
    email         = db.Column(db.String(200))
    phone         = db.Column(db.String(30))
    mobile        = db.Column(db.String(30))
    # Registration
    is_title      = db.Column(db.Boolean, default=False)
    routine_name  = db.Column(db.String(200))
    reg_type      = db.Column(db.String(100))   # workshop / opening / both
    tshirt_size   = db.Column(db.String(30))
    # Payment
    amount        = db.Column(db.Integer, default=0)  # in cents
    payment_id    = db.Column(db.String(200))
    payment_status= db.Column(db.String(50), default='pending')
    # Check-in
    checked_in    = db.Column(db.Boolean, default=False)
    checkin_time  = db.Column(db.DateTime)
    checkin_by    = db.Column(db.String(100))

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def amount_display(self):
        if self.is_title:
            return '$0.00 — Covered by Title Registration'
        return f'${self.amount / 100:.2f}'

    @property
    def reg_label(self):
        if self.is_title:
            return 'Title Registrant'
        labels = {
            'workshop': 'Workshop Only',
            'opening':  'Opening Number Only',
            'both':     'Both Workshop & Opening Number'
        }
        return labels.get(self.reg_type, self.reg_type or 'Workshop')

    def to_dict(self):
        return {
            'id':            self.id,
            'full_name':     self.full_name,
            'studio_name':   self.studio_name,
            'email':         self.email,
            'phone':         self.phone,
            'gender':        self.gender,
            'birth_date':    self.birth_date,
            'is_title':      self.is_title,
            'routine_name':  self.routine_name,
            'reg_type':      self.reg_type,
            'reg_label':     self.reg_label,
            'tshirt_size':   self.tshirt_size,
            'amount':        self.amount,
            'amount_display':self.amount_display,
            'payment_status':self.payment_status,
            'checked_in':    self.checked_in,
            'checkin_time':  self.checkin_time.strftime('%I:%M %p') if self.checkin_time else None,
            'checkin_date':  self.checkin_time.strftime('%m/%d/%Y') if self.checkin_time else None,
            'created_at':    self.created_at.strftime('%m/%d/%Y'),
        }

# ── PRICING ──
PRICES = {
    'workshop': 7500,   # $75.00
    'opening':  15000,  # $150.00
    'both':     22500,  # $225.00
}

# ── QR CODE HELPER ──
def generate_qr_base64(data: str) -> str:
    qr = qrcode.QRCode(version=1, box_size=8, border=3,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='#1E3A0F', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode()

def generate_qr_bytes(data: str) -> bytes:
    qr = qrcode.QRCode(version=1, box_size=10, border=4,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='#1E3A0F', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()

# ── EMAIL HELPER ──
def send_confirmation_email(reg: Registration):
    sg_key = os.environ.get('SENDGRID_API_KEY')
    if not sg_key:
        print('No SendGrid key — skipping email')
        return

    base_url  = os.environ.get('BASE_URL', 'http://localhost:5000')
    qr_data   = f'{base_url}/confirm/{reg.id}'
    qr_bytes  = generate_qr_bytes(qr_data)
    qr_b64    = base64.b64encode(qr_bytes).decode()
    qr_inline = generate_qr_base64(qr_data)

    base_url = os.environ.get('BASE_URL', 'http://localhost:5000')
    confirmation_url = f'{base_url}/confirm/{reg.id}'
    html = render_template('email_confirmation.html',
                           reg=reg,
                           qr_inline=qr_inline,
                           confirmation_url=confirmation_url)

    message = Mail(
        from_email=('osa@onstageamerica.com', 'On Stage America'),
        to_emails=reg.email,
        subject='You\'re Registered! — On Stage America Workshop',
        html_content=html
    )

    # Attach QR as inline image
    attachment = Attachment(
        FileContent(qr_b64),
        FileName('checkin_qr.png'),
        FileType('image/png'),
        Disposition('inline'),
    )
    attachment.content_id = 'checkin_qr'
    message.add_attachment(attachment)

    try:
        sg = SendGridAPIClient(sg_key)
        sg.send(message)
        print(f'Email sent to {reg.email}')
    except Exception as e:
        print(f'Email error: {e}')

# ── SQUARE HELPER ──
def get_square_client():
    env = os.environ.get('SQUARE_ENV', 'sandbox')
    token = os.environ.get('SQUARE_ACCESS_TOKEN', '')
    environment = SquareEnvironment.PRODUCTION if env == 'production' else SquareEnvironment.SANDBOX
    return Square(token=token, environment=environment)

# ────────────────────────────────────────
# ROUTES
# ────────────────────────────────────────

# ── REGISTRATION FORM ──
@app.route('/')
@app.route('/register')
def register():
    sq_app_id  = os.environ.get('SQUARE_APP_ID', '')
    sq_env     = os.environ.get('SQUARE_ENV', 'sandbox')
    sq_location = os.environ.get('SQUARE_LOCATION_ID', '')
    return render_template('register.html',
                           sq_app_id=sq_app_id,
                           sq_env=sq_env,
                           sq_location=sq_location,
                           prices=PRICES)

# ── PROCESS REGISTRATION ──
@app.route('/register/submit', methods=['POST'])
def submit_registration():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data received'}), 400

    is_title = data.get('is_title') == 'yes'
    reg_type = data.get('reg_type', '')
    amount   = 0 if is_title else PRICES.get(reg_type, 0)

    # Create registration record
    reg = Registration(
        studio_name  = data.get('studio_name', '').strip(),
        first_name   = data.get('first_name', '').strip(),
        last_name    = data.get('last_name', '').strip(),
        gender       = data.get('gender', '').strip(),
        birth_date   = data.get('birth_date', '').strip(),
        email        = data.get('email', '').strip().lower(),
        phone        = data.get('phone', '').strip(),
        mobile       = data.get('mobile', '').strip(),
        is_title     = is_title,
        routine_name = data.get('routine_name', '').strip(),
        reg_type     = reg_type,
        tshirt_size  = data.get('tshirt_size', '').strip(),
        amount       = amount,
        payment_status = 'free' if is_title else 'pending',
    )
    db.session.add(reg)
    db.session.flush()  # get ID before payment

    # Process Square payment if not title
    if not is_title and amount > 0:
        source_id = data.get('payment_token')
        if not source_id:
            return jsonify({'error': 'No payment token'}), 400
        try:
            client = get_square_client()
            result = client.payments.create(
                source_id=source_id,
                idempotency_key=reg.id,
                amount_money={'amount': amount, 'currency': 'USD'},
                note=f'OSA Workshop — {reg.full_name} — {reg.reg_label}',
                buyer_email_address=reg.email,
            )
            reg.payment_id     = result.payment.id
            reg.payment_status = 'paid'
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    elif is_title:
        reg.payment_status = 'free'

    db.session.commit()

    # Send confirmation email
    try:
        send_confirmation_email(reg)
    except Exception as e:
        print(f'Email failed: {e}')

    return jsonify({'success': True, 'id': reg.id})

# ── CONFIRMATION PAGE ──
@app.route('/confirm/<reg_id>')
def confirm(reg_id):
    reg = db.get_or_404(Registration, reg_id)
    base_url  = os.environ.get('BASE_URL', 'http://localhost:5000')
    qr_data   = f'{base_url}/confirm/{reg.id}'
    qr_inline = generate_qr_base64(qr_data)
    return render_template('confirm.html', reg=reg, qr_inline=qr_inline)

# ── QR CODE IMAGE ENDPOINT ──
@app.route('/qr/<reg_id>.png')
def qr_image(reg_id):
    from flask import Response
    reg = db.get_or_404(Registration, reg_id)
    base_url = os.environ.get('BASE_URL', 'http://localhost:5000')
    qr_bytes = generate_qr_bytes(f'{base_url}/confirm/{reg.id}')
    return Response(qr_bytes, mimetype='image/png')

# ── CHECK-IN KIOSK ──
@app.route('/checkin/login', methods=['GET', 'POST'])
def checkin_login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['checkin'] = True
            return redirect(url_for('checkin'))
        error = 'Incorrect password'
    return render_template('checkin_login.html', error=error)

@app.route('/checkin/logout')
def checkin_logout():
    session.pop('checkin', None)
    return redirect(url_for('checkin_login'))

@app.route('/checkin')
def checkin():
    if not session.get('checkin'):
        return redirect(url_for('checkin_login'))
    return render_template('checkin.html')

@app.route('/checkin/lookup', methods=['POST'])
def checkin_lookup():
    if not session.get('checkin'):
        return jsonify({'error': 'Unauthorized'}), 401
    data  = request.get_json()
    raw   = (data.get('id') or '').strip()
    # Handle full confirmation URL from QR (e.g. https://app.com/confirm/<uuid>)
    if '/confirm/' in raw:
        raw = raw.split('/confirm/')[-1].strip('/')
    # Legacy CHECKIN:uuid format
    elif raw.upper().startswith('CHECKIN:'):
        raw = raw.split(':', 1)[1]
    reg = db.session.get(Registration, raw)
    if not reg:
        return jsonify({'error': 'No registration found for this ID'}), 404
    return jsonify({'registration': reg.to_dict()})

@app.route('/checkin/confirm', methods=['POST'])
def checkin_confirm():
    if not session.get('checkin'):
        return jsonify({'error': 'Unauthorized'}), 401
    data   = request.get_json()
    reg_id = data.get('id')
    reg    = db.session.get(Registration, reg_id)
    if not reg:
        return jsonify({'error': 'Registration not found'}), 404
    if reg.checked_in:
        return jsonify({
            'already': True,
            'checkin_time': reg.checkin_time.strftime('%I:%M %p'),
            'checkin_date': reg.checkin_time.strftime('%m/%d/%Y'),
        })
    reg.checked_in   = True
    reg.checkin_time = datetime.utcnow()
    reg.checkin_by   = data.get('staff', 'Staff')
    db.session.commit()
    return jsonify({'success': True, 'registration': reg.to_dict()})

@app.route('/checkin/undo', methods=['POST'])
def checkin_undo():
    if not session.get('checkin'):
        return jsonify({'error': 'Unauthorized'}), 401
    data   = request.get_json()
    reg_id = data.get('id')
    reg    = db.session.get(Registration, reg_id)
    if not reg:
        return jsonify({'error': 'Not found'}), 404
    reg.checked_in   = False
    reg.checkin_time = None
    db.session.commit()
    return jsonify({'success': True})

@app.route('/checkin/stats')
def checkin_stats():
    if not session.get('checkin'):
        return jsonify({'error': 'Unauthorized'}), 401
    total    = Registration.query.count()
    checked  = Registration.query.filter_by(checked_in=True).count()
    return jsonify({'total': total, 'checked_in': checked, 'pending': total - checked})

# ── ADMIN DASHBOARD ──
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'osa2025')

@app.route('/admin')
def admin():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    regs = Registration.query.order_by(Registration.created_at.desc()).all()
    total    = len(regs)
    checked  = sum(1 for r in regs if r.checked_in)
    title    = sum(1 for r in regs if r.is_title)
    revenue  = sum(r.amount for r in regs if r.payment_status == 'paid')
    return render_template('admin.html',
                           regs=regs,
                           total=total,
                           checked=checked,
                           title=title,
                           revenue=revenue)

@app.route('/admin/stats')
def admin_stats():
    if not session.get('admin'):
        abort(403)
    regs = Registration.query.all()
    total    = len(regs)
    checked  = sum(1 for r in regs if r.checked_in)
    title    = sum(1 for r in regs if r.is_title)
    revenue  = sum(r.amount for r in regs if r.payment_status == 'paid')
    workshop = sum(1 for r in regs if r.reg_type == 'workshop')
    opening  = sum(1 for r in regs if r.reg_type == 'opening')
    both     = sum(1 for r in regs if r.reg_type == 'both')
    return jsonify({
        'total':    total,
        'checked':  checked,
        'pending':  total - checked,
        'title':    title,
        'revenue':  revenue,
        'workshop': workshop,
        'opening':  opening,
        'both':     both,
    })

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['admin'] = True
            return redirect(url_for('admin'))
        error = 'Incorrect password'
    return render_template('admin_login.html', error=error)

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    return redirect(url_for('admin_login'))

@app.route('/admin/export')
def admin_export():
    if not session.get('admin'):
        abort(403)
    import csv
    from flask import Response
    regs = Registration.query.order_by(Registration.created_at).all()
    def generate():
        headers = ['ID','First Name','Last Name','Studio','Gender','Birth Date',
                   'Email','Phone','Is Title','Routine','Registration','T-Shirt',
                   'Amount','Payment Status','Checked In','Check-In Time','Created']
        yield ','.join(headers) + '\n'
        for r in regs:
            row = [
                r.id, r.first_name, r.last_name, r.studio_name, r.gender,
                r.birth_date, r.email, r.phone,
                'Yes' if r.is_title else 'No',
                r.routine_name or '', r.reg_label, r.tshirt_size or '',
                f'${r.amount/100:.2f}', r.payment_status,
                'Yes' if r.checked_in else 'No',
                r.checkin_time.strftime('%m/%d/%Y %I:%M %p') if r.checkin_time else '',
                r.created_at.strftime('%m/%d/%Y'),
            ]
            yield ','.join(f'"{str(v)}"' for v in row) + '\n'
    return Response(generate(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=osa_registrations.csv'})

# ── INIT DB ──
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)

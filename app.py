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
database_url = os.environ.get('DATABASE_URL', f'sqlite:///{os.path.join(basedir, "osa_workshop.db")}')
# Render provides postgres:// — fix scheme and use psycopg3 driver
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql+psycopg://', 1)
elif database_url.startswith('postgresql://'):
    database_url = database_url.replace('postgresql://', 'postgresql+psycopg://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
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
    # Studio contact (for bulk CSV registrations without a student email)
    studio_email  = db.Column(db.String(200))
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

    # If no student email, fall back to studio contact email
    to_email   = (reg.email or '').strip() or (reg.studio_email or '').strip()
    to_studio  = not bool((reg.email or '').strip())  # True = sending to studio, not student

    if not to_email:
        print(f'No email for {reg.full_name} — skipping confirmation email')
        return

    base_url  = os.environ.get('BASE_URL', 'http://localhost:5000')
    qr_data   = f'{base_url}/confirm/{reg.id}'
    qr_bytes  = generate_qr_bytes(qr_data)
    qr_b64    = base64.b64encode(qr_bytes).decode()
    qr_inline = generate_qr_base64(qr_data)

    confirmation_url = f'{base_url}/confirm/{reg.id}'
    html = render_template('email_confirmation.html',
                           reg=reg,
                           qr_inline=qr_inline,
                           confirmation_url=confirmation_url,
                           to_studio=to_studio)

    subject = (f'{reg.full_name} is Registered! — On Stage America Workshop'
               if to_studio else
               'You\'re Registered! — On Stage America Workshop')

    message = Mail(
        from_email=('osa@onstageamerica.com', 'On Stage America'),
        to_emails=to_email,
        subject=subject,
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

# ── ADMIN NOTIFICATION EMAIL ──
NOTIFY_EMAIL = os.environ.get('NOTIFY_EMAIL', 'osa@onstageamerica.com')

def send_admin_notification(reg: Registration):
    sg_key = os.environ.get('SENDGRID_API_KEY')
    if not sg_key:
        return

    base_url         = os.environ.get('BASE_URL', 'http://localhost:5000')
    confirmation_url = f'{base_url}/confirm/{reg.id}'
    amount_str       = f'${reg.amount / 100:.2f}' if reg.amount else 'No charge'

    html = f'''
<div style="font-family:Arial,sans-serif;max-width:520px;padding:24px;background:#f5f2ec;">
  <div style="background:#111;border-radius:12px 12px 0 0;padding:20px 24px;">
    <p style="margin:0;font-size:10px;font-weight:700;letter-spacing:0.14em;text-transform:uppercase;color:#C9A84C;">On Stage America</p>
    <p style="margin:6px 0 0;font-size:20px;color:#fff;font-weight:700;">New Registration</p>
  </div>
  <div style="height:3px;background:linear-gradient(90deg,#C9A84C,#e8c96a,#C9A84C);"></div>
  <div style="background:#fff;border-radius:0 0 12px 12px;padding:24px;">
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
      <tr><td style="padding:8px 0;border-bottom:1px solid #f0ede6;color:#8a8780;width:130px;font-size:11px;text-transform:uppercase;font-weight:700;letter-spacing:0.04em;">Student</td>
          <td style="padding:8px 0;border-bottom:1px solid #f0ede6;font-weight:600;">{reg.full_name}</td></tr>
      <tr><td style="padding:8px 0;border-bottom:1px solid #f0ede6;color:#8a8780;font-size:11px;text-transform:uppercase;font-weight:700;letter-spacing:0.04em;">Studio</td>
          <td style="padding:8px 0;border-bottom:1px solid #f0ede6;">{reg.studio_name or '—'}</td></tr>
      <tr><td style="padding:8px 0;border-bottom:1px solid #f0ede6;color:#8a8780;font-size:11px;text-transform:uppercase;font-weight:700;letter-spacing:0.04em;">Email</td>
          <td style="padding:8px 0;border-bottom:1px solid #f0ede6;">{reg.email or '—'}</td></tr>
      <tr><td style="padding:8px 0;border-bottom:1px solid #f0ede6;color:#8a8780;font-size:11px;text-transform:uppercase;font-weight:700;letter-spacing:0.04em;">Registration</td>
          <td style="padding:8px 0;border-bottom:1px solid #f0ede6;">{reg.reg_label}</td></tr>
      <tr><td style="padding:8px 0;border-bottom:1px solid #f0ede6;color:#8a8780;font-size:11px;text-transform:uppercase;font-weight:700;letter-spacing:0.04em;">T-Shirt</td>
          <td style="padding:8px 0;border-bottom:1px solid #f0ede6;">{reg.tshirt_size or '—'}</td></tr>
      <tr><td style="padding:8px 0;border-bottom:1px solid #f0ede6;color:#8a8780;font-size:11px;text-transform:uppercase;font-weight:700;letter-spacing:0.04em;">Amount</td>
          <td style="padding:8px 0;border-bottom:1px solid #f0ede6;font-weight:700;color:{'#1a5a2a' if reg.is_title else '#1a1814'};">{amount_str}{' — Title (Free)' if reg.is_title else ''}</td></tr>
      <tr><td style="padding:8px 0;color:#8a8780;font-size:11px;text-transform:uppercase;font-weight:700;letter-spacing:0.04em;">Payment</td>
          <td style="padding:8px 0;">{reg.payment_status.title()}</td></tr>
    </table>
    <div style="margin-top:20px;text-align:center;">
      <a href="{confirmation_url}" style="display:inline-block;background:#111;color:#fff;padding:11px 22px;border-radius:8px;text-decoration:none;font-size:13px;font-weight:700;">View Confirmation →</a>
    </div>
  </div>
</div>'''

    message = Mail(
        from_email=('osa@onstageamerica.com', 'On Stage America'),
        to_emails=NOTIFY_EMAIL,
        subject=f'New Registration: {reg.full_name} ({reg.studio_name or "No Studio"})',
        html_content=html,
    )
    try:
        sg = SendGridAPIClient(sg_key)
        sg.send(message)
        print(f'Admin notification sent to {NOTIFY_EMAIL}')
    except Exception as e:
        print(f'Admin notification error: {e}')

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
        studio_name  = ' '.join(data.get('studio_name', '').strip().split()),
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

    # Send confirmation email to registrant and notification to admin
    try:
        send_confirmation_email(reg)
    except Exception as e:
        print(f'Confirmation email failed: {e}')
    try:
        send_admin_notification(reg)
    except Exception as e:
        print(f'Admin notification failed: {e}')

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

TSHIRT_SIZES = ['Youth Small','Youth Medium','Youth Large',
                'Adult Small','Adult Medium','Adult Large','Adult XL','Adult 2XL']

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
    tshirts  = {s: sum(1 for r in regs if r.tshirt_size == s) for s in TSHIRT_SIZES}
    return jsonify({
        'total':    total,
        'checked':  checked,
        'pending':  total - checked,
        'title':    title,
        'revenue':  revenue,
        'workshop': workshop,
        'opening':  opening,
        'both':     both,
        'tshirts':  tshirts,
    })

@app.route('/admin/tshirts')
def admin_tshirts():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    regs = Registration.query.all()
    tshirts = {s: sum(1 for r in regs if r.tshirt_size == s) for s in TSHIRT_SIZES}
    total   = sum(tshirts.values())
    # Per-studio breakdown
    studios = {}
    for r in regs:
        if r.tshirt_size:
            studios.setdefault(r.studio_name, {})
            studios[r.studio_name][r.tshirt_size] = studios[r.studio_name].get(r.tshirt_size, 0) + 1
    studios = dict(sorted(studios.items()))
    now = datetime.utcnow().strftime('%B %d, %Y at %I:%M %p UTC')
    return render_template('admin_tshirts.html',
                           tshirts=tshirts, total=total,
                           sizes=TSHIRT_SIZES, studios=studios, now=now)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['admin'] = True
            return redirect(url_for('admin'))
        error = 'Incorrect password'
    return render_template('admin_login.html', error=error)

@app.route('/admin/checkin', methods=['POST'])
def admin_checkin():
    if not session.get('admin'):
        abort(403)
    data   = request.get_json()
    reg_id = (data.get('id') or '').strip()
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
    reg.checkin_by   = 'Admin'
    db.session.commit()
    return jsonify({'success': True, 'checkin_time': reg.checkin_time.strftime('%I:%M %p')})

@app.route('/admin/undo-checkin', methods=['POST'])
def admin_undo_checkin():
    if not session.get('admin'):
        abort(403)
    data   = request.get_json()
    reg_id = (data.get('id') or '').strip()
    reg    = db.session.get(Registration, reg_id)
    if not reg:
        return jsonify({'error': 'Registration not found'}), 404
    reg.checked_in   = False
    reg.checkin_time = None
    reg.checkin_by   = None
    db.session.commit()
    return jsonify({'success': True})


@app.route('/admin/delete-selected', methods=['POST'])
def admin_delete_selected():
    if not session.get('admin'):
        abort(403)
    ids = request.get_json().get('ids', [])
    if not ids:
        return jsonify({'error': 'No IDs provided'}), 400
    deleted = Registration.query.filter(Registration.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()
    return jsonify({'deleted': deleted})

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

# ── STUDIO AUTOCOMPLETE (public — used by registration form) ──
@app.route('/api/studios')
def api_studios():
    rows = db.session.execute(
        db.text("SELECT DISTINCT studio_name FROM registration WHERE studio_name IS NOT NULL AND studio_name != '' ORDER BY studio_name")
    ).fetchall()
    return jsonify([r[0] for r in rows])

# ── STUDIO ADMIN ROUTES ──
@app.route('/admin/studios')
def admin_studios():
    if not session.get('admin'):
        abort(403)
    rows = db.session.execute(db.text(
        "SELECT studio_name, COUNT(*) as cnt FROM registration "
        "WHERE studio_name IS NOT NULL AND studio_name != '' "
        "GROUP BY studio_name ORDER BY studio_name"
    )).fetchall()
    return jsonify([{'name': r[0], 'count': r[1]} for r in rows])

@app.route('/admin/studio/rename', methods=['POST'])
def admin_studio_rename():
    if not session.get('admin'):
        abort(403)
    data     = request.get_json()
    old_name = (data.get('old_name') or '').strip()
    new_name = ' '.join((data.get('new_name') or '').strip().split())
    if not old_name or not new_name:
        return jsonify({'error': 'Missing name'}), 400
    result = db.session.execute(
        db.text("UPDATE registration SET studio_name = :new WHERE studio_name = :old"),
        {'new': new_name, 'old': old_name}
    )
    db.session.commit()
    return jsonify({'updated': result.rowcount, 'new_name': new_name})

# ── CSV TEMPLATE DOWNLOAD ──
@app.route('/registration-template.csv')
def registration_template():
    from flask import Response
    headers = [
        'studio_name', 'studio_email', 'first_name', 'last_name', 'gender', 'birth_date',
        'email', 'phone', 'mobile', 'is_title', 'routine_name',
        'reg_type', 'tshirt_size',
    ]
    notes = [
        'Your Studio Name', 'studio@example.com (receives QR if no student email)',
        'First', 'Last', 'Female / Male / Non-binary',
        'MM/DD/YYYY', 'student@example.com (leave blank if unknown)', '555-000-0000', '555-000-0000',
        'yes / no', 'Routine name if title registrant (else leave blank)',
        'workshop / opening / both', 'YS / YM / YL / AS / AM / AL / AXL / AXXL',
    ]
    def generate():
        yield ','.join(headers) + '\n'
        yield ','.join(f'"{n}"' for n in notes) + '\n'
    return Response(generate(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=osa_registration_template.csv'})

# ── ADMIN CSV UPLOAD ──
@app.route('/admin/upload', methods=['GET'])
def admin_upload():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    return render_template('admin_upload.html')

@app.route('/admin/upload/process', methods=['POST'])
def admin_upload_process():
    if not session.get('admin'):
        abort(403)
    import csv, io as _io
    file = request.files.get('csv_file')
    if not file or not file.filename.endswith('.csv'):
        return jsonify({'error': 'Please upload a .csv file'}), 400

    content = file.read().decode('utf-8-sig')  # strip BOM if Excel-generated
    reader  = csv.DictReader(_io.StringIO(content))

    # Normalise header names (strip spaces, lowercase)
    def norm(k):
        return k.strip().lower().replace(' ', '_')

    results = []
    errors  = []
    row_num = 1

    for raw_row in reader:
        row_num += 1
        row = {norm(k): (v or '').strip() for k, v in raw_row.items()}

        # Skip the sample/notes row if it looks like instructions
        if row.get('studio_name', '').lower() in ('your studio name', 'studio name'):
            continue

        first = row.get('first_name', '')
        last  = row.get('last_name', '')
        if not first or not last:
            errors.append(f'Row {row_num}: missing first or last name — skipped')
            continue

        is_title  = row.get('is_title', 'no').lower() in ('yes', 'y', '1', 'true')
        reg_type  = row.get('reg_type', 'workshop').lower().strip()
        if reg_type not in ('workshop', 'opening', 'both'):
            reg_type = 'workshop'

        amount = 0 if is_title else PRICES.get(reg_type, 0)

        reg = Registration(
            studio_name   = ' '.join(row.get('studio_name', '').strip().split()),
            first_name    = first,
            last_name     = last,
            gender        = row.get('gender', ''),
            birth_date    = row.get('birth_date', ''),
            email         = row.get('email', '').lower(),
            phone         = row.get('phone', ''),
            mobile        = row.get('mobile', ''),
            studio_email  = row.get('studio_email', '').lower(),
            is_title      = is_title,
            routine_name  = row.get('routine_name', ''),
            reg_type      = reg_type,
            tshirt_size   = row.get('tshirt_size', ''),
            amount        = amount,
            payment_status = 'free' if is_title else 'invoiced',
        )
        db.session.add(reg)
        results.append({'name': f'{first} {last}', 'reg_type': reg_type, 'is_title': is_title})

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Database error: {e}'}), 500

    return jsonify({'imported': len(results), 'errors': errors, 'rows': results})

# ── INIT DB ──
with app.app_context():
    db.create_all()
    # Add columns introduced after initial deploy (ALTER TABLE is idempotent via try/except)
    with db.engine.connect() as _conn:
        for _col_sql in [
            'ALTER TABLE registration ADD COLUMN studio_email VARCHAR(200)',
        ]:
            try:
                _conn.execute(db.text(_col_sql))
                _conn.commit()
            except Exception:
                _conn.rollback()  # column already exists — ignore

if __name__ == '__main__':
    app.run(debug=True)

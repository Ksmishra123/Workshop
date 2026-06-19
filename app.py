import os
import uuid
import qrcode
import io
import base64
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, abort

# Event-local timezone. Timestamps are stored in UTC (datetime.utcnow) and
# converted to Eastern only for display, so EDT/EST is handled automatically.
EVENT_TZ = ZoneInfo('America/New_York')

def to_local(dt):
    """Convert a naive-UTC datetime (as stored by datetime.utcnow) to Eastern."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(EVENT_TZ)
from flask_sqlalchemy import SQLAlchemy
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
from square import Square
from square.environment import SquareEnvironment

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-in-prod')
# Make helpers available in templates.
app.jinja_env.globals['to_local'] = to_local

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
            'checkin_time':  to_local(self.checkin_time).strftime('%I:%M %p') if self.checkin_time else None,
            'checkin_date':  to_local(self.checkin_time).strftime('%m/%d/%Y') if self.checkin_time else None,
            'created_at':    to_local(self.created_at).strftime('%m/%d/%Y') if self.created_at else '',
            'age':           compute_age(self.birth_date),
            'age_group':     age_group(self)[0],     # 'younger' | 'older' | 'unknown'
            'age_group_label': age_group(self)[1],   # '12 & Under' | '13 & Over' | 'Age Unknown'
        }

# ── SETTINGS (single key/value store for things like the registration deadline) ──
class Setting(db.Model):
    key   = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text)

def get_setting(key, default=None):
    s = db.session.get(Setting, key)
    return s.value if s and s.value is not None else default

def set_setting(key, value):
    s = db.session.get(Setting, key)
    if s is None:
        s = Setting(key=key)
        db.session.add(s)
    s.value = value
    db.session.commit()

# ── REGISTRATION DEADLINE ──
# Stored as setting 'reg_deadline' = 'YYYY-MM-DD' (the last day registration is
# open). Registration is open through the end of that day; the page shows a
# "closing soon" banner during the 7 days before, and "closed" the day after.
def registration_status():
    """Return a dict describing whether public registration is open:
       { open: bool, deadline: 'YYYY-MM-DD'|None, deadline_display: str|None,
         closing_soon: bool, days_left: int|None }"""
    raw = get_setting('reg_deadline')
    if not raw:
        return {'open': True, 'deadline': None, 'deadline_display': None,
                'closing_soon': False, 'days_left': None}
    try:
        deadline = datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return {'open': True, 'deadline': None, 'deadline_display': None,
                'closing_soon': False, 'days_left': None}
    today = to_local(datetime.utcnow()).date()
    is_open = today <= deadline
    days_left = (deadline - today).days  # negative once past
    return {
        'open': is_open,
        'deadline': raw,
        'deadline_display': deadline.strftime('%B %d, %Y'),
        'closing_soon': is_open and 0 <= days_left <= 7,
        'days_left': days_left,
    }

# ── PRICING ──
PRICES = {
    'workshop': 7500,   # $75.00
    'opening':  15000,  # $150.00
    'both':     22500,  # $225.00
}

# ── HAND-OUT ITEMS ──
# What a student is physically given at check-in, by registration type. Kept in
# sync with the kiosk's handoutItems() in templates/checkin.html so the report
# and the check-in screen always agree.
#   Workshop Only        → Workshop Wristband
#   Opening Number Only  → Opening Number Wristband + T-Shirt (size)
#   Workshop & Opening   → Workshop + Opening Number Wristbands + T-Shirt (size)
#   Title                → both Wristbands + T-Shirt (size) + Title Audition Info Package
def handout_items(reg):
    size = reg.tshirt_size or ''
    shirt = f'T-Shirt ({size})' if size else 'T-Shirt (size not on file)'
    if reg.is_title:
        return ['Workshop Wristband', 'Opening Number Wristband', shirt,
                'Title Audition Information Package']
    if reg.reg_type == 'both':
        return ['Workshop Wristband', 'Opening Number Wristband', shirt]
    if reg.reg_type == 'opening':
        return ['Opening Number Wristband', shirt]
    return ['Workshop Wristband']  # workshop (or unknown → default)

# ── AGE GROUPS ──
# Workshop is split into two rooms by age (as of the event day):
#   12 & Under  and  13 & Over
AGE_CUTOFF = 12   # this age and below → younger group

# Accepted input formats for a birth date (web sends YYYY-MM-DD; CSVs use
# MM/DD/YYYY etc.). All are normalised to MM/DD/YYYY for storage.
_BD_FORMATS = ('%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d', '%m-%d-%Y')

def parse_birth_date(birth_date):
    """Parse a birth-date string into a date object, or None if unparseable."""
    if not birth_date:
        return None
    s = str(birth_date).strip()
    for fmt in _BD_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

def validate_birth_date(birth_date):
    """Validate and normalise a birth date. Returns (normalized_mmddyyyy, error).
    On success error is None; on failure normalized is None and error is a
    human-readable message. Rejects unparseable dates and dates today/in the
    future. An empty value is reported as a 'required' error — callers that
    allow blanks (CSV import) handle that case themselves."""
    if not birth_date or not str(birth_date).strip():
        return None, 'Birth date is required'
    bd = parse_birth_date(birth_date)
    if bd is None:
        return None, 'Birth date is not a valid date (use MM/DD/YYYY)'
    today = to_local(datetime.utcnow()).date()
    if bd >= today:
        return None, 'Birth date must be in the past'
    return bd.strftime('%m/%d/%Y'), None

def compute_age(birth_date):
    """Return age in years as of today, or None if the date can't be parsed."""
    bd = parse_birth_date(birth_date)
    if bd is None:
        return None
    today = to_local(datetime.utcnow()).date()
    age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
    return age if 0 <= age < 120 else None

def age_group(reg):
    """Return (key, label) for the student's age group, or ('unknown', …) if the
    birth date is missing/unparseable so staff know to ask."""
    age = compute_age(reg.birth_date)
    if age is None:
        return ('unknown', 'Age Unknown')
    if age <= AGE_CUTOFF:
        return ('younger', '12 & Under')
    return ('older', '13 & Over')

# Expose age helpers to templates.
app.jinja_env.globals['compute_age'] = compute_age
app.jinja_env.globals['age_group'] = age_group

def age_counts(regs):
    """Tally regs into age-group counts, with checked-in subcounts."""
    c = {'younger': 0, 'older': 0, 'unknown': 0,
         'younger_ci': 0, 'older_ci': 0}
    for r in regs:
        key = age_group(r)[0]
        c[key] += 1
        if r.checked_in and key in ('younger', 'older'):
            c[key + '_ci'] += 1
    return c

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
def send_confirmation_email(reg: Registration, cc=None, to_override=None):
    """Send the QR confirmation email to the student (or studio if the student
    has no email). Returns (ok: bool, reason: str). `cc` is an optional email
    address to carbon-copy (e.g. the admin, to confirm delivery). `to_override`
    forces the email TO a specific address instead of the student/studio — used
    by the admin 'send to me (test)' flow to verify delivery without emailing
    the real studios; it also tags the subject with [TEST]."""
    sg_key = os.environ.get('SENDGRID_API_KEY')
    if not sg_key:
        print('No SendGrid key — skipping email')
        return False, 'no SendGrid key'

    is_test = bool(to_override and to_override.strip())
    if is_test:
        to_email  = to_override.strip()
        to_studio = False  # render the student-facing version for a clean preview
    else:
        # If no student email, fall back to studio contact email
        to_email  = (reg.email or '').strip() or (reg.studio_email or '').strip()
        to_studio = not bool((reg.email or '').strip())  # True = sending to studio, not student

    if not to_email:
        print(f'No email for {reg.full_name} — skipping confirmation email')
        return False, 'no email address'

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
    if is_test:
        subject = f'[TEST] {reg.full_name} — {subject}'

    message = Mail(
        from_email=('osa@onstageamerica.com', 'On Stage America'),
        to_emails=to_email,
        subject=subject,
        html_content=html
    )

    # Carbon-copy the admin so they can confirm the email went out. Don't CC
    # the same address we're already sending to (SendGrid rejects duplicates).
    # In test mode the message already goes straight to the admin, so skip CC.
    if cc and not is_test and cc.strip().lower() != to_email.strip().lower():
        message.add_cc(cc.strip())

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
        print(f'QR email sent to {to_email}' + (f' (cc {cc})' if cc else ''))
        return True, to_email
    except Exception as e:
        print(f'Email error: {e}')
        return False, str(e)

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
    status = registration_status()
    # Registration closed → show the closed page (unless an admin is previewing).
    if not status['open'] and not session.get('admin'):
        return render_template('registration_closed.html', status=status)
    sq_app_id  = os.environ.get('SQUARE_APP_ID', '')
    sq_env     = os.environ.get('SQUARE_ENV', 'sandbox')
    sq_location = os.environ.get('SQUARE_LOCATION_ID', '')
    return render_template('register.html',
                           sq_app_id=sq_app_id,
                           sq_env=sq_env,
                           sq_location=sq_location,
                           prices=PRICES,
                           reg_status=status,
                           admin_preview=(not status['open'] and session.get('admin')))

# ── PROCESS REGISTRATION ──
@app.route('/register/submit', methods=['POST'])
def submit_registration():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data received'}), 400

    # Enforce the registration deadline for the public (admins can still add).
    if not registration_status()['open'] and not session.get('admin'):
        return jsonify({'error': 'Registration is closed.'}), 403

    is_title = data.get('is_title') == 'yes'
    reg_type = data.get('reg_type', '')
    amount   = 0 if is_title else PRICES.get(reg_type, 0)

    # Validate & normalise birth date (rejects fake/future dates).
    birth_date, bd_error = validate_birth_date(data.get('birth_date', ''))
    if bd_error:
        return jsonify({'error': bd_error}), 400

    # Create registration record
    reg = Registration(
        studio_name  = ' '.join(data.get('studio_name', '').strip().split()),
        first_name   = data.get('first_name', '').strip(),
        last_name    = data.get('last_name', '').strip(),
        gender       = data.get('gender', '').strip(),
        birth_date   = birth_date,
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
            'checkin_time': to_local(reg.checkin_time).strftime('%I:%M %p'),
            'checkin_date': to_local(reg.checkin_time).strftime('%m/%d/%Y'),
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
    expected = sum(r.amount for r in regs if not r.is_title)  # total owed
    outstanding = expected - revenue
    return render_template('admin.html',
                           regs=regs,
                           total=total,
                           checked=checked,
                           title=title,
                           revenue=revenue,
                           expected=expected,
                           outstanding=outstanding,
                           counts_age=age_counts(regs),
                           reg_status=registration_status())

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
    expected = sum(r.amount for r in regs if not r.is_title)  # total owed
    outstanding = expected - revenue
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
        'expected': expected,
        'outstanding': outstanding,
        'workshop': workshop,
        'opening':  opening,
        'both':     both,
        'tshirts':  tshirts,
        'age':      age_counts(regs),
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
    now = to_local(datetime.utcnow()).strftime('%B %d, %Y at %I:%M %p ET')
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
            'checkin_time': to_local(reg.checkin_time).strftime('%I:%M %p'),
            'checkin_date': to_local(reg.checkin_time).strftime('%m/%d/%Y'),
        })
    reg.checked_in   = True
    reg.checkin_time = datetime.utcnow()
    reg.checkin_by   = 'Admin'
    db.session.commit()
    return jsonify({'success': True, 'checkin_time': to_local(reg.checkin_time).strftime('%I:%M %p')})

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


@app.route('/admin/send-email', methods=['POST'])
def admin_send_email():
    if not session.get('admin'):
        abort(403)
    sg_key = os.environ.get('SENDGRID_API_KEY')
    if not sg_key:
        return jsonify({'error': 'SendGrid API key not configured'}), 500

    data    = request.get_json()
    ids     = data.get('ids', [])
    subject = (data.get('subject') or '').strip()
    body    = (data.get('body') or '').strip()
    if not ids or not subject or not body:
        return jsonify({'error': 'Missing required fields'}), 400

    regs = Registration.query.filter(Registration.id.in_(ids)).all()
    # Paragraphs: split on double newline, single newlines become <br>
    def text_to_html(t):
        paras = t.split('\n\n')
        return ''.join(f'<p style="margin:0 0 14px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#5A5750;line-height:1.75;">{p.replace(chr(10),"<br>")}</p>' for p in paras)

    body_html = text_to_html(body)
    sent = skipped = 0
    sg = SendGridAPIClient(sg_key)

    for reg in regs:
        to_email = (reg.email or '').strip() or (reg.studio_email or '').strip()
        if not to_email:
            skipped += 1
            continue
        html = f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#F5F2EC;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#F5F2EC;padding:32px 0 64px;">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;">
      <tr><td style="background:#111111;border-radius:16px 16px 0 0;padding:28px 36px 24px;">
        <p style="margin:0 0 8px;font-size:10px;font-weight:bold;letter-spacing:0.16em;text-transform:uppercase;color:#C9A84C;">On Stage America</p>
        <p style="margin:0;font-family:Georgia,serif;font-size:28px;color:#fff;line-height:1.2;">{subject}</p>
      </td></tr>
      <tr><td style="background:linear-gradient(90deg,#C9A84C,#e8c96a,#C9A84C);height:3px;font-size:0;">&nbsp;</td></tr>
      <tr><td style="background:#fff;padding:32px 36px;">
        <p style="margin:0 0 20px;font-family:Georgia,serif;font-size:18px;color:#1A1814;">Hi {reg.first_name},</p>
        {body_html}
      </td></tr>
      <tr><td style="background:#1A1814;border-radius:0 0 16px 16px;padding:22px 36px;text-align:center;">
        <p style="margin:0 0 4px;font-family:Georgia,serif;font-size:16px;color:rgba(255,255,255,0.8);">On Stage America</p>
        <p style="margin:0;font-size:12px;color:rgba(255,255,255,0.3);">osa@onstageamerica.com &middot; 301-654-8939</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>'''
        try:
            sg.send(Mail(
                from_email=('osa@onstageamerica.com', 'On Stage America'),
                to_emails=to_email,
                subject=subject,
                html_content=html,
            ))
            sent += 1
        except Exception as e:
            print(f'Email to {to_email} failed: {e}')
            skipped += 1

    return jsonify({'sent': sent, 'skipped': skipped})

@app.route('/admin/send-qr-selected', methods=['POST'])
def admin_send_qr_selected():
    """Send each selected registration its QR confirmation email — to the
    student's email, or the studio email as a fallback. The admin is CC'd on
    every message so they can confirm delivery."""
    if not session.get('admin'):
        abort(403)
    if not os.environ.get('SENDGRID_API_KEY'):
        return jsonify({'error': 'SendGrid API key not configured'}), 500

    payload = request.get_json()
    ids  = payload.get('ids', [])
    test = bool(payload.get('test'))
    if not ids:
        return jsonify({'error': 'No records selected'}), 400

    admin_email = os.environ.get('NOTIFY_EMAIL', 'osa@onstageamerica.com')
    regs = Registration.query.filter(Registration.id.in_(ids)).all()

    sent = 0
    skipped = []
    for reg in regs:
        if test:
            # Test mode: send every QR TO the admin instead of the real
            # student/studio, so delivery can be verified without spamming them.
            ok, reason = send_confirmation_email(reg, to_override=admin_email)
        else:
            ok, reason = send_confirmation_email(reg, cc=admin_email)
        if ok:
            sent += 1
        else:
            skipped.append(f'{reg.full_name}: {reason}')

    return jsonify({'sent': sent, 'skipped': skipped, 'cc': admin_email,
                    'test': test, 'test_to': admin_email if test else None})

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

@app.route('/admin/mark-paid', methods=['POST'])
def admin_mark_paid():
    """Mark the given registrations as paid. Used for bulk-uploaded (invoiced)
    studio registrations once the studio settles up. Accepts one or many IDs.
    Title/free registrations are left alone (they owe nothing). Setting
    `status` to 'invoiced' reverses it (e.g. if marked paid by mistake)."""
    if not session.get('admin'):
        abort(403)
    payload = request.get_json()
    ids     = payload.get('ids', [])
    status  = payload.get('status', 'paid')
    if status not in ('paid', 'invoiced', 'pending'):
        status = 'paid'
    if not ids:
        return jsonify({'error': 'No records selected'}), 400

    regs = Registration.query.filter(Registration.id.in_(ids)).all()
    updated = 0
    for reg in regs:
        if reg.is_title:  # title registrants are free — nothing to pay
            continue
        reg.payment_status = status
        updated += 1
    db.session.commit()

    # Recompute revenue totals so the dashboard can update its stat cards.
    all_regs    = Registration.query.all()
    revenue     = sum(r.amount for r in all_regs if r.payment_status == 'paid')
    expected    = sum(r.amount for r in all_regs if not r.is_title)
    outstanding = expected - revenue
    return jsonify({'updated': updated, 'status': status,
                    'revenue': revenue, 'expected': expected,
                    'outstanding': outstanding})

@app.route('/admin/edit/<reg_id>', methods=['POST'])
def admin_edit(reg_id):
    """Edit a registration's details (notably birth date, so missing/bad DOBs
    can be fixed). Birth date is validated and normalised; other text fields
    are updated as-is."""
    if not session.get('admin'):
        abort(403)
    reg = db.session.get(Registration, reg_id)
    if not reg:
        return jsonify({'error': 'Registration not found'}), 404
    data = request.get_json() or {}

    # Birth date: if provided, validate; allow clearing it to blank.
    if 'birth_date' in data:
        raw = (data.get('birth_date') or '').strip()
        if raw:
            norm, err = validate_birth_date(raw)
            if err:
                return jsonify({'error': err}), 400
            reg.birth_date = norm
        else:
            reg.birth_date = ''

    # Other editable text fields.
    for field in ('first_name', 'last_name', 'studio_name', 'gender',
                  'email', 'phone', 'tshirt_size'):
        if field in data:
            val = (data.get(field) or '').strip()
            if field == 'email':
                val = val.lower()
            if field == 'studio_name':
                val = ' '.join(val.split())
            setattr(reg, field, val)

    # Registration type / Title — e.g. a Workshop kid adding the Opening Number.
    # Changing the type changes the price, so recalc the amount when the kid
    # hasn't paid yet; if they've already paid, keep their paid amount and
    # report the balance owed so it can be collected separately.
    price_note = None
    type_changed = ('reg_type' in data) or ('is_title' in data)
    if type_changed:
        if 'is_title' in data:
            reg.is_title = bool(data.get('is_title'))
        if 'reg_type' in data:
            rt = (data.get('reg_type') or '').strip().lower()
            if rt in ('workshop', 'opening', 'both'):
                reg.reg_type = rt
        new_price = 0 if reg.is_title else PRICES.get(reg.reg_type, 0)
        if reg.is_title:
            reg.amount = 0
            reg.payment_status = 'free'
        elif reg.payment_status == 'paid':
            # Already paid — keep what they paid, surface any balance owed.
            balance = new_price - (reg.amount or 0)
            if balance > 0:
                price_note = (f'Registration updated. New total is '
                              f'${new_price/100:.2f}; ${(reg.amount or 0)/100:.2f} '
                              f'already paid — ${balance/100:.2f} still owed.')
            elif balance < 0:
                price_note = (f'Registration updated. New total is '
                              f'${new_price/100:.2f}; ${(reg.amount or 0)/100:.2f} '
                              f'was paid — possible ${-balance/100:.2f} refund.')
        else:
            # Not paid yet — just set to the new price, keep it owing.
            reg.amount = new_price
            if reg.payment_status not in ('pending', 'invoiced'):
                reg.payment_status = 'pending'

    db.session.commit()
    out = {'success': True, 'registration': reg.to_dict()}
    if price_note:
        out['note'] = price_note
    return jsonify(out)

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    return redirect(url_for('admin_login'))

def _registrations_csv():
    """Build the full-registrations CSV as a single string (shared by export and
    the year-archive download). Materialised eagerly so it runs inside the
    request's app context rather than during lazy streaming."""
    regs = Registration.query.order_by(Registration.created_at).all()
    headers = ['ID','First Name','Last Name','Studio','Gender','Birth Date',
               'Email','Phone','Is Title','Routine','Registration','T-Shirt',
               'Amount','Payment Status','Checked In','Check-In Time','Created']
    lines = [','.join(headers)]
    for r in regs:
        row = [
            r.id, r.first_name, r.last_name, r.studio_name, r.gender,
            r.birth_date, r.email, r.phone,
            'Yes' if r.is_title else 'No',
            r.routine_name or '', r.reg_label, r.tshirt_size or '',
            f'${r.amount/100:.2f}', r.payment_status,
            'Yes' if r.checked_in else 'No',
            to_local(r.checkin_time).strftime('%m/%d/%Y %I:%M %p') if r.checkin_time else '',
            to_local(r.created_at).strftime('%m/%d/%Y') if r.created_at else '',
        ]
        lines.append(','.join(f'"{str(v)}"' for v in row))
    return '\n'.join(lines) + '\n'

@app.route('/admin/export')
def admin_export():
    if not session.get('admin'):
        abort(403)
    from flask import Response
    return Response(_registrations_csv(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=osa_registrations.csv'})

# ── REGISTRATION DEADLINE ADMIN ──
@app.route('/admin/set-deadline', methods=['POST'])
def admin_set_deadline():
    if not session.get('admin'):
        abort(403)
    raw = (request.get_json() or {}).get('deadline', '').strip()
    if raw == '':
        # Clear the deadline → registration open indefinitely.
        set_setting('reg_deadline', '')
        return jsonify({'success': True, 'status': registration_status()})
    try:
        datetime.strptime(raw, '%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400
    set_setting('reg_deadline', raw)
    return jsonify({'success': True, 'status': registration_status()})

# ── START NEW YEAR (archive CSV, then clear all registrations) ──
@app.route('/admin/archive-download')
def admin_archive_download():
    """Download a full CSV backup of all current registrations. Intended to be
    fetched right before clearing for a new year."""
    if not session.get('admin'):
        abort(403)
    from flask import Response
    stamp = to_local(datetime.utcnow()).strftime('%Y-%m-%d')
    return Response(_registrations_csv(), mimetype='text/csv',
                    headers={'Content-Disposition':
                             f'attachment; filename=osa_registrations_archive_{stamp}.csv'})

@app.route('/admin/start-new-year', methods=['POST'])
def admin_start_new_year():
    """Delete ALL registrations to start a fresh year. The admin UI downloads
    the archive CSV first; this requires a typed confirmation phrase."""
    if not session.get('admin'):
        abort(403)
    data = request.get_json() or {}
    if (data.get('confirm') or '').strip().upper() != 'NEW YEAR':
        return jsonify({'error': 'Confirmation phrase did not match'}), 400
    deleted = Registration.query.delete()
    db.session.commit()
    # A fresh year usually wants a fresh deadline too — clear it.
    set_setting('reg_deadline', '')
    return jsonify({'success': True, 'deleted': deleted})

# ── CHECK-IN REPORT ──
@app.route('/admin/checkin-report')
def admin_checkin_report():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    regs = (Registration.query
            .filter(Registration.checkin_time.isnot(None))
            .order_by(Registration.checkin_time).all())
    rows = [{
        'name':      r.full_name,
        'studio':    r.studio_name or '—',
        'reg_label': r.reg_label,
        'reg_type':  'title' if r.is_title else (r.reg_type or 'workshop'),
        'items':     handout_items(r),
        'time':      to_local(r.checkin_time).strftime('%a %m/%d/%Y %I:%M %p'),
        'by':        r.checkin_by or 'Staff',
    } for r in regs]
    return render_template('checkin_report.html', rows=rows, total=len(rows))

@app.route('/admin/checkin-report.csv')
def admin_checkin_report_csv():
    if not session.get('admin'):
        abort(403)
    import csv
    from flask import Response
    regs = (Registration.query
            .filter(Registration.checkin_time.isnot(None))
            .order_by(Registration.checkin_time).all())
    def generate():
        headers = ['Check-In Time', 'Checked In By', 'First Name', 'Last Name',
                   'Studio', 'Registration', 'Items Given']
        yield ','.join(headers) + '\n'
        for r in regs:
            row = [
                to_local(r.checkin_time).strftime('%m/%d/%Y %I:%M %p') if r.checkin_time else '',
                r.checkin_by or 'Staff',
                r.first_name, r.last_name, r.studio_name or '',
                r.reg_label, '; '.join(handout_items(r)),
            ]
            yield ','.join(f'"{str(v)}"' for v in row) + '\n'
    return Response(generate(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=osa_checkin_report.csv'})

# ── AGE-GROUP REPORT ──
def in_opening(reg):
    """True if a registration performs in the opening number — that's the
    'opening' and 'both' registration types, plus Title registrants."""
    return reg.is_title or reg.reg_type in ('opening', 'both')

def _age_grouped_rows(predicate=None):
    """Return {younger:[…], older:[…], unknown:[…]} of student dicts, each list
    sorted by age then name, for the age-group report. If `predicate` is given,
    only registrations for which predicate(reg) is True are included."""
    groups = {'younger': [], 'older': [], 'unknown': []}
    for r in Registration.query.order_by(Registration.last_name, Registration.first_name).all():
        if predicate and not predicate(r):
            continue
        key, label = age_group(r)
        groups[key].append({
            'name':       r.full_name,
            'studio':     r.studio_name or '—',
            'age':        compute_age(r.birth_date),
            'birth_date': r.birth_date or '—',
            'reg_label':  r.reg_label,
            'checked_in': bool(r.checkin_time),
        })
    for key in groups:
        groups[key].sort(key=lambda s: (s['age'] if s['age'] is not None else 999, s['name']))
    return groups

@app.route('/admin/age-report')
def admin_age_report():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    groups = _age_grouped_rows()
    return render_template('age_report.html', groups=groups,
                           counts={k: len(v) for k, v in groups.items()})

@app.route('/admin/age-report.csv')
def admin_age_report_csv():
    if not session.get('admin'):
        abort(403)
    from flask import Response
    groups = _age_grouped_rows()
    label_for = {'younger': '12 & Under', 'older': '13 & Over', 'unknown': 'Age Unknown'}
    def generate():
        yield ','.join(['Age Group', 'Age', 'First/Last Name', 'Studio',
                        'Birth Date', 'Registration', 'Checked In']) + '\n'
        for key in ('younger', 'older', 'unknown'):
            for s in groups[key]:
                row = [label_for[key], s['age'] if s['age'] is not None else '',
                       s['name'], s['studio'], s['birth_date'], s['reg_label'],
                       'Yes' if s['checked_in'] else 'No']
                yield ','.join(f'"{str(v)}"' for v in row) + '\n'
    return Response(generate(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=osa_age_groups.csv'})

# ── OPENING-NUMBER AGE REPORT ──
@app.route('/admin/opening-report')
def admin_opening_report():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    groups = _age_grouped_rows(predicate=in_opening)
    return render_template('age_report.html', groups=groups,
                           counts={k: len(v) for k, v in groups.items()},
                           opening=True)

@app.route('/admin/opening-report.csv')
def admin_opening_report_csv():
    if not session.get('admin'):
        abort(403)
    from flask import Response
    groups = _age_grouped_rows(predicate=in_opening)
    label_for = {'younger': '12 & Under', 'older': '13 & Over', 'unknown': 'Age Unknown'}
    def generate():
        yield ','.join(['Age Group', 'Age', 'First/Last Name', 'Studio',
                        'Birth Date', 'Registration', 'Checked In']) + '\n'
        for key in ('younger', 'older', 'unknown'):
            for s in groups[key]:
                row = [label_for[key], s['age'] if s['age'] is not None else '',
                       s['name'], s['studio'], s['birth_date'], s['reg_label'],
                       'Yes' if s['checked_in'] else 'No']
                yield ','.join(f'"{str(v)}"' for v in row) + '\n'
    return Response(generate(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=osa_opening_number_age_groups.csv'})

# ── STUDIO INVOICING ──
def _invoice_data(studio=None, ids=None):
    """Build invoice data for unpaid (pending/invoiced) registrations — either
    for a whole studio or a hand-picked set of IDs. Returns a dict with the
    studio name, studio email, line items, total (cents), and skipped counts."""
    q = Registration.query
    if ids:
        q = q.filter(Registration.id.in_(ids))
    elif studio is not None:
        q = q.filter(Registration.studio_name == studio)
    regs = q.order_by(Registration.last_name, Registration.first_name).all()

    items, total = [], 0
    skipped_paid = skipped_free = 0
    studio_name = studio or ''
    studio_email = ''
    for r in regs:
        if r.is_title:
            skipped_free += 1
            continue
        if r.payment_status == 'paid':
            skipped_paid += 1
            continue
        amt = r.amount or 0
        total += amt
        items.append({'name': r.full_name, 'reg_label': r.reg_label,
                      'amount': amt, 'amount_display': f'${amt/100:.2f}'})
        if not studio_name and r.studio_name:
            studio_name = r.studio_name
        if not studio_email and (r.studio_email or '').strip():
            studio_email = r.studio_email.strip()
    return {
        'studio': studio_name or 'Selected Students',
        'studio_email': studio_email,
        'items': items,
        'count': len(items),
        'total': total,
        'total_display': f'${total/100:.2f}',
        'skipped_paid': skipped_paid,
        'skipped_free': skipped_free,
        'date': to_local(datetime.utcnow()).strftime('%B %d, %Y'),
    }

@app.route('/admin/invoice')
def admin_invoice():
    """Printable invoice page for a studio's unpaid kids, or a selected set.
    Pass ?studio=<name> or ?ids=<comma-separated ids>."""
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    studio = request.args.get('studio')
    ids = [i for i in (request.args.get('ids', '').split(',')) if i]
    data = _invoice_data(studio=studio, ids=ids or None)
    return render_template('invoice.html', inv=data)

@app.route('/admin/invoice/send', methods=['POST'])
def admin_invoice_send():
    """Email a studio's invoice to a recipient (defaults to the studio email)."""
    if not session.get('admin'):
        abort(403)
    sg_key = os.environ.get('SENDGRID_API_KEY')
    if not sg_key:
        return jsonify({'error': 'SendGrid API key not configured'}), 500
    body = request.get_json() or {}
    studio = body.get('studio')
    ids = body.get('ids') or None
    data = _invoice_data(studio=studio, ids=ids)
    if not data['items']:
        return jsonify({'error': 'No unpaid registrations to invoice'}), 400
    to_email = (body.get('to') or data['studio_email'] or '').strip()
    if not to_email:
        return jsonify({'error': 'No studio email on file — enter a recipient address'}), 400

    rows_html = ''.join(
        f'<tr><td style="padding:8px 0;border-bottom:1px solid #eee;">{it["name"]}</td>'
        f'<td style="padding:8px 0;border-bottom:1px solid #eee;color:#5A5750;">{it["reg_label"]}</td>'
        f'<td style="padding:8px 0;border-bottom:1px solid #eee;text-align:right;">{it["amount_display"]}</td></tr>'
        for it in data['items'])
    cc = os.environ.get('NOTIFY_EMAIL', 'osa@onstageamerica.com')
    html = f'''<div style="font-family:Arial,sans-serif;max-width:600px;margin:auto;">
      <div style="background:#111;border-radius:12px 12px 0 0;padding:24px 28px;">
        <p style="margin:0;font-size:10px;font-weight:700;letter-spacing:0.16em;text-transform:uppercase;color:#C9A84C;">On Stage America</p>
        <p style="margin:6px 0 0;font-family:Georgia,serif;font-size:26px;color:#fff;">Workshop Invoice</p>
      </div>
      <div style="height:3px;background:linear-gradient(90deg,#C9A84C,#e8c96a,#C9A84C);"></div>
      <div style="background:#fff;padding:26px 28px;">
        <p style="font-size:14px;color:#1A1814;">Studio: <strong>{data["studio"]}</strong><br>Date: {data["date"]}</p>
        <table style="width:100%;border-collapse:collapse;font-size:14px;margin-top:14px;">
          <tr><th style="text-align:left;padding-bottom:8px;border-bottom:2px solid #111;">Student</th>
              <th style="text-align:left;padding-bottom:8px;border-bottom:2px solid #111;">Registration</th>
              <th style="text-align:right;padding-bottom:8px;border-bottom:2px solid #111;">Amount</th></tr>
          {rows_html}
          <tr><td colspan="2" style="padding:12px 0 0;text-align:right;font-weight:700;font-size:16px;">Total Due:</td>
              <td style="padding:12px 0 0;text-align:right;font-weight:700;font-size:16px;color:#1A5A2A;">{data["total_display"]}</td></tr>
        </table>
        <p style="font-size:13px;color:#5A5750;margin-top:20px;line-height:1.6;">Please remit payment for the {data["count"]} student(s) listed above. Thank you!</p>
      </div>
      <div style="background:#1A1814;border-radius:0 0 12px 12px;padding:18px 28px;text-align:center;">
        <p style="margin:0;font-size:12px;color:rgba(255,255,255,0.4);">osa@onstageamerica.com &middot; 301-654-8939</p>
      </div>
    </div>'''
    try:
        msg = Mail(from_email=('osa@onstageamerica.com', 'On Stage America'),
                   to_emails=to_email,
                   subject=f'On Stage America — Workshop Invoice for {data["studio"]} ({data["total_display"]})',
                   html_content=html)
        if cc and cc.lower() != to_email.lower():
            msg.add_cc(cc)
        SendGridAPIClient(sg_key).send(msg)
    except Exception as e:
        return jsonify({'error': f'Email failed: {e}'}), 500
    return jsonify({'success': True, 'to': to_email, 'total': data['total_display'],
                    'count': data['count']})

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

    # Normalise header names (strip spaces, lowercase)
    def norm(k):
        return (k or '').strip().lower().replace(' ', '_')

    # Find the real header row. Excel "Save As CSV" often prepends a title
    # row (e.g. the sheet name) or other junk above the actual columns, which
    # would otherwise make DictReader treat those as the field names and skip
    # every student. Scan the first several lines for the row that contains the
    # required columns and start parsing from there.
    all_lines  = content.splitlines()
    header_idx = 0
    for i, line in enumerate(all_lines[:10]):
        cells = {norm(c) for c in line.split(',')}
        if 'first_name' in cells and 'last_name' in cells:
            header_idx = i
            break
    body = '\n'.join(all_lines[header_idx:])
    reader = csv.DictReader(_io.StringIO(body))

    results = []
    errors  = []
    row_num = header_idx + 1  # 1-based row number of the header line

    # All rows in one file are from the same studio, which is often typed only
    # on the first student row. Carry the last non-blank studio name/email
    # forward so blank rows inherit it.
    last_studio_name  = ''
    last_studio_email = ''

    for raw_row in reader:
        row_num += 1
        row = {norm(k): (v or '').strip() for k, v in raw_row.items()}

        # Silently skip fully-blank rows (trailing empty rows from Excel).
        if not any(row.values()):
            continue

        # Skip the sample/notes row if it looks like instructions
        if row.get('studio_name', '').lower() in ('your studio name', 'studio name'):
            continue

        first = row.get('first_name', '')
        last  = row.get('last_name', '')
        if not first or not last:
            errors.append(f'Row {row_num}: missing first or last name — skipped')
            continue

        # Carry-forward studio name/email from whichever row last supplied a
        # *valid* one. CSVs often have placeholder junk in these columns
        # ('Studio', 'OR', a '#') on stray rows — only accept a studio_email
        # that looks like an email, and ignore obvious placeholder names, so
        # the carried value stays the real studio rather than the junk.
        PLACEHOLDER_STUDIO = {'studio', 'studio name', 'your studio name', 'n/a', 'na', '-'}

        studio_name  = ' '.join(row.get('studio_name', '').strip().split())
        studio_email = row.get('studio_email', '').strip().lower()

        def looks_like_email(e):
            return '@' in e and '.' in e.rsplit('@', 1)[-1]

        if studio_name and studio_name.lower() not in PLACEHOLDER_STUDIO:
            last_studio_name = studio_name
        else:
            studio_name = last_studio_name

        if studio_email and looks_like_email(studio_email):
            last_studio_email = studio_email
        else:
            studio_email = last_studio_email

        is_title  = row.get('is_title', 'no').lower() in ('yes', 'y', '1', 'true')
        reg_type  = row.get('reg_type', 'workshop').lower().strip()
        if reg_type not in ('workshop', 'opening', 'both'):
            reg_type = 'workshop'

        amount = 0 if is_title else PRICES.get(reg_type, 0)

        # Normalise the birth date if valid; flag (but still import) blank or
        # bad dates so they can be fixed later via the dashboard / age report.
        raw_bd = row.get('birth_date', '')
        norm_bd, bd_error = validate_birth_date(raw_bd)
        if bd_error:
            store_bd = ''  # don't store a fake/garbage date
            if (raw_bd or '').strip():
                errors.append(f'Row {row_num}: {first} {last} — bad birth date '
                              f'"{raw_bd}" (imported, needs fixing)')
            else:
                errors.append(f'Row {row_num}: {first} {last} — no birth date '
                              f'(imported, needs fixing)')
        else:
            store_bd = norm_bd

        reg = Registration(
            studio_name   = studio_name,
            first_name    = first,
            last_name     = last,
            gender        = row.get('gender', ''),
            birth_date    = store_bd,
            email         = row.get('email', '').lower(),
            phone         = row.get('phone', ''),
            mobile        = row.get('mobile', ''),
            studio_email  = studio_email,
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

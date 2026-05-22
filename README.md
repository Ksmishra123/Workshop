# OSA Workshop Registration & Check-In System

A complete registration, payment, and check-in system for On Stage America workshops.

## Pages

| URL | Description |
|-----|-------------|
| `/` or `/register` | Registration form with Square payment |
| `/confirm/<id>` | Confirmation page with QR code |
| `/checkin` | Staff check-in kiosk (scan or type ID) |
| `/admin` | Admin dashboard (password protected) |
| `/admin/export` | Export all registrations as CSV |

## Environment Variables

Set these in Render's dashboard under **Environment**:

```
SECRET_KEY=some-long-random-string-change-this
SQUARE_APP_ID=your-square-application-id
SQUARE_ACCESS_TOKEN=your-square-access-token
SQUARE_LOCATION_ID=your-square-location-id
SQUARE_ENV=production          # or 'sandbox' for testing
SENDGRID_API_KEY=your-sendgrid-api-key
ADMIN_PASSWORD=your-admin-password
```

## Deploy to Render

1. Push this folder to a new GitHub repo
2. Go to render.com → New → Web Service
3. Connect your GitHub repo
4. Set:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
5. Add all environment variables above
6. Deploy

## Square Setup

1. Go to developer.squareup.com
2. Create application → "OSA Workshop"
3. Get Application ID and Access Token from Credentials tab
4. Get Location ID from Locations tab
5. Use Sandbox for testing, Production for real events

## SendGrid Setup

1. Create account at sendgrid.com (free tier = 100 emails/day)
2. Settings → API Keys → Create Full Access key
3. Settings → Sender Authentication → verify osa@onstageamerica.com

## Admin Password

Set `ADMIN_PASSWORD` in environment variables. Default is `osa2025` — change this before going live.

## Pricing

| Registration | Amount |
|-------------|--------|
| Title Registrant | $0.00 (free) |
| Workshop Only | $75.00 |
| Opening Number Only | $150.00 |
| Both Workshop & Opening | $225.00 |

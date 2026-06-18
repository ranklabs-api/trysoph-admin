#!/usr/bin/env python3
"""
Sophia Admin Portal — Flask app serving the lead review dashboard.
Runs locally, exposed via Cloudflare Tunnel at admin.trysoph.com.
"""

import json
import os
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import psycopg2
import psycopg2.extras

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)

DB_CONFIG = {
    'host': 'localhost',
    'port': 5433,
    'dbname': 'postgres',
    'user': 'postgres',
    'password': 'ranklabs-dev',
}

def get_db():
    return psycopg2.connect(**DB_CONFIG)

# ── API Routes ────────────────────────────────────────────────────

@app.route('/api/leads')
def get_leads():
    """Get leads with optional filters."""
    status = request.args.get('status', 'new')
    category = request.args.get('category')
    city = request.args.get('city')
    limit = min(int(request.args.get('limit', 50)), 200)
    offset = int(request.args.get('offset', 0))
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    query = "SELECT * FROM sophia_leads WHERE status = %s"
    params = [status]
    
    if category:
        query += " AND category = %s"
        params.append(category)
    if city:
        query += " AND city ILIKE %s"
        params.append(f"%{city}%")
    
    cur.execute(f"SELECT COUNT(*) FROM ({query}) sub", params)
    total = cur.fetchone()['count']
    
    query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    
    cur.execute(query, params)
    leads = [dict(r) for r in cur.fetchall()]
    
    # Convert datetime objects to ISO strings
    for lead in leads:
        for key, val in lead.items():
            if isinstance(val, datetime):
                lead[key] = val.isoformat()
    
    conn.close()
    return jsonify({'leads': leads, 'total': total, 'offset': offset, 'limit': limit})

@app.route('/api/leads/<lead_id>', methods=['PATCH'])
def update_lead(lead_id):
    """Approve, reject, or update a lead."""
    data = request.json
    allowed_fields = {'status', 'email', 'contact_name', 'contact_title', 'rejection_reason', 'approved_by'}
    updates = {k: v for k, v in data.items() if k in allowed_fields}
    
    if not updates:
        return jsonify({'error': 'No valid fields to update'}), 400
    
    if updates.get('status') == 'approved':
        updates['approved_at'] = datetime.now(timezone.utc)
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    set_clause = ', '.join(f"{k} = %s" for k in updates.keys())
    values = list(updates.values()) + [lead_id]
    
    cur.execute(f"UPDATE sophia_leads SET {set_clause}, updated_at = NOW() WHERE id = %s RETURNING *", values)
    updated = cur.fetchone()
    conn.commit()
    conn.close()
    
    if not updated:
        return jsonify({'error': 'Lead not found'}), 404
    
    result = dict(updated)
    for key, val in result.items():
        if isinstance(val, datetime):
            result[key] = val.isoformat()
    
    return jsonify(result)

@app.route('/api/stats')
def get_stats():
    """Dashboard statistics."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    cur.execute("SELECT status, COUNT(*) as count FROM sophia_leads GROUP BY status")
    status_counts = {r['status']: r['count'] for r in cur.fetchall()}
    
    cur.execute("SELECT city, COUNT(*) as count FROM sophia_leads GROUP BY city ORDER BY count DESC LIMIT 10")
    top_cities = [dict(r) for r in cur.fetchall()]
    
    cur.execute("SELECT category, COUNT(*) as count FROM sophia_leads GROUP BY category ORDER BY count DESC")
    by_category = [dict(r) for r in cur.fetchall()]
    
    cur.execute("SELECT COUNT(*) as total FROM sophia_discovery_sessions WHERE status = 'completed'")
    sessions = cur.fetchone()['total']
    
    conn.close()
    
    return jsonify({
        'status_counts': status_counts,
        'total_leads': sum(status_counts.values()),
        'top_cities': top_cities,
        'by_category': by_category,
        'discovery_sessions': sessions,
    })

@app.route('/api/leads/<lead_id>/email')
def get_email_preview(lead_id):
    """Generate an email preview for a lead."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM sophia_leads WHERE id = %s", (lead_id,))
    lead = cur.fetchone()
    conn.close()
    
    if not lead:
        return jsonify({'error': 'Lead not found'}), 404
    
    lead = dict(lead)
    name = lead.get('business_name', 'there')
    contact = lead.get('contact_name') or 'Practice Manager'
    
    rating_str = ''
    if lead.get('google_review_count'):
        rating_str = f' with {lead["google_review_count"]} reviews and a {lead["google_rating"]}★ rating'
    
    # Templates by category
    templates = {
        'direct_user': {
            'subject': f"Your patients are waiting — Sophia AI can help {lead['business_name']}",
            'body': f"""Hi {contact},

I noticed {lead['business_name']} is serving {lead['city']}, {lead['state']} — looks like a busy practice{rating_str}.

What happens when all lines are busy and a patient needs to schedule? If they hit voicemail, that's a lost appointment.

Sophia is an AI receptionist that answers every call, looks up patients in your EHR, verifies insurance, and books appointments — 24/7, in natural conversation. No scripts, no "press 1 for scheduling."

Practices using Sophia are booking appointments they used to miss and freeing their front desk for in-person patients.

Would you be open to a 10-minute call to see if Sophia would work for {lead['business_name']}?

Best,
Jeremiah Earl
Founder, Sophia AI
trysoph.com"""
        },
        'medical_billing': {
            'subject': f"Add AI receptionist to your billing clients — new revenue stream",
            'body': f"""Hi {contact},

Your billing clients already trust you with their revenue cycle. What about their front desk?

Sophia is an AI receptionist that handles calls, scheduling, insurance verification, and patient intake. It integrates with major EHRs and works alongside existing staff.

We're looking for billing companies who want to offer AI receptionist services to their existing clients — a natural upsell with recurring revenue for you.

Interested in a partnership conversation?

Best,
Jeremiah Earl
Founder, Sophia AI
trysoph.com"""
        }
    }
    
    target = lead.get('target_type', 'direct_user')
    template = templates.get(target, templates['direct_user'])
    
    return jsonify({
        'to': lead.get('email'),
        'subject': template['subject'],
        'body': template['body'],
        'lead': {k: str(v) if isinstance(v, datetime) else v for k, v in lead.items()},
    })

# ── Static files ──────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('static', path)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8085))
    print(f"Sophia Admin Portal starting on port {port}...")
    app.run(host='0.0.0.0', port=port, debug=False)

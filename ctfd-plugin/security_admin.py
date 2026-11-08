"""Admin → Security: flag-sharing log (Cheaters) and Account sharing.

Rendered inside CTFd's admin panel. The Platform page shows an alert using
/admin/platform/security-summary.
"""

from flask import Blueprint, jsonify, render_template_string

# Players logged in to one account from this many different addresses are
# flagged. On a campus LAN a player's laptop normally keeps one address, so
# 3+ usually means the account is shared between several devices/people.
SHARING_THRESHOLD = 3

# Addresses that are the platform itself, not players: k3s pod and service
# networks, and localhost. Everything else (including private campus
# ranges like 10.x / 192.168.x) is a real client.
_INTERNAL_IPS = """
    tr.ip NOT LIKE '10.42.%%' AND tr.ip NOT LIKE '10.43.%%' AND tr.ip NOT LIKE '127.%%'
"""


def _cheater_rows(db, text, limit=None):
    sql = """
        SELECT c.id, st.name AS submitting_team, c.submitting_team_id,
               su.name AS submitting_user, c.submitting_user_id, c.submitted_flag,
               ot.name AS owner_team, c.owner_team_id, c.challenge_manifest_id,
               c.submitted_at
        FROM cheaters c
        LEFT JOIN teams st ON st.id = c.submitting_team_id
        LEFT JOIN users su ON su.id = c.submitting_user_id
        LEFT JOIN teams ot ON ot.id = c.owner_team_id
        ORDER BY c.submitted_at DESC
    """ + (f" LIMIT {int(limit)}" if limit else "")
    return db.session.execute(text(sql)).fetchall()


def _sharing_rows(db, text):
    return db.session.execute(text(f"""
        SELECT u.id AS user_id, u.name AS user, t.id AS team_id, t.name AS team,
               COUNT(DISTINCT tr.ip) AS distinct_ips, COUNT(*) AS total_events,
               GROUP_CONCAT(DISTINCT tr.ip ORDER BY tr.ip SEPARATOR ', ') AS ip_list,
               MAX(tr.date) AS last_seen
        FROM tracking tr
        JOIN users u ON u.id = tr.user_id
        LEFT JOIN teams t ON t.id = u.team_id
        WHERE {_INTERNAL_IPS}
        GROUP BY u.id, u.name, t.id, t.name
        ORDER BY distinct_ips DESC, total_events DESC
    """)).fetchall()


def register_security_admin(app):
    from CTFd.models import db
    from CTFd.plugins import register_admin_plugin_menu_bar
    from CTFd.utils.decorators import admins_only
    from sqlalchemy import text

    bp = Blueprint("security_admin", __name__)

    @bp.route("/admin/cheaters")
    @admins_only
    def cheaters_page():
        try:
            rows = _cheater_rows(db, text)
            error = None
        except Exception as e:  # table missing on a brand-new install, etc.
            rows, error = [], str(e)
        return render_template_string(PAGE, tab="cheaters", rows=rows, error=error,
                                      threshold=SHARING_THRESHOLD)

    @bp.route("/admin/account-sharing")
    @admins_only
    def sharing_page():
        try:
            rows = _sharing_rows(db, text)
            error = None
        except Exception as e:
            rows, error = [], str(e)
        return render_template_string(PAGE, tab="sharing", rows=rows, error=error,
                                      threshold=SHARING_THRESHOLD)

    @bp.route("/admin/platform/security-summary")
    @admins_only
    def security_summary():
        out = {"cheaters": 0, "last": None, "shared_accounts": 0}
        try:
            out["cheaters"] = db.session.execute(text("SELECT COUNT(*) FROM cheaters")).scalar() or 0
            last = _cheater_rows(db, text, limit=1)
            if last:
                r = last[0]
                out["last"] = {"team": r.submitting_team, "user": r.submitting_user,
                               "owner": r.owner_team, "challenge": r.challenge_manifest_id,
                               "at": str(r.submitted_at)[:16]}
            out["shared_accounts"] = sum(1 for r in _sharing_rows(db, text)
                                         if r.distinct_ips >= SHARING_THRESHOLD)
        except Exception:
            pass
        return jsonify(out)

    app.register_blueprint(bp)
    register_admin_plugin_menu_bar(title="Security", route="/admin/cheaters")


PAGE = r"""
{% extends "admin/base.html" %}
{% block content %}
<div class="jumbotron">
  <div class="container">
    <h1>Security</h1>
    <p class="text-muted mb-0">Flag sharing between teams, and accounts used from several devices.</p>
  </div>
</div>
<div class="container">
  <ul class="nav nav-tabs mb-4">
    <li class="nav-item">
      <a class="nav-link {{ 'active' if tab == 'cheaters' }}" href="/admin/cheaters">Flag sharing</a>
    </li>
    <li class="nav-item">
      <a class="nav-link {{ 'active' if tab == 'sharing' }}" href="/admin/account-sharing">Account sharing</a>
    </li>
    <li class="nav-item ms-auto ml-auto">
      <a class="nav-link" href="/admin/platform">← Platform</a>
    </li>
  </ul>

  {% if error %}
    <div class="alert alert-warning">Couldn't read the data: {{ error }}</div>
  {% endif %}

  {% if tab == 'cheaters' %}
    <p class="text-muted">
      Each row is a submission of <strong>another team's</strong> flag. Per-team flags make this
      detectable: the flag only works for the team it was made for. Challenges with one flag for
      everyone can't be checked this way.
    </p>
    <p><span class="badge bg-danger badge-danger">{{ rows|length }} incident{{ '' if rows|length == 1 else 's' }}</span></p>
    <table class="table table-striped align-middle">
      <thead><tr>
        <th>Time</th><th>Submitting team</th><th>Player</th><th>Flag belongs to</th>
        <th>Challenge</th><th>Submitted flag</th>
      </tr></thead>
      <tbody>
      {% for r in rows %}
        <tr>
          <td class="text-nowrap small">{{ (r.submitted_at|string)[:16] }}</td>
          <td>{% if r.submitting_team_id %}<a href="/admin/teams/{{ r.submitting_team_id }}">{{ r.submitting_team or '?' }}</a>{% else %}?{% endif %}</td>
          <td>{% if r.submitting_user_id %}<a href="/admin/users/{{ r.submitting_user_id }}">{{ r.submitting_user or '?' }}</a>{% else %}?{% endif %}</td>
          <td>{% if r.owner_team_id %}<a class="text-danger" href="/admin/teams/{{ r.owner_team_id }}">{{ r.owner_team or '?' }}</a>{% else %}?{% endif %}</td>
          <td><code>{{ r.challenge_manifest_id }}</code></td>
          <td class="small"><code>{{ (r.submitted_flag or '')[:60] }}</code></td>
        </tr>
      {% else %}
        <tr><td colspan="6" class="text-center text-muted py-4">No flag sharing recorded.</td></tr>
      {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p class="text-muted">
      Addresses each account was used from. {{ threshold }} or more different addresses is marked
      <strong>suspicious</strong>: a player normally keeps one, so several usually means the
      account is shared. The platform's own addresses are left out.
    </p>
    {% set sus = rows|selectattr('distinct_ips', 'ge', threshold)|list %}
    <p>
      <span class="badge bg-secondary badge-secondary">{{ rows|length }} account{{ '' if rows|length == 1 else 's' }} seen</span>
      <span class="badge bg-danger badge-danger">{{ sus|length }} suspicious</span>
    </p>
    <table class="table table-striped align-middle">
      <thead><tr>
        <th>Status</th><th>Player</th><th>Team</th><th class="text-center">Addresses</th>
        <th class="text-center">Events</th><th>Address list</th><th>Last seen</th>
      </tr></thead>
      <tbody>
      {% for r in rows %}
        <tr>
          <td>{% if r.distinct_ips >= threshold %}<span class="badge bg-danger badge-danger">Suspicious</span>{% else %}<span class="badge bg-success badge-success">OK</span>{% endif %}</td>
          <td><a href="/admin/users/{{ r.user_id }}">{{ r.user }}</a></td>
          <td>{% if r.team_id %}<a href="/admin/teams/{{ r.team_id }}">{{ r.team }}</a>{% else %}<span class="text-muted">no team</span>{% endif %}</td>
          <td class="text-center"><strong>{{ r.distinct_ips }}</strong></td>
          <td class="text-center">{{ r.total_events }}</td>
          <td class="small"><code>{{ r.ip_list or '' }}</code></td>
          <td class="text-nowrap small">{{ (r.last_seen|string)[:16] }}</td>
        </tr>
      {% else %}
        <tr><td colspan="7" class="text-center text-muted py-4">No logins recorded yet.</td></tr>
      {% endfor %}
      </tbody>
    </table>
  {% endif %}
</div>
{% endblock %}
"""

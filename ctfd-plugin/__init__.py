"""
CTFd Controller Plugin - manages team challenge instances.

Provides UI for starting/stopping challenge pods and displays their status.
Proxies browser requests to the cluster-internal controller API.
Uses CTFd's register_script() for reliable script injection.

Also registers a custom 'team' flag type that:
  - Derives the expected flag on the fly from the submitting team's ID + CTF_MASTER_SECRET
  - Detects cross-team flag submission (cheating) and logs it to the `cheaters` table
  - Makes it cryptographically impossible to score points using another team's flag
"""

from flask import Blueprint, request as flask_request, jsonify, Response, make_response, send_file
import urllib.request
import urllib.error
import json
import hmac as _hmac
import hashlib
import os
import io
import html
import zipfile
import threading
from datetime import datetime
import logging as _logging

_audit_log = _logging.getLogger("ctfd.audit")
_audit_log.setLevel(_logging.INFO)
if not _audit_log.handlers:
    _audit_handler = _logging.StreamHandler()
    _audit_handler.setLevel(_logging.INFO)
    _audit_handler.setFormatter(_logging.Formatter("%(message)s"))
    _audit_log.addHandler(_audit_handler)
_audit_log.propagate = False  # don't double-emit via root logger


# ── Helpers ──────────────────────────────────────────────────────────────────

def _slugify_name(name):
    """Convert a CTFd challenge name to the manifest-style slug used by downloads."""
    import re as _re
    slug = (name or "").lower().replace(" ", "-")
    return _re.sub(r"[^a-z0-9-]", "", slug)


def _get_current_team_id():
    """Return the current authenticated team id, or None."""
    try:
        from CTFd.utils.user import get_current_team
        team = get_current_team()
        return str(team.id) if team else None
    except Exception:
        return None


def _get_current_user_id():
    """Return the current authenticated user id, or None."""
    try:
        from CTFd.utils.user import get_current_user
        user = get_current_user()
        return user.id if user else None
    except Exception:
        return None


def _get_status_code(resp):
    """Extract HTTP status code from a Flask Response or (response, int) tuple."""
    if isinstance(resp, tuple):
        return resp[1]
    return getattr(resp, "status_code", 200)


def _challenge_is_visible(challenge_manifest_id):
    """Return True if the manifest slug maps to a visible CTFd challenge."""
    try:
        from CTFd.models import Challenges
        challenge = Challenges.query.all()
        for ch in challenge:
            if _slugify_name(getattr(ch, "name", "")) != challenge_manifest_id:
                continue
            return getattr(ch, "state", "") == "visible"
    except Exception:
        return False
    return False


def _challenge_id_is_visible(challenge_id):
    """Return True if the numeric CTFd challenge id is visible."""
    try:
        from CTFd.models import Challenges
        challenge = Challenges.query.get(int(challenge_id))
        return bool(challenge and getattr(challenge, "state", "") == "visible")
    except Exception:
        return False


def _escape_html(value):
    """Escape DB/user-controlled values before rendering custom admin HTML."""
    return html.escape("" if value is None else str(value), quote=True)


def _derive(team_id, challenge_manifest_id, master_secret):
    """Return the derived flag string for a given team + challenge."""
    message = f"{team_id}:{challenge_manifest_id}".encode()
    digest = _hmac.new(
        master_secret.encode(),
        message,
        hashlib.sha256,
    ).hexdigest()[:32]
    prefix = os.environ.get("FLAG_PREFIX", "infodays")
    return f"{prefix}{{{digest}}}"


def _log_cheater(submitting_team_id, submitting_user_id, submitted_flag, owner_team_id, challenge_manifest_id):
    """Insert a row into the cheaters table. Never raises."""
    try:
        from CTFd.models import db
        from sqlalchemy import text
        db.session.execute(
            text("""
                INSERT INTO cheaters
                    (submitting_team_id, submitting_user_id, submitted_flag, owner_team_id, challenge_manifest_id, submitted_at)
                VALUES
                    (:sub_tid, :sub_uid, :flag, :own_tid, :chal, :ts)
            """),
            {
                "sub_tid": submitting_team_id,
                "sub_uid": submitting_user_id,
                "flag":    submitted_flag,
                "own_tid": owner_team_id,
                "chal":    challenge_manifest_id,
                "ts":      datetime.utcnow(),
            },
        )
        db.session.commit()
    except Exception:
        pass  # Never let cheating detection crash flag validation


def _log_audit(action, user_id, team_id, challenge_id, remote_ip, outcome, status_code, requested_team_id=None):
    """
    Emit one structured JSON audit line to stdout.
    Grafana Alloy ships pod stdout to Loki automatically. Never raises.

    action            : "status" | "activate" | "deactivate" | "download"
    user_id           : CTFd user id of the caller (int or None)
    team_id           : effective team id after server-side enforcement
    challenge_id      : challenge manifest slug, or None for status calls
    remote_ip         : flask_request.remote_addr (real IP after ingress fix 9a10b46)
    outcome           : "allowed" | "denied"
    status_code       : HTTP status returned to the caller
    requested_team_id : raw team_id from URL/body — when it differs from team_id, cross-team abuse is indicated
    """
    try:
        _audit_log.info(json.dumps({
            "audit_event":       True,
            "action":            action,
            "user_id":           user_id,
            "team_id":           str(team_id) if team_id is not None else None,
            "requested_team_id": str(requested_team_id) if requested_team_id is not None else None,
            "challenge_id":      challenge_id,
            "remote_ip":         remote_ip,
            "outcome":           outcome,
            "status_code":       status_code,
            "ts":                datetime.utcnow().isoformat() + "Z",
        }))
    except Exception:
        pass  # Never let audit logging crash a user-facing route


# ── Team-bound flag type ─────────────────────────────────────────────────────

class CTFdTeamFlag:
    """
    Flag type that validates against the submitting team's derived flag.

    Stored in CTFd flags table as:
        type    = "team"
        content = "team-derived"   (unused placeholder)
        data    = challenge manifest ID  e.g. "pwn-bof"

    One flag row per challenge — no per-team rows needed.

    On wrong submission:
        - Checks if the flag belongs to any other team (O(n) HMAC checks, n=50 → fast)
        - If yes → logs to `cheaters` table and returns False
        - If no  → returns False silently (just a wrong guess)
    """
    name = "team"
    templates = {
        "create": "/plugins/flags/assets/static/create.html",
        "update": "/plugins/flags/assets/static/edit.html",
    }

    @staticmethod
    def compare(chal_key_obj, provided):
        from CTFd.utils.user import get_current_team, get_current_user

        team = get_current_team()
        if team is None:
            return False

        user = get_current_user()
        user_id = user.id if user else None

        challenge_manifest_id = chal_key_obj.data or ""
        master_secret = os.environ.get("CTF_MASTER_SECRET", "")
        if not master_secret or not challenge_manifest_id:
            return False

        # ── Check submitting team's own flag ──────────────────────────────────
        expected = _derive(team.id, challenge_manifest_id, master_secret)
        if _hmac.compare_digest(expected, provided):
            return True

        # ── Wrong flag — check if it belongs to another team (cheating) ───────
        try:
            from CTFd.models import Teams
            all_teams = Teams.query.filter(Teams.id != team.id).all()
            for other in all_teams:
                candidate = _derive(other.id, challenge_manifest_id, master_secret)
                if _hmac.compare_digest(candidate, provided):
                    _log_cheater(team.id, user_id, provided, other.id, challenge_manifest_id)
                    break
        except Exception:
            pass  # Never crash flag validation due to cheating detection

        return False


# ── Multi-flag challenge type ─────────────────────────────────────────────────

# Thread-local used to signal partial-progress returns in attempt() so that
# fail() can suppress the Fails-table insert for those cases.
_mf_tls = threading.local()


def _register_multi_flag_model():
    """Register a SQLAlchemy polymorphic subclass for multi_flag challenges.

    CTFd's Challenges model uses type as a polymorphic discriminator.
    Without a registered subclass, SQLAlchemy emits a warning when loading
    challenges with type='multi_flag' and may not refresh them correctly.
    This creates a minimal mapped subclass that satisfies the ORM.
    """
    try:
        from CTFd.models import Challenges, db
        # Check if already registered to avoid duplicate mapper error on hot-reload
        if "multi_flag" not in Challenges.__mapper__.polymorphic_map:
            class MultiFlagChallengeModel(Challenges):
                __mapper_args__ = {"polymorphic_identity": "multi_flag"}
            # Keep reference so the class isn't GC'd
            _register_multi_flag_model._model = MultiFlagChallengeModel
    except Exception:
        pass  # Non-fatal — challenge still works via raw SQL


class MultiFlagChallenge:
    """
    Custom CTFd challenge type for challenges with multiple independent flags.

    Each flag (component) has its own scoring params (initial/minimum/decay).
    Points are awarded incrementally via CTFd Awards as each component is found.
    A Solves row is only inserted when ALL components are found.

    Component data is stored in two plugin-managed tables:
        multi_flag_components  — per-challenge component definitions
        multi_flag_solves      — per-team per-component solve records
    """

    id = "multi_flag"
    name = "multi_flag"
    templates = {
        "create": "/plugins/challenges/assets/create.html",
        "update": "/plugins/challenges/assets/update.html",
        "view":   "/plugins/challenges/assets/view.html",
    }
    scripts = {
        "create": "/plugins/challenges/assets/create.js",
        "update": "/plugins/challenges/assets/update.js",
        "view":   "/plugins/challenges/assets/view.js",
    }

    @classmethod
    def attempt(cls, challenge, request):
        from CTFd.utils.user import get_current_user, get_current_team
        from CTFd.models import Awards, db
        from sqlalchemy import text

        provided = (request.json or {}).get("submission", "").strip()
        team = get_current_team()
        user = get_current_user()
        if not team or not user:
            return (False, "No team")

        master_secret = os.environ.get("CTF_MASTER_SECRET", "")
        if not master_secret:
            return (False, "Server configuration error")

        # Load all components for this challenge
        components = db.session.execute(
            text("SELECT component_id, env_var, initial, minimum, decay, "
                 "COALESCE(static_flag, '') "
                 "FROM multi_flag_components WHERE challenge_id = :cid "
                 "ORDER BY component_id"),
            {"cid": challenge.id}
        ).fetchall()

        if not components:
            return (False, "Challenge not configured — contact admin")

        # Load this team's already-found components
        solved_set = {row[0] for row in db.session.execute(
            text("SELECT component_id FROM multi_flag_solves "
                 "WHERE challenge_id = :cid AND team_id = :tid"),
            {"cid": challenge.id, "tid": team.id}
        ).fetchall()}

        total = len(components)

        import re as _re

        def _apk_expected(sf, cid, tid):
            """For per-team APK flags: static_flag is a template ending in _{hex8}}.
            Replace the stored placeholder hex8 with this team's derived hex8."""
            m = _re.search(r'_([0-9a-f]{8})\}$', sf, _re.IGNORECASE)
            if not m:
                return None
            apk_key = _re.sub(r'-\d+$', '', cid)
            team_hex8 = _hmac.new(
                master_secret.encode(),
                f"{tid}:{apk_key}".encode(),
                hashlib.sha256,
            ).digest()[:4].hex()
            return sf[:m.start(1)] + team_hex8 + '}'

        def _team_table_flag(cid, comp_id, tid):
            """Look up per-team flag from multi_flag_team_flags table."""
            row = db.session.execute(
                text("SELECT flag_content FROM multi_flag_team_flags "
                     "WHERE challenge_id=:cid AND component_id=:comp AND team_id=:tid"),
                {"cid": cid, "comp": comp_id, "tid": tid}
            ).fetchone()
            return row[0] if row else None

        for comp in components:
            comp_id, env_var, initial, minimum, decay, static_flag = comp

            # Check multi_flag_team_flags table first (per-team pre-generated flags)
            team_table_flag = _team_table_flag(challenge.id, comp_id, team.id)
            if team_table_flag is not None:
                if provided != team_table_flag:
                    continue
            elif static_flag:
                expected_flag = _apk_expected(static_flag, comp_id, team.id)
                if expected_flag is not None:
                    # Per-team APK flag: compare against team-derived version
                    if provided != expected_flag:
                        continue
                else:
                    # Plain static flag: exact match for all teams
                    if provided != static_flag:
                        continue
            else:
                # Service challenge: validate against HMAC-derived flag
                expected = _derive(team.id, comp_id, master_secret)
                if not _hmac.compare_digest(expected, provided):
                    continue

            # Matched this component
            if comp_id in solved_set:
                _mf_tls.is_progress = True
                return (False, "You already found this flag!")

            # How many teams have already found this component (for decay calculation)
            solve_count = db.session.execute(
                text("SELECT COUNT(*) FROM multi_flag_solves WHERE component_id = :cid"),
                {"cid": comp_id}
            ).scalar() or 0

            # Decayed score: linear from initial → minimum over decay solves
            pts = max(minimum, initial - (initial - minimum) * solve_count // max(decay, 1))

            # Record the solve
            db.session.execute(
                text("INSERT INTO multi_flag_solves "
                     "(challenge_id, team_id, component_id, awarded_at) "
                     "VALUES (:chid, :tid, :cmpid, NOW())"),
                {"chid": challenge.id, "tid": team.id, "cmpid": comp_id}
            )

            # Grant an Award for the component points
            db.session.add(Awards(
                user_id=user.id,
                team_id=team.id,
                name=f"{challenge.name} [{comp_id}]",
                description="multi_flag component",
                value=pts,
                category=challenge.category,
                icon="flag",
            ))
            db.session.commit()

            new_solved_count = len(solved_set) + 1
            if new_solved_count == total:
                # All flags found — solve() will be called to insert Solves row
                return (True, f"All {total} flags found! Challenge complete!")

            _mf_tls.is_progress = True
            return (False, f"Flag found! {new_solved_count}/{total} complete. +{pts} pts")

        # Cheating detection for HMAC-derived and per-team APK components
        try:
            from CTFd.models import Teams
            all_teams = Teams.query.filter(Teams.id != team.id).all()
            all_comps = db.session.execute(
                text("SELECT component_id, COALESCE(static_flag,'') "
                     "FROM multi_flag_components WHERE challenge_id = :cid"),
                {"cid": challenge.id}
            ).fetchall()
            user_obj = get_current_user()
            for other in all_teams:
                for (cmp_id, sf) in all_comps:
                    candidate = _team_table_flag(challenge.id, cmp_id, other.id)
                    if candidate is None:
                        if sf:
                            candidate = _apk_expected(sf, cmp_id, other.id)
                            if candidate is None:
                                continue
                        else:
                            candidate = _derive(other.id, cmp_id, master_secret)
                    if _hmac.compare_digest(candidate, provided):
                        _log_cheater(team.id, user_obj.id if user_obj else None,
                                     provided, other.id, cmp_id)
                        break
        except Exception:
            pass

        return (False, "Incorrect")

    @classmethod
    def solve(cls, user, team, challenge, request):
        """Insert a Solves row with value=0 (marks challenge fully complete)."""
        from CTFd.models import Solves, db
        solve = Solves(
            user_id=user.id,
            team_id=team.id if team else None,
            challenge_id=challenge.id,
            ip=request.remote_addr,
            provided=(request.json or {}).get("submission", ""),
        )
        db.session.add(solve)
        db.session.commit()

    @classmethod
    def fail(cls, user, team, challenge, request):
        """Suppress Fails-table insertion for partial-progress responses."""
        if getattr(_mf_tls, "is_progress", False):
            _mf_tls.is_progress = False
            return
        # True wrong guess — call base implementation
        try:
            from CTFd.plugins.challenges import BaseChallenge
            BaseChallenge.fail(user, team, challenge, request)
        except Exception:
            pass

    @classmethod
    def read(cls, challenge):
        """Serialize challenge for API responses."""
        data = {
            "id": challenge.id,
            "name": challenge.name,
            "value": challenge.value,
            "description": challenge.description,
            "category": challenge.category,
            "state": challenge.state,
            "max_attempts": challenge.max_attempts,
            "type": challenge.type,
            "type_data": {
                "id": cls.id,
                "name": cls.name,
                "templates": cls.templates,
                "scripts": cls.scripts,
            },
        }
        return data

    @classmethod
    def update(cls, challenge, request):
        data = request.form or request.get_json()
        for attr in ("name", "description", "value", "category", "state", "max_attempts"):
            if attr in data:
                setattr(challenge, attr, data[attr])
        from CTFd.models import db
        db.session.commit()
        return challenge

    @classmethod
    def delete(cls, challenge):
        from CTFd.models import db
        from sqlalchemy import text
        db.session.execute(
            text("DELETE FROM multi_flag_components WHERE challenge_id = :cid"),
            {"cid": challenge.id}
        )
        db.session.execute(
            text("DELETE FROM multi_flag_solves WHERE challenge_id = :cid"),
            {"cid": challenge.id}
        )
        db.session.delete(challenge)
        db.session.commit()

    @classmethod
    def create(cls, request):
        from CTFd.models import Challenges, db
        data = request.form or request.get_json()
        challenge = Challenges(
            name=data["name"],
            description=data.get("description", ""),
            value=int(data.get("value", data.get("initial", 500))),
            category=data.get("category", ""),
            state=data.get("state", "visible"),
            max_attempts=data.get("max_attempts", 0),
            type="multi_flag",
        )
        db.session.add(challenge)
        db.session.commit()
        return challenge


# ── Plugin entry point ───────────────────────────────────────────────────────

def load(app):
    """Load the CTFd plugin."""
    from CTFd.utils.plugins import register_script

    # Keep CTFd cookies host-only. Challenge services are served from
    # challenge NodePorts and must not receive CTFd session cookies.

    # ── Branding: inject stylesheet + Google Fonts ─────────────────────────
    GOOGLE_FONTS_PRECONNECT = '<link rel="preconnect" href="https://fonts.googleapis.com">'
    GOOGLE_FONTS_PRECONNECT2 = '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    GOOGLE_FONTS_URL = (
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        'family=Inter:wght@400;500;600;700'
        '&family=JetBrains+Mono:wght@400;500'
        '&display=swap" media="print" onload="this.media=\'all\'">'
    )
    BRAND_CSS_LINK = (
        '<link rel="stylesheet" href="/plugins/controller-plugin/assets/style.css">'
    )
    GOOGLE_FONTS_NOSCRIPT = (
        '<noscript><link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        'family=Inter:wght@400;500;600;700'
        '&family=JetBrains+Mono:wght@400;500'
        '&display=swap"></noscript>'
    )
    WIDGETS_CSS_LINK = (
        '<link rel="stylesheet" href="/plugins/controller-plugin/assets/widgets.css">'
    )
    # CTF_BRANDING=true: custom dark theme, fonts, GOAL! overlay.
    # Otherwise CTFd's default theme, plus only the instance-control styles.
    BRANDING = os.environ.get("CTF_BRANDING", "false").lower() == "true"
    if BRANDING:
        INJECT_TAGS = "\n".join([
            GOOGLE_FONTS_PRECONNECT,
            GOOGLE_FONTS_PRECONNECT2,
            GOOGLE_FONTS_URL,
            GOOGLE_FONTS_NOSCRIPT,
            WIDGETS_CSS_LINK,
            BRAND_CSS_LINK,
        ])
    else:
        INJECT_TAGS = WIDGETS_CSS_LINK

    @app.after_request
    def inject_brand_assets(response):
        if response.content_type and response.content_type.startswith("text/html"):
            data = response.get_data(as_text=True)
            if "</head>" in data:
                response.set_data(data.replace("</head>", INJECT_TAGS + "\n</head>", 1))
        return response

    blueprint = Blueprint(
        "controller_plugin",
        __name__,
        static_folder="assets",
        static_url_path="/plugins/controller-plugin/assets",
    )

    # ── Dynamic config endpoint ───────────────────────────────────────────────

    @blueprint.route("/controller-plugin/config.js")
    def controller_config_js():
        team_id = "null"
        is_admin = "false"
        try:
            from CTFd.utils.user import get_current_user
            user = get_current_user()
            if user:
                tid = getattr(user, "team_id", None)
                team_id = str(tid) if tid is not None else "null"
                if getattr(user, "type", "") == "admin":
                    is_admin = "true"
        except Exception:
            pass

        js = "window.CTF_CONTROLLER = {{ team_id: {}, is_admin: {}, branding: {} }};".format(
            team_id, is_admin, "true" if BRANDING else "false"
        )
        resp = make_response(js)
        resp.headers["Content-Type"] = "application/javascript"
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    @blueprint.route("/controller-plugin/challenge-map")
    def controller_challenge_map():
        """Return {ctfd_challenge_id: manifest_id} for all team-flag challenges.
        Player-accessible (requires login only). Used by plugin.js to resolve
        manifest IDs without needing admin access to /api/v1/flags."""
        if not _check_authed():
            return jsonify({"error": "unauthorized"}), 401
        try:
            from CTFd.models import Flags
            flags = Flags.query.filter_by(type="team").all()
            mapping = {}
            for f in flags:
                if f.data:
                    mapping[str(f.challenge_id)] = f.data
            resp = make_response(jsonify(mapping))
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return resp
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Proxy routes ──────────────────────────────────────────────────────────

    def _get_controller_config():
        from CTFd.models import Configs
        url_cfg = Configs.query.filter_by(key="CONTROLLER_URL").first()
        key_cfg = Configs.query.filter_by(key="X_CONTROLLER_KEY").first()
        return (
            url_cfg.value if url_cfg else "http://controller-api.ctfd.svc.cluster.local",
            key_cfg.value if key_cfg else "",
        )

    def _proxy(method, path, data=None):
        base_url, api_key = _get_controller_config()
        url = base_url.rstrip("/") + path
        headers = {"X-Controller-Key": api_key, "Content-Type": "application/json"}
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            return Response(resp.read(), status=resp.status, content_type="application/json")
        except urllib.error.HTTPError as e:
            return Response(e.read(), status=e.code, content_type="application/json")
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    def _check_authed():
        try:
            from CTFd.utils.decorators import authed
            return authed()
        except Exception:
            return False

    def _check_admin():
        try:
            from CTFd.utils.user import get_current_user
            user = get_current_user()
            return user and getattr(user, "type", "") == "admin"
        except Exception:
            return False

    @blueprint.route("/controller-proxy/status/<team_id>")
    def proxy_status(team_id):
        user_id         = _get_current_user_id()
        current_team_id = _get_current_team_id()
        remote_ip       = flask_request.remote_addr

        if not _check_authed():
            _log_audit("status", user_id, current_team_id, None, remote_ip, "denied", 401,
                       requested_team_id=team_id)
            return jsonify({"error": "unauthorized"}), 401

        if not _check_admin():
            if not current_team_id:
                _log_audit("status", user_id, None, None, remote_ip, "denied", 403,
                           requested_team_id=team_id)
                return jsonify({"error": "no team"}), 403
            if team_id != current_team_id:
                _log_audit("status", user_id, current_team_id, None, remote_ip, "denied", 403,
                           requested_team_id=team_id)
                return jsonify({"error": "forbidden"}), 403

        resp = _proxy("GET", "/status/" + team_id)
        _log_audit("status", user_id, current_team_id, None, remote_ip, "allowed",
                   _get_status_code(resp), requested_team_id=team_id)
        return resp

    @blueprint.route("/controller-proxy/activate", methods=["POST"])
    def proxy_activate():
        user_id   = _get_current_user_id()
        remote_ip = flask_request.remote_addr
        data      = flask_request.get_json(silent=True) or {}

        if not _check_authed():
            _log_audit("activate", user_id, None, data.get("challenge_id"), remote_ip, "denied", 401)
            return jsonify({"error": "unauthorized"}), 401

        if not _check_admin():
            current_team_id = _get_current_team_id()
            if not current_team_id:
                _log_audit("activate", user_id, None, data.get("challenge_id"), remote_ip, "denied", 403)
                return jsonify({"error": "no team"}), 403
            data["team_id"] = current_team_id
        else:
            current_team_id = data.get("team_id")

        resp = _proxy("POST", "/activate", data)
        _log_audit("activate", user_id, current_team_id, data.get("challenge_id"),
                   remote_ip, "allowed", _get_status_code(resp))
        return resp

    @blueprint.route("/controller-proxy/deactivate", methods=["POST"])
    def proxy_deactivate():
        user_id   = _get_current_user_id()
        remote_ip = flask_request.remote_addr
        data      = flask_request.get_json(silent=True) or {}

        if not _check_authed():
            _log_audit("deactivate", user_id, None, data.get("challenge_id"), remote_ip, "denied", 401)
            return jsonify({"error": "unauthorized"}), 401

        if not _check_admin():
            current_team_id = _get_current_team_id()
            if not current_team_id:
                _log_audit("deactivate", user_id, None, data.get("challenge_id"), remote_ip, "denied", 403)
                return jsonify({"error": "no team"}), 403
            data["team_id"] = current_team_id
        else:
            current_team_id = data.get("team_id")

        resp = _proxy("POST", "/deactivate", data)
        _log_audit("deactivate", user_id, current_team_id, data.get("challenge_id"),
                   remote_ip, "allowed", _get_status_code(resp))
        return resp

    # ── Per-team static challenge download ───────────────────────────────────

    @blueprint.route("/download/<challenge_manifest_id>")
    def download_static(challenge_manifest_id):
        """
        Serve a per-team zip for static/file-download challenges.

        Fetches {challenge_id}/{team_id}.zip from the static-files-server.
        Files contain the challenge artifacts with flags already baked in by
        generate-static-artifacts.py — no flag injection happens here.

        Auth: requires logged-in user with a team.
        """
        import re
        import urllib.request as _ureq
        from CTFd.utils.user import get_current_user, get_current_team

        if not re.match(r'^[a-z0-9-]+$', challenge_manifest_id):
            return jsonify({"error": "invalid challenge id"}), 400

        remote_ip = flask_request.remote_addr

        user = get_current_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401

        team = get_current_team()
        if not team:
            return jsonify({"error": "no team"}), 403

        if not _check_admin() and not _challenge_is_visible(challenge_manifest_id):
            _log_audit("download", user.id, str(team.id), challenge_manifest_id, remote_ip, "denied", 404)
            return jsonify({"error": "challenge not available"}), 404

        STATIC_FILES_URL = os.environ.get(
            "STATIC_FILES_URL",
            "http://static-files.ctfd.svc.cluster.local",
        )

        # Serve per-team zip: {challenge_id}/{team_id}.zip
        files_url = f"{STATIC_FILES_URL}/{challenge_manifest_id}/{team.id}.zip"
        try:
            with _ureq.urlopen(files_url, timeout=10) as resp:
                if resp.status != 200:
                    _log_audit("download", user.id, str(team.id), challenge_manifest_id, remote_ip, "denied", 404)
                    return jsonify({"error": "challenge files not available"}), 404
                zip_data = resp.read()
        except Exception:
            _log_audit("download", user.id, str(team.id), challenge_manifest_id, remote_ip, "denied", 404)
            return jsonify({"error": "challenge files not available"}), 404

        _log_audit("download", user.id, str(team.id), challenge_manifest_id, remote_ip, "allowed", 200)
        return send_file(
            io.BytesIO(zip_data),
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{challenge_manifest_id}.zip",
        )

    # ── Challenge isolation: nginx auth_request endpoint ─────────────────────

    @blueprint.route("/team-verify")
    def team_verify():
        """
        Called server-side by nginx auth_request before routing to a challenge pod.
        Verifies the logged-in team owns the requested challenge instance.

        nginx passes the original $host via ?host=<value>.
        Host format: team-<id>-<challenge>.challenges.infodays.net

        Returns 200 (allow), 401 (not logged in → redirect to login), 403 (wrong team).
        """
        from CTFd.utils.user import get_current_user, get_current_team

        user = get_current_user()
        if not user:
            return "", 401

        # Admins can access any challenge instance
        if getattr(user, "type", "") == "admin":
            return "", 200

        host = flask_request.args.get("host", "")
        subdomain = host.split(".")[0]        # "team-1-web-sqli"
        parts = subdomain.split("-")          # ["team", "1", "web", "sqli"]

        if len(parts) < 2 or parts[0] != "team":
            return "", 403

        try:
            challenge_team_id = int(parts[1])
        except ValueError:
            return "", 403

        team = get_current_team()
        if not team:
            return "", 401

        if team.id == challenge_team_id:
            return "", 200
        return "", 403

    # ── Multi-flag progress endpoint ──────────────────────────────────────────

    @blueprint.route("/api/plugin/multi-flag-progress/<int:challenge_id>")
    def multi_flag_progress(challenge_id):
        """
        Returns per-team progress for a multi_flag challenge.
        Any authenticated user can query their own progress.
        """
        if not _check_authed():
            return jsonify({"error": "unauthorized"}), 401

        try:
            from CTFd.utils.user import get_current_team
            from CTFd.models import db
            from sqlalchemy import text

            if not _check_admin() and not _challenge_id_is_visible(challenge_id):
                return jsonify({"error": "challenge not available"}), 404

            team = get_current_team()
            team_id = team.id if team else None

            components = db.session.execute(
                text("SELECT component_id, initial, minimum, decay "
                     "FROM multi_flag_components WHERE challenge_id = :cid "
                     "ORDER BY component_id"),
                {"cid": challenge_id}
            ).fetchall()

            total = len(components)
            found_ids = set()
            points_earned = 0

            if team_id:
                rows = db.session.execute(
                    text("SELECT component_id FROM multi_flag_solves "
                         "WHERE challenge_id = :cid AND team_id = :tid"),
                    {"cid": challenge_id, "tid": team_id}
                ).fetchall()
                found_ids = {r[0] for r in rows}

                # Sum points earned across all found components for this team
                if found_ids and components:
                    parent_prefix = components[0][0].rsplit("-", 1)[0]
                    award_total = db.session.execute(
                        text("SELECT COALESCE(SUM(value),0) FROM awards "
                             "WHERE team_id = :tid AND description = 'multi_flag component' "
                             "AND name LIKE :pattern"),
                        {"tid": team_id, "pattern": f"% [{parent_prefix}-%]"}
                    ).scalar() or 0
                    points_earned = int(award_total)

            return jsonify({
                "total": total,
                "found": len(found_ids),
                "complete": len(found_ids) == total and total > 0,
                "points_earned": points_earned,
                "components": [
                    {"component_id": row[0], "found": row[0] in found_ids}
                    for row in components
                ],
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Multi-flag component registration endpoint ─────────────────────────────

    @blueprint.route("/api/plugin/multi-flag-register", methods=["POST"])
    def multi_flag_register():
        """
        Register component scoring params for a multi_flag challenge.
        Called by import_flags.py after challenge creation.
        Auth: CTFd admin API token in Authorization header.
        Body: {challenge_id: int, components: [{component_id, env_var, initial, minimum, decay, flag?}]}
        """
        # Validate admin token
        auth = flask_request.headers.get("Authorization", "")
        if not auth.startswith("Token "):
            return jsonify({"error": "unauthorized"}), 401
        token_value = auth[6:].strip()

        try:
            from CTFd.models import db, Tokens, Users
            tok = Tokens.query.filter_by(value=token_value).first()
            if not tok:
                return jsonify({"error": "invalid token"}), 401
            user = Users.query.get(tok.user_id)
            if not user or user.type != "admin":
                return jsonify({"error": "admin only"}), 403
        except Exception as e:
            return jsonify({"error": str(e)}), 500

        body = flask_request.get_json(silent=True) or {}
        challenge_id = body.get("challenge_id")
        components = body.get("components", [])

        if not challenge_id or not components:
            return jsonify({"error": "challenge_id and components required"}), 400

        try:
            from CTFd.models import db
            from sqlalchemy import text

            upserted = 0
            for comp in components:
                static_flag = comp.get("flag") or comp.get("static_flag") or None
                db.session.execute(
                    text("""
                        INSERT INTO multi_flag_components
                            (challenge_id, component_id, env_var, initial, minimum, decay,
                             static_flag)
                        VALUES
                            (:cid, :comp_id, :env, :initial, :minimum, :decay, :sflag)
                        ON DUPLICATE KEY UPDATE
                            env_var     = VALUES(env_var),
                            initial     = VALUES(initial),
                            minimum     = VALUES(minimum),
                            decay       = VALUES(decay),
                            static_flag = VALUES(static_flag)
                    """),
                    {
                        "cid":     challenge_id,
                        "comp_id": comp["component_id"],
                        "env":     comp.get("env_var", ""),
                        "initial": int(comp.get("initial", 500)),
                        "minimum": int(comp.get("minimum", 50)),
                        "decay":   int(comp.get("decay", 25)),
                        "sflag":   static_flag,
                    }
                )
                upserted += 1
            db.session.commit()
            return jsonify({"message": f"Registered {upserted} components for challenge {challenge_id}"})
        except Exception as e:
            db.session.rollback()
            return jsonify({"error": str(e)}), 500

    @blueprint.route("/api/plugin/multi-flag-team-flags", methods=["POST"])
    def multi_flag_team_flags_register():
        """
        Register per-team flag values for multi_flag challenge components.
        Called by generate-static-artifacts.py after artifact generation.
        Auth: CTFd admin API token in Authorization header.
        Body: {
            challenge_id: int,
            team_id: int,
            flags: [{"component_id": str, "flag_content": str}]
        }
        """
        auth = flask_request.headers.get("Authorization", "")
        if not auth.startswith("Token "):
            return jsonify({"error": "unauthorized"}), 401
        token_value = auth[6:].strip()

        try:
            from CTFd.models import db, Tokens, Users
            tok = Tokens.query.filter_by(value=token_value).first()
            if not tok:
                return jsonify({"error": "invalid token"}), 401
            user = Users.query.get(tok.user_id)
            if not user or user.type != "admin":
                return jsonify({"error": "admin only"}), 403
        except Exception as e:
            return jsonify({"error": str(e)}), 500

        body = flask_request.get_json(silent=True) or {}
        challenge_id = body.get("challenge_id")
        team_id      = body.get("team_id")
        flags        = body.get("flags", [])

        if not challenge_id or not team_id or not flags:
            return jsonify({"error": "challenge_id, team_id, and flags required"}), 400

        try:
            from CTFd.models import db
            from sqlalchemy import text

            upserted = 0
            for entry in flags:
                db.session.execute(
                    text("""
                        INSERT INTO multi_flag_team_flags
                            (challenge_id, team_id, component_id, flag_content)
                        VALUES
                            (:cid, :tid, :comp, :flag)
                        ON DUPLICATE KEY UPDATE
                            flag_content = VALUES(flag_content)
                    """),
                    {
                        "cid":  challenge_id,
                        "tid":  team_id,
                        "comp": entry["component_id"],
                        "flag": entry["flag_content"],
                    }
                )
                upserted += 1
            db.session.commit()
            return jsonify({"message": f"Registered {upserted} team flags for team {team_id} on challenge {challenge_id}"})
        except Exception as e:
            db.session.rollback()
            return jsonify({"error": str(e)}), 500

    # ── Cheaters admin endpoint ───────────────────────────────────────────────

    def _admin_page(title, nav_links, content_html):
        """Wrap content in a minimal CTFd-styled admin HTML page."""
        nav_html = "".join(
            '<a href="%s" style="color:var(--bs-primary);text-decoration:none;'
            'margin-right:1.5rem;font-size:.9rem;font-weight:500;">%s</a>' % (h, l)
            for l, h in nav_links
        )
        return """<!doctype html>
<html data-bs-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>%s — InfoDays CTF</title>
  <link rel="stylesheet" href="/themes/core/static/assets/main.e9ec7884.css">
  <link rel="stylesheet" href="/plugins/controller-plugin/assets/style.css">
  <style>
    body { padding: 2rem; }
    .ctf-admin-nav { margin-bottom: 2rem; padding-bottom: 1rem;
                     border-bottom: 1px solid var(--bs-border-color); }
    table { width: 100%%; border-collapse: collapse; font-size: .875rem; }
    th { text-align: left; padding: .6rem .8rem; border-bottom: 2px solid var(--bs-border-color);
         color: var(--bs-secondary-color); font-weight: 600; text-transform: uppercase;
         font-size: .75rem; letter-spacing: .05em; }
    td { padding: .55rem .8rem; border-bottom: 1px solid var(--bs-border-color);
         vertical-align: top; }
    tr:hover td { background: rgba(255,255,255,.03); }
    .badge-sus  { background: rgba(255,80,80,.15); color:#ff6b6b;
                  border: 1px solid rgba(255,80,80,.3); border-radius:4px;
                  padding:2px 7px; font-size:.75rem; font-weight:600; }
    .badge-ok   { background: rgba(0,196,255,.1); color:#00c4ff;
                  border: 1px solid rgba(0,196,255,.2); border-radius:4px;
                  padding:2px 7px; font-size:.75rem; }
    .ip-list    { font-family: 'JetBrains Mono', monospace; font-size:.8rem;
                  color: var(--bs-secondary-color); word-break: break-all; }
    .page-title { font-size:1.6rem; font-weight:700; margin-bottom:.3rem; }
    .page-sub   { color: var(--bs-secondary-color); font-size:.9rem; margin-bottom:1.5rem; }
    .stat-pill  { display:inline-block; background:rgba(0,196,255,.08);
                  border:1px solid rgba(0,196,255,.2); border-radius:6px;
                  padding:.3rem .8rem; font-size:.8rem; margin-right:.5rem; }
  </style>
</head>
<body>
  <div class="ctf-admin-nav">
    <a href="/admin" style="color:var(--bs-secondary-color);text-decoration:none;
       margin-right:1.5rem;font-size:.9rem;">← Admin</a>
    %s
  </div>
  %s
</body>
</html>""" % (title, nav_html, content_html)

    @blueprint.route("/admin/account-sharing")
    def admin_account_sharing():
        if not _check_admin():
            return jsonify({"error": "admin only"}), 403
        try:
            from CTFd.models import db
            from sqlalchemy import text
            rows = db.session.execute(
                text("""
                    SELECT
                        u.id          AS user_id,
                        u.name        AS user,
                        t.id          AS team_id,
                        t.name        AS team,
                        COUNT(DISTINCT tr.ip)                        AS distinct_ips,
                        COUNT(*)                                     AS total_events,
                        GROUP_CONCAT(DISTINCT tr.ip
                            ORDER BY tr.ip SEPARATOR ', ')           AS ip_list,
                        MIN(tr.date)                                 AS first_seen,
                        MAX(tr.date)                                 AS last_seen
                    FROM tracking tr
                    JOIN users  u ON u.id = tr.user_id
                    LEFT JOIN teams t ON t.id = u.team_id
                    WHERE tr.ip NOT LIKE '10.%%'
                      AND tr.ip NOT LIKE '172.%%'
                      AND tr.ip NOT LIKE '192.168.%%'
                    GROUP BY u.id
                    ORDER BY distinct_ips DESC, total_events DESC
                """)
            ).fetchall()

            sus_count = sum(1 for r in rows if r.distinct_ips >= 4)
            rows_html = ""
            for r in rows:
                badge = '<span class="badge-sus">SUSPICIOUS</span>' if r.distinct_ips >= 4 \
                        else '<span class="badge-ok">OK</span>'
                team_link = '<a href="/admin/teams/%d" style="color:var(--bs-primary)">%s</a>' % (
                    r.team_id, _escape_html(r.team or "(no team)")
                ) if r.team_id else "(no team)"
                user_link = '<a href="/admin/users/%d" style="color:var(--bs-primary)">%s</a>' % (
                    r.user_id, _escape_html(r.user)
                )
                rows_html += (
                    "<tr><td>%s</td><td>%s</td><td>%s</td>"
                    "<td style='text-align:center'><strong>%d</strong></td>"
                    "<td style='text-align:center'>%d</td>"
                    "<td class='ip-list'>%s</td>"
                    "<td style='color:var(--bs-secondary-color);font-size:.8rem'>%s</td></tr>"
                ) % (badge, user_link, team_link, r.distinct_ips, r.total_events,
                     _escape_html(r.ip_list or ""), _escape_html(str(r.last_seen)[:16]))

            stats = (
                '<span class="stat-pill">%d users tracked</span>'
                '<span class="stat-pill" style="color:#ff6b6b">%d suspicious</span>'
                '<span class="stat-pill" style="color:var(--bs-secondary-color);font-size:.75rem">'
                'Threshold: 4+ distinct IPs = suspicious</span>'
            ) % (len(rows), sus_count)

            content = """
<div class="page-title">Account Sharing Detection</div>
<div class="page-sub">Players logged in from 4+ distinct public IPs are flagged as suspicious.</div>
<div style="margin-bottom:1.5rem">%s</div>
<table>
  <thead><tr>
    <th>Status</th><th>User</th><th>Team</th>
    <th style="text-align:center">Distinct IPs</th>
    <th style="text-align:center">Events</th>
    <th>IP Addresses</th><th>Last Seen</th>
  </tr></thead>
  <tbody>%s</tbody>
</table>""" % (stats, rows_html if rows_html else
               "<tr><td colspan='7' style='text-align:center;padding:2rem;"
               "color:var(--bs-secondary-color)'>No tracking data yet</td></tr>")

            nav = [("Cheaters", "/admin/cheaters"), ("Account Sharing", "/admin/account-sharing")]
            resp = make_response(_admin_page("Account Sharing", nav, content))
            resp.headers["Content-Type"] = "text/html"
            return resp
        except Exception as e:
            return "<pre>Error: %s</pre>" % _escape_html(e), 500

    @blueprint.route("/admin/cheaters")
    def admin_cheaters():
        if not _check_admin():
            return jsonify({"error": "admin only"}), 403
        try:
            from CTFd.models import db
            from sqlalchemy import text
            rows = db.session.execute(
                text("""
                    SELECT
                        c.id,
                        st.name  AS submitting_team,
                        c.submitting_team_id,
                        su.name  AS submitting_user,
                        c.submitting_user_id,
                        c.submitted_flag,
                        ot.name  AS owner_team,
                        c.owner_team_id,
                        c.challenge_manifest_id,
                        c.submitted_at
                    FROM cheaters c
                    LEFT JOIN teams st ON st.id = c.submitting_team_id
                    LEFT JOIN users su ON su.id = c.submitting_user_id
                    LEFT JOIN teams ot ON ot.id = c.owner_team_id
                    ORDER BY c.submitted_at DESC
                """)
            ).fetchall()

            rows_html = ""
            for r in rows:
                sub_team = '<a href="/admin/teams/%d" style="color:var(--bs-primary)">%s</a>' % (
                    r.submitting_team_id, _escape_html(r.submitting_team or "?")
                ) if r.submitting_team_id else "?"
                sub_user = '<a href="/admin/users/%d" style="color:var(--bs-primary)">%s</a>' % (
                    r.submitting_user_id, _escape_html(r.submitting_user or "?")
                ) if r.submitting_user_id else "?"
                own_team = '<a href="/admin/teams/%d" style="color:#ff6b6b">%s</a>' % (
                    r.owner_team_id, _escape_html(r.owner_team or "?")
                ) if r.owner_team_id else "?"
                rows_html += (
                    "<tr><td>%s</td><td>%s</td><td>%s</td>"
                    "<td style='font-family:monospace;font-size:.8rem'>%s</td>"
                    "<td>%s</td>"
                    "<td style='color:var(--bs-secondary-color);font-size:.8rem'>%s</td></tr>"
                ) % (sub_team, sub_user, own_team,
                     _escape_html((r.submitted_flag or "")[:60]),
                     _escape_html(r.challenge_manifest_id),
                     _escape_html(str(r.submitted_at)[:16]))

            content = """
<div class="page-title">Flag Cheating Log</div>
<div class="page-sub">Players who submitted another team's flag. Each row is one cross-team attempt.</div>
<div style="margin-bottom:1.5rem">
  <span class="stat-pill" style="color:#ff6b6b">%d incidents</span>
</div>
<table>
  <thead><tr>
    <th>Submitting Team</th><th>Player</th><th>Flag Owner</th>
    <th>Submitted Flag</th><th>Challenge</th><th>Time</th>
  </tr></thead>
  <tbody>%s</tbody>
</table>""" % (len(rows), rows_html if rows_html else
               "<tr><td colspan='6' style='text-align:center;padding:2rem;"
               "color:var(--bs-secondary-color)'>No cheating incidents recorded</td></tr>")

            nav = [("Cheaters", "/admin/cheaters"), ("Account Sharing", "/admin/account-sharing")]
            resp = make_response(_admin_page("Cheaters", nav, content))
            resp.headers["Content-Type"] = "text/html"
            return resp
        except Exception as e:
            return "<pre>Error: %s</pre>" % _escape_html(e), 500

    app.register_blueprint(blueprint)

    # ── Register custom flag + challenge types ────────────────────────────────
    from CTFd.plugins.flags import FLAG_CLASSES
    FLAG_CLASSES["team"] = CTFdTeamFlag

    from CTFd.plugins.challenges import CHALLENGE_CLASSES
    CHALLENGE_CLASSES["multi_flag"] = MultiFlagChallenge
    _register_multi_flag_model()

    # ── Register scripts ──────────────────────────────────────────────────────
    register_script("/controller-plugin/config.js")
    register_script("/plugins/controller-plugin/assets/plugin.js")

    # ── Initialize DB ─────────────────────────────────────────────────────────
    with app.app_context():
        from CTFd.models import Configs, db
        from sqlalchemy import text

        # Create cheaters table if it doesn't exist.
        # Wrapped in try/except because ProxySQL may route DDL to the read
        # replica, which rejects writes.  The table is created by whichever
        # pod reaches the primary first; subsequent pods can safely skip.
        try:
            db.session.execute(text("""
                CREATE TABLE IF NOT EXISTS multi_flag_components (
                    id           INT AUTO_INCREMENT PRIMARY KEY,
                    challenge_id INT          NOT NULL,
                    component_id VARCHAR(255) NOT NULL,
                    env_var      VARCHAR(50)  NOT NULL DEFAULT '',
                    initial      INT          NOT NULL DEFAULT 500,
                    minimum      INT          NOT NULL DEFAULT 50,
                    decay        INT          NOT NULL DEFAULT 25,
                    static_flag  VARCHAR(255) DEFAULT NULL,
                    UNIQUE KEY uq_cc (challenge_id, component_id)
                )
            """))
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.warning("multi_flag_components table CREATE skipped (likely read-replica routing)")

        # Add static_flag column to existing deployments that pre-date this column.
        try:
            db.session.execute(text(
                "ALTER TABLE multi_flag_components "
                "ADD COLUMN IF NOT EXISTS static_flag VARCHAR(255) DEFAULT NULL"
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()  # Column already exists or routing issue — safe to ignore

        try:
            db.session.execute(text("""
                CREATE TABLE IF NOT EXISTS multi_flag_solves (
                    id           INT AUTO_INCREMENT PRIMARY KEY,
                    challenge_id INT          NOT NULL,
                    team_id      INT          NOT NULL,
                    component_id VARCHAR(255) NOT NULL,
                    awarded_at   DATETIME     NOT NULL,
                    UNIQUE KEY uq_tc (team_id, component_id),
                    INDEX idx_challenge_team (challenge_id, team_id)
                )
            """))
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.warning("multi_flag_solves table CREATE skipped (likely read-replica routing)")

        try:
            db.session.execute(text("""
                CREATE TABLE IF NOT EXISTS cheaters (
                    id                    INT AUTO_INCREMENT PRIMARY KEY,
                    submitting_team_id    INT         NOT NULL,
                    submitting_user_id    INT             NULL,
                    submitted_flag        TEXT        NOT NULL,
                    owner_team_id         INT         NOT NULL,
                    challenge_manifest_id VARCHAR(255) NOT NULL,
                    submitted_at          DATETIME    NOT NULL,
                    INDEX idx_submitting (submitting_team_id),
                    INDEX idx_user       (submitting_user_id),
                    INDEX idx_owner      (owner_team_id)
                )
            """))
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.warning("cheaters table CREATE skipped (likely read-replica routing) — table should already exist")

        if not Configs.query.filter_by(key="CONTROLLER_URL").first():
            db.session.add(
                Configs(key="CONTROLLER_URL", value="http://controller-api.ctfd.svc.cluster.local")
            )
            db.session.commit()

        if not Configs.query.filter_by(key="X_CONTROLLER_KEY").first():
            app.logger.error(
                "X_CONTROLLER_KEY not found in database — "
                "run setup-ctfd.sh or set it manually in /admin/config before the event"
            )

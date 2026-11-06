#!/usr/bin/env python3
import os
import sqlite3
from flask import Flask, request, redirect, session, render_template_string

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Initialize in-memory database with users table
def init_db():
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE users (username TEXT, password TEXT)')
    cursor.execute("INSERT INTO users VALUES ('admin', 's3cr3t')")
    conn.commit()
    return conn

# Create DB on app startup
db = init_db()

LOGIN_FORM = '''
<!DOCTYPE html>
<html>
<head><title>Login</title></head>
<body>
<h1>Login</h1>
<form method="POST" action="/login">
  <label>Username: <input type="text" name="username" required></label><br>
  <label>Password: <input type="password" name="password" required></label><br>
  <button type="submit">Login</button>
</form>
{% if message %}<p style="color: red;">{{ message }}</p>{% endif %}
</body>
</html>
'''

@app.route('/')
def login_page():
    return render_template_string(LOGIN_FORM, message=None)

@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username', '')
    password = request.form.get('password', '')

    # Intentional SQL injection vulnerability
    query = f"SELECT * FROM users WHERE username='{username}' AND password='{password}'"

    cursor = db.cursor()
    try:
        cursor.execute(query)
        row = cursor.fetchone()

        if row:
            session['logged_in'] = True
            return redirect('/flag')
        else:
            return render_template_string(LOGIN_FORM, message='Invalid credentials.')
    except sqlite3.Error:
        return render_template_string(LOGIN_FORM, message='Invalid credentials.')

@app.route('/flag')
def get_flag():
    if session.get('logged_in'):
        return os.environ['FLAG']
    else:
        return 'Forbidden.', 403

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)

#!/usr/bin/env python3
"""Set a Nebula user's password directly in SQLite (there is no password-change UI).

Usage:
  backend/.venv/bin/python deploy-local/set-admin-password.py EMAIL 'NEW-PASSWORD'
"""
import sqlite3
import sys
from pathlib import Path
from argon2 import PasswordHasher

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / 'data' / 'nebula.db'

if len(sys.argv) != 3 or len(sys.argv[2]) < 12:
    raise SystemExit("Usage: set-admin-password.py EMAIL 'NEW-PASSWORD'  (12+ characters)")
email, password = sys.argv[1].strip().lower(), sys.argv[2]
if not DB.exists():
    raise SystemExit(f'Database not found: {DB}')
con = sqlite3.connect(DB)
row = con.execute('select id from users where email=?', (email,)).fetchone()
if not row:
    raise SystemExit(f'User not found: {email}')
con.execute('update users set password_hash=? where id=?', (PasswordHasher().hash(password), row[0]))
con.execute('delete from login_sessions where user_id=?', (row[0],))
con.commit()
print(f'Password updated for {email}; existing sessions invalidated.')

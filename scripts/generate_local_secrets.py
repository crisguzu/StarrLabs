"""Generate only missing/placeholder local secrets; never print credential values."""
from pathlib import Path
import os
import secrets
from dotenv import dotenv_values, set_key
ROOT=Path(__file__).resolve().parents[1]

def main():
    path=ROOT/'.env'
    if path.is_symlink() or not path.is_file():
        raise SystemExit('Copy .env.example to a regular .env file first.')
    values=dotenv_values(path)
    placeholders={'','dev-only-change-me','GENERATE_A_LONG_RANDOM_LOCAL_SECRET','GENERATE_A_DIFFERENT_LONG_RANDOM_LOCAL_SECRET'}
    changed=[]
    for key in ('SESSION_SECRET','EMAIL_AUTH_SECRET'):
        if (values.get(key) or '') in placeholders:
            set_key(path,key,secrets.token_urlsafe(48));changed.append(key)
        elif len(values.get(key) or "") < 32:
            raise SystemExit(key+" is too short; provide a strong value or intentionally leave it blank for generation.")
    os.chmod(path,0o600)
    print('Generated placeholder/missing secrets: '+(', '.join(changed) if changed else 'none')+'. Existing values preserved; .env mode 0600.')

if __name__=='__main__':main()

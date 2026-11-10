"""Transactional admission before operator-paid API work."""
from fastapi import HTTPException
from services.email_auth import hash_request_value

def admit_request(connect, identity_email, scope, per_identity=120, global_limit=240):
    if not identity_email:
        raise HTTPException(status_code=401, detail="Verified email is required")
    digest=hash_request_value(identity_email)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (742310031,))
        cur.execute("DELETE FROM standalone_request_admissions WHERE created_at < now()-interval '24 hours'")
        cur.execute("SELECT count(*), count(*) FILTER (WHERE subject_digest=%s) FROM standalone_request_admissions WHERE operation=%s AND created_at >= now()-interval '1 hour'", (digest,scope))
        total,subject=cur.fetchone()
        if int(total)>=global_limit or int(subject)>=per_identity:
            raise HTTPException(status_code=429, detail="Operation budget exhausted; try later")
        cur.execute("INSERT INTO standalone_request_admissions(operation,subject_digest) VALUES (%s,%s)",(scope,digest))

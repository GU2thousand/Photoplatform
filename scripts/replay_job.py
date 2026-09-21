"""Operator replay for a single durable dead-letter job, including failed deletion."""
import argparse
import os
import uuid
import psycopg

parser=argparse.ArgumentParser()
parser.add_argument("job_id",type=uuid.UUID)
args=parser.parse_args()
with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    identity=conn.execute("SELECT media_id FROM media_processing_jobs WHERE id=%s",(args.job_id,)).fetchone()
    if not identity: raise SystemExit("Job not found")
    # Image first, then job, matching API lifecycle operations and worker commits.
    conn.execute("SELECT id FROM image_assets WHERE id=%s FOR UPDATE",(identity[0],))
    row=conn.execute("""SELECT j.media_id,j.job_type,a.deleted_at FROM media_processing_jobs j
      JOIN image_assets a ON a.id=j.media_id WHERE j.id=%s AND j.status='DLQ' FOR UPDATE OF j""",(args.job_id,)).fetchone()
    if not row: raise SystemExit("Job is not in DLQ")
    media_id,kind,deleted_at=row
    if (kind=="DELETE") != (deleted_at is not None): raise SystemExit("Job no longer matches the asset lifecycle")
    if kind=="MEDIA_PROCESS":
        cleaned=conn.execute("SELECT cleaned_at FROM upload_sessions WHERE media_id=%s",(media_id,)).fetchone()
        if not cleaned or cleaned[0]: raise SystemExit("Source expired; upload the image again")
    conn.execute("UPDATE media_processing_jobs SET status='QUEUED',attempt=0,claim_token=NULL,lease_until=NULL,next_attempt_at=now(),last_error_code=NULL,started_at=NULL,finished_at=NULL,worker_id=NULL,updated_at=now() WHERE id=%s",(args.job_id,))
    conn.execute("UPDATE media_outbox SET last_published_at=NULL WHERE job_id=%s",(args.job_id,))
    if kind!="DELETE":
        column="processing_status" if kind=="MEDIA_PROCESS" else "embedding_status"
        conn.execute(f"UPDATE image_assets SET {column}=%s WHERE id=%s",("PROCESSING" if kind=="MEDIA_PROCESS" else "QUEUED",media_id))
print("Job requeued:",args.job_id)

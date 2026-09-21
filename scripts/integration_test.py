"""Real PostgreSQL/MinIO/RabbitMQ/API/worker tests. Run only against a disposable stack."""
import hashlib
import io
import os
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import requests
from PIL import Image

API=os.getenv("TEST_API_URL","http://localhost:8081")
DB=os.environ.get("TEST_DATABASE_URL")


def request(method,path,token=None,**kwargs):
    headers=kwargs.pop("headers",{})
    if token: headers["Authorization"]="Bearer "+token
    return requests.request(method,API+path,headers=headers,timeout=30,**kwargs)


def fixture(color="orange"):
    buffer=io.BytesIO()
    Image.new("RGB",(640,480),color).save(buffer,"JPEG")
    return buffer.getvalue()


class PipelineIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getenv("ALLOW_INTEGRATION_WRITES")!="1" or not DB:
            raise RuntimeError("Set ALLOW_INTEGRATION_WRITES=1 and TEST_DATABASE_URL for a disposable database")
        cls.suffix=uuid.uuid4().hex[:10]
        cls.accounts=[]
        for name in ("owner","other","admin"):
            email=f"{name}-{cls.suffix}@test.example"
            response=request("POST","/api/auth/register",json={"name":name,"email":email,"password":"test-password-123"})
            response.raise_for_status()
            cls.accounts.append(response.json())
        cls.owner,cls.other,cls.admin=[a["token"] for a in cls.accounts]
        with psycopg.connect(DB,autocommit=True) as conn:
            conn.execute("UPDATE user_accounts SET role='ADMIN' WHERE email=%s",(f"admin-{cls.suffix}@test.example",))
        cls.admin=request("POST","/api/auth/login",json={"email":f"admin-{cls.suffix}@test.example","password":"test-password-123"}).json()["token"]

    def create(self,data=None,visibility="PRIVATE",token=None,key=None,**overrides):
        data=data if data is not None else fixture()
        body={"filename":"photo.jpg","contentType":"image/jpeg","size":len(data),"sha256":hashlib.sha256(data).hexdigest(),
              "title":"sunset "+uuid.uuid4().hex,"description":"Orange city skyline","category":"Nature","tags":"sunset,city","visibility":visibility}
        body.update(overrides)
        response=request("POST","/api/uploads",token or self.owner,headers={"Idempotency-Key":key or str(uuid.uuid4())},json=body)
        self.assertEqual(response.status_code,200,response.text)
        return response.json(),body,data

    def put(self,upload,data):
        response=requests.put(upload["uploadUrl"],headers=upload["headers"],data=data,timeout=30)
        self.assertEqual(response.status_code,200,response.text)

    def complete(self,upload,token=None):
        response=request("POST",f"/api/uploads/{upload['uploadId']}/complete",token or self.owner)
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def wait(self,upload,status="READY",timeout=60):
        end=time.monotonic()+timeout
        current={}
        while time.monotonic()<end:
            response=request("GET",f"/api/uploads/{upload['uploadId']}",self.owner)
            self.assertEqual(response.status_code,200,response.text)
            current=response.json()
            if current["status"]==status: return current
            if current["status"]=="FAILED" and status!="FAILED": break
            time.sleep(.5)
        self.fail(f"Upload did not reach {status}: {current}")

    def ready(self,**kwargs):
        upload,_,data=self.create(**kwargs); self.put(upload,data); self.complete(upload); self.wait(upload); return upload

    def test_01_direct_upload_checksum_idempotency_and_variants(self):
        key=str(uuid.uuid4())
        upload,body,data=self.create(key=key)
        with ThreadPoolExecutor(max_workers=3) as pool:
            responses=list(pool.map(lambda _:request("POST","/api/uploads",self.owner,headers={"Idempotency-Key":key},json=body),range(3)))
        self.assertTrue(all(r.status_code==200 and r.json()["mediaId"]==upload["mediaId"] for r in responses))
        changed=dict(body,title="different")
        self.assertEqual(request("POST","/api/uploads",self.owner,headers={"Idempotency-Key":key},json=changed).status_code,400)
        self.put(upload,data)
        self.assertEqual(requests.put(upload["uploadUrl"],headers=upload["headers"],data=data,timeout=30).status_code,412)
        self.complete(upload); self.complete(upload); self.wait(upload)
        with psycopg.connect(DB) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM media_processing_jobs WHERE media_id=%s AND job_type='MEDIA_PROCESS'",(upload["mediaId"],)).fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT count(*) FROM media_variants WHERE media_id=%s",(upload["mediaId"],)).fetchone()[0],5)
            self.assertEqual(conn.execute("SELECT content_sha256 FROM image_assets WHERE id=%s",(upload["mediaId"],)).fetchone()[0],hashlib.sha256(data).hexdigest())

    def test_02_private_media_and_upload_sessions_are_owner_scoped(self):
        upload=self.ready()
        self.assertEqual(request("GET",f"/api/uploads/{upload['uploadId']}",self.other).status_code,404)
        self.assertEqual(request("POST",f"/api/uploads/{upload['uploadId']}/complete",self.other).status_code,404)
        path=f"/api/files/{upload['mediaId']}/url?variant=thumbnail"
        self.assertEqual(request("GET",path,self.other).status_code,403)
        self.assertEqual(request("GET",path).status_code,403)
        delivery=request("GET",path,self.owner)
        self.assertEqual(delivery.status_code,200,delivery.text)
        media=requests.get(delivery.json()["url"],timeout=30)
        self.assertEqual(media.status_code,200)
        self.assertEqual(media.headers["Cache-Control"],"private, no-store")
        self.assertEqual(media.headers["Content-Type"],"image/webp")
        unsigned=delivery.json()["url"].split("?")[0]
        self.assertEqual(requests.get(unsigned,timeout=30).status_code,403)

    def test_03_pending_public_media_is_not_public_until_approved(self):
        upload=self.ready(visibility="PUBLIC")
        path=f"/api/files/{upload['mediaId']}/url"
        self.assertEqual(request("GET",path).status_code,403)
        result=request("GET","/api/search?mode=keyword&q=sunset&scope=public").json()
        self.assertNotIn(upload["mediaId"],[h["image"]["id"] for h in result["items"]])
        response=request("PATCH",f"/api/images/{upload['mediaId']}/moderation?status=APPROVED",self.admin)
        self.assertEqual(response.status_code,200,response.text)
        delivery=request("GET",path)
        self.assertEqual(delivery.status_code,200,delivery.text)
        self.assertEqual(requests.get(delivery.json()["url"],timeout=30).headers["Cache-Control"],"public, max-age=60")
        request("PATCH",f"/api/images/{upload['mediaId']}/moderation?status=REJECTED",self.admin).raise_for_status()
        self.assertEqual(request("GET",path).status_code,403)

    def test_04_team_membership_is_rechecked_at_completion(self):
        team=request("POST","/api/teams",self.owner,json={"name":"Pipeline team","description":"test"}).json()
        email=f"other-{self.suffix}@test.example"
        request("POST",f"/api/teams/{team['id']}/members",self.owner,json={"email":email}).raise_for_status()
        upload,_,data=self.create(visibility="TEAM",token=self.other,teamId=team["id"])
        self.put(upload,data)
        with psycopg.connect(DB,autocommit=True) as conn:
            conn.execute("DELETE FROM team_members WHERE team_id=%s AND user_id=(SELECT id FROM user_accounts WHERE email=%s)",(team["id"],email))
        self.assertEqual(request("POST",f"/api/uploads/{upload['uploadId']}/complete",self.other).status_code,403)
        request("DELETE",f"/api/uploads/{upload['uploadId']}",self.other).raise_for_status()

    def test_05_invalid_image_goes_to_durable_dead_letter_state(self):
        upload,_,data=self.create(data=b"not really a JPEG")
        self.put(upload,data); self.complete(upload)
        current=self.wait(upload,"FAILED")
        self.assertEqual(current["errorCode"],"INVALID_IMAGE")
        with psycopg.connect(DB) as conn:
            self.assertEqual(conn.execute("SELECT status,attempt FROM media_processing_jobs WHERE media_id=%s",(upload["mediaId"],)).fetchone(),("DLQ",1))

    def test_06_wrong_checksum_is_rejected_by_storage(self):
        upload,_,data=self.create()
        changed=b"x"+data[1:]
        response=requests.put(upload["uploadUrl"],headers=upload["headers"],data=changed,timeout=30)
        self.assertIn(response.status_code,(400,403),response.text)
        request("DELETE",f"/api/uploads/{upload['uploadId']}",self.owner).raise_for_status()

    def test_07_delete_does_not_leave_processed_variants(self):
        upload=self.ready()
        response=request("DELETE",f"/api/images/{upload['mediaId']}",self.other)
        self.assertEqual(response.status_code,403)
        request("DELETE",f"/api/images/{upload['mediaId']}",self.owner).raise_for_status()
        self.wait(upload,"DELETED")
        self.assertEqual(request("GET",f"/api/files/{upload['mediaId']}/url",self.owner).status_code,404)
        with psycopg.connect(DB) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM media_variants WHERE media_id=%s",(upload["mediaId"],)).fetchone()[0],0)

    def test_08_expired_upload_cannot_complete(self):
        upload,_,data=self.create(); self.put(upload,data)
        with psycopg.connect(DB,autocommit=True) as conn:
            conn.execute("UPDATE upload_sessions SET expires_at=now()-interval '1 hour' WHERE id=%s",(upload["uploadId"],))
        self.assertEqual(request("POST",f"/api/uploads/{upload['uploadId']}/complete",self.owner).status_code,400)

    def test_09_missing_idempotency_key_and_oversized_request(self):
        _,body,_=self.create()
        self.assertEqual(request("POST","/api/uploads",self.owner,json=body).status_code,400)
        body["size"]=100_000_000
        self.assertEqual(request("POST","/api/uploads",self.owner,headers={"Idempotency-Key":str(uuid.uuid4())},json=body).status_code,400)

    def test_10_duplicates_do_not_leak_private_images(self):
        first=self.ready(); second=self.ready()
        result=request("GET",f"/api/images/{first['mediaId']}/duplicates",self.owner)
        self.assertEqual(result.status_code,200,result.text)
        self.assertIn(second["mediaId"],[h["id"] for h in result.json()])
        self.assertEqual(request("GET",f"/api/images/{first['mediaId']}/duplicates",self.other).status_code,403)


if __name__=="__main__": unittest.main(verbosity=2)

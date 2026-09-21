"""Real CLIP/pgvector authorization checks; requires the optional search profile."""
import os
import time
import unittest
import uuid
from integration_test import request, fixture, PipelineIntegration, DB
import psycopg


class SemanticPermissions(unittest.TestCase):
    create=PipelineIntegration.create
    put=PipelineIntegration.put
    complete=PipelineIntegration.complete
    wait=PipelineIntegration.wait
    ready=PipelineIntegration.ready

    @classmethod
    def setUpClass(cls):
        PipelineIntegration.setUpClass.__func__(cls)

    def search_ids(self,token=None,**params):
        r=request('GET','/api/search',token,params={'q':'orange sunset','mode':'semantic','scope':'accessible','limit':50,**params})
        self.assertEqual(r.status_code,200,r.text)
        return [hit['image']['id'] for hit in r.json()['items']]

    def wait_embedding(self,upload):
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            r=request('GET',f"/api/uploads/{upload['uploadId']}",self.owner)
            if r.status_code==200 and r.json()['embeddingStatus']=='READY': return
            time.sleep(.5)
        self.fail('Embedding did not complete')

    def test_real_embedding_and_permission_filter_before_ranking(self):
        private=self.ready();self.wait_embedding(private)
        public=self.ready(visibility='PUBLIC');self.wait_embedding(public)
        for mode in ('semantic','hybrid'):
            self.assertIn(private['mediaId'],self.search_ids(self.owner,mode=mode,scope='mine'))
            self.assertNotIn(private['mediaId'],self.search_ids(self.other,mode=mode))
            self.assertNotIn(public['mediaId'],self.search_ids(mode=mode,scope='public'))
        request('PATCH',f"/api/images/{public['mediaId']}/moderation?status=APPROVED",self.admin).raise_for_status()
        for mode in ('semantic','hybrid'):
            self.assertIn(public['mediaId'],self.search_ids(mode=mode,scope='public'))
        request('PATCH',f"/api/images/{public['mediaId']}/moderation?status=REJECTED",self.admin).raise_for_status()
        self.assertNotIn(public['mediaId'],self.search_ids(scope='public'))
        request('DELETE',f"/api/images/{private['mediaId']}",self.owner).raise_for_status()
        self.assertNotIn(private['mediaId'],self.search_ids(self.owner,scope='mine'))

    def test_team_membership_revocation_filters_semantic_results(self):
        team=request('POST','/api/teams',self.owner,json={'name':'Semantic team','description':'test'}).json()
        request('POST',f"/api/teams/{team['id']}/members",self.owner,json={'email':f'other-{self.suffix}@test.example'}).raise_for_status()
        upload=self.ready(visibility='TEAM',teamId=team['id']);self.wait_embedding(upload)
        self.assertIn(upload['mediaId'],self.search_ids(self.other,scope='team',teamId=team['id']))
        with psycopg.connect(DB,autocommit=True) as conn:
            conn.execute('DELETE FROM team_members WHERE team_id=%s AND user_id=(SELECT id FROM user_accounts WHERE email=%s)',(team['id'],f'other-{self.suffix}@test.example'))
        self.assertNotIn(upload['mediaId'],self.search_ids(self.other,scope='team',teamId=team['id']))

    def test_incomplete_embedding_never_blocks_image_delivery(self):
        upload=self.ready();self.wait_embedding(upload)
        with psycopg.connect(DB,autocommit=True) as conn:
            conn.execute("UPDATE image_assets SET embedding_status='FAILED' WHERE id=%s",(upload['mediaId'],))
            conn.execute('DELETE FROM media_embeddings WHERE media_id=%s',(upload['mediaId'],))
        self.assertEqual(request('GET',f"/api/files/{upload['mediaId']}/url",self.owner).status_code,200)
        self.assertNotIn(upload['mediaId'],self.search_ids(self.owner,scope='mine'))
        self.assertIn(upload['mediaId'],self.search_ids(self.owner,scope='mine',mode='keyword',q='sunset'))


if __name__=='__main__':
    unittest.main(defaultTest='SemanticPermissions',verbosity=2)

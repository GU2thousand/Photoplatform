package com.generatecloud.app.pipeline;

import com.generatecloud.app.storage.ObjectStorageService;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.util.*;
import java.util.concurrent.*;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

/** Executes the actual production claim SQL with concurrent schedulers on PostgreSQL. */
@EnabledIfEnvironmentVariable(named="OUTBOX_POSTGRES_TEST", matches="true")
class OutboxClaimIntegrationTests {
    private JdbcTemplate jdbc;
    private String schema;
    private DriverManagerDataSource source;

    @BeforeEach void setup() {
        source = new DriverManagerDataSource(System.getenv("OUTBOX_JDBC_URL"), "generatecloud", "generatecloud");
        jdbc = new JdbcTemplate(source);
        schema="outbox_test_"+UUID.randomUUID().toString().replace("-", "");
        jdbc.execute("CREATE SCHEMA "+schema);
        source.setUrl(System.getenv("OUTBOX_JDBC_URL")+"?currentSchema="+schema);
        jdbc.execute("CREATE TABLE media_processing_jobs(id uuid PRIMARY KEY,job_type text,status text,attempt int,traceparent text,next_attempt_at timestamptz,lease_until timestamptz,created_at timestamptz DEFAULT now())");
        jdbc.execute("CREATE TABLE media_outbox(job_id uuid PRIMARY KEY,last_published_at timestamptz,publication_claim_token uuid,publication_lease_until timestamptz)");
    }
    @AfterEach void cleanup() { jdbc.execute("DROP SCHEMA "+schema+" CASCADE"); }
    private UUID insert() {
        UUID id=UUID.randomUUID();
        jdbc.update("INSERT INTO media_processing_jobs(id,job_type,status,attempt,next_attempt_at) VALUES (?,'MEDIA_PROCESS','QUEUED',0,now())",id);
        jdbc.update("INSERT INTO media_outbox(job_id) VALUES (?)",id); return id;
    }
    private OutboxDispatcher dispatcher(MessagePublisher publisher) {
        return new OutboxDispatcher(jdbc,publisher,new SimpleMeterRegistry(),mock(ObjectStorageService.class),new DataSourceTransactionManager(source));
    }
    @Test void twoSchedulersPublishEachDueRowOnceWithoutLongTransaction() throws Exception {
        List<UUID> jobs=new ArrayList<>(); for(int i=0;i<20;i++) jobs.add(insert());
        Map<UUID,Integer> counts=new ConcurrentHashMap<>();
        MessagePublisher publisher=(queue,id,trace)-> { counts.merge(id,1,Integer::sum); Thread.sleep(10); };
        ExecutorService threads=Executors.newFixedThreadPool(2);
        try { Future<?> one=threads.submit(dispatcher(publisher)::dispatch); Future<?> two=threads.submit(dispatcher(publisher)::dispatch);
            one.get(20,TimeUnit.SECONDS); two.get(20,TimeUnit.SECONDS); }
        finally { threads.shutdownNow(); }
        assertThat(counts).hasSize(20); assertThat(counts.values()).allMatch(n->n==1);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM media_outbox WHERE last_published_at IS NOT NULL AND publication_claim_token IS NULL",Integer.class)).isEqualTo(20);
    }
    @Test void expiredClaimRecoversAndStaleConfirmationCannotOverwriteNewOwner() {
        UUID id=insert(); UUID current=UUID.randomUUID();
        jdbc.update("UPDATE media_outbox SET publication_claim_token=?,publication_lease_until=now()-interval '1 second' WHERE job_id=?",UUID.randomUUID(),id);
        dispatcher((queue,job,trace)->jdbc.update("UPDATE media_outbox SET publication_claim_token=?,publication_lease_until=now()+interval '1 hour' WHERE job_id=?",current,job)).dispatch();
        assertThat(jdbc.queryForObject("SELECT publication_claim_token FROM media_outbox WHERE job_id=?",UUID.class,id)).isEqualTo(current);
        assertThat(jdbc.queryForObject("SELECT last_published_at FROM media_outbox WHERE job_id=?",java.sql.Timestamp.class,id)).isNull();
    }
    @Test void failedBrokerPublishReleasesClaimWithoutMarkingConfirmed() {
        UUID id=insert(); dispatcher((queue,job,trace)-> { throw new java.io.IOException("broker down"); }).dispatch();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM media_outbox WHERE job_id=? AND publication_claim_token IS NULL AND last_published_at IS NULL",Integer.class,id)).isEqualTo(1);
    }
}

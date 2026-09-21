package com.generatecloud.app.pipeline;

import io.micrometer.core.instrument.MeterRegistry;
import jakarta.annotation.PostConstruct;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.amqp.core.*;
import org.springframework.amqp.rabbit.connection.CorrelationData;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

@Component
@RequiredArgsConstructor
@Slf4j
@ConditionalOnProperty(name="app.pipeline.enabled",havingValue="true")
public class OutboxDispatcher {
    private final JdbcTemplate jdbc;
    private final RabbitTemplate rabbit;
    private final MeterRegistry metrics;
    private final PipelineStorage storage;
    private final PlatformTransactionManager transactions;

    @Bean
    public Declarables mediaQueues() {
        return new Declarables(new Queue("media.process",true),new Queue("media.embed",true),
                new Queue("media.delete",true),new Queue("media.dlq",true));
    }
    @PostConstruct
    void gauges() {
        metrics.gauge("media_queue_depth",this,self -> self.count("status IN ('QUEUED','RETRY')"));
        metrics.gauge("worker_active_jobs",this,self -> self.count("status='RUNNING' AND lease_until>now()"));
        metrics.gauge("media_dead_letter_jobs",this,self -> self.count("status='DLQ'"));
    }
    private double count(String predicate) {
        try { return jdbc.queryForObject("SELECT count(*) FROM media_processing_jobs WHERE "+predicate,Long.class); }
        catch(Exception exception) { return Double.NaN; }
    }

    @Scheduled(fixedDelayString="${app.pipeline.dispatch-interval-ms:500}")
    public void dispatch() {
        // Unconfirmed publications and expired worker leases remain recoverable in PostgreSQL.
        var due=jdbc.queryForList("""
            SELECT j.id,j.job_type,j.status,j.attempt,j.traceparent FROM media_outbox o JOIN media_processing_jobs j ON j.id=o.job_id
            WHERE ((j.status IN ('QUEUED','RETRY') AND j.next_attempt_at<=now())
                   OR (j.status='RUNNING' AND j.lease_until<now()) OR j.status='DLQ')
            AND (o.last_published_at IS NULL OR (j.status<>'DLQ' AND o.last_published_at<now()-interval '5 minutes'))
            ORDER BY o.last_published_at NULLS FIRST,j.created_at LIMIT 100
            """);
        for(var row:due) {
            String id=row.get("id").toString();
            String queue=row.get("status").equals("DLQ")?"media.dlq":switch(row.get("job_type").toString()) {
                case "EMBED" -> "media.embed"; case "DELETE" -> "media.delete"; default -> "media.process";
            };
            try {
                var properties=new MessageProperties();
                properties.setContentType("application/json"); properties.setMessageId(id);
                properties.setDeliveryMode(MessageDeliveryMode.PERSISTENT);
                var correlation=new CorrelationData(UUID.randomUUID().toString());
                String traceparent=row.get("traceparent")==null?"":row.get("traceparent").toString();
                rabbit.send("",queue,new Message(("{\"jobId\":\""+id+"\",\"traceparent\":\""+traceparent+"\"}").getBytes(StandardCharsets.UTF_8),properties),correlation);
                var confirm=correlation.getFuture().get(3,TimeUnit.SECONDS);
                if(!confirm.isAck() || correlation.getReturned()!=null) throw new IllegalStateException("Message was not routed and confirmed");
                // A fast worker can fail before the publisher receives its confirmation. Do
                // not erase that worker's retry/DLQ publication request with a stale timestamp.
                jdbc.update("""
                    UPDATE media_outbox o SET last_published_at=now() FROM media_processing_jobs j
                    WHERE o.job_id=j.id AND j.id=? AND j.status=? AND j.attempt=?
                    """,UUID.fromString(id),row.get("status"),row.get("attempt"));
            } catch(Exception exception) {
                metrics.counter("media_publish_failures").increment();
                log.warn("Media job {} publication deferred: {}",id,exception.getClass().getSimpleName());
                break; // avoid repeatedly waiting against an unavailable broker
            }
        }
    }

    @Scheduled(fixedDelayString="${app.pipeline.cleanup-interval-ms:30000}")
    public void expireUploads() {
        int expired=jdbc.update("""
            UPDATE image_assets a SET processing_status='ABORTED',updated_at=now()
            FROM upload_sessions s WHERE s.media_id=a.id AND a.processing_status='UPLOADING' AND s.expires_at<now()
            """);
        metrics.counter("upload_abandoned").increment(expired);
        var rows=jdbc.queryForList("""
            SELECT s.id,s.media_id FROM upload_sessions s JOIN image_assets a ON a.id=s.media_id
            WHERE (s.cleaned_at IS NULL OR s.cleaned_at<now()-interval '1 hour')
            AND s.expires_at<now()-interval '1 minute'
            AND a.processing_status IN ('ABORTED','READY','FAILED','DELETED')
            ORDER BY s.cleaned_at NULLS FIRST,s.expires_at LIMIT 100
            """);
        for(var row:rows) {
            try {
                new TransactionTemplate(transactions).executeWithoutResult(tx -> {
                    // Retry locks the image first as well. Recheck after locking so cleanup
                    // cannot delete the source of a concurrently accepted processing retry.
                    var locked=jdbc.queryForList("SELECT id FROM image_assets WHERE id=? FOR UPDATE SKIP LOCKED",row.get("media_id"));
                    if(locked.isEmpty()) return;
                    var candidate=jdbc.queryForList("""
                        SELECT s.object_key FROM upload_sessions s JOIN image_assets a ON a.id=s.media_id
                        WHERE s.id=? AND (s.cleaned_at IS NULL OR s.cleaned_at<now()-interval '1 hour')
                        AND s.expires_at<now()-interval '1 minute'
                        AND a.processing_status IN ('ABORTED','READY','FAILED','DELETED')
                        FOR UPDATE OF s
                        """,row.get("id"));
                    if(candidate.isEmpty()) return;
                    storage.delete(candidate.get(0).get("object_key").toString());
                    jdbc.update("UPDATE upload_sessions SET cleaned_at=now() WHERE id=?",row.get("id"));
                });
                // Repeat cleanup after one hour: a PUT accepted before signature expiry
                // can finish after the first sweep and recreate an already-cleaned key.
            } catch(Exception exception) { log.warn("Staging cleanup deferred for upload {}",row.get("id")); }
        }
    }

    @Scheduled(fixedDelayString="${app.pipeline.cleanup-interval-ms:30000}")
    public void reconcileDeletedObjects() {
        // A worker can lose its database connection/advisory lock during external
        // storage I/O, then finish a stale PUT after DELETE. Reconcile tombstoned
        // prefixes hourly so these objects cannot become permanent orphans.
        var rows=jdbc.queryForList("""
            SELECT j.id,j.media_id FROM media_processing_jobs j JOIN image_assets a ON a.id=j.media_id
            WHERE j.job_type='DELETE' AND j.status='DONE' AND j.finished_at<now()-interval '1 hour'
            AND a.deleted_at IS NOT NULL AND a.processing_status='DELETED'
            ORDER BY j.finished_at LIMIT 100
            """);
        for(var row:rows) {
            new TransactionTemplate(transactions).executeWithoutResult(tx -> {
                var locked=jdbc.queryForList("SELECT id FROM image_assets WHERE id=? FOR UPDATE SKIP LOCKED",row.get("media_id"));
                if(locked.isEmpty()) return;
                int claimed=jdbc.update("""
                    UPDATE media_processing_jobs j SET status='QUEUED',attempt=0,claim_token=NULL,lease_until=NULL,
                        next_attempt_at=now(),last_error_code=NULL,updated_at=now(),started_at=NULL,finished_at=NULL,worker_id=NULL
                    FROM image_assets a WHERE j.id=? AND a.id=j.media_id
                        AND j.status='DONE' AND j.finished_at<now()-interval '1 hour'
                        AND a.deleted_at IS NOT NULL AND a.processing_status='DELETED'
                    """,row.get("id"));
                if(claimed==1) jdbc.update("UPDATE media_outbox SET last_published_at=NULL WHERE job_id=?",row.get("id"));
            });
        }
    }
}

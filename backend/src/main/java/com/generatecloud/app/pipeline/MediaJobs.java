package com.generatecloud.app.pipeline;

import com.generatecloud.app.entity.ImageAsset;
import java.util.UUID;
import lombok.RequiredArgsConstructor;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

@Service
@RequiredArgsConstructor
@ConditionalOnProperty(name = "app.pipeline.enabled", havingValue = "true")
public class MediaJobs {
    private final JdbcTemplate jdbc;
    private final PipelineProperties properties;

    @Transactional(propagation = Propagation.MANDATORY)
    public void enqueue(ImageAsset image, String type) {
        String version = type.equals("EMBED") ? properties.getModelVersion() : "media-v1";
        var context=io.opentelemetry.api.trace.Span.current().getSpanContext();
        String traceparent=context.isValid()?"00-"+context.getTraceId()+"-"+context.getSpanId()+"-"+context.getTraceFlags().asHex():null;
        jdbc.update("""
            INSERT INTO media_processing_jobs(id,media_id,job_type,asset_version,pipeline_version,traceparent)
            VALUES (?,?,?,?,?,?) ON CONFLICT(media_id,job_type,asset_version,pipeline_version) DO NOTHING
            """, UUID.randomUUID(), image.getId(), type, image.getAssetVersion(), version,traceparent);
        jdbc.update("""
            INSERT INTO media_outbox(job_id) SELECT id FROM media_processing_jobs
            WHERE media_id=? AND job_type=? AND asset_version=? AND pipeline_version=?
            ON CONFLICT DO NOTHING
            """, image.getId(), type, image.getAssetVersion(), version);
    }

    @Transactional(propagation = Propagation.MANDATORY)
    public void delete(ImageAsset image) {
        jdbc.update("UPDATE media_processing_jobs SET status='CANCELLED',updated_at=now() WHERE media_id=? AND job_type<>'DELETE' AND status<>'DONE'", image.getId());
        enqueue(image, "DELETE");
    }
}

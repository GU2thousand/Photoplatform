package com.generatecloud.app.pipeline;

import com.generatecloud.app.entity.enums.Role;
import com.generatecloud.app.exception.*;
import com.generatecloud.app.repository.ImageAssetRepository;
import com.generatecloud.app.security.AppUserPrincipal;
import com.generatecloud.app.service.*;
import java.util.Map;
import lombok.RequiredArgsConstructor;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.*;

@RestController
@RequiredArgsConstructor
@ConditionalOnProperty(name="app.pipeline.enabled",havingValue="true")
public class JobController {
    private final AuthService auth;
    private final ImageService images;
    private final ImageAssetRepository repository;
    private final JdbcTemplate jdbc;
    private final MediaJobs jobs;
    private final PipelineProperties settings;
    @PostMapping("/api/images/{id}/retry")
    @Transactional
    public Map<String,String> retry(@AuthenticationPrincipal AppUserPrincipal principal,@PathVariable long id,
            @RequestParam(defaultValue="MEDIA_PROCESS") String type) {
        var actor=auth.requireUser(principal);
        if(jdbc.queryForList("SELECT id FROM image_assets WHERE id=? FOR UPDATE",Long.class,id).isEmpty())
            throw new NotFoundException("Image not found");
        var image=images.getImage(id);
        if(actor.getRole()!=Role.ADMIN && !actor.getId().equals(image.getUploader().getId()))
            throw new UnauthorizedAccessException("Only the owner or an admin can replay this job");
        if(!type.equals("MEDIA_PROCESS") && !type.equals("EMBED")) throw new BadRequestException("Unknown job type");
        if(type.equals("EMBED") && (!settings.isSearchEnabled() || !image.getProcessingStatus().equals("READY")))
            throw new BadRequestException("Image must be ready and semantic search enabled");
        if(type.equals("MEDIA_PROCESS") && !image.getProcessingStatus().equals("FAILED"))
            throw new BadRequestException("Only failed processing can be retried");
        if(type.equals("MEDIA_PROCESS") && jdbc.queryForObject("SELECT count(*) FROM upload_sessions WHERE media_id=? AND cleaned_at IS NULL",Long.class,id)==0)
            throw new BadRequestException("Source expired; upload the image again");
        jobs.enqueue(image,type);
        String version=type.equals("EMBED")?settings.getModelVersion():"media-v1";
        jdbc.update("""
            UPDATE media_processing_jobs SET status='QUEUED',attempt=0,lease_until=NULL,claim_token=NULL,
            last_error_code=NULL,next_attempt_at=now(),updated_at=now(),started_at=NULL,finished_at=NULL,worker_id=NULL
            WHERE media_id=? AND job_type=? AND asset_version=? AND pipeline_version=? AND status IN ('DLQ','DONE')
            """,id,type,image.getAssetVersion(),version);
        jdbc.update("""
            UPDATE media_outbox SET last_published_at=NULL WHERE job_id IN
            (SELECT id FROM media_processing_jobs WHERE media_id=? AND job_type=? AND asset_version=? AND pipeline_version=?)
            """,id,type,image.getAssetVersion(),version);
        if(type.equals("MEDIA_PROCESS")) image.setProcessingStatus("PROCESSING");
        else image.setEmbeddingStatus("QUEUED");
        repository.flush();
        return Map.of("status","QUEUED");
    }
}

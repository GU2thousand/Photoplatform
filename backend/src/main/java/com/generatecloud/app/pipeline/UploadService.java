package com.generatecloud.app.pipeline;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.generatecloud.app.entity.ImageAsset;
import com.generatecloud.app.entity.UserAccount;
import com.generatecloud.app.entity.enums.*;
import com.generatecloud.app.exception.*;
import com.generatecloud.app.repository.ImageAssetRepository;
import com.generatecloud.app.service.TeamService;
import io.micrometer.core.instrument.MeterRegistry;
import jakarta.validation.constraints.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.Instant;
import java.util.*;
import lombok.RequiredArgsConstructor;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import software.amazon.awssdk.services.s3.model.S3Exception;

@Service
@RequiredArgsConstructor
@ConditionalOnProperty(name = "app.pipeline.enabled", havingValue = "true")
public class UploadService {
    private final JdbcTemplate jdbc;
    private final ImageAssetRepository images;
    private final TeamService teams;
    private final PipelineStorage storage;
    private final PipelineProperties settings;
    private final MediaJobs jobs;
    private final MeterRegistry metrics;
    private final ObjectMapper json;

    public record Request(@NotBlank @Size(max=255) String filename,
            @NotBlank String contentType, @Positive long size,
            @NotBlank @Pattern(regexp="[a-f0-9]{64}") String sha256,
            @NotBlank @Size(max=140) String title, @Size(max=1500) String description,
            @Size(max=120) String category, @Size(max=1000) String tags,
            @NotNull Visibility visibility, Long teamId) {}

    public record Response(UUID uploadId, long mediaId, String status, String embeddingStatus,
            String uploadUrl, Map<String,String> headers, Instant expiresAt, String objectKey,
            String errorCode) {}

    private record Session(UUID id, long mediaId, long ownerId, String fingerprint, String key,
            String type, long size, String hash, Instant expiresAt, Instant completedAt) {}

    private List<Session> sessions(String predicate, Object... args) {
        return jdbc.query("SELECT * FROM upload_sessions WHERE " + predicate, (r,n) -> new Session(
                r.getObject("id", UUID.class), r.getLong("media_id"), r.getLong("owner_id"),
                r.getString("request_fingerprint"), r.getString("object_key"), r.getString("content_type"),
                r.getLong("expected_bytes"), r.getString("expected_sha256"), r.getTimestamp("expires_at").toInstant(),
                r.getTimestamp("completed_at") == null ? null : r.getTimestamp("completed_at").toInstant()), args);
    }

    @Transactional
    public Response create(UserAccount actor, UUID idempotencyKey, Request request) {
        if (request.size() > settings.getMaxBytes()) throw new BadRequestException("Image exceeds the upload limit");
        if (!Set.of("image/jpeg","image/png","image/webp","image/gif","image/bmp").contains(request.contentType()))
            throw new BadRequestException("Unsupported image type");
        // Serialize creations per owner: quota and idempotency are safe under concurrent requests.
        jdbc.queryForObject("SELECT pg_advisory_xact_lock(?)", Object.class, -actor.getId());
        String fingerprint;
        try { fingerprint = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(json.writeValueAsBytes(request))); }
        catch (Exception exception) { throw new IllegalStateException(exception); }
        var previous = sessions("owner_id=? AND idempotency_key=?", actor.getId(), idempotencyKey);
        if (!previous.isEmpty()) {
            if (!previous.get(0).fingerprint().equals(fingerprint)) throw new BadRequestException("Idempotency key belongs to a different upload");
            return response(previous.get(0), actor, true);
        }
        Long active = jdbc.queryForObject("""
            SELECT count(*) FROM upload_sessions s JOIN image_assets a ON a.id=s.media_id
            WHERE s.owner_id=? AND a.processing_status IN ('UPLOADING','PROCESSING') AND a.deleted_at IS NULL
            """, Long.class, actor.getId());
        if (active >= settings.getMaxActiveUploads()) throw new BadRequestException("Too many active uploads; finish or cancel an upload first");
        var team = request.visibility() == Visibility.TEAM ? teams.getTeam(requireTeam(request.teamId(), actor)) : null;
        var image = images.saveAndFlush(ImageAsset.builder().title(request.title().trim())
                .description(value(request.description(), "Uploaded image asset"))
                .category(value(request.category(), "General"))
                .tags(value(request.tags(), "general").toLowerCase(Locale.ROOT))
                .originalFileName(request.filename()).sizeBytes(request.size()).visibility(request.visibility())
                .moderationStatus(request.visibility() == Visibility.PUBLIC && actor.getRole() == Role.USER ? ModerationStatus.PENDING : ModerationStatus.APPROVED)
                .uploader(actor).team(team).storageLayout("VERSIONED").processingStatus("UPLOADING").build());
        UUID id = UUID.randomUUID();
        Instant expires = Instant.now().plusSeconds(settings.getUploadTtlSeconds());
        String key = "staging/" + id + "/original";
        jdbc.update("""
            INSERT INTO upload_sessions(id,media_id,owner_id,idempotency_key,request_fingerprint,object_key,
                content_type,expected_bytes,expected_sha256,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)
            """, id,image.getId(),actor.getId(),idempotencyKey,fingerprint,key,request.contentType(),request.size(),request.sha256(),java.sql.Timestamp.from(expires));
        metrics.counter("upload_sessions").increment();
        return response(sessions("id=?",id).get(0), actor, true);
    }

    @Transactional
    public Response complete(UUID id, UserAccount actor) {
        Session session = require(id, actor);
        jdbc.queryForObject("SELECT id FROM image_assets WHERE id=? FOR UPDATE", Long.class, session.mediaId());
        ImageAsset image = images.findById(session.mediaId()).orElseThrow(() -> new NotFoundException("Upload not found"));
        if (image.getTeam()!=null) teams.requireMembership(image.getTeam().getId(),actor);
        if (image.getDeletedAt()!=null || Set.of("ABORTED","DELETING","DELETED").contains(image.getProcessingStatus()))
            throw new BadRequestException("Upload is no longer active");
        if (!image.getProcessingStatus().equals("UPLOADING")) return response(session,actor,false);
        if (!session.expiresAt().isAfter(Instant.now())) throw new BadRequestException("Upload session expired; start a new upload");
        try {
            var head = storage.head(session.key());
            if (head.contentLength()!=session.size() || !session.type().equals(head.contentType())
                    || !id.toString().equals(head.metadata().get("upload-id"))) throw new BadRequestException("Uploaded object does not match the session");
        } catch (S3Exception exception) {
            if (exception.statusCode()==404) throw new BadRequestException("Upload the file before completing this session");
            throw exception;
        }
        image.setProcessingStatus("PROCESSING");
        images.flush();
        jdbc.update("UPDATE upload_sessions SET completed_at=now() WHERE id=?", id);
        jobs.enqueue(image,"MEDIA_PROCESS");
        metrics.counter("upload_completed").increment();
        return response(session,actor,false);
    }

    @Transactional(readOnly=true)
    public Response get(UUID id, UserAccount actor) { return response(require(id,actor),actor,false); }

    @Transactional
    public Response abort(UUID id, UserAccount actor) {
        Session session=require(id,actor);
        jdbc.queryForObject("SELECT id FROM image_assets WHERE id=? FOR UPDATE",Long.class,session.mediaId());
        var image=images.findById(session.mediaId()).orElseThrow();
        if (image.getProcessingStatus().equals("UPLOADING")) { image.setProcessingStatus("ABORTED"); metrics.counter("upload_abandoned").increment(); }
        else if (!image.getProcessingStatus().equals("ABORTED")) throw new BadRequestException("Only an uncompleted upload can be cancelled");
        return response(session,actor,false);
    }

    private Session require(UUID id, UserAccount actor) {
        return sessions("id=? AND owner_id=?", id, actor.getId()).stream().findFirst()
                .orElseThrow(() -> new NotFoundException("Upload not found"));
    }

    private Response response(Session s, UserAccount actor, boolean sign) {
        var image=images.findById(s.mediaId()).orElseThrow();
        PipelineStorage.UploadUrl signed=null;
        if (sign && image.getProcessingStatus().equals("UPLOADING") && s.expiresAt().isAfter(Instant.now())) {
            if(image.getTeam()!=null) teams.requireMembership(image.getTeam().getId(),actor);
            signed=storage.signUpload(s.key(),s.type(),s.size(),s.hash(),s.id().toString(),Duration.between(Instant.now(),s.expiresAt()));
        }
        var errors=jdbc.queryForList("SELECT last_error_code FROM media_processing_jobs WHERE media_id=? AND status='DLQ' ORDER BY updated_at DESC LIMIT 1", String.class,s.mediaId());
        return new Response(s.id(),s.mediaId(),image.getProcessingStatus(),image.getEmbeddingStatus(),
                signed==null?null:signed.url(),signed==null?Map.of():signed.headers(),s.expiresAt(),s.key(),errors.isEmpty()?null:errors.get(0));
    }

    private long requireTeam(Long id, UserAccount actor) {
        if(id==null) throw new BadRequestException("Team uploads require a team");
        teams.requireMembership(id,actor); return id;
    }
    private String value(String input,String fallback) { return input==null||input.isBlank()?fallback:input.trim(); }
}

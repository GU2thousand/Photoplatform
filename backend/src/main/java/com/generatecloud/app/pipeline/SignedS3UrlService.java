package com.generatecloud.app.pipeline;

import com.generatecloud.app.storage.S3ClientFactory;
import com.generatecloud.app.storage.StorageProperties;
import java.time.Duration;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;
import software.amazon.awssdk.services.s3.presigner.S3Presigner;

public class SignedS3UrlService implements MediaUrlService, AutoCloseable {
    private final StorageProperties storage;
    private final int ttl;
    private final S3Presigner presigner;

    public SignedS3UrlService(StorageProperties storage, PipelineProperties pipeline) {
        this.storage = storage;
        this.ttl = pipeline.getMediaUrlTtlSeconds();
        MediaDeliveryConfig.validateTtl(ttl);
        this.presigner = S3ClientFactory.presigner(storage, pipeline.getPublicStorageEndpoint());
    }
    public String download(String key, boolean approvedPublic) {
        // Browser caches must not extend access beyond the bearer URL's lifetime.
        var get = GetObjectRequest.builder().bucket(storage.getBucket()).key(storage.qualify(key))
                .responseCacheControl("private, no-store").build();
        return presigner.presignGetObject(b -> b.signatureDuration(Duration.ofSeconds(ttl)).getObjectRequest(get)).url().toString();
    }
    public int ttlSeconds() { return ttl; }
    public void close() { presigner.close(); }
}

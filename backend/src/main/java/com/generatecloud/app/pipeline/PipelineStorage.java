package com.generatecloud.app.pipeline;

import com.generatecloud.app.storage.StorageProperties;
import com.generatecloud.app.storage.ObjectStorageService;
import com.generatecloud.app.storage.S3ClientFactory;
import io.micrometer.core.instrument.MeterRegistry;
import jakarta.annotation.PreDestroy;
import java.time.Duration;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.Base64;
import java.util.Map;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Service;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.*;
import software.amazon.awssdk.services.s3.presigner.S3Presigner;

@Service
@ConditionalOnProperty(name = "app.pipeline.enabled", havingValue = "true")
public class PipelineStorage implements ObjectStorageService {
    private final StorageProperties storage;
    private final S3Client client;
    private final S3Presigner presigner;
    private final MeterRegistry metrics;

    public PipelineStorage(StorageProperties storage, PipelineProperties pipeline, MeterRegistry metrics) {
        this.storage = storage;
        this.metrics = metrics;
        client = S3ClientFactory.client(storage);
        presigner = S3ClientFactory.presigner(storage, pipeline.getPublicStorageEndpoint());
    }

    public UploadUrl signUpload(String key, String type, long size, String sha256, String session, Duration ttl) {
        var put = PutObjectRequest.builder().bucket(storage.getBucket()).key(storage.qualify(key))
                .contentType(type).contentLength(size).ifNoneMatch("*")
                .checksumSHA256(Base64.getEncoder().encodeToString(HexFormat.of().parseHex(sha256)))
                .metadata(Map.of("upload-id", session)).build();
        var signed = presigner.presignPutObject(b -> b.signatureDuration(ttl).putObjectRequest(put));
        var headers = new HashMap<String, String>();
        signed.signedHeaders().forEach((name, values) -> {
            if (!name.equalsIgnoreCase("host") && !name.equalsIgnoreCase("content-length")) headers.put(name, String.join(",", values));
        });
        return new UploadUrl(signed.url().toString(), headers);
    }

    public Metadata head(String key) {
        return measured("head", () -> {
            var result = client.headObject(b -> b.bucket(storage.getBucket()).key(storage.qualify(key))
                    .checksumMode(ChecksumMode.ENABLED));
            return new Metadata(result.contentLength(), result.contentType(), result.metadata(), result.checksumSHA256());
        });
    }

    public void delete(String key) {
        measured("delete", () -> client.deleteObject(b -> b.bucket(storage.getBucket()).key(storage.qualify(key))));
    }

    private <T> T measured(String operation, java.util.function.Supplier<T> action) {
        return metrics.timer("storage_request_duration", "operation", operation).record(() -> {
            try { return action.get(); }
            catch (RuntimeException exception) {
                metrics.counter("storage_request_errors", "operation", operation).increment();
                throw exception;
            }
        });
    }

    @PreDestroy
    public void close() { client.close(); presigner.close(); }
}

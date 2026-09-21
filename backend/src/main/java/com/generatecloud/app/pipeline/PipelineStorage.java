package com.generatecloud.app.pipeline;

import com.generatecloud.app.storage.StorageProperties;
import io.micrometer.core.instrument.MeterRegistry;
import jakarta.annotation.PreDestroy;
import java.net.URI;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.Base64;
import java.util.Map;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Service;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.S3Configuration;
import software.amazon.awssdk.services.s3.model.*;
import software.amazon.awssdk.services.s3.presigner.S3Presigner;
import software.amazon.awssdk.services.cloudfront.CloudFrontUtilities;
import software.amazon.awssdk.services.cloudfront.model.CannedSignerRequest;

@Service
@ConditionalOnProperty(name = "app.pipeline.enabled", havingValue = "true")
public class PipelineStorage {
    private final StorageProperties storage;
    private final PipelineProperties pipeline;
    private final S3Client client;
    private final S3Presigner presigner;
    private final MeterRegistry metrics;

    public PipelineStorage(StorageProperties storage, PipelineProperties pipeline, MeterRegistry metrics) {
        if (!"s3".equals(storage.getProvider())) throw new IllegalStateException("Direct uploads require S3 storage");
        this.storage = storage;
        this.pipeline = pipeline;
        this.metrics = metrics;
        var credentials = StaticCredentialsProvider.create(AwsBasicCredentials.create(storage.getAccessKey(), storage.getSecretKey()));
        var config = S3Configuration.builder().pathStyleAccessEnabled(storage.isPathStyleAccess()).build();
        var builder = S3Client.builder().region(Region.of(storage.getRegion())).credentialsProvider(credentials)
                .serviceConfiguration(config).overrideConfiguration(c -> c.apiCallTimeout(Duration.ofSeconds(10)));
        if (!storage.getEndpoint().isBlank()) builder.endpointOverride(URI.create(storage.getEndpoint()));
        client = builder.build();
        var signer = S3Presigner.builder().region(Region.of(storage.getRegion())).credentialsProvider(credentials)
                .serviceConfiguration(config);
        if (!pipeline.getPublicStorageEndpoint().isBlank()) signer.endpointOverride(URI.create(pipeline.getPublicStorageEndpoint()));
        presigner = signer.build();
    }

    public record UploadUrl(String url, Map<String, String> headers) {}

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

    public HeadObjectResponse head(String key) {
        return metrics.timer("storage_request_duration", "operation", "head").record(() ->
                client.headObject(b -> b.bucket(storage.getBucket()).key(storage.qualify(key))));
    }

    public void delete(String key) {
        metrics.timer("storage_request_duration", "operation", "delete").record(() ->
                client.deleteObject(b -> b.bucket(storage.getBucket()).key(storage.qualify(key))));
    }

    public String download(String key, boolean approvedPublic) {
        // CloudFront must require signatures on EVERY behavior and use a private origin.
        if (approvedPublic && !pipeline.getCdnDomain().isBlank()) {
            try {
            var request = CannedSignerRequest.builder()
                    .resourceUrl("https://" + pipeline.getCdnDomain() + "/" + storage.qualify(key))
                    .keyPairId(pipeline.getCdnKeyPairId()).privateKey(Path.of(pipeline.getCdnPrivateKeyPath()))
                    .expirationDate(Instant.now().plusSeconds(60)).build();
            return CloudFrontUtilities.create().getSignedUrlWithCannedPolicy(request).url();
            } catch (Exception exception) { throw new IllegalStateException("CDN signing configuration is invalid",exception); }
        }
        var get = GetObjectRequest.builder().bucket(storage.getBucket()).key(storage.qualify(key))
                .responseCacheControl(approvedPublic ? "public, max-age=60" : "private, no-store").build();
        return presigner.presignGetObject(b -> b.signatureDuration(Duration.ofSeconds(60)).getObjectRequest(get)).url().toString();
    }

    @PreDestroy
    public void close() { client.close(); presigner.close(); }
}
